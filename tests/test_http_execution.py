"""Real separate Core process + local deterministic HTTP providers; no paid calls."""
import hashlib
import base64
import io
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
import httpx
from flask import Flask, Response, jsonify, request
from werkzeug.serving import make_server
from PIL import Image
from test_core import configuration


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); return sock.getsockname()[1]


class HTTPExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.calls=[];cls.callbacks=[];cls.ocr=[]
        buf=io.BytesIO();Image.new('RGB',(8,8),'white').save(buf,format='PNG')
        cls.png=base64.b64encode(buf.getvalue()).decode()
        provider=Flask('test-provider')
        @provider.post('/v1/chat/completions')
        def model():
            value=request.get_json();cls.calls.append(value)
            messages=value['messages'];user=next((m['content'] for m in messages if m['role']=='user'),'')
            count=sum(m['role']=='tool' for m in messages)
            if user=='slow': time.sleep(1)
            if user=='recall twice' and count<2:
                delta={'tool_calls':[{'index':0,'id':'call'+str(count),'function':{'name':'recall','arguments':json.dumps({'question':'steel'})}}]}
            elif user=='load candidate' and count==0:
                delta={'tool_calls':[{'index':0,'id':'candidate','function':{'name':'cad_get_candidate_evidence','arguments':'{"candidate_id":"plate-5"}'}}]}
            elif user=='load candidate' and count==1:
                delta={'tool_calls':[{'index':0,'id':'ocr','function':{'name':'ocr_analyze','arguments':'{"question":"extract","imageIndices":[0]}'}}]}
            elif user=='load document' and count==0:
                delta={'tool_calls':[{'index':0,'id':'document','function':{'name':'cad_get_document_evidence','arguments':'{"region_id":"table"}'}}]}
            elif user=='load document' and count==1:
                delta={'tool_calls':[{'index':0,'id':'ocr-document','function':{'name':'ocr_analyze','arguments':'{"question":"reconstruct","imageIndices":[0],"capability":"cad.document_reconstruction"}'}}]}
            else: delta={'content':'已核实 [1] [2]。'}
            return Response('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':'tool_calls' if 'tool_calls' in delta else 'stop'}]},ensure_ascii=False)+'\n\ndata: '+json.dumps({'choices':[],'usage':{'prompt_tokens':10,'completion_tokens':4}})+'\n\ndata: [DONE]\n\n',mimetype='text/event-stream')
        @provider.post('/rag')
        def rag():
            value=request.get_json();cls.callbacks.append(value)
            return jsonify({'query':'steel','results':[{'documentId':'doc','passage':'steel specification','source':{'page':7}}]})
        @provider.post('/candidate')
        def candidate():
            return jsonify({'contractVersion':'1','status':'succeeded','data':{'candidate_id':'plate-5','design_data':{'candidate':'plate-5','constraints':['12mm']}},
                            'artifacts':[{'type':'image','mediaType':'image/png','data':cls.png,'source_ref':{'handle':'AA','transform':[1,0,0,1,0,0]}}]})
        @provider.post('/vision/jobs')
        def vision():
            cls.ocr.append(request.get_json());return jsonify({'job_id':'job1'}),202
        @provider.get('/vision/jobs/job1')
        def vision_status(): return jsonify({'state':'completed'})
        @provider.get('/vision/jobs/job1/result')
        def vision_result():
            if cls.ocr[-1].get('capability')=='cad.document_reconstruction':
                return jsonify({'result':{'schema':'cad-document-reconstruction/1','status':'draft',
                    'document':cls.ocr[-1]['design_data']['document'],'summary':'Steel','regions':[],
                    'evidence':[{'evidence_id':'e1','source_type':'image','image_id':'0','quote':'Steel','confidence':None}],
                    'tables':[],'requirements':[],'issues':[],'warnings':[], 'limitations':['draft'],
                    'input_images':[{'image_id':'0','width':8,'height':8,'sha256':hashlib.sha256(base64.b64decode(cls.png)).hexdigest()}],
                    'source_model':{'model':'FAKE','node_id':'fake'},
                    'image_evidence_coverage':{'observed_image_ids':['0'],'unobserved_image_ids':[],'complete':True}}})
            return jsonify({'result':{'schema_version':1,'summary':'观察到12mm标注','regions':[],
            'evidence':[{'evidence_id':'image1','source_type':'image','image_id':'0','quote':'12mm','confidence':None}],
            'part_proposals':[],'warnings':[]}})
        @provider.post('/document')
        def document():
            return jsonify({'contractVersion':'1','status':'succeeded','data':{'design_data':{
                'schema':'cad-document-input/1','document':{'document_id':'drawing','revision':'v1'},
                'frames':[{'frame_id':'model','space':'model','unit':None}],
                'native_texts':[{'text_id':'AA','handle':'AA','text':'Steel','frame_id':'model','position':[0,0,0]}],
                'image_contexts':[{'image_id':'original','role':'table','frame_id':'model','pixel_to_native':[1,0,0,0,-1,8,0,0,1]}],
                'coarse_structure':{}}},'artifacts':[{'type':'image','mediaType':'image/png','data':cls.png,
                'source_ref':{'document_id':'drawing','revision':'v1','region_id':'table','source_version_id':'source',
                              'width':8,'height':8,'pixel_to_model':[1,0,0,-1,0,8]}}]})
        cls.upstream=make_server('127.0.0.1',0,provider,threaded=True)
        cls.server_thread=threading.Thread(target=cls.upstream.serve_forever,daemon=True);cls.server_thread.start()
        base=f'http://127.0.0.1:{cls.upstream.server_port}'
        conf=configuration(Path(cls.temp.name)/'core.db')
        conf['models'][0]['baseUrl']=base+'/v1'
        conf['services'][0]['ragCallback']={'url':base+'/rag','tokenEnv':'TEST_CORE_CALLBACK_KEY'}
        conf['services'][0]['tools']=['cad_get_candidate_evidence','cad_get_document_evidence','ocr_analyze']
        conf['httpTools']=[{'name':'cad_get_candidate_evidence','description':'load candidate','version':'1','effect':'read','url':base+'/candidate','tokenEnv':'TEST_CORE_CALLBACK_KEY',
                           'inputSchema':{'type':'object','properties':{'candidate_id':{'type':'string'}},'required':['candidate_id'],'additionalProperties':False}},
                          {'name':'cad_get_document_evidence','description':'document region','version':'1','effect':'read','url':base+'/document','tokenEnv':'TEST_CORE_CALLBACK_KEY',
                           'inputSchema':{'type':'object','properties':{'region_id':{'type':'string'}},'required':['region_id'],'additionalProperties':False}},
                          {'name':'ocr_analyze','description':'OCR images','version':'1','effect':'read','transport':'ocr-vision-v1','url':base+'/vision','tokenEnv':'TEST_CORE_CALLBACK_KEY',
                           'inputSchema':{'type':'object','properties':{'question':{'type':'string'},'capability':{'type':'string'},'imageIndices':{'type':'array','items':{'type':'integer','minimum':0}}},'required':['question','imageIndices'],'additionalProperties':False}}]
        config_path=Path(cls.temp.name)/'config.json';config_path.write_text(json.dumps(conf),encoding='utf8')
        cls.port=free_port();cls.base=f'http://127.0.0.1:{cls.port}'
        cls.log=open(Path(cls.temp.name)/'process.log','w+',encoding='utf8')
        env={**os.environ,'TEST_CORE_MODEL_KEY':'not-a-real-key','TEST_CORE_CALLBACK_KEY':'callback-test','PYTHONUNBUFFERED':'1'}
        cls.process=subprocess.Popen([str(Path(__file__).resolve().parents[1]/'.venv/Scripts/python.exe'),'-m','agent_core','--config',str(config_path),'--port',str(cls.port)],
                                     cwd=Path(__file__).resolve().parents[1],env=env,stdout=cls.log,stderr=cls.log)
        cls.client=httpx.Client(base_url=cls.base,headers={'Authorization':'Bearer first'},timeout=10,trust_env=False)
        for _ in range(100):
            if cls.process.poll() is not None: break
            try:
                if cls.client.get('/health').status_code==200:return
            except httpx.ConnectError: pass
            time.sleep(.05)
        cls.log.seek(0);message=cls.log.read()
        cls.tearDownClass()
        raise AssertionError('Core failed to start: '+message)

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate();cls.process.wait(timeout=10)
        cls.client.close();cls.upstream.shutdown();cls.server_thread.join(timeout=3);cls.log.close();cls.temp.cleanup()

    def chat(self,text):
        return {'request_id':str(uuid.uuid4()),'model_profile':'local','messages':[{'role':'user','content':text}],
                'context':{'conversation_id':'thread1','knowledge_base_id':'kb','actor_id':'alice','tenant_id':'tenant'}}

    def events(self,rid):
        response=self.client.get('/v1/runs/'+rid+'/events')
        self.assertEqual(response.status_code,200,response.text)
        return response.text

    def test_separate_process_graph_rag_labels_and_replay_without_reexecution(self):
        body=self.chat('recall twice')
        response=self.client.post('/v1/chat/runs',json=body)
        self.assertEqual(response.status_code,202,response.text);rid=response.json()['id']
        stream=self.events(rid)
        self.assertIn('event: complete',stream,stream)
        retrievals=[json.loads(block.split('data: ',1)[1])['payload'] for block in stream.split('\n\n') if 'event: retrieval\n' in block]
        self.assertEqual([p['results'][0]['label'] for p in retrievals],[1,2])
        self.assertEqual(retrievals[0]['results'][0]['source'],{'page':7})
        self.assertEqual(self.callbacks[-1]['context']['actor_id'],'alice')
        self.assertEqual(self.callbacks[-1]['context']['caller_id'],'first')
        before=len(self.calls)
        self.assertEqual(self.client.post('/v1/chat/runs',json=body).json()['id'],rid)
        events_only=lambda value:[block for block in value.split('\n\n') if block.startswith('id: ')]
        self.assertEqual(events_only(self.events(rid)),events_only(stream))
        self.assertEqual(len(self.calls),before)

    def test_cancel_does_not_accept_late_model_result(self):
        rid=self.client.post('/v1/chat/runs',json=self.chat('slow')).json()['id']
        deadline=time.monotonic()+10
        while time.monotonic()<deadline and not any(any(m.get('content')=='slow' for m in call['messages']) for call in self.calls):time.sleep(.02)
        self.assertTrue(any(any(m.get('content')=='slow' for m in call['messages']) for call in self.calls),'provider did not receive slow request')
        response=self.client.post('/v1/runs/'+rid+'/cancel',json={})
        self.assertEqual(response.json()['status'],'CANCELLED')
        time.sleep(1.1)
        stream=self.events(rid)
        self.assertNotIn('event: complete',stream)
        self.assertNotIn('event: delta',stream)

    def test_candidate_image_and_design_data_reach_only_ocr(self):
        context={'project_id':'project','drawing_id':'drawing','revision':'v1'}
        sid=self.client.post('/api/service/v1/spaces',json={'context':context}).json()['id']
        body={'requestId':str(uuid.uuid4()),'question':'load candidate','spaceId':sid,'context':context,'tools':['cad_get_candidate_evidence','ocr_analyze']}
        response=self.client.post('/api/service/v1/runs',json=body)
        self.assertEqual(response.status_code,202,response.text)
        stream=self.events(response.json()['id'])
        self.assertIn('event: complete',stream,stream)
        self.assertEqual(self.ocr[-1]['design_data']['candidate'],'plate-5')
        self.assertTrue(all(isinstance(v,str) for v in self.ocr[-1]['images'][0]['source_ref'].values()))
        tool_results=[json.loads(block.split('data: ',1)[1]) for block in stream.split('\n\n') if 'event: tool_result\n' in block]
        ocr_result=next(r['result'] for r in tool_results if r['tool']=='ocr_analyze')
        self.assertEqual(ocr_result['sources'][0]['source_ref']['transform'],[1,0,0,1,0,0])
        self.assertNotIn(self.png,json.dumps(self.calls[-1]))

    def test_document_region_reaches_ocr_with_new_contract_and_keeps_transport_sources(self):
        context={'project_id':'project','drawing_id':'drawing','revision':'v1'}
        sid=self.client.post('/api/service/v1/spaces',json={'context':context}).json()['id']
        body={'requestId':str(uuid.uuid4()),'question':'load document','spaceId':sid,'context':context,
              'visionMode':'ocr','tools':['cad_get_document_evidence','ocr_analyze']}
        response=self.client.post('/api/service/v1/runs',json=body)
        self.assertEqual(response.status_code,202,response.text)
        stream=self.events(response.json()['id'])
        self.assertIn('event: complete',stream,stream)
        payload=self.ocr[-1]
        self.assertEqual(payload['capability'],'cad.document_reconstruction')
        self.assertEqual(payload['design_data']['image_contexts'][0]['image_id'],'0')
        self.assertEqual(payload['design_data']['native_texts'][0]['text'],'Steel')
        results=[json.loads(block.split('data: ',1)[1]) for block in stream.split('\n\n') if 'event: tool_result\n' in block]
        result=next(r['result'] for r in results if r['tool']=='ocr_analyze')
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(result['data']['schema'],'cad-document-reconstruction/1')
        self.assertEqual(result['sources'][0]['source_ref']['source_version_id'],'source')

    def test_caller_cannot_supply_endpoint_or_remote_image(self):
        body=self.chat('hello');body['base_url']='http://example.com'
        self.assertEqual(self.client.post('/v1/chat/runs',json=body).status_code,400)
        body=self.chat([{'type':'image_url','image_url':{'url':'http://example.com/image.png'}}])
        self.assertEqual(self.client.post('/v1/chat/runs',json=body).status_code,400)


if __name__=='__main__': unittest.main()
