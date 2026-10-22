"""Server-owned capability policy. Registry entries never install or execute code."""
import copy
import re

from .security import CoreError, endpoint

IDENTIFIER = re.compile(r'^[A-Za-z0-9_-]{1,48}$')
EFFECTS = {'read', 'analyze', 'compute', 'propose', 'write'}


def alias(server_id, tool_name):
    return 'mcp__' + server_id + '__' + tool_name


def plugin_allowed(config, service, entry):
    identifier = entry.get('pluginId')
    if not identifier:
        return True
    return identifier in service.get('plugins', []) and any(
        p['id'] == identifier and p.get('enabled', True) for p in config.get('plugins', []))


def active_skills(config, service, domain):
    return {s['id']: copy.deepcopy(s) for s in config.get('skills', [])
            if s['id'] in service.get('skills', []) and s.get('enabled', True)
            and domain in s.get('domains', ['cad', 'chat']) and plugin_allowed(config, service, s)}


def validate_config(config):
    """Validate references and schemas; network discovery is never validation."""
    from .registry import validate
    registries = {}
    for key in ('mcpServers', 'skills', 'plugins'):
        rows = config.get(key, [])
        if not isinstance(rows, list) or len(rows) > 256:
            raise CoreError('INVALID_CAPABILITY_CONFIGURATION', 503)
        ids = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not IDENTIFIER.fullmatch(row['id']):
                raise CoreError('INVALID_CAPABILITY_ID', 503)
            if 'enabled' in row and type(row['enabled']) is not bool:
                raise CoreError('INVALID_CAPABILITY_CONFIGURATION', 503)
            if 'domains' in row and (not isinstance(row['domains'], list) or any(d not in ('chat', 'cad') for d in row['domains'])):
                raise CoreError('INVALID_CAPABILITY_DOMAINS', 503)
            ids.append(row['id'])
        if len(ids) != len(set(ids)):
            raise CoreError('DUPLICATE_CAPABILITY_ID', 503)
        registries[key] = set(ids)
    http = {t['name']: t for t in config.get('httpTools', [])}
    aliases = set()
    for server in config.get('mcpServers', []):
        transport = server.get('transport')
        if transport not in ('streamable-http', 'http-bridge', 'rag-bridge'):
            raise CoreError('INVALID_MCP_TRANSPORT', 503)
        if transport == 'streamable-http':
            endpoint(server.get('url', ''))
        elif any(k in server for k in ('url', 'tokenEnv', 'tokenFile')):
            raise CoreError('BRIDGE_ENDPOINT_NOT_ALLOWED', 503)
        if 'timeoutSeconds' in server and (type(server['timeoutSeconds']) not in (int, float) or not 1 <= server['timeoutSeconds'] <= 120):
            raise CoreError('INVALID_MCP_TIMEOUT', 503)
        tools = server.get('tools', [])
        if not isinstance(tools, list) or len(tools) > 128:
            raise CoreError('INVALID_MCP_TOOLS', 503)
        names = set()
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get('name'), str) or not IDENTIFIER.fullmatch(tool['name']):
                raise CoreError('INVALID_MCP_TOOL', 503)
            name = tool['name']; qualified = alias(server['id'], name)
            if name in names or len(qualified) > 64 or qualified in http:
                raise CoreError('DUPLICATE_MCP_TOOL', 503)
            names.add(name); aliases.add(qualified)
            if tool.get('effect') not in EFFECTS or not isinstance(tool.get('description'), str) or len(tool['description']) > 4096:
                raise CoreError('INVALID_MCP_TOOL', 503)
            if 'enabled' in tool and type(tool['enabled']) is not bool:
                raise CoreError('INVALID_MCP_TOOL', 503)
            schema = tool.get('inputSchema', {'type': 'object'})
            if not isinstance(schema, dict) or schema.get('type') != 'object':
                raise CoreError('INVALID_MCP_SCHEMA', 503)
            # Validate the schema independently from its required application fields.
            try:
                validate(schema, {})
            except CoreError as exc:
                if exc.code != 'SCHEMA_VALIDATION_FAILED': raise
                from jsonschema import Draft202012Validator, SchemaError
                try: Draft202012Validator.check_schema(schema)
                except SchemaError: raise CoreError('INVALID_MCP_SCHEMA', 503) from None
            if transport == 'http-bridge':
                target = http.get(tool.get('httpTool'))
                if target is None or tool['effect'] != target.get('effect'):
                    raise CoreError('INVALID_MCP_HTTP_BRIDGE', 503)
            if transport == 'rag-bridge' and (name not in ('recall', 'list_documents') or tool['effect'] != 'read'):
                raise CoreError('INVALID_MCP_RAG_BRIDGE', 503)
    for skill in config.get('skills', []):
        if not isinstance(skill.get('instructions'), str) or not 1 <= len(skill['instructions']) <= 16000:
            raise CoreError('INVALID_SKILL_INSTRUCTIONS', 503)
    for row in [*config.get('skills', []), *config.get('httpTools', []), *config.get('mcpServers', [])]:
        if row.get('pluginId') and row['pluginId'] not in registries['plugins']:
            raise CoreError('UNKNOWN_PLUGIN', 503)
    for service in config.get('services', []):
        for key, known in {**registries, 'mcpTools': aliases}.items():
            grants = service.get(key, [])
            if not isinstance(grants, list) or any(not isinstance(g, str) for g in grants) or set(grants) - known:
                raise CoreError('UNKNOWN_CAPABILITY_GRANT', 503)
        if sum(len(s['instructions']) for s in config.get('skills', []) if s['id'] in service.get('skills', [])) > 64000:
            raise CoreError('SKILL_INSTRUCTION_LIMIT', 503)
    return config


def discovery(config, server_id):
    from .mcp import discover
    return discover(config, server_id)
