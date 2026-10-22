import copy
import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import httpx

from agent_core.security import CoreError


def fixture():
    tool = {'name': 'search', 'description': 'Search', 'effect': 'read',
            'inputSchema': {'type': 'object', 'properties': {'q': {'type': 'string'}}, 'required': ['q'], 'additionalProperties': False}}
    config = {'httpTools': [], 'mcpServers': [{'id': 'rag', 'enabled': True, 'transport': 'streamable-http',
              'url': 'http://127.0.0.1:9999/mcp', 'domains': ['chat'], 'tools': [tool]}],
              'skills': [], 'plugins': []}
    service = {'id': 'caller', 'tenantId': 'tenant', 'enabled': True, 'domains': ['chat'],
               'tools': [], 'mcpServers': ['rag'], 'mcpTools': ['mcp__rag__search'], 'skills': [], 'plugins': []}
    config['services'] = [service]
    context = {'tenant_id': 'tenant', 'actor_id': 'user', 'conversation_id': 'c', 'knowledge_base_id': 'kb'}
    return config, service, context


class CapabilityTests(unittest.TestCase):
    def test_mcp_double_grant_and_plugin_revocation(self):
        from agent_core import registry
        config, service, _ = fixture()
        self.assertIn('mcp__rag__search', registry.catalog(config, service, 'chat'))
        service['mcpTools'] = []
        self.assertNotIn('mcp__rag__search', registry.catalog(config, service, 'chat'))
        service['mcpTools'] = ['mcp__rag__search']
        config['plugins'] = [{'id': 'pack', 'enabled': True}]
        config['mcpServers'][0]['pluginId'] = 'pack'
        self.assertEqual({}, registry.catalog(config, service, 'chat'))
        service['plugins'] = ['pack']
        self.assertIn('mcp__rag__search', registry.catalog(config, service, 'chat'))
        config['plugins'][0]['enabled'] = False
        self.assertEqual({}, registry.catalog(config, service, 'chat'))

    def test_client_initializes_discovers_and_calls_with_trusted_identity(self):
        from agent_core import mcp, registry
        config, service, context = fixture()
        requests = []
        @contextmanager
        def respond(method, url, **kw):
            req = kw['json']; requests.append((req, kw['headers']))
            if req['method'] == 'notifications/initialized':
                yield httpx.Response(202)
                return
            result = {'protocolVersion': mcp.PROTOCOL, 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'test', 'version': '1'}}
            if req['method'] == 'tools/list': result = {'tools': config['mcpServers'][0]['tools']}
            if req['method'] == 'tools/call': result = {'content': [{'type': 'text', 'text': 'found'}], 'isError': False}
            yield httpx.Response(200, json={'jsonrpc': '2.0', 'id': req['id'], 'result': result}, headers={'Mcp-Session-Id': 'sess'})
        with patch('agent_core.mcp.httpx.stream', side_effect=respond):
            self.assertEqual('search', mcp.discover(config, 'rag')['tools'][0]['name'])
            result = registry.execute(config, service, context, 'chat', 'mcp__rag__search', {'q': 'hello'})
        self.assertEqual('succeeded', result['status'])
        call = next(req for req, _ in requests if req['method'] == 'tools/call')
        self.assertEqual('tenant', call['params']['_meta']['context']['tenant_id'])
        self.assertEqual('caller', call['params']['_meta']['context']['caller_id'])
        self.assertEqual('sess', requests[-1][1]['Mcp-Session-Id'])

    def test_skill_instructions_and_revocation_are_runtime_enforced(self):
        from agent_core.runtime import Runtime
        config, service, context = fixture()
        config['skills'] = [{'id': 'citation', 'enabled': True, 'instructions': 'Cite sources.', 'domains': ['chat']}]
        service['skills'] = ['citation']
        runtime = Runtime(config, None, service, {'id': 'r', 'domain': 'chat'}, {'context': context, 'tools': []}, lambda: False)
        self.assertIn('Cite sources.', runtime.skill_instructions)
        config['skills'][0]['enabled'] = False
        with self.assertRaisesRegex(CoreError, 'SKILL_REVOKED'): runtime.check()

    def test_rag_bridge_uses_caller_callback_and_rejects_forged_tenant(self):
        from agent_core import mcp, registry
        config, service, context = fixture()
        config['mcpServers'][0].update(transport='rag-bridge', tools=[{'name': 'recall', 'description': 'Recall', 'effect': 'read', 'inputSchema': {'type': 'object'}}])
        service['mcpTools'] = ['mcp__rag__recall']
        service['ragCallback'] = {'url': 'http://127.0.0.1:7000/recall', 'tokenEnv': 'RAG_TOKEN'}
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'recall', 'arguments': {'question': 'steel'}, '_meta': {'domain': 'chat', 'context': context}}}
        with patch.object(registry, 'json_request', return_value={'results': []}) as call:
            result, status = mcp.handle(config, service, 'rag', body)
            self.assertEqual(200, status)
            self.assertFalse(result['result']['isError'])
            self.assertEqual(service['ragCallback']['url'], call.call_args.args[1])
            self.assertEqual('tenant', call.call_args.args[3]['context']['tenant_id'])
        body['params']['_meta']['context'] = {**context, 'tenant_id': 'other'}
        with patch.object(registry, 'json_request') as call:
            result, _ = mcp.handle(config, service, 'rag', body)
            self.assertIn('TENANT_FORBIDDEN', result['error']['message'])
            call.assert_not_called()

    def test_discovery_does_not_approve_and_invalid_registry_rejected(self):
        from agent_core import capabilities
        config, service, _ = fixture()
        capabilities.validate_config(config)
        config['mcpServers'][0]['url'] = 'file:///etc/passwd'
        with self.assertRaises(CoreError): capabilities.validate_config(config)

    def test_mcp_sse_response_is_matched_and_bounded(self):
        from agent_core import mcp
        config, _, _ = fixture()
        @contextmanager
        def respond(method, url, **kw):
            rid = kw['json']['id']
            payload = 'event: message\r\ndata: '+json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': {'tools': []}})+'\r\n\r\n'
            yield httpx.Response(200, content=payload, headers={'Content-Type': 'text/event-stream'})
        with patch('agent_core.mcp.httpx.stream', side_effect=respond):
            self.assertEqual({'tools': []}, mcp.Client(config['mcpServers'][0]).request('tools/list'))
        @contextmanager
        def oversize(*a, **kw):
            yield httpx.Response(200, content=b'x'*100)
        with patch('agent_core.mcp.httpx.stream', side_effect=oversize), patch.object(mcp, 'RESPONSE_LIMIT', 10):
            with self.assertRaisesRegex(CoreError, 'MCP_RESPONSE_TOO_LARGE'):
                mcp.Client(config['mcpServers'][0]).request('tools/list')

    def test_discovery_preview_cannot_bypass_approval_or_schema(self):
        from agent_core import mcp, registry
        config, service, context = fixture()
        approved = copy.deepcopy(config)
        discovered = {'name': 'surprise', 'inputSchema': {'type': 'object'}}
        with patch.object(mcp.Client, 'initialize', return_value={}), patch.object(mcp.Client, 'list_tools', return_value=[discovered]):
            self.assertEqual('surprise', mcp.discover(config, 'rag')['tools'][0]['name'])
            self.assertEqual(approved, config)
            with self.assertRaisesRegex(CoreError, 'MCP_TOOL_SCHEMA_CHANGED'):
                registry.execute(config, service, context, 'chat', 'mcp__rag__search', {'q': 'x'})
        service['mcpServers'] = []
        with patch.object(mcp.Client, 'initialize') as call:
            with self.assertRaisesRegex(CoreError, 'TOOL_FORBIDDEN'):
                registry.execute(config, service, context, 'chat', 'mcp__rag__search', {'q': 'x'})
            call.assert_not_called()

    def test_http_bridge_requires_both_grants_and_keeps_standalone_transport(self):
        from agent_core import mcp, registry
        config, service, context = fixture()
        config['httpTools'] = [{'name': 'legacy', 'description': 'Legacy', 'effect': 'read', 'domains': ['chat'],
                                'url': 'http://127.0.0.1:7000/tool', 'inputSchema': {'type': 'object'}}]
        config['mcpServers'][0].update(transport='http-bridge', tools=[{'name': 'search', 'httpTool': 'legacy', 'description': 'Bridge', 'effect': 'read', 'inputSchema': {'type': 'object'}}])
        self.assertEqual({}, registry.catalog(config, service, 'chat'))
        service['tools'] = ['legacy']
        expected = {'contractVersion': '1', 'status': 'succeeded', 'data': {'answer': 3}}
        with patch.object(registry, 'json_request', return_value=expected) as call:
            self.assertEqual(expected, registry.execute(config, service, context, 'chat', 'mcp__rag__search', {}))
            self.assertEqual('legacy', call.call_args.args[3]['tool'])
        config['httpTools'][0]['enabled'] = False
        self.assertEqual({}, registry.catalog(config, service, 'chat'))

    def test_ocr_facade_uses_async_adapter_and_reports_partial(self):
        from agent_core import mcp
        config, service, context = fixture()
        config['httpTools'] = [{'name': 'ocr', 'transport': 'ocr-vision-v1', 'url': 'http://127.0.0.1:7000',
                                'description': 'OCR', 'effect': 'analyze', 'inputSchema': {'type': 'object'}}]
        config['mcpServers'][0].update(transport='http-bridge', tools=[{'name': 'search', 'httpTool': 'ocr', 'description': 'OCR', 'effect': 'analyze', 'inputSchema': {'type': 'object'}}])
        service['tools'] = ['ocr']
        image = {'mediaType': 'image/png', 'data': 'base64-data'}
        body = {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'search', 'arguments': {'images': [image]},
                '_meta': {'domain': 'chat', 'context': context}}}
        events = [{'type': 'progress', 'jobId': 'job'}, {'type': 'result', 'result': {'summary': 'uncertain'},
                   'coverage': {'error': 'VISION_IMAGE_UNVERIFIED'}, 'sources': [], 'source_refs': []}]
        with patch('agent_core.images.validate_images', return_value=[image]), patch('agent_core.vision.analyze', return_value=iter(events)) as analyze:
            result, _ = mcp.handle(config, service, 'rag', body)
            self.assertTrue(result['result']['isError'])
            self.assertEqual('partial', result['result']['structuredContent']['status'])
            self.assertEqual('image.describe', analyze.call_args.kwargs['capability'])
            self.assertEqual('tenant', analyze.call_args.args[4]['tenant_id'])

    def test_enabled_skill_reaches_model_and_does_not_grant_tools(self):
        from agent_core.agent import stream_agent
        from agent_core.runtime import Runtime
        config, service, context = fixture()
        config['skills'] = [{'id': 'citation', 'instructions': 'Cite sources.', 'domains': ['chat']}]
        service['skills'] = ['citation']; service['mcpTools'] = []
        runtime = Runtime(config, None, service, {'id': 'r', 'domain': 'chat'}, {'context': context, 'tools': []}, lambda: False)
        with patch('agent_core.agent.OpenAICompatibleClient.stream', return_value=(item for item in [{'type': 'delta', 'content': 'done'}])) as model:
            list(stream_agent({}, [{'role': 'user', 'content': 'hello'}], runtime=runtime))
        self.assertIn('Cite sources.', model.call_args.args[1][0]['content'])
        self.assertEqual(['current_time'], [t['function']['name'] for t in runtime.tools])

    def test_direct_artifacts_are_queued_as_unverified_and_policy_can_revoke(self):
        from agent_core.runtime import Runtime
        config, service, context = fixture()
        profile = {'id': 'vision', 'model': 'vision', 'baseUrl': 'http://127.0.0.1:9', 'capabilities': {'vision': True}}
        config['models'] = [profile]; service.update(visionPolicy='both', modelProfiles=['vision'], defaultModelProfile='vision')
        image = {'mediaType': 'image/png', 'data': 'aGVsbG8='}
        body = {'context': context, 'tools': [], '_visionMode': 'direct', '_profile': copy.deepcopy(profile), 'images': [image]}
        runtime = Runtime(config, None, service, {'id': 'r', 'domain': 'chat'}, body, lambda: False)
        with patch('agent_core.runtime.validate_images', side_effect=copy.deepcopy):
            result = runtime.capture_artifacts({'data': {}, 'artifacts': [{'type': 'image', **image}]})
        self.assertEqual('core_vlm', result['artifacts'][0]['inspectionTool'])
        self.assertNotIn('data', result['artifacts'][0])
        pending = runtime.drain_visual_messages()
        self.assertEqual('image_url', pending[0]['content'][-1]['type'])
        self.assertEqual([], runtime.drain_visual_messages())
        self.assertEqual([0, 1], runtime.outcome()['directVision']['submittedImageIndices'])
        self.assertEqual('unverified', runtime.outcome()['directVision']['fieldVerification'])
        service['visionPolicy'] = 'ocr'
        with self.assertRaisesRegex(CoreError, 'VISION_POLICY_REVOKED'): runtime.check()

    def test_direct_only_policy_blocks_ocr_and_bridge_despite_explicit_grants(self):
        from agent_core import mcp, registry
        config, service, context = fixture()
        config['httpTools'] = [{'name': 'ocr', 'transport': 'ocr-vision-v1', 'url': 'http://127.0.0.1:7000',
                               'description': 'OCR', 'effect': 'analyze', 'inputSchema': {'type': 'object'}}]
        config['mcpServers'][0].update(transport='http-bridge', tools=[{'name': 'search', 'httpTool': 'ocr',
                                     'description': 'OCR', 'effect': 'analyze', 'inputSchema': {'type': 'object'}}])
        service['tools'] = ['ocr']
        expected = {'ocr', 'mcp__rag__search'}
        self.assertEqual(expected, set(registry.catalog(config, service, 'chat')))
        service['visionPolicy'] = 'both'
        self.assertEqual(expected, set(registry.catalog(config, service, 'chat')))
        service['visionPolicy'] = 'direct'
        self.assertEqual({}, registry.catalog(config, service, 'chat', agent=False))
        with self.assertRaisesRegex(CoreError, 'TOOL_FORBIDDEN'):
            registry.execute(config, service, context, 'chat', 'ocr', {}, agent=False)
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'search', 'arguments': {},
                '_meta': {'domain': 'chat', 'context': context}}}
        with patch('agent_core.vision.analyze') as analyze:
            result, _ = mcp.handle(config, service, 'rag', body)
            self.assertEqual('TOOL_FORBIDDEN', result['error']['message'])
            analyze.assert_not_called()

    def test_rag_via_mcp_denies_builtin_fallback_and_preserves_legacy_default(self):
        from agent_core.runtime import Runtime
        config, service, context = fixture()
        service['ragCallback'] = {'url': 'http://127.0.0.1:7000/recall'}
        body = {'context': context, 'tools': []}
        legacy = Runtime(config, None, service, {'id': 'r', 'domain': 'chat'}, body, lambda: False)
        self.assertIn('recall', [t['function']['name'] for t in legacy.tools])
        service['ragViaMcp'] = True
        current = Runtime(config, None, service, {'id': 'r', 'domain': 'chat'}, body, lambda: False)
        self.assertNotIn('recall', [t['function']['name'] for t in current.tools])
        self.assertNotIn('list_documents', [t['function']['name'] for t in current.tools])
        with patch('agent_core.registry.json_request') as call:
            with self.assertRaisesRegex(CoreError, 'TOOL_FORBIDDEN'):
                current.execute('recall', '{"question":"x"}', {'operationId': 'a'})
            with self.assertRaisesRegex(CoreError, 'TOOL_REVOKED'):
                legacy.execute('recall', '{"question":"x"}', {'operationId': 'b'})
            call.assert_not_called()

    def test_rag_bridge_projection_preserves_passage_labels_and_full_storage(self):
        from agent_core.runtime import Runtime
        config, service, context = fixture()
        config['mcpServers'][0].update(transport='rag-bridge', tools=[{'name':'recall','description':'Recall','effect':'read','inputSchema':{'type':'object'}}])
        service.update(ragCallback={'url':'http://127.0.0.1:7000/recall'}, ragViaMcp=True, mcpTools=['mcp__rag__recall'])
        runtime = Runtime(config, None, service, {'id':'r','domain':'chat'}, {'context':context,'tools':['mcp__rag__recall']}, lambda:False)
        wrapper = {'contractVersion':'1','status':'succeeded','data':{'results':[{'passage':'Engineering source. '*400,'label':7}]}}
        projected = runtime.model_result('mcp__rag__recall', wrapper, {'operationId':'rag-result'})
        self.assertIsInstance(projected['results'][0]['passage'], str)
        self.assertEqual(6000, len(projected['results'][0]['passage']))
        self.assertEqual(7, projected['results'][0]['label'])
        self.assertTrue(projected['results'][0]['passageTruncated'])
        self.assertEqual(wrapper, runtime.results['rag-result'])
        self.assertEqual(['mcp__rag__recall'],runtime.result_authorities('mcp__rag__recall',{'operationId':'rag-result'}))

    def test_chat_without_space_can_read_large_mcp_results_and_revocation_applies(self):
        from agent_core.runtime import Runtime
        config, service, context = fixture()
        runtime = Runtime(config, None, service, {'id':'r','domain':'chat'}, {'context':context,'tools':['mcp__rag__search']}, lambda:False)
        self.assertIn('session_read_result',[t['function']['name'] for t in runtime.tools])
        result={'contractVersion':'1','status':'succeeded','data':{'text':'Evidence. '*800}}
        projected=runtime.model_result('mcp__rag__search',result,{'operationId':'large'})
        pointer=projected['data']['text']['pointer']
        args=json.dumps({'resultRef':'large','pointer':pointer})
        page=runtime.execute('session_read_result',args,{'operationId':'read'})
        self.assertEqual(result['data']['text'],page['value'])
        service['mcpTools']=[]
        with self.assertRaisesRegex(CoreError,'TOOL_REVOKED'):
            runtime.execute('session_read_result',args,{'operationId':'revoked'})


if __name__ == '__main__': unittest.main()
