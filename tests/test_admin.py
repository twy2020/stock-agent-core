import copy
import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent_core.app import create_app


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'config.json'
        self.config = {
            'database': str(Path(self.temp.name) / 'core.db'),
            'adminTokenSha256': hashlib.sha256(b'admin').hexdigest(),
            'services': [{'id': 'app', 'tenantId': 'tenant', 'tokenSha256': hashlib.sha256(b'app').hexdigest(),
                          'domains': ['chat'], 'modelProfiles': ['model'], 'defaultModelProfile': 'model', 'tools': ['tool']}],
            'models': [{'id': 'model', 'model': 'test', 'baseUrl': 'http://localhost:1/v1', 'apiKeyFile': 'secret-path'}],
            'httpTools': [{'name': 'tool', 'url': 'http://localhost:1/tool?private=hidden', 'effect': 'read', 'version': '1', 'description': 'Evidence'}]
        }
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        self.app = create_app(self.config, start_worker=False)
        self.client = self.app.test_client()
        self.headers = {'X-Core-Admin-Request': '1'}
        self.login_admin()
        self.store = self.app.extensions['core_store']

    def tearDown(self):
        self.temp.cleanup()

    def writable(self):
        import inspect
        self.assertIn('config_path', inspect.signature(create_app).parameters)
        self.app = create_app(self.config, start_worker=False, config_path=self.path)
        self.client = self.app.test_client()
        self.store = self.app.extensions['core_store']
        self.login_admin()

    def login_admin(self):
        result = self.client.post('/admin/api/auth/login', headers=self.headers,
                                  json={'username': 'admin', 'password': 'admin123'})
        if result.status_code != 200:
            result = self.client.post('/admin/api/auth/login', headers=self.headers,
                                      json={'username': 'admin', 'password': 'test-admin-password'})
        self.assertEqual(result.status_code, 200)
        if result.json['mustChangePassword']:
            self.assertEqual(self.client.post('/admin/api/auth/password', headers=self.headers,
                json={'currentPassword': 'admin123', 'newPassword': 'test-admin-password'}).status_code, 200)

    def run_record(self, text='hello'):
        body = {'context': {'conversation_id': 'c'}, 'messages': [{'role': 'user', 'content': text}],
                'images': [{'mediaType': 'image/png', 'width': 4, 'height': 5, 'data': 'secret-image-bytes'}]}
        return self.store.enqueue(self.config['services'][0], 'chat', body, str(uuid.uuid4()))['id']

    def get(self, path):
        return self.client.get('/admin/api/' + path, headers=self.headers)

    def update(self, changes, kind='callers', id='app', revision=None):
        revision = revision or self.get('configuration').json['revision']
        return self.client.patch('/admin/api/configuration', headers=self.headers,
                                 json={'revision': revision, 'kind': kind, 'id': id, 'changes': changes})

    def test_all_admin_apis_require_admin_not_service_credentials(self):
        anonymous = self.app.test_client()
        for route in ('status', 'runs', 'runs/missing', 'runs/missing/events', 'configuration'):
            self.assertEqual(anonymous.get('/admin/api/' + route, headers={'Authorization': 'Bearer app'}).status_code, 401)
        self.assertEqual(self.get('runs').status_code, 200)

    def test_runs_filter_page_and_count(self):
        first = self.run_record(); self.store.cancel(first)
        second = self.run_record()
        response = self.get('runs?status=CANCELLED&caller=app&limit=1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['total'], 1)
        self.assertEqual(response.json['items'][0]['id'], first)
        self.assertEqual(self.get('runs?q=' + second).json['total'], 1)
        self.assertEqual(self.get('runs?offset=1&limit=1').json['items'][0]['id'], first)
        self.assertEqual(self.get('runs?limit=0').status_code, 400)
        self.assertEqual(self.get('status').json['summary']['counts']['CANCELLED'], 1)

    def test_details_use_events_and_keep_safe_input_after_finish(self):
        rid = self.run_record(); self.store.claim(rid)
        self.store.append(rid, 'model', {'id': 'model', 'model': 'test'})
        self.store.append(rid, 'tool_call', {'operationId': 'one', 'tool': 'tool', 'arguments': {'question': 'why', 'api_key': 'hidden-key'}})
        self.store.append(rid, 'tool_result', {'operationId': 'one', 'tool': 'tool', 'result': {'data': {'answer': 42, 'password': 'secret-password'}}})
        self.store.append(rid, 'usage', {'prompt_tokens': 3, 'completion_tokens': 2})
        self.store.append(rid, 'delta', {'content': 'answer'})
        self.store.finish(rid, outcome={'status': 'complete'})
        response = self.get('runs/' + rid)
        self.assertEqual(response.status_code, 200)
        result = response.json
        self.assertEqual(result['output'], 'answer')
        self.assertTrue(result['input']['available'])
        self.assertEqual(result['input']['messages'][0]['content'], 'hello')
        self.assertEqual(result['input']['images'][0]['mediaType'], 'image/png')
        self.assertEqual(result['toolCalls'][0]['arguments']['question'], 'why')
        self.assertEqual(result['toolCalls'][0]['result']['data']['answer'], 42)
        self.assertEqual(result['usage']['prompt_tokens'], 3)
        self.assertEqual(result['outcome'], {'status': 'complete'})
        for secret in ('hidden-key', 'secret-password', 'secret-image-bytes'):
            self.assertNotIn(secret, response.text)
        self.assertNotIn('time', result['timeline'][0])

    def test_missing_historical_input_is_explicit_and_cancel_is_idempotent(self):
        rid = self.run_record()
        with self.store.db() as db:
            db.execute("DELETE FROM events WHERE run_id=? AND name='input'", (rid,))
        response = self.client.post('/admin/api/runs/' + rid + '/cancel', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['run']['status'], 'CANCELLED')
        self.assertFalse(response.json['input']['available'])
        count = len(self.store.events(rid, 0))
        self.client.post('/admin/api/runs/' + rid + '/cancel', headers=self.headers)
        self.assertEqual(len(self.store.events(rid, 0)), count)

    def test_events_cursor_redacts_recursively_and_limits(self):
        rid = self.run_record(); self.store.claim(rid)
        self.store.append(rid, 'retrieval', {'nested': {'Authorization': 'secret-auth', 'base64': 'secret-bytes'}, 'text': 'x' * 30000})
        response = self.get('runs/' + rid + '/events?limit=1')
        self.assertEqual(response.status_code, 200)
        page = response.json
        self.assertEqual(len(page['items']), 1)
        self.assertTrue(page['hasMore'])
        response = self.get('runs/' + rid + '/events?after=' + str(page['nextCursor']))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('secret-auth', response.text)
        self.assertNotIn('secret-bytes', response.text)
        self.assertNotIn('x' * 30000, response.text)

    def test_configuration_safe_projection_and_readonly_without_file(self):
        result = self.get('configuration')
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json['writable'])
        self.assertEqual(result.json['tools'][0]['endpoint'], 'http://localhost:1/tool')
        for secret in ('secret-path', 'tokenSha256', 'apiKeyFile', self.temp.name, 'private=hidden'):
            self.assertNotIn(secret, result.text)
        self.assertEqual(self.update({'enabled': False}).status_code, 409)

    def test_atomic_configuration_update_and_stale_revision(self):
        self.writable()
        revision = self.get('configuration').json['revision']
        response = self.update({'enabled': False, 'maxActiveRuns': 3, 'maxOutputTokens': 2048}, revision=revision)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(self.config['services'][0]['enabled'])
        self.assertFalse(json.loads(self.path.read_text())['services'][0]['enabled'])
        self.assertNotEqual(response.json['revision'], revision)
        self.assertEqual(self.update({'enabled': True}, revision=revision).status_code, 409)
        self.assertEqual(self.update({'name': 'New name'}, kind='models', id='model').status_code, 200)
        self.assertEqual(self.update({'enabled': False}, kind='tools', id='tool').status_code, 200)
        from agent_core.registry import catalog
        self.assertEqual(catalog(self.config, self.config['services'][0], 'chat'), {})

    def test_configuration_rejects_active_runs_external_edits_and_unsafe_fields(self):
        self.writable()
        for changes in ({'tools': []}, {'maxActiveRuns': 9}, {'maxOutputTokens': True}, {'enabled': 'yes'}):
            self.assertEqual(self.update(changes).status_code, 400)
        rid = self.run_record()
        self.assertEqual(self.update({'enabled': False}).status_code, 409)
        self.store.cancel(rid)
        original = self.path.read_bytes(); self.path.write_bytes(original + b' ')
        self.assertEqual(self.update({'enabled': False}).status_code, 409)
        self.assertNotIn('enabled', self.config['services'][0])

    def test_configuration_write_failure_leaves_memory_and_file_unchanged(self):
        self.writable(); before = copy.deepcopy(self.config); raw = self.path.read_bytes()
        with patch('agent_core.admin.os.replace', side_effect=OSError('private filesystem detail')):
            response = self.update({'enabled': False})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.config, before)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertNotIn('private filesystem detail', response.text)

    def test_real_tool_progress_and_failure_are_reported_without_invented_arguments(self):
        rid = self.run_record(); self.store.claim(rid)
        self.store.append(rid, 'progress', {'stage': 'TOOL_RUNNING', 'tool': 'tool', 'operationId': 'one', 'status': 'RUNNING'})
        self.store.append(rid, 'tool_result', {'operationId': 'one', 'tool': 'tool', 'result': {'status': 'failed', 'error': 'UPSTREAM_FAILED'}})
        self.store.append(rid, 'progress', {'stage': 'TOOL_FAILED', 'tool': 'tool', 'operationId': 'one', 'status': 'FAILED', 'code': 'UPSTREAM_FAILED'})
        self.store.append(rid, 'progress', {'stage': 'TOOL_RUNNING', 'tool': 'tool', 'operationId': 'two', 'status': 'RUNNING'})
        calls = self.get('runs/' + rid).json['toolCalls']
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]['error'], 'UPSTREAM_FAILED')
        self.assertEqual(calls[0]['status'], 'FAILED')
        self.assertIsNone(calls[0]['arguments'])
        self.assertEqual(calls[1]['status'], 'RUNNING')

    def test_disabled_caller_cannot_enqueue_after_config_changes(self):
        self.writable(); service = self.config['services'][0]
        self.assertEqual(self.update({'enabled': False}).status_code, 200)
        from agent_core.security import CoreError
        with self.assertRaises(CoreError) as exc:
            self.store.enqueue(service, 'chat', {'context': {}}, str(uuid.uuid4()))
        self.assertEqual(exc.exception.code, 'SERVICE_REVOKED')


if __name__ == '__main__':
    unittest.main()
