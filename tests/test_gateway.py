import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from flask import Flask, g, jsonify, request
from PIL import Image
from agent_core.config import validate_config
from agent_core.gateway import register_gateway
from agent_core.providers import usage_summary
from agent_core.security import CoreError, authenticate
from agent_core.store import Store
from test_providers import configuration, mock_stream


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.config = configuration(Path(self.temp.name) / 'core.db')
        validate_config(self.config); self.store = Store(self.config['database']); app = Flask(__name__)
        @app.before_request
        def auth(): g.service = authenticate(self.config, request.headers.get('Authorization', ''))
        @app.errorhandler(CoreError)
        def error(exc): return jsonify({'code': exc.code}), exc.status
        register_gateway(app, self.config, self.store); self.client = app.test_client(); self.headers = {'Authorization': 'Bearer app'}
        self.env = patch.dict('os.environ', {'TEST_GATEWAY_KEY': 'never-expose'}); self.env.start()

    def tearDown(self): self.env.stop(); self.temp.cleanup()

    def post(self, **extra):
        return self.client.post('/v1/chat/completions', headers=self.headers, json={'model': 'chat', 'messages': [{'role': 'user', 'content': 'hi'}], **extra})

    def completion(self, usage=True):
        value = {'id': 'response', 'model': 'private-model', 'choices': [{'message': {'role': 'assistant', 'content': 'hello'}, 'finish_reason': 'stop'}]}
        if usage: value['usage'] = {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}
        return value

    def test_lists_only_authorized_enabled_models_and_denies_unapproved_calls(self):
        self.config['models'].append({'id': 'hidden', 'keyId': 'k', 'model': 'hidden'})
        self.assertEqual([v['id'] for v in self.client.get('/v1/models', headers=self.headers).json['data']], ['chat'])
        self.assertEqual(self.post(model='hidden').status_code, 403)
        self.assertEqual(self.client.get('/v1/models').status_code, 401)
        self.config['providerAccounts'][0]['enabled'] = False
        self.assertEqual(self.post().status_code, 503)
        self.assertEqual(self.client.get('/v1/models', headers=self.headers).json['data'], [])

    def test_nonstream_routes_profile_records_usage_and_bounds_options(self):
        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(payload['model'], 'upstream-model'); self.assertFalse(payload['stream'])
            self.assertEqual(request.headers['authorization'], 'Bearer never-expose')
            return httpx.Response(200, json=self.completion())
        with patch('agent_core.gateway.httpx.stream', side_effect=lambda *a, **kw: mock_stream(handler, *a, **kw)):
            response = self.post()
        self.assertEqual(response.status_code, 200); self.assertEqual(response.json['model'], 'chat')
        group = usage_summary(self.store)['groups'][0]
        self.assertEqual(group['completed'], 1); self.assertEqual(group['totalTokens'], 5)
        self.assertIsNone(group['estimatedCost'])
        for value in ({'baseUrl': 'http://evil'}, {'max_tokens': 8193}, {'temperature': float('nan')}, {'tools': [{'type': 'web_search'}]}):
            self.assertEqual(self.post(**value).status_code, 400)

    def test_stream_relays_sse_and_records_trailing_usage(self):
        chunks = [{'choices': [{'delta': {'content': 'hello'}, 'finish_reason': None}]},
                  {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}, {'choices': [], 'usage': {'prompt_tokens': 7, 'completion_tokens': 4}}]
        def handler(request):
            self.assertTrue(json.loads(request.content)['stream_options']['include_usage'])
            return httpx.Response(200, content=''.join('data: ' + json.dumps(v) + '\n\n' for v in chunks) + 'data: [DONE]\n\n')
        with patch('agent_core.gateway.httpx.stream', side_effect=lambda *a, **kw: mock_stream(handler, *a, **kw)):
            response = self.post(stream=True); text = response.text
        self.assertEqual(response.status_code, 200); self.assertTrue(text.endswith('data: [DONE]\n\n'))
        self.assertIn('hello', text); self.assertEqual(usage_summary(self.store)['groups'][0]['totalTokens'], 11)
        self.assertEqual(usage_summary(self.store)['groups'][0]['completed'], 1)

    def test_upstream_errors_and_incomplete_stream_do_not_leak_body(self):
        def denied(request): return httpx.Response(401, json={'error': {'message': 'never-expose'}})
        with patch('agent_core.gateway.httpx.stream', side_effect=lambda *a, **kw: mock_stream(denied, *a, **kw)):
            result = self.post(stream=True)
        self.assertEqual(result.status_code, 502); self.assertNotIn('never-expose', result.text)
        def partial(request): return httpx.Response(200, content='data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\ndata: [DONE]\n\n')
        with patch('agent_core.gateway.httpx.stream', side_effect=lambda *a, **kw: mock_stream(partial, *a, **kw)):
            result = self.post(stream=True); text = result.text
        self.assertIn('INCOMPLETE_UPSTREAM_RESPONSE', text); self.assertNotIn('data: [DONE]', text)
        self.assertEqual(usage_summary(self.store)['groups'][0]['unknownUsageRequests'], 2)

    def test_inline_images_require_direct_policy_and_explicit_model_capability(self):
        buffer = io.BytesIO(); Image.new('RGB', (2, 2)).save(buffer, format='PNG')
        messages = [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()}}]}]
        self.assertEqual(self.post(messages=messages).status_code, 403)
        self.config['services'][0]['visionPolicy'] = 'direct'
        self.assertEqual(self.post(messages=messages).status_code, 403)
        self.config['models'][0]['capabilities'] = {'vision': True}
        def handler(request):
            self.assertEqual(json.loads(request.content)['messages'], messages)
            return httpx.Response(200, json=self.completion(False))
        with patch('agent_core.gateway.httpx.stream', side_effect=lambda *a, **kw: mock_stream(handler, *a, **kw)):
            self.assertEqual(self.post(messages=messages).status_code, 200)
        messages[0]['content'][0]['image_url']['url'] = 'https://private/image.png'
        self.assertEqual(self.post(messages=messages).status_code, 400)

    def test_tool_calls_are_returned_without_executing_and_can_be_continued(self):
        tools = [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object'}}}]
        call = {'id': 'call1', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}}
        messages = [{'role': 'user', 'content': 'lookup'}, {'role': 'assistant', 'content': None, 'tool_calls': [call]},
                    {'role': 'tool', 'tool_call_id': 'call1', 'content': 'result'}]
        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(payload['tools'], tools); self.assertEqual(payload['messages'], messages)
            return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [call]}, 'finish_reason': 'tool_calls'}]})
        with patch('agent_core.gateway.httpx.stream', side_effect=lambda *a, **kw: mock_stream(handler, *a, **kw)):
            result = self.post(messages=messages, tools=tools, tool_choice='auto')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['choices'][0]['message']['tool_calls'], [call])

    def test_client_disconnect_records_unknown_usage_and_releases_capacity(self):
        self.config['services'][0]['maxActiveRuns'] = 1
        def handler(request):
            return httpx.Response(200, content='data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        with patch('agent_core.gateway.httpx.stream', side_effect=lambda *a, **kw: mock_stream(handler, *a, **kw)):
            first = self.post(stream=True)
            self.assertEqual(self.post(stream=True).status_code, 429)
            first.close()
            second = self.post(stream=True)
            self.assertEqual(second.status_code, 200)
            second.close()
        self.assertEqual(usage_summary(self.store)['recent'][0]['status'], 'disconnected')

    def test_oversized_upstream_body_is_rejected(self):
        def handler(request):
            return httpx.Response(200, content=b' ' * (4 * 1024 * 1024 + 1))
        with patch('agent_core.gateway.httpx.stream', side_effect=lambda *a, **kw: mock_stream(handler, *a, **kw)):
            result = self.post()
        self.assertEqual(result.status_code, 502); self.assertEqual(result.json['code'], 'UPSTREAM_RESPONSE_LIMIT')


if __name__ == '__main__': unittest.main()
