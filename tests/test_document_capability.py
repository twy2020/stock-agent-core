import unittest
from copy import deepcopy

from agent_core.security import CoreError


class DocumentCapabilityTests(unittest.TestCase):
    def test_document_coverage_accepts_new_contract_and_rejects_fake_coverage(self):
        from agent_core.vision import observation_coverage
        result = {'schema':'cad-document-reconstruction/1','status':'draft','summary':'table',
                  'document':{'document_id':'d','revision':'1'},'source_model':{},
                  'regions':[],'tables':[],'requirements':[],'issues':[],'warnings':[],'limitations':[],
                  'input_images':[{'image_id':'0'}],
                  'evidence':[{'source_type':'image','image_id':'0','quote':'visible table'}],
                  'image_evidence_coverage':{'observed_image_ids':['0'],'unobserved_image_ids':[],'complete':True}}
        self.assertTrue(observation_coverage(result,['0'],'cad.document_reconstruction')['visual_observations_available'])
        for changes in ({'evidence':[]},{'schema':'old'},{'input_images':[{'image_id':'other'}]},
                        {'image_evidence_coverage':{'complete':True}}):
            with self.assertRaises(CoreError):
                observation_coverage({**result,**changes},['0'],'cad.document_reconstruction')

    def test_document_context_remaps_images_and_preserves_original(self):
        from agent_core.vision import document_design_context
        design = {'schema':'cad-document-input/1','document':{'document_id':'d','revision':'1'},
                  'frames':[{'frame_id':'model','unit':None,'space':'model'}],
                  'native_texts':[{'text_id':'t','text':'steel'}],
                  'image_contexts':[{'image_id':'original','role':'table'}],
                  'coarse_structure':{'region':'one'}}
        original=deepcopy(design)
        merged=document_design_context([design,design],['1','2'])
        self.assertEqual([i['image_id'] for i in merged['image_contexts']],['1','2'])
        self.assertEqual(len(merged['native_texts']),1)
        self.assertEqual(design,original)
        for changed in ({**design,'document':{'document_id':'d','revision':'2'}},
                        {**design,'native_texts':[{'text_id':'t','text':'changed'}]}):
            with self.assertRaises(CoreError): document_design_context([design,changed],['1','2'])
    def test_default_capabilities_keep_existing_domains(self):
        from agent_core.vision import select_capability
        self.assertEqual(select_capability('cad', {}), 'cad.analysis')
        self.assertEqual(select_capability('chat', {}), 'image.describe')

    def test_document_reconstruction_is_only_available_in_cad_domain(self):
        from agent_core.vision import select_capability
        self.assertEqual(select_capability('cad', {'capability':'cad.document_reconstruction'}), 'cad.document_reconstruction')
        for domain, args in [('chat', {'capability':'cad.document_reconstruction'}),
                             ('cad', {'capability':'foreign'}), ('cad', {'capability':None})]:
            with self.assertRaises(CoreError): select_capability(domain, args)


if __name__ == '__main__': unittest.main()
