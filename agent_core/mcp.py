"""Bounded MCP Streamable HTTP client and authenticated Core HTTP/RAG facade.

Only server-owned registry URLs are used. Discovery previews never grant tools.
The facade adapts existing HTTP/OCR transports; it does not re-label them native MCP.
"""
import copy
import json
import time
import uuid

import httpx

from .capabilities import alias, plugin_allowed
from .security import CoreError, authorize, credential, endpoint

PROTOCOL = '2025-06-18'
SUPPORTED_PROTOCOLS = (PROTOCOL, '2025-03-26', '2024-11-05')
RESPONSE_LIMIT = 8 * 1024 * 1024


def server_entry(config, identifier):
    row = next((s for s in config.get('mcpServers', []) if s['id'] == identifier and s.get('enabled', True)), None)
    if row is None: raise CoreError('MCP_SERVER_UNAVAILABLE', 404)
    return row


class Client:
    def __init__(self, entry):
        if entry.get('transport') != 'streamable-http': raise CoreError('MCP_REMOTE_TRANSPORT_REQUIRED')
        self.entry = copy.deepcopy(entry)
        self.url = endpoint(entry['url'])
        self.session = None
        self.protocol = PROTOCOL
        self.deadline = time.monotonic() + min(120, max(1, entry.get('timeoutSeconds', 60)))

    def request(self, method, params=None, notification=False):
        rid = str(uuid.uuid4())
        payload = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        if not notification: payload['id'] = rid
        headers = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json',
                   'MCP-Protocol-Version': self.protocol}
        if self.entry.get('tokenEnv') or self.entry.get('tokenFile'):
            headers['Authorization'] = 'Bearer ' + credential(self.entry)
        if self.session: headers['Mcp-Session-Id'] = self.session
        remaining = self.deadline - time.monotonic()
        if remaining <= 0: raise CoreError('MCP_TIMEOUT', 504)
        try:
            with httpx.stream('POST', self.url, headers=headers, json=payload, follow_redirects=False,
                              trust_env=False, timeout=httpx.Timeout(remaining, connect=min(10, remaining))) as response:
                if response.status_code not in (200, 202, 204): raise CoreError('MCP_UPSTREAM_ERROR', 502)
                session = response.headers.get('Mcp-Session-Id')
                if session:
                    if len(session) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in session):
                        raise CoreError('MCP_INVALID_SESSION', 502)
                    self.session = session
                if notification and response.status_code in (202, 204): return None
                if response.status_code != 200: raise CoreError('MCP_INVALID_RESPONSE', 502)
                stream = 'text/event-stream' in response.headers.get('content-type', '')
                content = bytearray(); pending = bytearray()
                for block in response.iter_bytes():
                    if time.monotonic() > self.deadline: raise CoreError('MCP_TIMEOUT', 504)
                    content.extend(block)
                    if len(content) > RESPONSE_LIMIT: raise CoreError('MCP_RESPONSE_TOO_LARGE', 502)
                    if stream:
                        pending.extend(block)
                        # Normalize CRLF before splitting complete SSE messages.
                        pending = bytearray(pending.replace(b'\r\n', b'\n'))
                        while b'\n\n' in pending:
                            event, rest = pending.split(b'\n\n', 1); pending = bytearray(rest)
                            data = b'\n'.join(line[5:].lstrip() for line in event.split(b'\n') if line.startswith(b'data:'))
                            if not data: continue
                            value = json.loads(data)
                            if isinstance(value, dict) and value.get('id') == rid:
                                return self._result(value, rid)
                if notification and not content: return None
                if stream: raise CoreError('MCP_INVALID_RESPONSE', 502)
                return self._result(json.loads(content), rid)
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            if isinstance(exc, CoreError): raise
            raise CoreError('MCP_UPSTREAM_ERROR', 502) from None

    @staticmethod
    def _result(value, rid):
        if not isinstance(value, dict) or value.get('jsonrpc') != '2.0' or value.get('id') != rid:
            raise CoreError('MCP_INVALID_RESPONSE', 502)
        if 'error' in value: raise CoreError('MCP_REMOTE_ERROR', 502)
        if not isinstance(value.get('result'), dict): raise CoreError('MCP_INVALID_RESPONSE', 502)
        return value['result']

    def initialize(self):
        result = self.request('initialize', {'protocolVersion': PROTOCOL, 'capabilities': {},
                                            'clientInfo': {'name': 'agent-core', 'version': '1'}})
        if result.get('protocolVersion') not in SUPPORTED_PROTOCOLS or not isinstance(result.get('capabilities'), dict):
            raise CoreError('MCP_PROTOCOL_UNSUPPORTED', 502)
        self.protocol = result['protocolVersion']
        self.request('notifications/initialized', notification=True)
        return result

    def list_tools(self):
        tools = []; cursor = None; seen = set()
        for _ in range(8):
            page = self.request('tools/list', {'cursor': cursor} if cursor else {})
            rows = page.get('tools')
            if not isinstance(rows, list) or any(not isinstance(t, dict) or not isinstance(t.get('name'), str)
                                               or not isinstance(t.get('inputSchema'), dict) for t in rows):
                raise CoreError('MCP_INVALID_TOOLS', 502)
            tools.extend(rows)
            if len(tools) > 256 or len({t['name'] for t in tools}) != len(tools): raise CoreError('MCP_TOOL_LIMIT', 502)
            cursor = page.get('nextCursor')
            if not cursor: return tools
            if not isinstance(cursor, str) or len(cursor) > 2048 or cursor in seen: raise CoreError('MCP_INVALID_CURSOR', 502)
            seen.add(cursor)
        raise CoreError('MCP_TOOL_LIMIT', 502)


def catalog(config, service, domain, agent=True):
    from . import registry
    result = {}
    for server in config.get('mcpServers', []):
        if not server.get('enabled', True) or server['id'] not in service.get('mcpServers', []): continue
        if domain not in server.get('domains', ['cad', 'chat']) or not plugin_allowed(config, service, server): continue
        for tool in server.get('tools', []):
            name = alias(server['id'], tool['name'])
            if name not in service.get('mcpTools', []) or not tool.get('enabled', True) or (agent and tool.get('effect') == 'write'): continue
            base = {}
            if server['transport'] == 'http-bridge':
                base = registry.http_catalog(config, service, domain, agent).get(tool.get('httpTool'))
                if base is None: continue
            elif server['transport'] == 'rag-bridge' and (domain != 'chat' or not service.get('ragEnabled',True) or not service.get('ragCallback')): continue
            result[name] = {**copy.deepcopy(base), **copy.deepcopy(tool), 'name': name,
                            'transport': 'ocr-vision-v1' if base.get('transport') == 'ocr-vision-v1' else 'mcp',
                            '_mcpServer': copy.deepcopy(server), '_mcpTool': tool['name']}
    return result


def discover(config, server_id):
    server = server_entry(config, server_id)
    if server['transport'] == 'streamable-http':
        client = Client(server); initialized = client.initialize(); rows = client.list_tools()
        tools = [{k: t[k] for k in ('name', 'description', 'inputSchema', 'outputSchema', 'annotations') if k in t} for t in rows]
        return {'serverId': server_id, 'protocolVersion': client.protocol, 'serverInfo': initialized.get('serverInfo', {}),
                'tools': tools, 'approvalRequired': True}
    return {'serverId': server_id, 'transport': server['transport'],
            'tools': [{k: t[k] for k in ('name', 'description', 'inputSchema', 'effect', 'httpTool') if k in t}
                      for t in server.get('tools', [])], 'approvalRequired': True}


def execute(config, service, context, domain, entry, arguments, request_id=None, agent=True):
    from . import registry
    authorize(service, context, domain)
    server = server_entry(config, entry['_mcpServer']['id'])
    live = catalog(config, service, domain, agent).get(entry['name'])
    if live != entry: raise CoreError('TOOL_REVOKED', 403)
    registry.validate(entry.get('inputSchema', {'type': 'object'}), arguments)
    rid = request_id or str(uuid.uuid4())
    identity = {**context, 'service_id': service['id'], 'caller_id': service['id'], 'tenant_id': service['tenantId']}
    if server['transport'] == 'http-bridge':
        result = registry.execute(config, service, context, domain, entry['httpTool'], arguments, rid, agent)
    elif server['transport'] == 'rag-bridge':
        callback = copy.deepcopy(service.get('ragCallback'))
        if domain != 'chat' or not context.get('knowledge_base_id') or not callback: raise CoreError('RAG_FORBIDDEN', 403)
        data = registry.json_request('POST', callback['url'], callback,
                                    {'operation': entry['_mcpTool'], 'arguments': arguments, 'context': identity, 'request_id': rid}, rid)
        if service.get('ragCallback') != callback: raise CoreError('TOOL_REVOKED', 403)
        if entry['_mcpTool'] == 'recall' and not isinstance(data.get('results'), list): raise CoreError('RAG_INVALID_RESPONSE', 502)
        result = {'contractVersion': '1', 'status': 'succeeded', 'data': data}
    else:
        client = Client(server); client.initialize()
        remote = next((t for t in client.list_tools() if t['name'] == entry['_mcpTool']), None)
        if remote is None or remote['inputSchema'] != entry.get('inputSchema', {'type': 'object'}):
            raise CoreError('MCP_TOOL_SCHEMA_CHANGED', 409)
        data = client.request('tools/call', {'name': entry['_mcpTool'], 'arguments': arguments,
                                          '_meta': {'context': identity, 'domain': domain, 'requestId': rid}})
        if not isinstance(data.get('content'), list) or ('isError' in data and type(data['isError']) is not bool):
            raise CoreError('MCP_INVALID_RESULT', 502)
        if entry.get('outputSchema') and not data.get('isError'):
            registry.validate(entry['outputSchema'], data.get('structuredContent'))
        result = {'contractVersion': '1', 'status': 'failed' if data.get('isError') else 'succeeded', 'data': data}
        if data.get('isError'): result['error'] = 'MCP_TOOL_EXECUTION_FAILED'
    if catalog(config, service, domain, agent).get(entry['name']) != entry: raise CoreError('TOOL_REVOKED', 403)
    if not service.get('enabled', True): raise CoreError('SERVICE_REVOKED', 403)
    return result


def _facade_schema(entry):
    if entry.get('transport') != 'ocr-vision-v1': return entry.get('inputSchema', {'type': 'object'})
    return {'type': 'object', 'properties': {
        'images': {'type': 'array', 'minItems': 1, 'maxItems': 6, 'items': {'type': 'object'}},
        'question': {'type': 'string', 'maxLength': 8192}, 'design_data': {'type': 'object'},
        'source_refs': {'type': 'array', 'maxItems': 100},
        'capability': {'enum':['cad.analysis','image.describe','cad.document_reconstruction']}}, 'required': ['images'], 'additionalProperties': False}


def handle(config, service, server_id, body):
    """Stateless MCP POST endpoint; authentication must bind `service` first.

    tools/call params._meta must contain domain and normal Core context. Images
    for OCR use Core's mediaType/data format. The existing asynchronous OCR job
    transport is polled with its normal deadline before returning an MCP result.
    """
    rid = body.get('id') if isinstance(body, dict) else None
    try:
        if not isinstance(body, dict) or body.get('jsonrpc') != '2.0' or not isinstance(body.get('method'), str):
            raise CoreError('INVALID_MCP_REQUEST')
        if isinstance(rid, (dict, list, bool)) or (rid is not None and not isinstance(rid, (str, int))):
            rid = None; raise CoreError('INVALID_MCP_REQUEST')
        server = server_entry(config, server_id)
        if not service.get('enabled', True) or server_id not in service.get('mcpServers', []) or not plugin_allowed(config, service, server):
            raise CoreError('MCP_SERVER_FORBIDDEN', 403)
        method = body['method']; params = body.get('params', {})
        if not isinstance(params, dict): raise CoreError('INVALID_MCP_PARAMS')
        if method == 'notifications/initialized' and 'id' not in body: return None, 202
        if 'id' not in body: raise CoreError('INVALID_MCP_REQUEST')
        if method == 'initialize':
            requested = params.get('protocolVersion', PROTOCOL)
            result = {'protocolVersion': requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL,
                      'capabilities': {'tools': {'listChanged': False}}, 'serverInfo': {'name': 'agent-core/' + server_id, 'version': '1'},
                      'instructions': 'tools/call requires _meta.domain and _meta.context bound to this authenticated caller.'}
        elif method == 'ping': result = {}
        elif method == 'tools/list':
            entries = {}
            for domain in service.get('domains', []): entries.update(catalog(config, service, domain, agent=False))
            result = {'tools': [{'name': e['_mcpTool'], 'description': e['description'], 'inputSchema': _facade_schema(e)}
                                for e in entries.values() if e['_mcpServer']['id'] == server_id]}
        elif method == 'tools/call':
            meta = params.get('_meta', {})
            if not isinstance(meta, dict): raise CoreError('INVALID_MCP_CONTEXT')
            domain = meta.get('domain'); context = authorize(service, meta.get('context'), domain)
            name = params.get('name')
            if not isinstance(name, str): raise CoreError('INVALID_MCP_PARAMS')
            entry = catalog(config, service, domain, agent=False).get(alias(server_id, name))
            if entry is None: raise CoreError('TOOL_FORBIDDEN', 403)
            arguments = params.get('arguments', {})
            from . import registry
            registry.validate(_facade_schema(entry), arguments)
            if entry.get('transport') == 'ocr-vision-v1':
                from .images import validate_images
                from .vision import analyze
                from .vision import select_capability
                images = validate_images(arguments['images'])
                wire = [{'image_id': str(i), 'mime_type': image['mediaType'], 'data_base64': image['data'],
                         'source_ref': image.get('source_ref') or context} for i, image in enumerate(images)]
                identity = {**context, 'service_id': service['id'], 'caller_id': service['id'], 'tenant_id': service['tenantId']}
                value = None
                def revoked():
                    return not service.get('enabled', True) or catalog(config, service, domain, False).get(entry['name']) != entry
                for event in analyze(entry, wire, arguments.get('question', ''), arguments.get('design_data', {}), identity,
                                     str(uuid.uuid4()), revoked, arguments.get('source_refs', []),
                                     capability=select_capability(domain, arguments)):
                    if event['type'] == 'result':
                        value = {'contractVersion': '1', 'status': 'partial' if event['coverage'].get('error') else 'succeeded',
                                 'data': event['result'], 'coverage': event['coverage'], 'sources': event['sources'],
                                 'source_refs': event['source_refs']}
                if revoked(): raise CoreError('TOOL_REVOKED', 403)
                if value is None: raise CoreError('VISION_INVALID_RESPONSE', 502)
            else:
                value = execute(config, service, context, domain, entry, arguments, agent=False)
            result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}],
                      'structuredContent': value, 'isError': value.get('status') in ('failed', 'partial')}
        else:
            return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': 'METHOD_NOT_FOUND'}}, 200
        return {'jsonrpc': '2.0', 'id': rid, 'result': result}, 200
    except CoreError as exc:
        return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32602 if exc.status == 400 else -32000,
                                                    'message': exc.code, 'data': {'status': exc.status}}}, 200
