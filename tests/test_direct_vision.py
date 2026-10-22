import base64
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from test_core import configuration
from agent_core.app import create_app
from agent_core.images import direct_image_message
from agent_core.runtime import Runtime
from agent_core.security import CoreError


class DirectVisionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.config=configuration(Path(self.temp.name)/'core.db')
        self.config['models'][0]['capabilities']={'vision':True}
        self.config['services'][0]['visionPolicy']='both'
        self.app=create_app(self.config,start_worker=False);self.client=self.app.test_client()
        self.headers={'Authorization':'Bearer first'}
        self.image={'mediaType':'image/png','data':base64.b64encode(b'\x89PNG\r\n\x1a\nexample').decode()}

    def tearDown(self):self.temp.cleanup()

    def body(self):
        return {'request_id':str(uuid.uuid4()),'messages':[{'role':'user','content':'描述图片，未知字段留空'}],
                'context':{'conversation_id':'test','knowledge_base_id':None,'actor_id':'actor','tenant_id':'tenant'},'images':[self.image]}

    def test_direct_images_reach_model_without_ocr_grant(self):
        response=self.client.post('/v1/chat/runs',headers=self.headers,json=self.body())
        self.assertEqual(response.status_code,202,response.json)
        store=self.app.extensions['core_store'];row=store.run(response.json['id']);body=json.loads(row['payload'])
        self.assertEqual(body['_visionMode'],'direct')
        self.assertEqual(body['tools'],[])
        message=body['_messages'][-1]
        self.assertEqual(message['content'][-1]['image_url']['url'],'data:image/png;base64,'+self.image['data'])
        runtime=Runtime(self.config,store,self.config['services'][0],row,body,lambda:False)
        runtime.capture_artifacts({'artifacts':[{'type':'image',**self.image}]})
        pending=runtime.drain_visual_messages()
        self.assertEqual(len(pending),1)
        self.assertIn('image_url',str(pending))
        self.assertEqual(runtime.drain_visual_messages(),[])
        self.config['services'][0]['visionPolicy']='ocr'
        with self.assertRaises(CoreError):runtime.check()

    def test_direct_requires_both_caller_policy_and_model_capability(self):
        body=self.body();body['vision_mode']='direct';self.config['services'][0]['visionPolicy']='ocr'
        self.assertEqual(self.client.post('/v1/chat/runs',headers=self.headers,json=body).status_code,403)
        self.config['services'][0]['visionPolicy']='both';self.config['models'][0]['capabilities']['vision']=False
        self.assertEqual(self.client.post('/v1/chat/runs',headers=self.headers,json=body).status_code,400)

    def test_ocr_selection_does_not_silently_switch_to_direct(self):
        body=self.body();body['vision_mode']='ocr'
        result=self.client.post('/v1/chat/runs',headers=self.headers,json=body)
        self.assertEqual(result.status_code,503)
        self.assertEqual(result.json['code'],'VISION_UNAVAILABLE')

    def test_message_helper_preserves_indices(self):
        message=direct_image_message([self.image,self.image],[1])
        self.assertEqual(message['content'][1]['text'],'图像索引 1')

    def test_console_assets_have_executable_mime_under_nosniff(self):
        result=self.client.get('/admin/assets/app.js')
        self.assertEqual(result.mimetype,'text/javascript')
        self.assertEqual(result.headers['X-Content-Type-Options'],'nosniff')
        result.close()
        with self.client.get('/admin/assets/registry.js') as registry:
            self.assertEqual(registry.mimetype,'text/javascript')


if __name__=='__main__':unittest.main()
