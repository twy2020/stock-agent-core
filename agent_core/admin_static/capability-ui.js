"use strict";
window.CoreCapabilities=function(ctx){
  const {esc,input,select,text,bool,checks,help,getData,state}=ctx;
  const $=id=>document.getElementById(id),arr=x=>Array.isArray(x)?x:[],effects=[{id:'read',name:'读取'},{id:'analyze',name:'分析'},{id:'compute',name:'计算'},{id:'propose',name:'提交提案'},{id:'write',name:'写入'}];
  let candidates=[];
  const group=(title,body,open=false)=>`<details class="config-group" ${open?'open':''}><summary>${esc(title)}</summary><div class="detail-inner">${body}</div></details>`;
  const domains=item=>checks('domains','能力域',item.domains||['chat','cad'],[{id:'chat',name:'对话 / SaaS'},{id:'cad',name:'图纸 / CAD'}]);
  const credentials=()=>`<section class="editor-section"><h3>Authentication</h3>${input('secret','Bearer Token','','password','留空保留已保存凭据；无鉴权接口可留空。')}</section>`;
  function fields(kind,item,existing){
    const d=getData();
    if(kind==='services')return domains(item)+group('模型与运行',checks('modelProfiles','允许模型',item.modelProfiles,d.models||[])+select('defaultModelProfile','默认模型',item.defaultModelProfile,d.models||[])+select('visionPolicy','图片准用路径',item.visionPolicy||'ocr',[{id:'ocr',name:'只允许 OCR 工具'},{id:'direct',name:'只允许 Core 视觉模型'},{id:'both',name:'OCR / Core 视觉模型均可'}])+input('maxActiveRuns','Agent 同时运行上限',item.maxActiveRuns||2,'number')+input('maxOutputTokens','单次输出上限',item.maxOutputTokens||4096,'number'),true)+group('MCP 服务与工具',bool('ragViaMcp','RAG 仅经 MCP 授权',item.ragViaMcp===true)+checks('mcpServers','允许 MCP 服务',item.mcpServers,d.mcpServers||[])+arr(d.mcpServers).map(s=>checks('mcpTools',s.name||s.id,item.mcpTools,arr(s.tools).map(t=>({id:`mcp__${s.id}__${t.name}`,name:t.name,description:t.description})))).join(''))+group('Tools、Skills 与插件',checks('tools','允许 HTTP 工具',item.tools,d.httpTools||[])+checks('skills','允许 Skills',item.skills,d.skills||[])+checks('plugins','允许插件',item.plugins,d.plugins||[]))+group('CAD 项目范围',text('projectIds','允许项目 ID',arr(item.projectIds).join('\n'),'每行一个项目 ID；CAD 任务只能访问清单中的项目。'))+'<div id="policy-feedback" aria-live="polite"></div>';
    let result='';
    if(['mcpServers','httpTools','skills'].includes(kind))result+=domains(item)+select('pluginId','所属插件',item.pluginId,d.plugins||[]);
    if(kind==='mcpServers')result+=select('transport','Transport',item.transport||'streamable-http',[{id:'streamable-http',name:'Streamable HTTP'},{id:'http-bridge',name:'HTTP Bridge'},{id:'rag-bridge',name:'RAG Bridge'}])+`<div id="mcp-remote-fields">${input('url','URL',item.url||'')}${credentials()}</div>`+input('timeoutSeconds','Timeout (s)',item.timeoutSeconds||30,'number')+`<section class="tool-editor"><div class="editor-section-heading"><h3>Tools</h3></div><div id="mcp-tool-rows"></div><div class="editor-actions"><button type="button" class="button" data-add-mcp-tool>＋ 添加工具</button>${existing?'<button type="button" class="button" data-discover-mcp>发现工具</button>':''}</div><div id="mcp-discovery"></div></section>`;
    if(kind==='httpTools')result+=input('url','URL',item.url||'')+select('effect','操作类型',item.effect||'read',effects)+select('transport','Transport',item.transport||'http',[{id:'http',name:'HTTP'},{id:'ocr-vision-v1',name:'OCR Vision API'}])+input('timeoutSeconds','Timeout (s)',item.timeoutSeconds||30,'number')+credentials()+group('JSON Schema',text('inputSchema','Input Schema',JSON.stringify(item.inputSchema||{type:'object',properties:{}},null,2))+text('outputSchema','Output Schema',item.outputSchema?JSON.stringify(item.outputSchema,null,2):''),true);
    if(kind==='skills')result+=text('instructions','技能指令',item.instructions||'','按应用和能力域注入 Agent；技能不自动授予工具权限。');
    if(['mcpServers','httpTools','skills','plugins'].includes(kind))result+=text('description','说明',item.description||'');
    if(kind==='plugins'&&existing){
      const members=['mcpServers','httpTools','skills'].flatMap(k=>arr(d[k]).filter(x=>x.pluginId===item.id).map(x=>`<button type="button" class="member-link" data-reg-kind="${k}" data-reg-id="${esc(x.id||x.name)}"><span>${esc(x.name||x.id)}</span><small>${esc({mcpServers:'MCP',httpTools:'Tool',skills:'Skill'}[k])} · ${x.enabled===false?'停用':'启用'}</small><span aria-hidden="true">›</span></button>`));
      result+=group(`关联能力 · ${members.length}`,members.join('')||'<p class="muted">在 MCP、Tool 或 Skill 的属性中选择此插件。</p>',true);
    }
    return result;
  }
  function toolRow(t,i){
    const data=getData(),control=(key,label,value,type='text')=>`<label class="tool-field">${esc(label)}<input data-tool-field="${key}" name="_tool_${i}_${key}" value="${esc(value??'')}" type="${type}" ${key==='name'?'required pattern="[A-Za-z0-9_-]{1,48}"':''}></label>`;
    return `<details class="mcp-tool-row" ${!t.name?'open':''}><summary>${esc(t.name||'新工具')} <span class="muted">${t.enabled===false?'停用':'启用'}</span></summary><div class="detail-inner">${control('name','工具标识',t.name)}<label class="switch-label tool-field"><input data-tool-field="enabled" name="_tool_${i}_enabled" type="checkbox" ${t.enabled!==false?'checked':''}>启用工具</label>${control('description','工具说明',t.description)}<label class="tool-field">操作类型<select data-tool-field="effect" name="_tool_${i}_effect" required><option value="">请选择</option>${effects.map(x=>`<option value="${x.id}" ${t.effect===x.id?'selected':''}>${x.name}</option>`).join('')}</select></label><label class="tool-field bridge-target">桥接 HTTP 工具<select data-tool-field="httpTool" name="_tool_${i}_httpTool"><option value="">请选择</option>${arr(data.httpTools).map(x=>`<option value="${esc(x.name)}" ${t.httpTool===x.name?'selected':''}>${esc(x.name)}</option>`).join('')}</select></label><label class="tool-field">输入 JSON Schema<textarea data-tool-field="inputSchema" name="_tool_${i}_schema" rows="5" spellcheck="false">${esc(JSON.stringify(t.inputSchema||{type:'object',properties:{}},null,2))}</textarea></label><button type="button" class="button subtle" data-remove-mcp-tool>移除工具</button></div></details>`;
  }
  function toolsFromDOM(){return [...document.querySelectorAll('.mcp-tool-row')].map(row=>{
    const get=k=>row.querySelector(`[data-tool-field="${k}"]`),tool={name:get('name').value.trim(),description:get('description').value.trim(),effect:get('effect').value,enabled:get('enabled').checked};
    try{tool.inputSchema=JSON.parse(get('inputSchema').value);}catch{throw new Error(`工具 ${tool.name||'（未命名）'} 的输入 Schema 不是有效 JSON。`);}
    if($('reg-transport').value==='http-bridge')tool.httpTool=get('httpTool').value;
    return tool;
  });}
  function renderTools(tools){$('mcp-tool-rows').innerHTML=tools.map(toolRow).join('');syncTransport();}
  function syncTransport(){
    if(!$('mcp-tool-rows'))return;const transport=$('reg-transport').value,remote=$('mcp-remote-fields');
    remote.hidden=transport!=='streamable-http';remote.querySelectorAll('input').forEach(el=>el.disabled=remote.hidden);$('reg-url').required=!remote.hidden;
    document.querySelectorAll('.bridge-target').forEach(el=>{el.hidden=transport!=='http-bridge';el.querySelector('select').required=!el.hidden;});
  }
  function serviceDraft(form){const f=new FormData(form),draft={};for(const k of ['domains','modelProfiles','tools','mcpServers','mcpTools','skills','plugins'])draft[k]=f.getAll(k);draft.defaultModelProfile=f.get('defaultModelProfile');draft.visionPolicy=f.get('visionPolicy');return draft;}
  function updatePolicy(){const host=$('policy-feedback');if(!host)return;const issues=window.CoreCapabilityData.policyIssues(serviceDraft($('registry-form')),getData());host.innerHTML=issues.length?`<div class="notice"><strong>授权依赖 · ${issues.length}</strong><ul>${issues.map(s=>`<li>${esc(s)}</li>`).join('')}</ul></div>`:'<p class="detail-subtitle">当前选择未发现授权依赖冲突。</p>';}
  function setup(kind,item){candidates=[];if(kind==='mcpServers')renderTools(arr(item.tools));if(kind==='services')updatePolicy();if(kind==='httpTools'){$('reg-url').required=true;}if(kind==='skills')$('reg-instructions').required=true;}
  function read(kind,form,result){
    if(['services','mcpServers','httpTools','skills'].includes(kind))result.domains=new FormData(form).getAll('domains');
    if(kind==='services'){
      result.projectIds=String(new FormData(form).get('projectIds')||'').split(/[\n,]/).map(v=>v.trim()).filter(Boolean);
      if(result.defaultModelProfile&&!result.modelProfiles.includes(result.defaultModelProfile))throw new Error('请将默认模型加入允许模型，或更换默认模型。');
    }
    if(kind==='mcpServers'){
      result.tools=toolsFromDOM();const names=result.tools.map(t=>t.name);
      if(new Set(names).size!==names.length)throw new Error('工具标识重复，请修改或移除重复项。');
      window.CoreCapabilityData.mergeTools([],result.tools,result.transport,getData().httpTools);
      if(result.tools.some(t=>`mcp__${new FormData(form).get('id')}__${t.name}`.length>64))throw new Error('MCP 服务与工具标识组合过长，请缩短标识。');
    }
  }
  function showDiscovery(result){
    candidates=arr(result.tools);const current=toolsFromDOM();
    $('mcp-discovery').innerHTML=`<div class="discovery-review"><h3>发现 ${candidates.length} 项工具</h3><p class="detail-subtitle">勾选后加入待保存配置。</p>${candidates.map((t,i)=>{const known=current.find(x=>x.name===t.name);return `<div class="discovered-tool"><label class="switch-label"><input type="checkbox" name="_candidate" data-candidate="${i}"><strong>${esc(t.name)}</strong></label><p>${esc(t.description||'无说明')}</p><label class="tool-field">操作类型<select name="_candidate_effect_${i}" data-candidate-effect="${i}"><option value="">请确认操作类型</option>${effects.map(e=>`<option value="${e.id}" ${known?.effect===e.id?'selected':''}>${e.name}</option>`).join('')}</select></label><details><summary>参数结构${known?' · 已登记':''}</summary><pre>${esc(JSON.stringify(t.inputSchema||{type:'object'},null,2))}</pre></details></div>`;}).join('')}<button type="button" class="button" data-apply-discovery>加入所选工具</button><p class="detail-subtitle">新工具默认停用，保存后再向应用授权。</p></div>`;
  }
  function error(ex){const host=$('registry-error');if(host){host.hidden=false;host.textContent=ex.message;}}
  document.addEventListener('change',e=>{
    if(!e.target.closest('#registry-form'))return;state.dirty=true;
    if(e.target.id==='reg-transport'){$('mcp-discovery')?.replaceChildren();syncTransport();}
    if(e.target.dataset.toolField==='httpTool'){
      const tool=arr(getData().httpTools).find(t=>t.name===e.target.value),row=e.target.closest('.mcp-tool-row');
      if(tool){row.querySelector('[data-tool-field="effect"]').value=tool.effect;row.querySelector('[data-tool-field="inputSchema"]').value=JSON.stringify(tool.inputSchema||{type:'object'},null,2);}
    }
    updatePolicy();
  });
  document.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;try{
    if(b.hasAttribute('data-add-mcp-tool')){const rows=toolsFromDOM();rows.push({enabled:false});renderTools(rows);state.dirty=true;}
    if(b.hasAttribute('data-remove-mcp-tool')){b.closest('.mcp-tool-row').remove();state.dirty=true;}
    if(b.hasAttribute('data-apply-discovery')){
      const selected=[...document.querySelectorAll('[data-candidate]:checked')].map(el=>({...candidates[Number(el.dataset.candidate)],effect:document.querySelector(`[data-candidate-effect="${el.dataset.candidate}"]`).value}));
      if(!selected.length)throw new Error('请先选择需要登记的工具。');
      renderTools(window.CoreCapabilityData.mergeTools(toolsFromDOM(),selected,$('reg-transport').value,getData().httpTools));
      $('mcp-discovery').replaceChildren();state.dirty=true;
    }
  }catch(ex){error(ex);}});
  return {fields,setup,read,showDiscovery,policyIssues:window.CoreCapabilityData.policyIssues};
};
