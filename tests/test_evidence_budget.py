import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_core import configuration


def compact(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def expand_design(value):
    def resolve(node):
        if isinstance(node,dict):
            if set(node)=={'$core_ref'}:
                target=value
                for key in node['$core_ref'].split('/')[1:]:target=target[key]
                return resolve(target)
            if set(node)=={'$core_object'}:
                shape,values=node['$core_object']
                return {k:resolve(v) for k,v in zip(value['object_fields'][shape],values)}
            if set(node)=={'$core_literal'}:
                return {k:resolve(v) for k,v in node['$core_literal']}
            return {k:resolve(v) for k,v in node.items()}
        if isinstance(node,list):return [resolve(v) for v in node]
        return node
    return [resolve(c) for c in value['contexts']]


class EvidenceBudgetTests(unittest.TestCase):
    def designs(self):
        shared={'schema':'cad-design-evidence/1','document_id':'doc','source_version_id':'v1',
                'native_document':{'fields':[{'id':str(i),'value':'工程参数原文'+str(i)*18,'unit':'mm','bbox':[0,1,2,3]} for i in range(520)]},
                'constraints':[{'id':'constraint','text':'所有接头需要核实','source_handles':['A']}],'rules':{'no_pixel_measurements':True}}
        # Two distinct >62KB contexts contain the same native engineering data.
        return [{**copy.deepcopy(shared),'candidates':[{'id':'global','quantity':2}]},
                {**copy.deepcopy(shared),'candidates':[{'id':'local','quantity':3,'material':'TA2'}]}]

    def test_mixed_images_share_lossless_context_and_preserve_bindings(self):
        from agent_core.evidence import merge_design_contexts
        designs=self.designs()
        self.assertGreater(len(compact(designs[0]).encode()),62000)
        merged=merge_design_contexts(designs,['0','3'])
        self.assertLessEqual(len(compact(merged).encode()),65536)
        self.assertEqual(expand_design(merged),designs)
        self.assertEqual(merged['image_contexts'],[{'image_id':'0','context':0},{'image_id':'3','context':1}])

    def test_merged_context_satisfies_actual_ocr_schema_without_source_loss(self):
        import importlib.util
        import io
        import base64
        from PIL import Image
        from agent_core.evidence import merge_design_contexts
        from agent_core.vision import prepare_request
        path=Path('F:/my_projects/2026-8-3_mingli/tools/ocr/src/plugins/vision/schema.py')
        if not path.exists():self.skipTest('External OCR checkout unavailable')
        spec=importlib.util.spec_from_file_location('budget_ocr_contract',path)
        schema=importlib.util.module_from_spec(spec);spec.loader.exec_module(schema)
        buf=io.BytesIO();Image.new('RGB',(8,8),'white').save(buf,format='PNG')
        designs=self.designs()
        fixture=Path(__file__).resolve().parents[1]/'.local/regression_evidence.json'
        if fixture.exists():
            events=json.loads(fixture.read_text('utf8'))
            designs=[events[i]['result']['data']['design_data'] for i in (0,1)]
        merged=merge_design_contexts(designs,['0','3'])
        images=[{'image_id':i,'mime_type':'image/png','data_base64':base64.b64encode(buf.getvalue()).decode(),
                 'source_ref':{'image_id':i,'matrix':[1,0,0,1,0,0]}} for i in ('0','3')]
        payload,sources=prepare_request(images,'检查候选',merged,{'project_id':'project'},'cad.analysis')
        normalized=schema.normalize_request(payload)
        self.assertEqual(expand_design(normalized['design_data']),designs)
        self.assertEqual(sources[1]['source_ref']['matrix'],[1,0,0,1,0,0])

    def test_real_failed_run_context_and_large_directory_projection(self):
        from agent_core.evidence import merge_design_contexts, model_projection
        path=Path(__file__).resolve().parents[1]/'.local/regression_evidence.json'
        if not path.exists():self.skipTest('Local read-only event export is not available')
        events=json.loads(path.read_text('utf8'));directory=events[0]['result'];candidate=events[1]['result']
        designs=[directory['data']['design_data'],candidate['data']['design_data']]
        merged=merge_design_contexts(designs,['0','1'])
        self.assertLessEqual(len(compact(merged).encode()),65536)
        self.assertEqual(expand_design(merged),designs)
        projected=model_projection('cad_get_drawing_evidence',directory,'0:call','run')
        self.assertLess(len(compact(projected).encode()),24576)
        self.assertEqual(projected['data']['candidates'][0]['id'],directory['data']['candidates'][0]['id'])
        local=model_projection('cad_get_candidate_evidence',candidate,'1:call','run')
        self.assertEqual(local['artifacts'][0]['imageIndex'],1)
        self.assertEqual(local['data']['evidence_id'],candidate['data']['evidence_id'])
        self.assertEqual(local['data']['records'][0]['record_id'],candidate['data']['records'][0]['record_id'])
        self.assertNotIn('preview',local)

    def test_large_directory_keeps_ids_and_image_index_as_structured_fields(self):
        from agent_core.evidence import model_projection
        result={'status':'succeeded','data':{'evidence_id':'bundle','candidate_id':'c1','native_document':{'raw':'x'*90000},
                'records':[{'record_id':'r1','text':'尺寸 12 mm','source_handle':'AA'}],
                'candidates':[{'id':'c1','name':'plate','dimensions':{'thickness':12}}]},
                'artifacts':[{'type':'image','imageIndex':7,'source_ref':{'record_id':'img7'}}]}
        projected=model_projection('cad_get_candidate_evidence',result,'0:call','run')
        self.assertLess(len(compact(projected).encode()),24576)
        self.assertEqual(projected['data']['records'][0]['text'],'尺寸 12 mm')
        self.assertEqual(projected['artifacts'][0]['imageIndex'],7)
        self.assertEqual(projected['_core']['resultRef'],'0:call')
        self.assertEqual(result['data']['native_document']['raw'],'x'*90000)

    def test_repeat_read_reuses_result_without_new_image_or_callback(self):
        from agent_core.app import create_app
        from agent_core.runtime import Runtime
        with tempfile.TemporaryDirectory() as td:
            config=configuration(Path(td)/'core.db');service=config['services'][0]
            service['tools']=['evidence'];config['httpTools']=[{'name':'evidence','description':'read','effect':'read','url':'http://127.0.0.1:1/tool','inputSchema':{'type':'object'}}]
            app=create_app(config,start_worker=False);store=app.extensions['core_store']
            context={'project_id':'project','drawing_id':'drawing','revision':'v1'}
            space=store.create_space(service,{'context':context})
            runtime=Runtime(config,store,service,{'id':'run','domain':'cad'},{'context':context,'spaceId':space['id'],'tools':['evidence']},lambda:False)
            result={'status':'succeeded','data':{'evidence_id':'bundle','records':[{'record_id':'r','text':'12mm'}]}}
            with patch('agent_core.registry.execute',return_value=result) as callback:
                runtime.execute('evidence','{}',{'operationId':'0:call'})
                duplicate=runtime.execute('evidence','{}',{'operationId':'1:call'})
                self.assertEqual(callback.call_count,1)
            self.assertEqual(duplicate['_core_cached_from'],'0:call')
            exact=runtime.execute('session_read_result',json.dumps({'resultRef':'0:call','pointer':'/data/records/0/text'}),{'operationId':'2:call'})
            self.assertEqual(exact['value'],'12mm')

    def test_read_only_cad_answer_allowed_but_fake_submission_rejected(self):
        from agent_core.runtime import validate_final_answer
        from agent_core.security import CoreError
        validate_final_answer('cad','该尺寸尚待确认；未提交提案。',[])
        for text in ('无法确认已提交','没有证据证明已提交','用户问是否已提交，我未执行提交'):
            validate_final_answer('cad',text,[])
        with self.assertRaisesRegex(CoreError,'UNEXECUTED_TOOL_CALL'):
            validate_final_answer('cad','<｜DSML｜invoke name="cad_propose_parts">',[])
        with self.assertRaisesRegex(CoreError,'UNCONFIRMED_SUBMISSION'):
            validate_final_answer('cad','已提交提案。',[])
        validate_final_answer('cad','已提交提案。',[{'analysis_id':'a','proposal_ids':['p']}])

    def test_original_reference_shaped_fields_round_trip_as_literal_data(self):
        from agent_core.evidence import merge_design_contexts
        designs=[{'candidate':'a','original':{'$core_ref':'/shared/missing'},'nested':{'$core_object':['unknown',[1,2]]}},
                 {'candidate':'b','original':{'$core_literal':[['x',3]]}}]
        merged=merge_design_contexts(designs,['0','1'])
        self.assertEqual(expand_design(merged),designs)

    def test_shared_context_identity_preserves_json_scalar_types(self):
        from agent_core.evidence import merge_design_contexts
        designs=[{'value':True},{'value':1},{'value':1.0}]
        merged=merge_design_contexts(designs,['0','1','2'])
        expanded=expand_design(merged)
        self.assertEqual([type(value['value']) for value in expanded],[bool,int,float])
        self.assertEqual([b['context'] for b in merged['image_contexts']],[0,1,2])

    def test_wide_result_objects_have_resolvable_references_and_key_pages(self):
        from agent_core.evidence import model_projection, read_pointer, result_page
        wide={str(i):i for i in range(3000)}
        raw={'value':wide}
        projected=model_projection('session_read_result',raw,'read','run')
        self.assertEqual(read_pointer(raw,projected['result']['pointer']),raw)
        page=result_page(wide,offset=20,limit=3)
        self.assertEqual(page['value'],{'20':20,'21':21,'22':22})
        self.assertEqual(page['nextOffset'],23)

    def test_cached_result_rechecks_original_tool_grant_and_mutation_freshness(self):
        from agent_core.app import create_app
        from agent_core.runtime import Runtime
        from agent_core.security import CoreError
        with tempfile.TemporaryDirectory() as td:
            config=configuration(Path(td)/'core.db');service=config['services'][0]
            service['tools']=['evidence','proposal']
            config['httpTools']=[{'name':name,'description':name,'effect':effect,'url':'http://127.0.0.1:1/tool','inputSchema':{'type':'object'}} for name,effect in [('evidence','read'),('proposal','propose')]]
            app=create_app(config,start_worker=False);store=app.extensions['core_store']
            context={'project_id':'project','drawing_id':'drawing','revision':'v1'}
            sid=store.create_space(service,{'context':context})['id']
            runtime=Runtime(config,store,service,{'id':'run','domain':'cad'},{'context':context,'spaceId':sid,'tools':['evidence','proposal']},lambda:False)
            with patch('agent_core.registry.execute',side_effect=[{'status':'succeeded','data':{'version':1}},{'status':'succeeded','data':{}},{'status':'succeeded','data':{'version':2}}]):
                runtime.execute('evidence','{}',{'operationId':'read1'})
                service['tools'].remove('evidence')
                with self.assertRaisesRegex(CoreError,'TOOL_REVOKED'):
                    runtime.execute('session_read_result','{"resultRef":"read1"}',{'operationId':'read-revoked'})
                service['tools'].append('evidence')
                runtime.execute('proposal','{}',{'operationId':'propose'})
                updated=runtime.execute('evidence','{}',{'operationId':'read2'})
                self.assertEqual(updated['data']['version'],2)


if __name__=='__main__':unittest.main()
