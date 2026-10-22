import hashlib
import tempfile
import unittest
import uuid
from unittest.mock import patch
from pathlib import Path


def configuration(path):
    return {'database': str(path), 'models': [{'id':'local','model':'test','baseUrl':'http://127.0.0.1:1/v1','apiKeyEnv':'TEST_CORE_MODEL_KEY'}],
            'services': [{'id':name,'tenantId':'tenant','tokenSha256':hashlib.sha256(name.encode()).hexdigest(),
                          'domains':['chat','cad'],'modelProfiles':['local'],'defaultModelProfile':'local',
                          'projectIds':['project'],'tools':[]} for name in ['first','second']], 'httpTools':[]}


class CoreContractTests(unittest.TestCase):
    def setUp(self):
        from agent_core.app import create_app
        self.temp=tempfile.TemporaryDirectory()
        self.config=configuration(Path(self.temp.name)/'core.sqlite3')
        self.app=create_app(self.config, start_worker=False)
        self.client=self.app.test_client()
        self.headers={'Authorization':'Bearer first'}
        self.other={'Authorization':'Bearer second'}
        self.context={'project_id':'project','drawing_id':'drawing','revision':'v1'}

    def tearDown(self):
        self.temp.cleanup()

    def chat(self):
        return {'request_id':str(uuid.uuid4()),'model_profile':'local','messages':[{'role':'user','content':'hello'}],
                'context':{'conversation_id':'conversation','knowledge_base_id':None,'actor_id':'actor','tenant_id':'tenant'}}

    def test_auth_domains_and_tenant_are_enforced(self):
        self.assertEqual(self.client.get('/v1/capabilities').status_code,401)
        body=self.chat();body['context']['tenant_id']='other'
        self.assertEqual(self.client.post('/v1/chat/runs',json=body,headers=self.headers).status_code,403)
        body=self.chat();body['model_profile']='caller-controlled'
        self.assertEqual(self.client.post('/v1/chat/runs',json=body,headers=self.headers).status_code,403)

    def test_run_idempotence_ownership_and_cancel_replay(self):
        body=self.chat()
        first=self.client.post('/v1/chat/runs',json=body,headers=self.headers)
        self.assertEqual(first.status_code,202,first.json)
        rid=first.json['id']
        duplicate=self.client.post('/v1/chat/runs',json=body,headers=self.headers)
        self.assertEqual(duplicate.json['id'],rid)
        body['messages'][0]['content']='different'
        self.assertEqual(self.client.post('/v1/chat/runs',json=body,headers=self.headers).status_code,409)
        self.assertEqual(self.client.get('/v1/runs/'+rid,headers=self.other).status_code,404)
        cancel=self.client.post('/v1/runs/'+rid+'/cancel',json={},headers=self.headers)
        self.assertEqual(cancel.json['status'],'CANCELLED')
        events=self.client.get('/v1/runs/'+rid+'/events',headers=self.headers).text
        self.assertIn('CANCELLED',events)
        self.assertIn('id: 1',events)
        replay=self.client.get('/v1/runs/'+rid+'/events?after=1',headers=self.headers).text
        self.assertNotIn('id: 1\n',replay)
        self.assertIn('CANCELLED',replay)

    def test_space_isolation_search_package_and_revision(self):
        base='/api/service/v1/spaces'
        result=self.client.post(base,json={'context':self.context},headers=self.headers)
        self.assertEqual(result.status_code,201,result.json)
        sid=result.json['id']
        doc={'title':'钢板','revision':'v1','chunks':[{'id':'c','text':'钢板厚度 12 mm','source':{'handle':'AA'}}]}
        self.assertEqual(self.client.put(f'{base}/{sid}/documents/plate',json=doc,headers=self.headers).status_code,200)
        found=self.client.post(f'{base}/{sid}/search',json={'question':'钢板厚度'},headers=self.headers).json
        self.assertEqual(found['results'][0]['source'],{'handle':'AA'})
        self.assertEqual(self.client.get(f'{base}/{sid}',headers=self.other).status_code,404)
        self.assertEqual(self.client.get(f'{base}/{sid}/package',headers=self.headers).json['documents'][0]['revision'],'v1')
        run={'requestId':str(uuid.uuid4()),'question':'分析','context':{**self.context,'revision':'v2'},'spaceId':sid,'tools':[]}
        self.assertEqual(self.client.post('/api/service/v1/runs',json=run,headers=self.headers).status_code,409)

    def test_interrupted_execution_is_failed_not_retried_on_restart(self):
        from agent_core.app import create_app
        rid=self.client.post('/v1/chat/runs',json=self.chat(),headers=self.headers).json['id']
        store=self.app.extensions['core_store']
        self.assertTrue(store.claim(rid))
        restarted=create_app(self.config,start_worker=False).test_client()
        self.assertEqual(restarted.get('/v1/runs/'+rid,headers=self.headers).json['status'],'FAILED')
        self.assertIn('WORKER_RESTARTED',restarted.get('/v1/runs/'+rid+'/events',headers=self.headers).text)

    def test_admin_is_separate_and_redacts_credentials(self):
        self.config['adminTokenSha256']=hashlib.sha256(b'admin').hexdigest()
        self.config['models'][0]['apiKeyFile']='secret-model-path'
        self.assertEqual(self.client.get('/admin/api/status',headers=self.headers).status_code,401)
        admin_headers={'X-Core-Admin-Request':'1'}
        self.assertEqual(self.client.get('/admin/api/status',headers={'Authorization':'Bearer admin'}).status_code,401)
        self.client.post('/admin/api/auth/login',headers=admin_headers,json={'username':'admin','password':'admin123'})
        self.client.post('/admin/api/auth/password',headers=admin_headers,json={'currentPassword':'admin123','newPassword':'test-admin-password'})
        response=self.client.get('/admin/api/status')
        self.assertEqual(response.status_code,200)
        self.assertNotIn('secret-model-path',response.text)
        self.assertNotIn('tokenSha256',response.text)
        self.assertEqual(response.json['models'][0]['id'],'local')

    def test_capabilities_follow_default_model_reasoning_support(self):
        self.config['models'][0].update(providerType='DEEPSEEK',model='deepseek-pro')
        response=self.client.get('/v1/capabilities',headers=self.headers)
        self.assertIs(response.json['reasoningToggle'],True)

    def test_unknown_tools_and_foreign_project_cannot_be_executed(self):
        response=self.client.post('/api/service/v1/tools/execute',headers=self.headers,
            json={'tool':'not-approved','context':self.context,'arguments':{}})
        self.assertEqual(response.status_code,403)

    def test_legacy_compute_and_analyze_tools_are_accepted(self):
        from agent_core.config import validate_config
        config=configuration(Path(self.temp.name)/'other.db')
        config['httpTools']=[{'name':'weight','effect':'compute','version':'1','url':'http://127.0.0.1/tool'},
                             {'name':'vision','effect':'analyze','version':'1','url':'http://127.0.0.1/vision'}]
        self.assertEqual(len(validate_config(config)['httpTools']),2)
        response=self.client.post('/api/service/v1/spaces',headers=self.headers,
            json={'context':{**self.context,'project_id':'elsewhere'}})
        self.assertEqual(response.status_code,403)

    def test_persisted_tool_result_is_available_only_to_run_owner(self):
        self.config['services'][0]['tools']=['evidence']
        self.config['httpTools']=[{'name':'evidence','effect':'read','url':'http://127.0.0.1:1/tool'}]
        rid=self.client.post('/v1/chat/runs',json=self.chat(),headers=self.headers).json['id']
        store=self.app.extensions['core_store'];store.claim(rid)
        value={'status':'succeeded','data':{'records':[{'record_id':'last','text':'x'*90000}]}}
        store.append(rid,'tool_result',{'operationId':'0:call','tool':'evidence','result':value})
        response=self.client.get('/v1/runs/'+rid+'/results/0:call',headers=self.headers)
        self.assertEqual(response.json,value)
        self.assertEqual(self.client.get('/v1/runs/'+rid+'/results/0:call',headers=self.other).status_code,404)
        self.config['services'][0]['tools']=[]
        self.assertEqual(self.client.get('/v1/runs/'+rid+'/results/0:call',headers=self.headers).status_code,403)

    def test_unexecuted_dsml_fails_run_and_replaces_false_submission_content(self):
        sid=self.client.post('/api/service/v1/spaces',json={'context':self.context},headers=self.headers).json['id']
        body={'requestId':str(uuid.uuid4()),'context':self.context,'spaceId':sid,'question':'只读分析','tools':[]}
        rid=self.client.post('/api/service/v1/runs',json=body,headers=self.headers).json['id']
        with patch('agent_core.model.OpenAICompatibleClient.stream',return_value=iter([{'type':'delta','content':'已提交提案 <｜DSML｜invoke name="cad_propose_parts">'}])):
            self.app.extensions['core_worker'].execute(rid)
        run=self.client.get('/v1/runs/'+rid,headers=self.headers).json
        self.assertEqual(run['status'],'FAILED')
        self.assertNotIn('DSML',run['content'])
        self.assertIn('不能视为已提交',run['content'])
        self.assertNotIn('event: complete',self.client.get('/v1/runs/'+rid+'/events',headers=self.headers).text)

    def test_empty_visual_result_preserves_native_proposal_and_reports_partial_outcome(self):
        import base64
        import io
        import json
        from PIL import Image
        from agent_core.security import CoreError
        service=self.config['services'][0];service['tools']=['ocr_analyze','cad_propose_parts']
        self.config['httpTools']=[{'name':name,'description':name,'effect':effect,'transport':transport,
            'url':'http://127.0.0.1:1/tool','inputSchema':{'type':'object'}} for name,effect,transport in
            [('ocr_analyze','analyze','ocr-vision-v1'),('cad_propose_parts','propose','http')]]
        image=io.BytesIO();Image.new('RGB',(8,8),'white').save(image,format='PNG')
        sid=self.client.post('/api/service/v1/spaces',json={'context':self.context},headers=self.headers).json['id']
        body={'requestId':str(uuid.uuid4()),'context':self.context,'spaceId':sid,'question':'原生证据可提交，视觉必须如实报告',
              'tools':service['tools'],'images':[{'mediaType':'image/png','data':base64.b64encode(image.getvalue()).decode()}]}
        rid=self.client.post('/api/service/v1/runs',json=body,headers=self.headers).json['id']
        def call(name,arguments,index):
            return iter([{'type':'tool_call','index':0,'id':'call'+str(index),'function':{'name':name,'arguments':json.dumps(arguments)}}])
        failure=CoreError('VISION_EMPTY_RESULT',502)
        failure.details={'coverage':{'visual_observations_available':False,'field_verification':'unverified'}}
        receipt={'contractVersion':'1','status':'succeeded','data':{'analysis_id':'native-analysis','proposals':[{'proposal_id':'native-proposal'}]}}
        streams=[call('ocr_analyze',{'question':'inspect','imageIndices':[0]},1),call('cad_propose_parts',{},2),iter([{'type':'delta','content':'原生提案已保存，视觉尚未核实。'}])]
        with patch('agent_core.model.OpenAICompatibleClient.stream',side_effect=streams),patch('agent_core.vision.analyze',side_effect=failure),patch('agent_core.registry.execute',return_value=receipt):
            self.app.extensions['core_worker'].execute(rid)
        events=self.client.get('/v1/runs/'+rid+'/events',headers=self.headers).text
        completed=next(json.loads(block.split('data: ',1)[1]) for block in events.split('\n\n') if 'event: complete\n' in block)
        outcome=completed['outcome']
        self.assertEqual(outcome['status'],'partial')
        self.assertTrue(outcome['proposalSubmitted'])
        self.assertEqual(outcome['proposalReceipts'][0]['proposal_ids'],['native-proposal'])
        self.assertEqual(outcome['toolFailures'][0]['code'],'VISION_EMPTY_RESULT')
        self.assertIn('"visual_observations_available": false',events)


if __name__=='__main__': unittest.main()
