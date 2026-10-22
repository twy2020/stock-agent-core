import copy
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from langgraph.config import get_stream_writer
from . import knowledge, registry, vision, capabilities
from .images import validate_images
from .evidence import canonical, merge_design_contexts, model_projection, result_page
from .security import CoreError, authorize


def tool(name,description,properties,required=()):
    return {'type':'function','function':{'name':name,'description':description,'parameters':{
        'type':'object','properties':properties,'required':list(required),'additionalProperties':False}}}


def validate_final_answer(domain,content,proposal_receipts):
    if domain!='cad':return
    if re.search(r'DSML|<\s*(?:tool_call|function_call|invoke)\b',content,re.I):raise CoreError('UNEXECUTED_TOOL_CALL')
    # Only an entire, unequivocal completion assertion is rejected. Negation,
    # questions and quoted status discussion are normal read-only analysis.
    if not proposal_receipts and re.fullmatch(r'\s*已(?:经)?(?:成功)?提交(?:提案|参数提案|审核表)[。.!！]?\s*',content):
        raise CoreError('UNCONFIRMED_SUBMISSION')


class Runtime:
    def __init__(self,config,store,service,run,body,cancelled):
        self.config,self.store,self.service,self.run,self.body,self.cancelled=config,store,service,run,body,cancelled
        self.context=body['context'];self.domain=run['domain'];self.sid=body.get('spaceId')
        self.deadline=time.monotonic()+300
        self.max_output_tokens=min(8192,max(256,int(service.get('maxOutputTokens',4096))))
        names=body.get('tools',[])
        self.entries={k:copy.deepcopy(v) for k,v in registry.catalog(config,service,self.domain).items() if k in names}
        self.skills=capabilities.active_skills(config,service,self.domain)
        self.skill_instructions='\n\n'.join(s['instructions'] for s in self.skills.values())
        self.rag_callback=copy.deepcopy(service.get('ragCallback'))
        self.vision_mode=body.get('_visionMode','ocr');self.pending_visual_indices=[]
        self.direct_visual_indices=list(range(len(body.get('images',[])))) if self.vision_mode=='direct' else []
        self.images=copy.deepcopy(body.get('images',[]));self.design_data=copy.deepcopy(body.get('design_data',{}))
        self.image_design_data={i:copy.deepcopy(self.design_data) for i in range(len(self.images))}
        self.next_label=1
        self.read_cache={};self.results={};self.result_grants={};self.proposal_receipts=[];self.tool_failures=[];self.visual_observations=[]
        self.tools=[{'type':'function','function':{'name':name,'description':e['description'],'parameters':e.get('inputSchema',{'type':'object'})}} for name,e in self.entries.items()]
        self.tools.append(tool('current_time','获取当前真实日期、星期和时间，时区 Asia/Shanghai。',{}))
        if self.domain=='chat' and service.get('ragEnabled',True) and self.context.get('knowledge_base_id') and service.get('ragCallback') and not service.get('ragViaMcp',False):
            self.tools += [tool('list_documents','分页获取当前知识库已发布文档目录。',{'offset':{'type':'integer','minimum':0,'maximum':1000000},'limit':{'type':'integer','minimum':1,'maximum':100}}),
                           tool('recall','检索当前知识库并保留引用。专业知识和标准问题必须先查证。',{'question':{'type':'string','minLength':1,'maxLength':8192},'documentIds':{'type':'array','items':{'type':'string'},'maxItems':50},'topK':{'type':'integer','minimum':1,'maximum':20}},['question'])]
        if self.sid or self.entries:
            self.tools.append(tool('session_read_result','按结果引用读取本次运行完整证据字段；避免重复请求外部证据。pointer为JSON pointer，可对数组/长文本分页。',{
                'resultRef':{'type':'string'},'pointer':{'type':'string','maxLength':2048},'offset':{'type':'integer','minimum':0},
                'limit':{'type':'integer','minimum':1,'maximum':50}},['resultRef']))
        if self.sid:
            self.tools += [tool('session_list_documents','列出当前会话装载的资料',{}),
                           tool('session_recall','检索当前会话证据，保留来源、版本和坐标。',{'question':{'type':'string','minLength':1,'maxLength':8192},'topK':{'type':'integer','minimum':1,'maximum':20},'documentIds':{'type':'array','items':{'type':'string'},'maxItems':100}},['question']),
                           tool('session_unload_document','从临时会话卸载资料，不删除原文件。',{'documentId':{'type':'string'}},['documentId'])]
            readable=[n for n,e in self.entries.items() if e.get('effect')=='read' and e.get('transport')!='ocr-vision-v1']
            if readable: self.tools.append(tool('session_load_document','调用获准证据工具，装载返回的 data.document。',{'tool':{'type':'string','enum':readable},'arguments':{'type':'object'}},['tool','arguments']))

    def model_result(self,name,result,operation):
        self.results[operation['operationId']]=copy.deepcopy(result)
        self.result_grants.setdefault(operation['operationId'],{name} if name in self.entries else set())
        entry=self.entries.get(name,{})
        if entry.get('_mcpServer',{}).get('transport')=='rag-bridge' and isinstance(result.get('data'),dict):
            return model_projection(entry['_mcpTool'],result['data'],operation['operationId'],self.run['id'])
        return model_projection(name,result,operation['operationId'],self.run['id'])

    def result_authorities(self,name,operation):
        return sorted(self.result_grants.get(operation['operationId'],{name} if name in self.entries else set()))

    def validate_answer(self,content):
        validate_final_answer(self.domain,content,self.proposal_receipts)

    def record_answer_rejection(self,content,reason):
        from .admin_safety import safe_data
        details=safe_data({'reason':reason,'contentChars':len(content),'excerpt':content[:2000],
                           'truncated':len(content)>2000,'caller':self.service['id']})
        with self.store.db() as db:
            db.execute('INSERT INTO admin_audit(created,kind,object_id,changes) VALUES(?,?,?,?)',
                       (time.time(),'answer_rejected',self.run['id'],json.dumps(details,ensure_ascii=False)))

    def note_tool_result(self,name,result,operation):
        code=result.get('error')
        if isinstance(code,dict):code=code.get('code')
        if code or result.get('status') in ('partial','failed'):
            self.tool_failures.append({'tool':name,'operationId':operation['operationId'],'code':code or 'TOOL_PARTIAL'})
        if isinstance(result.get('coverage'),dict):
            self.visual_observations.append({'operationId':operation['operationId'],**copy.deepcopy(result['coverage'])})

    def outcome(self):
        state={'status':'partial' if self.tool_failures else 'completed','toolFailures':copy.deepcopy(self.tool_failures)}
        if self.vision_mode=='direct':
            state['directVision']={'submittedImageIndices':list(self.direct_visual_indices),'fieldVerification':'unverified',
                                   'notice':'图片已提交核心视觉模型；发送图片不代表模型已核实字段，也不是独立证据。'}
        if self.domain!='cad':return state if self.tool_failures or self.vision_mode=='direct' else {}
        return {**state,'visualObservations':copy.deepcopy(self.visual_observations),
                'proposalSubmitted':bool(self.proposal_receipts),'proposalReceipts':copy.deepcopy(self.proposal_receipts),
                'notice':'提案已由CAD接收，仍需人工审核。' if self.proposal_receipts else '未提交提案；只读分析不代表写入CAD提案库。'}

    def check(self):
        if self.cancelled(): raise CoreError('CANCELLED')
        if time.monotonic()>self.deadline: raise CoreError('RUN_DEADLINE')
        if not self.service.get('enabled',True): raise CoreError('SERVICE_REVOKED',403)
        authorize(self.service,self.context,self.domain)
        live_skills=capabilities.active_skills(self.config,self.service,self.domain)
        if any(live_skills.get(k)!=v for k,v in self.skills.items()):raise CoreError('SKILL_REVOKED',403)
        if self.vision_mode=='direct':
            from .config import model_profile
            profile=self.body.get('_profile',{})
            live=model_profile(self.config,self.service,profile.get('id'))
            if self.service.get('visionPolicy','ocr') not in ('direct','both') or not live.get('capabilities',{}).get('vision'):
                raise CoreError('VISION_POLICY_REVOKED',403)
            if live!=profile:raise CoreError('MODEL_PROFILE_CHANGED',409)
        if self.sid and self.store.space(self.service,self.sid)['context']!=self.context:raise CoreError('CONTEXT_CONFLICT',409)

    def drain_visual_messages(self):
        if not self.pending_visual_indices:return []
        from .images import direct_image_message
        self.check()
        indices=list(self.pending_visual_indices);self.pending_visual_indices.clear()
        self.direct_visual_indices.extend(indices)
        return [direct_image_message(self.images,indices)]

    def capture_artifacts(self,result):
        result=copy.deepcopy(result)
        data=result.get('data') or {}
        design=data.get('design_data',self.design_data)
        if not isinstance(design,dict): raise CoreError('INVALID_DESIGN_DATA')
        if 'design_data' in data: self.design_data=copy.deepcopy(design)
        for artifact in result.get('artifacts',[]):
            if artifact.get('type')=='image' and ('data' in artifact or 'data_base64' in artifact):
                new=validate_images([artifact])[0]
                validate_images([*self.images,new])
                index=len(self.images);self.images.append(new);self.image_design_data[index]=copy.deepcopy(design)
                artifact.pop('data',None);artifact.pop('data_base64',None)
                artifact['imageIndex']=index;artifact['inspectionTool']='core_vlm' if self.vision_mode=='direct' else 'ocr_analyze'
                if self.vision_mode=='direct':self.pending_visual_indices.append(index)
        return result

    def execute(self,name,raw,operation):
        self.check()
        function=next((t['function'] for t in self.tools if t['function']['name']==name),None)
        if not function: raise CoreError('TOOL_FORBIDDEN',403)
        try: args=json.loads(raw)
        except (ValueError,TypeError): raise CoreError('INVALID_ARGUMENTS') from None
        registry.validate(function['parameters'],args)
        request_id=str(uuid.uuid5(uuid.NAMESPACE_URL,self.run['id']+':'+str(operation['operationId'])))
        if name=='session_read_result':
            result=self.results.get(args['resultRef'])
            if result is None:raise CoreError('RESULT_NOT_FOUND',404)
            grants=self.result_grants.get(args['resultRef'],set())
            live=registry.catalog(self.config,self.service,self.domain)
            if any(tool_name not in live or live[tool_name]!=self.entries.get(tool_name) for tool_name in grants):raise CoreError('TOOL_REVOKED',403)
            self.result_grants[operation['operationId']]=set(grants)
            return result_page(result,args.get('pointer',''),args.get('offset',0),args.get('limit',10))
        if name=='current_time':
            now=datetime.now(timezone(timedelta(hours=8)))
            return {'time':now.isoformat(),'timezone':'Asia/Shanghai','weekday':now.isoweekday()}
        if name in ('recall','list_documents'):
            if self.service.get('ragViaMcp',False) or not self.service.get('ragCallback') or self.service.get('ragCallback')!=self.rag_callback:raise CoreError('TOOL_REVOKED',403)
            identity={**self.context,'caller_id':self.service['id'],'service_id':self.service['id'],'tenant_id':self.service['tenantId']}
            entry=self.service['ragCallback']
            result=registry.json_request('POST',entry['url'],entry,{'operation':name,'arguments':args,'context':identity,'request_id':request_id},request_id)
            self.check()
            if name=='recall':
                if not isinstance(result.get('results'),list): raise CoreError('RAG_INVALID_RESPONSE',502)
                for item in result['results']:
                    item['label']=self.next_label;self.next_label+=1
                get_stream_writer()({'type':'retrieval','payload':result})
            return result
        if name in self.entries:
            live=registry.catalog(self.config,self.service,self.domain).get(name)
            if not live or live!=self.entries[name]: raise CoreError('TOOL_REVOKED',403)
            if live.get('transport')=='ocr-vision-v1':
                self.read_cache.clear()
                capability=vision.select_capability(self.domain,args)
                selected=args.get('imageIndices',list(range(len(self.images))))
                if any(type(i) is not int or i<0 or i>=len(self.images) for i in selected): raise CoreError('IMAGE_NOT_FOUND')
                images=[{'image_id':str(i),'mime_type':self.images[i]['mediaType'],'data_base64':self.images[i]['data'],
                         'source_ref':self.images[i].get('source_ref') or self.context} for i in selected]
                designs=[self.image_design_data[i] for i in selected]
                design=(vision.document_design_context(designs,[str(i) for i in selected]) if capability=='cad.document_reconstruction'
                        else merge_design_contexts(designs,[str(i) for i in selected]) if designs else self.design_data)
                identity={**self.context,'service_id':self.service['id'],'caller_id':self.service['id'],'tenant_id':self.service['tenantId']}
                result=None;sources=[];source_refs=[];coverage={}
                for event in vision.analyze(live,images,args.get('question',args.get('text','')),design,identity,request_id,self.cancelled,self.body.get('source_refs',[]),
                                            capability=capability):
                    if event['type']=='result':result=event['result'];sources=event['sources'];source_refs=event['source_refs'];coverage=event['coverage']
                    else:get_stream_writer()({**event,'parentOperationId':operation['operationId']})
                self.check()
                return {'contractVersion':'1','status':'partial' if coverage.get('error') else 'succeeded','data':result,
                        'coverage':coverage,'error':coverage.get('error'),'sources':sources,'source_refs':source_refs,
                        'instruction':'coverage只表示可用图片观察，不代表工程字段已核实。原生文字、设计参数、矩形区域和模型元数据不能充当图片字段识读证据。不得声称检查了unobserved_image_ids中的图片；缺少图像证据时仅按原生证据提案，并保留视觉未核实说明。'}
            cache_key=canonical({'tool':name,'arguments':args})
            if live.get('effect')=='read' and cache_key in self.read_cache:
                previous=self.read_cache[cache_key]
                self.result_grants[operation['operationId']]={name}
                return {**copy.deepcopy(self.results[previous]),'_core_cached_from':previous}
            if live.get('effect')!='read':self.read_cache.clear()
            result=registry.execute(self.config,self.service,self.context,self.domain,name,args,request_id)
            self.check();result=self.capture_artifacts(result)
            if live.get('_mcpServer',{}).get('transport')=='rag-bridge' and live.get('_mcpTool')=='recall':
                for item in result['data']['results']:
                    item['label']=self.next_label;self.next_label+=1
                get_stream_writer()({'type':'retrieval','payload':result['data']})
            self.results[operation['operationId']]=copy.deepcopy(result)
            self.result_grants[operation['operationId']]={name}
            if live.get('effect')=='read':self.read_cache[cache_key]=operation['operationId']
            if name=='cad_propose_parts' or live.get('httpTool')=='cad_propose_parts':
                data=result.get('data') or {};proposals=data.get('proposals') or []
                ids=[p['proposal_id'] for p in proposals if isinstance(p,dict) and p.get('proposal_id')]
                if data.get('analysis_id') and ids:self.proposal_receipts.append({'analysis_id':data['analysis_id'],'proposal_ids':ids})
            return result
        space=self.store.space(self.service,self.sid)
        if space['context']!=self.context: raise CoreError('CONTEXT_CONFLICT',409)
        if name=='session_list_documents': return {'items':[{k:d[k] for k in ('id','title','revision')} for d in self.store.documents(self.sid)]}
        if name=='session_recall': return knowledge.search(self.store,space,args)
        if name=='session_unload_document':
            self.read_cache.clear()
            if space['kind']!='temporary': raise CoreError('PERSISTENT_SPACE_READ_ONLY',403)
            with self.store.db() as db: db.execute('DELETE FROM documents WHERE space_id=? AND id=?',(self.sid,args['documentId']))
            return {'unloaded':args['documentId']}
        if name=='session_load_document':
            self.read_cache.clear()
            result=registry.execute(self.config,self.service,self.context,self.domain,args['tool'],args['arguments'],request_id)
            self.check();result=self.capture_artifacts(result)
            self.result_grants[operation['operationId']]={args['tool']}
            document=(result.get('data') or {}).get('document')
            if not isinstance(document,dict): raise CoreError('DOCUMENT_RESULT_REQUIRED')
            return knowledge.put(self.store,space,document.get('id'),{k:v for k,v in document.items() if k!='id'})
        raise CoreError('TOOL_FORBIDDEN',403)
