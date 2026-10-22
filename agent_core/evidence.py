"""Lossless OCR context sharing and bounded, navigable model projections."""
import copy
import json
from collections import Counter
from urllib.parse import quote
from .security import CoreError


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':'))


def merge_design_contexts(designs,image_ids):
    if len(designs)!=len(image_ids):raise CoreError('INVALID_EVIDENCE_CONTEXT')
    if not designs:return {}
    unique=[];bindings=[];identities=[]
    for image_id,design in zip(image_ids,designs):
        if not isinstance(design,dict):raise CoreError('INVALID_DESIGN_DATA')
        identity=canonical(design)
        if identity not in identities:identities.append(identity);unique.append(copy.deepcopy(design))
        bindings.append({'image_id':str(image_id),'context':identities.index(identity)})
    if len(unique)==1 and len(canonical(unique[0]).encode())<=65536:return unique[0]
    def escape(node):
        if isinstance(node,dict):
            if set(node)&{'$core_ref','$core_object','$core_literal'}:
                return {'$core_literal':[[key,escape(value)] for key,value in node.items()]}
            return {key:escape(value) for key,value in node.items()}
        if isinstance(node,list):return [escape(value) for value in node]
        return node
    unique=[escape(design) for design in unique]
    # Share identical top-level engineering sections. Contexts remain separate:
    # a global candidate directory never replaces a selected candidate's fields.
    counts=Counter((key,canonical(value)) for design in unique for key,value in design.items())
    shared={};references={};contexts=[]
    for design in unique:
        context={}
        for key,value in design.items():
            identity=(key,canonical(value))
            if counts[identity]>1 and len(identity[1].encode())>96:
                if identity not in references:
                    sid='s'+str(len(shared));references[identity]=sid;shared[sid]=copy.deepcopy(value)
                context[key]={'$core_ref':'/shared/'+references[identity]}
            else:context[key]=copy.deepcopy(value)
        contexts.append(context)
    result={'schema':'core-shared-design/1','shared':shared,'contexts':contexts,'image_contexts':bindings,
            'encoding':'$core_ref is an exact JSON-pointer copy. $core_object=[field-set,values] maps object_fields[field-set] to values in order. $core_literal is an object encoded as literal key/value pairs; its keys are data, not operators. No field is omitted.'}
    if len(canonical(result).encode())<=65536:return result
    # Repeated field names are metadata, not engineering content. Store those
    # names once while retaining every value, unit, ID, handle and coordinate.
    shapes=Counter()
    def count(node):
        if isinstance(node,dict):
            if '$core_ref' not in node:shapes[tuple(sorted(node))]+=1
            for child in node.values():count(child)
        elif isinstance(node,list):
            for child in node:count(child)
    count(shared);count(contexts)
    selected={keys:'f'+str(i) for i,(keys,n) in enumerate(shapes.items())
              if len(keys)>=3 and n>=3 and (sum(len(k)+3 for k in keys)-24)*n>sum(len(k)+3 for k in keys)+10}
    def pack(node):
        if isinstance(node,dict):
            keys=tuple(sorted(node))
            if keys in selected:return {'$core_object':[selected[keys],[pack(node[k]) for k in keys]]}
            return {k:pack(v) for k,v in node.items()}
        if isinstance(node,list):return [pack(v) for v in node]
        return node
    result['shared']=pack(shared);result['contexts']=pack(contexts)
    result['object_fields']={identifier:list(keys) for keys,identifier in selected.items()}
    # OCR's adapter enforces the final 64 KiB bound. Truly oversized unique
    # engineering data is rejected there; this function never drops fields.
    return result


def pointer_token(value):return str(value).replace('~','~0').replace('/','~1')


def read_pointer(value,pointer):
    if not isinstance(pointer,str) or len(pointer)>2048 or pointer and not pointer.startswith('/'):
        raise CoreError('INVALID_RESULT_POINTER')
    for raw in pointer.split('/')[1:] if pointer else []:
        token=raw.replace('~1','/').replace('~0','~')
        if isinstance(value,dict) and token in value:value=value[token]
        elif isinstance(value,list) and token.isdigit() and int(token)<len(value):value=value[int(token)]
        else:raise CoreError('RESULT_POINTER_NOT_FOUND',404)
    return value


def model_projection(name,result,operation_id,run_id):
    """Keep actionable fields as JSON; large source sections stay addressable."""
    if name.startswith(('stock_','mcp__stock__')):
        result=copy.deepcopy(result)
        artifact=result.get('data',{}).get('stockArtifact')
        if isinstance(artifact,dict):
            artifact.pop('table',None)
            artifact['preview']=artifact.get('preview',[])[:20]
            artifact['charts']=[{k:c[k] for k in ('kind','title') if k in c} for c in artifact.get('charts',[])]
    reference={'resultRef':operation_id,'runId':run_id,
               'fullResultUrl':f'/v1/runs/{run_id}/results/{quote(operation_id,safe="")}',
               'readTool':'session_read_result'}
    if result.get('_core_cached_from'):
        return {'status':result.get('status'),'cached':True,'_core':{**reference,'resultRef':result['_core_cached_from']},
                'artifacts':copy.deepcopy(result.get('artifacts',[])),
                'instruction':'相同只读证据已在本次运行获取。复用已有记录和图片索引；不要再次获取同一证据。需要字段时按resultRef和pointer读取。'}
    # Preserve chat recall behavior and its frontend citations separately.
    if name in ('recall','list_documents','current_time'):
        out=copy.deepcopy(result)
        for item in out.get('results',[]):
            if len(item.get('passage',''))>6000:item['passage']=item['passage'][:6000];item['passageTruncated']=True
        return out
    heavy={'native_document','design_data'}
    def ref(node,path):
        return {'resultRef':operation_id,'pointer':path,'type':type(node).__name__,
                'count':len(node) if isinstance(node,(list,dict,str)) else 1}
    def project(node,path,limit,text_limit):
        if isinstance(node,str):return node if len(node)<=text_limit else ref(node,path)
        if isinstance(node,list):
            # Parent metadata exposes nextOffset without inventing partial JSON.
            return [project(v,path+'/'+str(i),limit,text_limit) for i,v in enumerate(node[:limit])]
        if isinstance(node,dict):
            out={}
            for key,value in node.items():
                child=path+'/'+pointer_token(key)
                if key in heavy:
                    out[key]=ref(value,child)
                    if key=='design_data' and isinstance(value,dict):out[key]['sections']=list(value)
                elif key=='chunks' and isinstance(value,list):out[key]=ref(value,child)
                else:
                    local_limit=50 if key=='candidates' and name=='cad_get_drawing_evidence' else limit
                    out[key]=project(value,child,local_limit,text_limit)
                    if isinstance(value,list) and len(value)>local_limit:
                        out[key+'Page']={**ref(value,child),'offset':0,'nextOffset':local_limit}
            return out
        return node
    for limit,text_limit in ((12,4000),(6,2000),(3,1000),(1,256)):
        out=project(result,'',limit,text_limit)
        out['_core']=reference
        if name.startswith('cad_get_'):
            out['_core']['nextActions']='目录只需获取一次。按候选取局部证据后直接使用artifacts.imageIndex调用OCR；核实字段与record_id后按需求调用提案工具。原始完整字段可按pointer分页读取，无需重复取证。'
            if name=='cad_get_document_evidence':
                out['_core']['nextActions']='使用artifacts.imageIndex调用OCR，capability=cad.document_reconstruction。Core会自动携带本图片的原文和粗结构。结果保留为文档待审草稿，不需要提交零件提案。'
        if len(canonical(out).encode())<=24576:return out
    # An unusually wide object still has a structured, exact full-result handle.
    return {'status':result.get('status'),'_core':reference,'result':ref(result,''),'availableKeys':list(result)[:20],
            'artifacts':[{k:a[k] for k in ('type','imageIndex','inspectionTool') if k in a} for a in result.get('artifacts',[])],
            'instruction':'结果超过模型上下文预算。请用session_read_result按pointer分页读取完整字段。'}


def result_page(result,pointer='',offset=0,limit=10):
    value=read_pointer(result,pointer)
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=50:raise CoreError('INVALID_RESULT_PAGE')
    if isinstance(value,list):
        return {'value':copy.deepcopy(value[offset:offset+limit]),'total':len(value),'offset':offset,'nextOffset':offset+limit if offset+limit<len(value) else None}
    if isinstance(value,str):
        end=offset+8000
        return {'value':value[offset:end],'total':len(value),'offset':offset,'nextOffset':end if end<len(value) else None}
    if isinstance(value,dict):
        items=list(value.items())
        return {'value':copy.deepcopy(dict(items[offset:offset+limit])),'total':len(items),'offset':offset,
                'nextOffset':offset+limit if offset+limit<len(items) else None}
    return {'value':copy.deepcopy(value)}
