import json
import tempfile
import unittest
from pathlib import Path
from agent_core.app import create_app
from test_core import configuration


class ServiceModelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        path=Path(self.temp.name)/'core.json'
        config=configuration(Path(self.temp.name)/'core.db')
        config['models'].append({**config['models'][0],'id':'alternate','model':'alternate-model'})
        config['services'][0]['modelProfiles'].append('alternate')
        path.write_text(json.dumps(config),encoding='utf-8')
        self.app=create_app(config,start_worker=False,config_path=path)
        self.client=self.app.test_client();self.path=path
        self.headers={'Authorization':'Bearer first'}

    def tearDown(self):self.temp.cleanup()

    def test_settings_are_scoped_and_default_is_persisted_and_used(self):
        result=self.client.get('/v1/settings/model',headers=self.headers).json
        self.assertEqual([m['id'] for m in result['models']],['local','alternate'])
        self.assertNotIn('apiKey',str(result));self.assertNotIn('baseUrl',str(result))
        denied=self.client.put('/v1/settings/model',headers={'Authorization':'Bearer second'},json={'revision':result['revision'],'modelProfile':'alternate'})
        self.assertEqual(denied.status_code,403)
        updated=self.client.put('/v1/settings/model',headers=self.headers,json={'revision':result['revision'],'modelProfile':'alternate'})
        self.assertEqual(updated.status_code,200,updated.json)
        self.assertEqual(json.loads(self.path.read_text())['services'][0]['defaultModelProfile'],'alternate')
        self.assertEqual(json.loads(self.path.read_text())['services'][1]['defaultModelProfile'],'local')
        self.assertEqual(self.client.get('/v1/capabilities',headers=self.headers).json['defaultModelProfile'],'alternate')
        from agent_core.config import model_profile
        self.assertEqual(model_profile(self.app.extensions['core_config'],self.app.extensions['core_config']['services'][0])['model'],'alternate-model')
        stale=self.client.put('/v1/settings/model',headers=self.headers,json={'revision':result['revision'],'modelProfile':'local'})
        self.assertEqual(stale.status_code,409)

    def test_caller_cannot_change_grants_or_other_service(self):
        result=self.client.get('/v1/settings/model',headers=self.headers).json
        response=self.client.put('/v1/settings/model',headers=self.headers,json={'revision':result['revision'],'modelProfile':'local','modelProfiles':['alternate'],'serviceId':'second'})
        self.assertEqual(response.status_code,400)
