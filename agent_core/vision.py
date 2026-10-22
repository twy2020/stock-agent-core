"""Independent OCR jobs transport; all visual models and credentials remain in OCR."""
import base64
import copy
import hashlib
import io
import json
import re
import time
import warnings
from PIL import Image, UnidentifiedImageError
from .registry import json_request
from .security import CoreError, endpoint


def select_capability(domain, arguments):
    default = 'image.describe' if domain == 'chat' else 'cad.analysis'
    capability = arguments.get('capability', default)
    allowed = {'image.describe'} if domain == 'chat' else {'cad.analysis', 'cad.document_reconstruction'}
    if not isinstance(capability, str) or capability not in allowed:
        raise CoreError('VISION_CAPABILITY_FORBIDDEN', 403)
    return capability


def document_design_context(designs, image_ids):
    """Keep OCR's document contract intact while Core assigns its image indices."""
    if not designs or len(designs) != len(image_ids): raise CoreError('INVALID_DOCUMENT_CONTEXT')
    result = copy.deepcopy(designs[0])
    if result.get('schema') != 'cad-document-input/1': raise CoreError('INVALID_DOCUMENT_CONTEXT')
    result['native_texts'] = []; result['frames'] = []; result['image_contexts'] = []
    contexts = []
    for design, image_id in zip(designs, image_ids):
        if design.get('schema') != result['schema'] or design.get('document') != result['document']:
            raise CoreError('DOCUMENT_VERSION_MISMATCH')
        images = design.get('image_contexts', [])
        if len(images) != 1: raise CoreError('INVALID_DOCUMENT_CONTEXT')
        result['image_contexts'].append({**copy.deepcopy(images[0]), 'image_id':str(image_id)})
        for key, identifier in (('frames','frame_id'), ('native_texts','text_id')):
            for item in design.get(key, []):
                previous = next((v for v in result[key] if v[identifier] == item[identifier]), None)
                if previous is not None and previous != item: raise CoreError('DOCUMENT_CONTEXT_CONFLICT')
                if previous is None: result[key].append(copy.deepcopy(item))
        contexts.append(copy.deepcopy(design.get('coarse_structure', {})))
    result['coarse_structure'] = contexts[0] if len(contexts) == 1 else {'regions':contexts}
    return result


def observation_coverage(result,image_ids,capability):
    """Metadata proves transport, not that the model observed an image field."""
    if capability == 'cad.document_reconstruction':
        if (not isinstance(result,dict) or result.get('schema') != 'cad-document-reconstruction/1'
            or result.get('status') != 'draft' or not isinstance(result.get('summary'),str)
            or any(not isinstance(result.get(key),list) for key in
                   ('regions','evidence','tables','requirements','issues','warnings','input_images','limitations'))
            or not isinstance(result.get('document'),dict) or not isinstance(result.get('source_model'),dict)):
            raise CoreError('VISION_INVALID_RESPONSE',502)
        requested = list(dict.fromkeys(str(i) for i in image_ids))
        received = [i.get('image_id') for i in result['input_images'] if isinstance(i,dict)]
        if len(received) != len(requested) or set(received) != set(requested):
            raise CoreError('VISION_INVALID_RESPONSE',502)
        evidence = [e for e in result['evidence'] if isinstance(e,dict) and
                    e.get('source_type') == 'image' and e.get('image_id') in requested and
                    isinstance(e.get('quote'),str) and e['quote'].strip()]
        observed = [i for i in requested if any(e['image_id']==i for e in evidence)]
        expected = {'observed_image_ids':observed,
                    'unobserved_image_ids':[i for i in requested if i not in observed],
                    'complete':len(observed)==len(requested)}
        actual = result.get('image_evidence_coverage')
        if not isinstance(actual,dict) or any(actual.get(k)!=v for k,v in expected.items()):
            raise CoreError('VISION_INVALID_RESPONSE',502)
        if not observed: raise CoreError('VISION_IMAGE_UNVERIFIED',502)
        return {**expected,'visual_observations_available':True,'image_evidence_count':len(evidence),
                'field_verification':'unverified','table_count':len(result['tables']),
                'requirement_count':len(result['requirements']),'error':None}
    if not isinstance(result,dict) or result.get('schema_version')!=1 or not isinstance(result.get('summary'),str):
        raise CoreError('VISION_INVALID_RESPONSE',502)
    if any(not isinstance(result.get(key),list) for key in ('regions','evidence','part_proposals','warnings')):
        raise CoreError('VISION_INVALID_RESPONSE',502)
    requested=list(dict.fromkeys(str(i) for i in image_ids))
    evidence=[e for e in result['evidence'] if isinstance(e,dict) and isinstance(e.get('quote'),str) and e['quote'].strip()]
    image_evidence=[e for e in evidence if e.get('source_type')=='image' and e.get('image_id') in requested]
    observed=[i for i in requested if any(e['image_id']==i for e in image_evidence)]
    regions=[r for r in result['regions'] if isinstance(r,dict) and r.get('image_id') in requested and isinstance(r.get('label'),str) and r['label'].strip()]
    proposals=[p for p in result['part_proposals'] if isinstance(p,dict) and isinstance(p.get('name'),str) and p['name'].strip()]
    coverage={'visual_observations_available':bool(image_evidence),'image_evidence_count':len(image_evidence),
              'region_available':bool(regions),'region_count':len(regions),'field_verification':'unverified',
              'requested_image_ids':requested,'observed_image_ids':observed,'unobserved_image_ids':[i for i in requested if i not in observed],
              'error':'VISION_IMAGE_UNVERIFIED' if capability=='cad.analysis' and not image_evidence else None}
    if not result['summary'].strip() and not evidence and not regions and not proposals:
        coverage['error']='VISION_EMPTY_RESULT'
        error=CoreError('VISION_EMPTY_RESULT',502)
        error.details={'coverage':coverage,'data':copy.deepcopy(result)}
        raise error
    return coverage


def prepare_request(images,text,design_data,context,capability):
    """Adapt rich Core provenance to OCR's bounded string-map wire contract."""
    if not 1<=len(images)<=6:raise CoreError('VISION_IMAGE_LIMIT')
    try: design_bytes=json.dumps(design_data,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf8')
    except (ValueError,TypeError):raise CoreError('INVALID_DESIGN_DATA') from None
    if len(design_bytes)>64*1024:raise CoreError('VISION_DESIGN_SIZE_LIMIT',413)
    wire=[];sources=[];total=0;ids=set()
    for item in images:
        try:raw=base64.b64decode(item['data_base64'],validate=True)
        except (ValueError,TypeError):raise CoreError('INVALID_IMAGE') from None
        if not raw or len(raw)>5*1024*1024:raise CoreError('VISION_IMAGE_SIZE_LIMIT',413)
        if base64.b64encode(raw).decode()!=item['data_base64']:raise CoreError('INVALID_IMAGE')
        total+=len(raw)
        if total>20*1024*1024:raise CoreError('VISION_TOTAL_IMAGE_SIZE_LIMIT',413)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error',Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as image:
                    if image.width*image.height>16_000_000 or getattr(image,'n_frames',1)!=1:raise CoreError('VISION_IMAGE_DIMENSION_LIMIT',413)
                    if image.format!={'image/png':'PNG','image/jpeg':'JPEG','image/webp':'WEBP'}.get(item['mime_type']):raise CoreError('INVALID_IMAGE')
                    image.verify()
                with Image.open(io.BytesIO(raw)) as image:image.load()
        except (UnidentifiedImageError,OSError,Image.DecompressionBombError,Image.DecompressionBombWarning):raise CoreError('INVALID_IMAGE') from None
        image_id=item['image_id']
        if not isinstance(image_id,str) or not 1<=len(image_id)<=128 or image_id in ids:raise CoreError('INVALID_IMAGE_ID')
        ids.add(image_id)
        original=copy.deepcopy(item.get('source_ref',{}))
        source_id=hashlib.sha256(json.dumps(original,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        source={'source_id':source_id,'image_id':image_id}
        for key in ('provider','document_id','drawing_id','revision','candidate_id','evidence_id','resource_id'):
            if isinstance(original.get(key),str) and len(original[key])<=1024:source[key]=original[key]
        wire.append({**item,'source_ref':source})
        sources.append({'image_id':image_id,'source_id':source_id,'source_ref':original})
    identity={k:str(v) for k,v in context.items() if v is not None}
    if len(identity)>32 or any(len(k)>128 or len(v)>1024 for k,v in identity.items()):raise CoreError('INVALID_VISION_CONTEXT')
    if design_data.get('schema')=='core-shared-design/1':
        text=('设计数据采用无损共享编码：image_contexts将每张image_id关联contexts下标；$core_ref为shared中的精确复制；'
              '$core_object=[字段组ID,值数组]按object_fields中的字段顺序恢复对象。所有工程字段均保留。'
              '引用证据时使用实际存储标量的JSON pointer（shared或contexts内values数组），不要把引用标记当作工程数值。\n'+text)
    return {'schema_version':1,'capability':capability,'images':wire,'text':text[:20000],'design_data':design_data,'context':identity},sources


def analyze(entry,images,text,design_data,context,request_id,cancelled=lambda:False,source_refs=None,capability='cad.analysis'):
    base=endpoint(entry['url']); started=time.monotonic();job=None
    payload,sources=prepare_request(images,text,design_data,context,capability)
    def call(method,path,payload=None):
        return json_request(method,base+path,entry,payload,request_id,limit=8*1024*1024)
    try:
        if cancelled(): raise CoreError('CANCELLED')
        submitted=call('POST','/jobs',payload)
        job=submitted.get('job_id')
        if not isinstance(job,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}',job): raise CoreError('VISION_INVALID_RESPONSE',502)
        yield {'type':'progress','stage':'IMAGE_ANALYZING','jobId':job,'status':'RUNNING','elapsedMs':0}
        previous=None
        while True:
            if cancelled() or time.monotonic()-started>240:
                try: call('POST',f'/jobs/{job}/cancel',{})
                except CoreError: pass
                raise CoreError('CANCELLED' if cancelled() else 'VISION_TIMEOUT',408)
            status=call('GET',f'/jobs/{job}');state=status.get('state')
            if state=='completed':
                result=call('GET',f'/jobs/{job}/result').get('result')
                if cancelled(): raise CoreError('CANCELLED')
                try:
                    coverage=observation_coverage(result,[i['image_id'] for i in payload['images']],capability)
                    if capability == 'cad.document_reconstruction' and result['document'] != design_data.get('document'):
                        raise CoreError('DOCUMENT_VERSION_MISMATCH',502)
                except CoreError as exc:
                    exc.details={**getattr(exc,'details',{}),'jobId':job,'sources':sources,'source_refs':source_refs or []}
                    raise
                yield {'type':'result','jobId':job,'result':result,'coverage':coverage,'sources':sources,'source_refs':source_refs or []};return
            if state in ('failed','cancelled'): raise CoreError('VISION_FAILED' if state=='failed' else 'CANCELLED',502)
            if state not in ('queued','running','cancel_requested'): raise CoreError('VISION_INVALID_RESPONSE',502)
            if state!=previous:
                yield {'type':'progress','stage':'IMAGE_ANALYZING','jobId':job,'state':state,'status':'RUNNING','elapsedMs':int((time.monotonic()-started)*1000)}
                previous=state
            time.sleep(.2)
    except GeneratorExit:
        if job:
            try: call('POST',f'/jobs/{job}/cancel',{})
            except CoreError: pass
        raise
