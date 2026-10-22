"""Validate the adapter against the actual independently owned OCR schema."""
import base64
import importlib.util
import io
import unittest
from pathlib import Path
from PIL import Image
from unittest.mock import patch


class VisionObservationTests(unittest.TestCase):
    def result(self):
        return {'schema_version':1,'summary':'','regions':[],'evidence':[],'part_proposals':[],'warnings':[],
                'input_images':[{'image_id':'0','width':1600,'height':1200}],
                'source_model':{'model':'Qwen/Qwen3-VL-8B-Instruct','node_id':'primary'},'limitations':['unverified']}

    def test_metadata_only_empty_result_is_not_successful_visual_observation(self):
        from agent_core.vision import observation_coverage
        from agent_core.security import CoreError
        with self.assertRaisesRegex(CoreError,'VISION_EMPTY_RESULT') as error:
            observation_coverage(self.result(),['0','1'],'cad.analysis')
        self.assertFalse(error.exception.details['coverage']['visual_observations_available'])

    def test_empty_ocr_job_does_not_emit_success_result(self):
        from agent_core.vision import analyze
        from agent_core.security import CoreError
        responses=[{'job_id':'job1'},{'state':'completed'},{'result':self.result()}]
        with patch('agent_core.vision.prepare_request',return_value=({'images':[{'image_id':'0'}]},[])),patch('agent_core.vision.json_request',side_effect=responses):
            stream=analyze({'url':'http://127.0.0.1/vision'},[],'inspect',{}, {},'request')
            self.assertEqual(next(stream)['type'],'progress')
            with self.assertRaisesRegex(CoreError,'VISION_EMPTY_RESULT'):list(stream)

    def test_native_evidence_and_regions_do_not_claim_image_field_verification(self):
        from agent_core.vision import observation_coverage
        result=self.result();result['summary']='原生参数表存在尺寸12mm'
        result['evidence']=[{'evidence_id':'native1','source_type':'design_data','json_pointer':'/thickness','quote':'12'}]
        result['regions']=[{'region_id':'region1','image_id':'0','bbox':[0,0,.5,.5],'label':'drawing'}]
        coverage=observation_coverage(result,['0','1'],'cad.analysis')
        self.assertFalse(coverage['visual_observations_available'])
        self.assertTrue(coverage['region_available'])
        self.assertEqual(coverage['field_verification'],'unverified')
        self.assertEqual(coverage['error'],'VISION_IMAGE_UNVERIFIED')

    def test_only_image_grounded_quotes_count_as_visual_observations(self):
        from agent_core.vision import observation_coverage
        result=self.result();result['summary']='观察到局部文字'
        result['evidence']=[{'evidence_id':'image1','source_type':'image','image_id':'1','quote':'12mm'},
                            {'evidence_id':'foreign','source_type':'image','image_id':'foreign','quote':'not requested'}]
        coverage=observation_coverage(result,['0','1'],'cad.analysis')
        self.assertTrue(coverage['visual_observations_available'])
        self.assertEqual(coverage['observed_image_ids'],['1'])
        self.assertEqual(coverage['unobserved_image_ids'],['0'])
        self.assertEqual(coverage['field_verification'],'unverified')
        self.assertIsNone(coverage['error'])


class VisionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path=Path('F:/my_projects/2026-8-3_mingli/tools/ocr/src/plugins/vision/schema.py')
        if not path.exists():raise unittest.SkipTest('External OCR schema checkout is not installed')
        spec=importlib.util.spec_from_file_location('external_ocr_schema',path)
        cls.schema=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.schema)

    def image(self):
        buf=io.BytesIO();Image.new('RGB',(8,8),'white').save(buf,format='PNG')
        return {'image_id':'0','mime_type':'image/png','data_base64':base64.b64encode(buf.getvalue()).decode(),
                'source_ref':{'document_id':'doc','bbox':[0,1,20,30],'transform':[1,0,0,1,0,0],'messageIndex':0}}

    def test_ocr_wire_accepts_numeric_chat_identity_and_full_cad_provenance(self):
        from agent_core.vision import prepare_request
        payload,sources=prepare_request([self.image()],'inspect',{'candidate':'plate-5'},
            {'actor_id':'user','knowledge_base_id':None,'imageIndex':0},'image.describe')
        normalized=self.schema.normalize_request(payload)
        self.assertEqual(normalized['capability'],'image.describe')
        self.assertEqual(normalized['design_data'],{'candidate':'plate-5'})
        self.assertEqual(sources[0]['source_ref']['transform'],[1,0,0,1,0,0])
        self.assertNotIn('source_refs',payload)

    def test_oversized_design_and_too_many_images_fail_before_ocr_job(self):
        from agent_core.vision import prepare_request
        from agent_core.security import CoreError
        with self.assertRaisesRegex(CoreError,'VISION_DESIGN_SIZE_LIMIT'):
            prepare_request([self.image()],'inspect',{'text':'a'*65536},{},'cad.analysis')
        with self.assertRaisesRegex(CoreError,'VISION_IMAGE_LIMIT'):
            prepare_request([{**self.image(),'image_id':str(i)} for i in range(7)],'inspect',{}, {},'cad.analysis')


if __name__=='__main__':unittest.main()
