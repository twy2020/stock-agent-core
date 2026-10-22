import copy
import hashlib
import json
import tempfile
import threading
import unittest
import httpx
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from flask import Flask, jsonify
from agent_core.security import CoreError
from agent_core.store import Store


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'config.json'
        self.config = {
            'database': str(Path(self.temp.name) / 'core.db'),
            'services': [{'id': 'app', 'tenantId': 'tenant', 'tokenSha256': hashlib.sha256(b'caller-secret').hexdigest(),
                          'domains': ['chat'], 'modelProfiles': ['model'], 'defaultModelProfile': 'model', 'tools': ['tool'],
                          'ragCallback': {'url': 'http://localhost:1/rag', 'tokenFile': 'caller-private-path'}}],
            'models': [{'id': 'model', 'model': 'test', 'baseUrl': 'http://localhost:1/v1', 'apiKeyFile': 'model-key-file'}],
            'httpTools': [{'name': 'tool', 'url': 'http://localhost:1/tool?private=hidden', 'effect': 'read', 'version': '1'}]
        }
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        self.app = Flask(__name__)
        self.app.testing = True
        self.app.register_error_handler(CoreError, lambda e: (jsonify({'error': {'code': e.code}}), e.status))
        self.store = Store(self.config['database'])
        from agent_core.management import register_management
        register_management(self.app, self.config, self.store, self.path)
        self.client = self.app.test_client()

    def get(self):
        return self.client.get('/admin/api/registry')

    def test_provider_proxy_is_validated_inherited_and_can_be_cleared(self):
        self.connection('providers','proxied',{'name':'Proxy','baseUrl':'https://example.com/v1'})
        response=self.update({'proxyUrl':'http://host.docker.internal:10808'},'providers','proxied')
        self.assertEqual(response.status_code,200,response.json)
        from agent_core.providers import resolve_key
        key=self.config['providerKeys'][0]
        self.assertEqual(resolve_key(self.config,key['id'])['proxyUrl'],'http://host.docker.internal:10808')
        for invalid in ('http://user:password@proxy:1234','http://proxy:1234/path','http://proxy:1234?key=private','socks5://proxy:1080'):
            self.assertEqual(self.update({'proxyUrl':invalid},'providers','proxied').status_code,400)
        self.assertEqual(self.update({'proxyUrl':''},'providers','proxied').status_code,200)
        self.assertIsNone(resolve_key(self.config,key['id'])['proxyUrl'])

    def test_model_probe_is_bounded_private_and_does_not_change_configuration(self):
        before=self.path.read_bytes()
        with patch('agent_core.model.OpenAICompatibleClient.stream',return_value=iter([{'type':'delta','content':'OK'},{'type':'usage','prompt_tokens':5,'completion_tokens':1}])) as stream:
            result=self.client.post('/admin/api/models/model/test',json={'revision':self.get().json['revision'],'mode':'text'})
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json['status'],'succeeded')
        self.assertEqual(result.json['mode'],'text')
        self.assertEqual(stream.call_args.kwargs['timeout_seconds'],15)
        self.assertNotIn('model-key-file',result.text)
        self.assertEqual(self.path.read_bytes(),before)
        from agent_core.providers import usage_summary
        usage=usage_summary(self.store)['recent'][0]
        self.assertEqual(usage['source'],'diagnostic')
        self.assertEqual(usage['prompt_tokens'],5)

    def test_model_probe_reports_failure_and_rejects_stale_configuration(self):
        from agent_core.model import ProviderError
        with patch('agent_core.model.OpenAICompatibleClient.stream',side_effect=ProviderError('AUTHENTICATION',http_status=401)):
            result=self.client.post('/admin/api/models/model/test',json={'revision':self.get().json['revision'],'mode':'tools'})
        self.assertEqual(result.json['code'],'AUTHENTICATION')
        self.assertEqual(result.json['httpStatus'],401)
        self.assertEqual(self.client.post('/admin/api/models/model/test',json={'revision':'old','mode':'text'}).status_code,409)

    def test_tool_probe_requires_a_complete_valid_tool_call(self):
        for arguments,status in [('{"value":"OK"}','succeeded'),('{','failed'),('{"value":"wrong"}','failed')]:
            with self.subTest(arguments=arguments),patch('agent_core.model.OpenAICompatibleClient.stream',return_value=iter([
                {'type':'tool_call','index':0,'id':'probe','function':{'name':'connection_probe','arguments':arguments}}])):
                result=self.client.post('/admin/api/models/model/test',json={'revision':self.get().json['revision'],'mode':'tools'})
                self.assertEqual(result.json['status'],status)


    def connection(self,kind,identifier,changes,api_key='private-entered-key',**extra):
        return self.client.post('/admin/api/connections',json={
            'revision':self.get().json['revision'],'kind':kind,'id':identifier,
            'changes':changes,'apiKey':api_key,**extra})

    def test_provider_and_key_can_be_saved_atomically_from_one_form(self):
        response=self.connection('providers','official',{'name':'Official','baseUrl':'https://api.example/v1','providerType':'OPENAI','enabled':True})
        self.assertEqual(response.status_code,200,response.json)
        key=self.config['providerKeys'][0];account=self.config['providerAccounts'][0]
        self.assertEqual(account['providerId'],'official')
        self.assertEqual(key['accountId'],account['id'])
        self.assertEqual(Path(key['apiKeyFile']).read_text(),'private-entered-key')
        self.assertTrue(response.json['providerKeys'][0]['credentialConfigured'])
        for private in ('private-entered-key',key['apiKeyFile']):self.assertNotIn(private,response.text)
        response=self.connection('providers','official',{'name':'Updated'},'replacement',credentialId=key['id'])
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(len(self.config['providerKeys']),1)
        self.assertEqual(Path(self.config['providerKeys'][0]['apiKeyFile']).read_text(),'replacement')

    def test_legacy_model_and_tool_accept_visible_credentials_without_reference_fields(self):
        response=self.connection('models','model',{'baseUrl':'https://api.example/v1','model':'updated'})
        self.assertEqual(response.status_code,200,response.json)
        self.assertNotIn('apiKeyEnv',self.config['models'][0])
        self.assertTrue(response.json['models'][0]['credentialConfigured'])
        response=self.connection('httpTools','tool',{'url':'https://tool.example/execute'},'private-tool-token')
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(Path(self.config['httpTools'][0]['tokenFile']).read_text(),'private-tool-token')
        self.assertNotIn('private-tool-token',response.text)

    def test_connection_rejects_other_provider_key_and_invalid_changes_without_partial_save(self):
        for identifier in ('one','two'):
            self.assertEqual(self.connection('providers',identifier,{'name':identifier,'baseUrl':'https://api.example/v1'}).status_code,200)
        before=self.path.read_bytes();key=self.config['providerKeys'][0]
        response=self.connection('providers','two',{'name':'bad'},credentialId=key['id'])
        self.assertEqual(response.status_code,400,response.json)
        self.assertEqual(self.path.read_bytes(),before)
        response=self.connection('models','model',{'apiKeyFile':'arbitrary'})
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.path.read_bytes(),before)

    def test_model_key_rotation_updates_live_shared_key_and_can_switch_to_direct(self):
        self.assertEqual(self.connection('providers','official',{'name':'Official','baseUrl':'https://api.example/v1'}).status_code,200)
        key=self.config['providerKeys'][0]
        response=self.connection('models','model',{'keyId':key['id']},'shared-key')
        self.assertEqual(response.status_code,200,response.json)
        self.assertIs(key,self.config['providerKeys'][0])
        self.assertEqual(Path(key['apiKeyFile']).read_text(),'shared-key')
        self.assertNotIn('apiKeyFile',self.config['models'][0])
        response=self.connection('models','model',{'keyId':'','baseUrl':'https://direct.example/v1'},'direct-key')
        self.assertEqual(response.status_code,200,response.json)
        self.assertNotIn('keyId',self.config['models'][0])
        self.assertEqual(Path(self.config['models'][0]['apiKeyFile']).read_text(),'direct-key')
        self.assertEqual(Path(key['apiKeyFile']).read_text(),'shared-key')

    def update(self, changes, kind='services', identifier='app', revision=None, client=None):
        return (client or self.client).patch('/admin/api/registry', json={
            'kind': kind, 'id': identifier, 'changes': changes,
            'revision': revision or self.get().json['revision']})

    def test_capability_can_detach_plugin_without_losing_its_configuration(self):
        self.assertEqual(self.update({'name': 'Bundle'}, 'plugins', 'bundle').status_code, 200)
        self.assertEqual(self.update({'name': 'Review', 'instructions': 'Review evidence', 'domains': ['cad'],
                                     'pluginId': 'bundle'}, 'skills', 'review').status_code, 200)
        self.assertEqual(self.delete('plugins', 'bundle').status_code, 409)
        response = self.update({'pluginId': ''}, 'skills', 'review')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertNotIn('pluginId', self.config['skills'][0])
        self.assertEqual(self.config['skills'][0]['instructions'], 'Review evidence')
        self.assertEqual(self.delete('plugins', 'bundle').status_code, 200)

    def test_registry_exposes_editable_metadata_without_caller_credentials(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        for group in ('providers', 'providerAccounts', 'providerKeys', 'models', 'services', 'mcpServers', 'skills', 'plugins', 'httpTools'):
            self.assertIsInstance(response.json[group], list)
        for secret in ('tokenSha256', 'caller-private-path', 'private=hidden', self.config['services'][0]['tokenSha256']):
            self.assertNotIn(secret, response.text)
        self.assertEqual(response.json['httpTools'][0]['url'], 'http://localhost:1/tool')
        self.assertTrue(response.json['writable'])

    def test_existing_caller_grants_persist_and_identity_is_preserved(self):
        service = self.config['services'][0]
        response = self.update({'name': 'Client', 'tools': [], 'maxOutputTokens': 2048, 'enabled': False})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIs(service, self.config['services'][0])
        self.assertFalse(service['enabled'])
        self.assertEqual(response.json['services'][0]['maxOutputTokens'], 2048)
        self.assertEqual(json.loads(self.path.read_text())['services'][0]['tools'], [])
        for changes in ({'tenantId': 'evil'}, {'tokenSha256': 'a' * 64}, {'ragCallback': {}}, {'maxActiveRuns': True}):
            self.assertEqual(self.update(changes).status_code, 400)
        self.assertEqual(self.update({'name': 'New'}, identifier='new').status_code, 404)

    def test_hierarchy_upsert_and_credential_reference_switch(self):
        for kind, identifier, changes in (
            ('providers', 'p', {'baseUrl': 'https://api.example.test/v1', 'providerType': 'OPENAI'}),
            ('providerAccounts', 'a', {'providerId': 'p'}),
            ('providerKeys', 'k', {'accountId': 'a', 'apiKeyEnv': 'PROVIDER_KEY'}),
            ('models', 'm', {'keyId': 'k', 'model': 'upstream', 'capabilities': {'vision': True}, 'pricing': {'inputPerMillion': 1, 'outputPerMillion': 2, 'currency': 'USD'}}),
        ):
            response = self.update(changes, kind, identifier)
            self.assertEqual(response.status_code, 200, response.json)
        response = self.update({'apiKeyFile': 'keys/provider.txt'}, 'providerKeys', 'k')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertNotIn('apiKeyEnv', self.config['providerKeys'][0])
        self.assertEqual(self.config['providerKeys'][0]['apiKeyFile'], 'keys/provider.txt')

    def test_invalid_references_and_plaintext_never_persist(self):
        before = self.path.read_bytes()
        cases = [
            ('providerAccounts', 'a', {'providerId': 'missing'}),
            ('models', 'm', {'keyId': 'missing', 'model': 'x'}),
            ('services', 'app', {'tools': ['missing']}),
            ('services', 'app', {'defaultModelProfile': 'missing'}),
            ('services', 'app', {'skills': ['missing']}),
            ('skills', 's', {'instructions': 'hello', 'pluginId': 'missing'}),
            ('providerKeys', 'k', {'accountId': 'missing', 'apiKey': 'plaintext-secret'}),
            ('mcpServers', 's', {'transport': 'streamable-http', 'url': 'https://example.test/mcp', 'headers': {'Authorization': 'secret'}}),
            ('mcpServers', 's', {'transport': 'streamable-http', 'url': 'https://example.test/mcp?api_key=secret'}),
            ('mcpServers', 's', {'transport': 'streamable-http', 'url': 'https://example.test/mcp', 'tokenEnv': 'bad name'}),
            ('mcpServers', 's', {'transport': 'streamable-http', 'url': 'https://example.test/mcp', 'tools': [{'name': 'x', 'description': '', 'effect': 'read', 'token': 'secret'}]}),
            ('httpTools', 'tool', {'inputSchema': {'type': 'nonsense'}}),
            ('httpTools', 'tool', {'outputSchema': {'$ref': 'https://example.test/schema'}}),
            ('httpTools', 'tool', {'timeoutSeconds': 'slow'}),
            ('httpTools', 'tool', {'transport': 'shell'}),
        ]
        for kind, identifier, changes in cases:
            with self.subTest(changes=changes):
                response = self.update(changes, kind, identifier)
                self.assertEqual(response.status_code, 400, response.json)
                self.assertEqual(self.path.read_bytes(), before)

    def test_same_revision_concurrent_writers_only_one_succeeds(self):
        revision = self.get().json['revision']; barrier = threading.Barrier(2)
        def write(name):
            with self.app.test_client() as client:
                barrier.wait()
                return self.update({'name': name}, revision=revision, client=client).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(write, ['A', 'B'])), [200, 409])
        self.assertEqual(json.loads(self.path.read_text()), self.config)

    def test_active_runs_and_external_file_edits_block_updates(self):
        run = self.store.enqueue(self.config['services'][0], 'chat', {'context': {}}, 'request')
        self.assertEqual(self.update({'enabled': False}).status_code, 409)
        self.store.cancel(run['id'])
        self.path.write_bytes(self.path.read_bytes() + b' ')
        response = self.update({'enabled': False})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json['error']['code'], 'CONFIGURATION_FILE_CHANGED')
        self.assertNotIn('enabled', self.config['services'][0])

    def test_active_gateway_calls_block_registry_mutations(self):
        self.app.extensions['core_gateway_state'] = {'lock': threading.RLock(), 'active': {('app', 'tenant'): 1}}
        before = self.path.read_bytes()
        response = self.update({'enabled': False})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json['error']['code'], 'ACTIVE_GATEWAY_CONFIGURATION_LOCKED')
        self.assertEqual(self.path.read_bytes(), before)
        self.app.extensions['core_gateway_state']['active'].clear()
        self.assertEqual(self.update({'enabled': False}).status_code, 200)

    def test_replace_failure_keeps_memory_file_revision_and_audit(self):
        before = copy.deepcopy(self.config); raw = self.path.read_bytes(); revision = self.get().json['revision']
        with patch('agent_core.management.os.replace', side_effect=OSError('private-details')):
            response = self.update({'enabled': False})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private-details', response.text)
        self.assertEqual(self.config, before)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.get().json['revision'], revision)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM admin_audit').fetchone()[0], 0)

    def test_usage_preserves_unknown_values(self):
        from agent_core.providers import record_usage
        record_usage(self.store, self.config['services'][0], self.config['models'][0], {}, 'failed')
        response = self.client.get('/admin/api/usage')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json['groups'][0]['estimatedCost'])
        self.assertEqual(response.json['groups'][0]['unknownUsageRequests'], 1)

    def test_discovery_preview_does_not_register_unapproved_models_or_tools(self):
        for kind, identifier, changes in (
            ('providers', 'p', {'baseUrl': 'https://api.example.test/v1'}),
            ('providerAccounts', 'a', {'providerId': 'p'}),
            ('providerKeys', 'k', {'accountId': 'a', 'apiKeyEnv': 'TEST_DISCOVERY_KEY'}),
            ('mcpServers', 'bridge', {'transport': 'http-bridge', 'tools': [
                {'name': 'lookup', 'description': 'Lookup', 'effect': 'read', 'httpTool': 'tool'}]}),
        ):
            self.assertEqual(self.update(changes, kind, identifier).status_code, 200)
        before = copy.deepcopy(self.config); raw = self.path.read_bytes()
        with patch.dict('os.environ', {'TEST_DISCOVERY_KEY': 'private-provider-key'}), patch(
                'agent_core.providers.httpx.stream', return_value=nullcontext(httpx.Response(200, json={
                    'data': [{'id': 'new-model', 'api_key': 'server-secret', 'capabilities': {'vision': True}, 'pricing': {'madeup': 4}}]}))):
            response = self.client.post('/admin/api/providers/k/discover')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['models'][0]['model'], 'new-model')
        self.assertNotIn('server-secret', response.text)
        self.assertNotIn('private-provider-key', response.text)
        response = self.client.post('/admin/api/mcp/bridge/discover')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json['approvalRequired'])
        self.assertEqual(response.json['tools'][0]['name'], 'lookup')
        self.assertEqual(self.config, before)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_unconfigured_billing_is_explicitly_unsupported(self):
        self.assertEqual(self.update({'baseUrl': 'https://api.example.test/v1'}, 'providers', 'p').status_code, 200)
        self.assertEqual(self.update({'providerId': 'p'}, 'providerAccounts', 'a').status_code, 200)
        response = self.client.post('/admin/api/accounts/a/billing', json={'start': 1000, 'end': 2000})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(response.json['supported'])

    def test_billing_metadata_edit_preserves_reference_and_can_switch_source(self):
        self.assertEqual(self.update({'baseUrl': 'https://api.example.test/v1'}, 'providers', 'p').status_code, 200)
        response = self.update({'providerId': 'p', 'billing': {'adapter': 'openai-organization', 'apiKeyEnv': 'BILLING_KEY'}}, 'providerAccounts', 'a')
        self.assertEqual(response.status_code, 200, response.json)
        response = self.update({'billing': {'adapter': 'openai-organization'}}, 'providerAccounts', 'a')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.config['providerAccounts'][0]['billing']['apiKeyEnv'], 'BILLING_KEY')
        response = self.update({'billing': {'apiKeyFile': 'billing.key'}}, 'providerAccounts', 'a')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertNotIn('apiKeyEnv', self.config['providerAccounts'][0]['billing'])

    def test_nonfinite_schema_values_are_rejected_without_server_error(self):
        response = self.update({'inputSchema': {'type': 'object', 'default': float('nan')}}, 'httpTools', 'tool')
        self.assertEqual(response.status_code, 400)

    def test_remote_mcp_to_bridge_drops_old_endpoint_and_credentials(self):
        self.assertEqual(self.update({'transport': 'streamable-http', 'url': 'https://example.test/mcp',
                                      'tokenEnv': 'MCP_KEY', 'tools': []}, 'mcpServers', 'mcp').status_code, 200)
        response = self.update({'transport': 'http-bridge', 'tools': [
            {'name': 'lookup', 'description': 'Lookup', 'effect': 'read', 'httpTool': 'tool'}]}, 'mcpServers', 'mcp')
        self.assertEqual(response.status_code, 200, response.json)
        saved = json.loads(self.path.read_text())['mcpServers'][0]
        for field in ('url', 'tokenEnv', 'tokenFile'):
            self.assertNotIn(field, saved)
            self.assertNotIn(field, self.config['mcpServers'][0])

    def test_legacy_admin_writer_rejects_active_gateway(self):
        from agent_core.app import create_app
        self.config['adminTokenSha256'] = hashlib.sha256(b'admin-secret').hexdigest()
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        app = create_app(self.config, start_worker=False, config_path=self.path)
        client = app.test_client(); admin = {'X-Core-Admin-Request': '1'}
        client.post('/admin/api/auth/login', headers=admin, json={'username': 'admin', 'password': 'admin123'})
        client.post('/admin/api/auth/password', headers=admin, json={'currentPassword': 'admin123', 'newPassword': 'test-admin-password'})
        revision = client.get('/admin/api/configuration', headers=admin).json['revision']
        gateway = app.extensions['core_gateway_state']
        gateway['active'][('app', 'tenant')] = 1
        before = self.path.read_bytes()
        payload = {'revision': revision, 'kind': 'callers', 'id': 'app', 'changes': {'enabled': False}}
        response = client.patch('/admin/api/configuration', headers=admin, json=payload)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json['code'], 'ACTIVE_GATEWAY_CONFIGURATION_LOCKED')
        self.assertEqual(self.path.read_bytes(), before)
        gateway['active'].clear()
        self.assertEqual(client.patch('/admin/api/configuration', headers=admin, json=payload).status_code, 200)

    def test_integrated_admin_auth_and_shared_configuration_revision(self):
        from agent_core.app import create_app
        self.config['adminTokenSha256'] = hashlib.sha256(b'admin-secret').hexdigest()
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        app = create_app(self.config, start_worker=False, config_path=self.path)
        client = app.test_client(); admin = {'X-Core-Admin-Request': '1'}
        caller = {'Authorization': 'Bearer caller-secret'}
        for method, route in [('get', 'registry'), ('patch', 'registry'), ('get', 'usage'),
                              ('post', 'providers/missing/discover'), ('post', 'mcp/missing/discover'), ('post', 'accounts/missing/billing'),
                              ('post', 'provider-keys'), ('delete', 'registry/models/model')]:
            self.assertEqual(getattr(client, method)('/admin/api/' + route, headers=caller).status_code, 401)
        client.post('/admin/api/auth/login', headers=admin, json={'username': 'admin', 'password': 'admin123'})
        client.post('/admin/api/auth/password', headers=admin, json={'currentPassword': 'admin123', 'newPassword': 'test-admin-password'})
        revision = client.get('/admin/api/registry', headers=admin).json['revision']
        response = client.patch('/admin/api/registry', headers=admin, json={
            'revision': revision, 'kind': 'services', 'id': 'app', 'changes': {'name': 'Renamed'}})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(client.get('/admin/api/configuration', headers=admin).json['revision'], response.json['revision'])
        old = client.patch('/admin/api/configuration', headers=admin, json={
            'revision': response.json['revision'], 'kind': 'callers', 'id': 'app', 'changes': {'maxOutputTokens': 2048}})
        self.assertEqual(old.status_code, 200, old.json)
        self.assertEqual(client.get('/admin/api/registry', headers=admin).json['revision'], old.json['revision'])

    def provider_setup(self):
        self.assertEqual(self.update({'baseUrl': 'https://api.example.test/v1'}, 'providers', 'p').status_code, 200)
        self.assertEqual(self.update({'providerId': 'p'}, 'providerAccounts', 'a').status_code, 200)

    def save_secret(self, secret='private-key-value', **overrides):
        return self.client.post('/admin/api/provider-keys', json={
            'revision': self.get().json['revision'], 'id': 'k', 'accountId': 'a', 'name': 'Key',
            'enabled': True, 'secret': secret, **overrides})

    def delete(self, kind, identifier):
        return self.client.delete('/admin/api/registry/' + kind + '/' + identifier,
                                  json={'revision': self.get().json['revision']})

    def test_write_only_provider_secret_is_saved_without_echo_or_audit_retention(self):
        self.provider_setup()
        response = self.save_secret()
        self.assertEqual(response.status_code, 200, response.json)
        key = self.config['providerKeys'][0]; credential = Path(key['apiKeyFile'])
        self.assertEqual(credential.parent, self.path.parent / 'credentials')
        self.assertEqual(credential.suffix, '.token')
        self.assertEqual(credential.read_text(), 'private-key-value')
        for output in (response.text, self.get().text, self.path.read_text()):
            self.assertNotIn('private-key-value', output)
        with self.store.db() as db:
            self.assertNotIn('private-key-value', str([dict(row) for row in db.execute('SELECT * FROM admin_audit')]))
        self.assertEqual(self.save_secret('replacement-key').status_code, 200)
        self.assertNotEqual(self.config['providerKeys'][0]['apiKeyFile'], str(credential))
        self.assertEqual(credential.read_text(), 'private-key-value')

    def test_provider_secret_failed_config_save_removes_staged_file_and_keeps_old_ref(self):
        self.provider_setup()
        self.assertEqual(self.update({'accountId': 'a', 'apiKeyEnv': 'OLD_PROVIDER_KEY'}, 'providerKeys', 'k').status_code, 200)
        before = copy.deepcopy(self.config); raw = self.path.read_bytes()
        with patch('agent_core.management.os.replace', side_effect=OSError('private-key-value')):
            response = self.save_secret()
        self.assertEqual(response.status_code, 503, response.json)
        self.assertNotIn('private-key-value', response.text)
        self.assertEqual(self.config, before)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(list((self.path.parent / 'credentials').glob('*.token')), [])

    def test_provider_secret_rejects_arbitrary_path_bad_references_and_active_calls(self):
        self.provider_setup()
        for overrides in ({'apiKeyFile': '/arbitrary'}, {'accountId': 'missing'}, {'secret': ''}, {'secret': 'x\ny'}, {'secret': '\ud800'}):
            self.assertEqual(self.save_secret(**overrides).status_code, 400)
        self.app.extensions['core_gateway_state'] = {'lock': threading.RLock(), 'active': {('app', 'tenant'): 1}}
        self.assertEqual(self.save_secret().status_code, 409)
        self.assertFalse((self.path.parent / 'credentials').exists())

    def test_delete_blocks_provider_chain_and_model_grants_without_cascade(self):
        self.provider_setup()
        self.assertEqual(self.save_secret().status_code, 200)
        self.assertEqual(self.update({'keyId': 'k', 'model': 'new-model'}, 'models', 'new').status_code, 200)
        for kind, identifier in [('providers', 'p'), ('providerAccounts', 'a'), ('providerKeys', 'k'), ('models', 'model')]:
            response = self.delete(kind, identifier)
            self.assertEqual(response.status_code, 409, response.json)
            self.assertEqual(response.json['error']['code'], 'CONFIGURATION_ITEM_REFERENCED')
        self.assertEqual(self.delete('models', 'new').status_code, 200)
        self.assertEqual(self.delete('providerKeys', 'k').status_code, 200)
        self.assertEqual(self.delete('providerAccounts', 'a').status_code, 200)
        self.assertEqual(self.delete('providers', 'p').status_code, 200)
        self.assertEqual(self.config, json.loads(self.path.read_text()))
        self.assertEqual(self.delete('services', 'app').status_code, 400)

    def test_delete_blocks_plugin_and_mcp_wrapper_references(self):
        self.assertEqual(self.update({'name': 'Plugin'}, 'plugins', 'p').status_code, 200)
        self.assertEqual(self.update({'instructions': 'do useful work', 'pluginId': 'p'}, 'skills', 's').status_code, 200)
        self.assertEqual(self.delete('plugins', 'p').status_code, 409)
        self.assertEqual(self.update({'transport': 'http-bridge', 'tools': [
            {'name': 'lookup', 'description': 'Lookup', 'effect': 'read', 'httpTool': 'tool'}]}, 'mcpServers', 'm').status_code, 200)
        self.assertEqual(self.update({'tools': []}).status_code, 200)
        self.assertEqual(self.delete('httpTools', 'tool').status_code, 409)
        self.assertEqual(self.delete('mcpServers', 'm').status_code, 200)
        self.assertEqual(self.delete('httpTools', 'tool').status_code, 200)

    def test_lifecycle_routes_obey_stale_revision_and_active_run_lock(self):
        self.provider_setup(); revision = self.get().json['revision']
        self.assertEqual(self.update({'name': 'Renamed'}, 'providers', 'p').status_code, 200)
        self.assertEqual(self.save_secret(revision=revision).status_code, 409)
        run = self.store.enqueue(self.config['services'][0], 'chat', {'context': {}}, 'lifecycle')
        self.assertEqual(self.save_secret().status_code, 409)
        self.assertEqual(self.delete('providerAccounts', 'a').status_code, 409)
        self.store.cancel(run['id'])
        self.assertEqual(self.delete('providerAccounts', 'a').status_code, 200)

    def test_two_secret_writers_with_same_revision_leave_only_committed_file(self):
        self.provider_setup(); revision = self.get().json['revision']; barrier = threading.Barrier(2)
        def write(secret):
            with self.app.test_client() as client:
                barrier.wait()
                return client.post('/admin/api/provider-keys', json={
                    'revision': revision, 'id': 'k', 'accountId': 'a', 'name': 'Key', 'enabled': True, 'secret': secret}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(write, ['first-key', 'second-key'])), [200, 409])
        files = list((self.path.parent / 'credentials').glob('*.token'))
        self.assertEqual(len(files), 1)
        self.assertEqual(str(files[0]), self.config['providerKeys'][0]['apiKeyFile'])

    def test_delete_write_failure_preserves_entry_file_and_revision(self):
        self.provider_setup(); before = copy.deepcopy(self.config); raw = self.path.read_bytes()
        revision = self.get().json['revision']
        with patch('agent_core.management.os.replace', side_effect=OSError('private-error')):
            response = self.delete('providerAccounts', 'a')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.config, before)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.get().json['revision'], revision)


if __name__ == '__main__':
    unittest.main()
