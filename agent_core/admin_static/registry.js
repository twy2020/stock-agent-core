"use strict";
// Configuration UI shares the console's authenticated transport and inspector.
window.CoreRegistry = function(ctx) {
  const {api,esc,icon,badge,enabledBadge,help,heading,panel,empty,tags,num,date,toast,showError,detailHeading,revealDetail,closeDetail,state} = ctx;
  const $ = id=>document.getElementById(id), arr=x=>Array.isArray(x)?x:[];
  let data={},usage={},section='mcpServers',selection=null,discovery=null,version=0,usageFilter={},resourceQuery='',resourceStatus='';
  const names={providers:'供应商',providerAccounts:'供应商账号',providerKeys:'API Key',models:'聚合模型',services:'接入应用',mcpServers:'MCP 服务',httpTools:'Tools',skills:'Skills',plugins:'插件'};
  const options=(items,current,blank=true)=>`${blank?'<option value="">未选择</option>':''}${items.map(x=>`<option value="${esc(x.id||x.name)}" ${(x.id||x.name)===current?'selected':''}>${esc(x.name||x.id)}</option>`).join('')}`;
  const input=(key,label,value='',type='text',tip='')=>`<div class="field"><label for="reg-${esc(key)}">${esc(label)}</label><div class="${type==='password'?'secret-control':'input-control'}"><input id="reg-${esc(key)}" name="${esc(key)}" type="${type}" value="${esc(value??'')}" ${type==='number'?'min="0" step="any"':''} autocomplete="${type==='password'?'new-password':'off'}" ${type==='password'?`placeholder="输入 ${esc(label)}；留空保留已保存凭据" spellcheck="false"`:''}>${type==='password'?`<button type="button" class="icon-button" data-toggle-secret="reg-${esc(key)}" title="显示输入内容" aria-label="显示输入内容">${icon('eye')}</button>`:''}</div>${tip?`<small class="field-hint">${esc(tip)}</small>`:''}</div>`;
  const select=(key,label,value,items)=>`<div class="field"><label for="reg-${key}">${esc(label)}</label><select id="reg-${key}" name="${key}">${options(items,value)}</select></div>`;
  const text=(key,label,value,tip='')=>`<div class="field"><label for="reg-${key}">${esc(label)} ${tip?help(tip):''}</label><textarea id="reg-${key}" name="${key}" rows="6" spellcheck="false">${esc(value||'')}</textarea></div>`;
  const checks=(key,label,values,items)=>`<fieldset class="grant-group"><legend>${esc(label)}</legend>${items.length?items.map(x=>`<label><input type="checkbox" name="${key}" value="${esc(x.id||x.name)}" ${arr(values).includes(x.id||x.name)?'checked':''}><span>${esc(x.name||x.id)}${x.description?`<small>${esc(x.description).slice(0,90)}</small>`:''}</span></label>`).join(''):'<span class="muted">暂无可授权项</span>'}</fieldset>`;
  const bool=(key,label,value)=>`<div class="field"><label class="switch-label"><input type="checkbox" name="${key}" ${value?'checked':''}>${esc(label)}</label></div>`;
  const capabilities=window.CoreCapabilities({esc,input,select,text,bool,checks,help,getData:()=>data,state});
  const providerTree=window.CoreProviderTree({esc,icon,heading,empty,enabledBadge,getData:()=>data});
  const protocols=['OPENAI_COMPATIBLE','OPENAI','DEEPSEEK','SILICONFLOW'].map(id=>({id,name:id==='OPENAI_COMPATIBLE'?'OpenAI Compatible':id==='OPENAI'?'OpenAI':id==='DEEPSEEK'?'DeepSeek':'SiliconFlow'}));
  const sectionFields=(title,body)=>`<section class="editor-section"><h3>${esc(title)}</h3><div class="editor-grid">${body}</div></section>`;
  function credentialField(item,label='API Key'){
    return input('secret',label,'','password',item.credentialConfigured?'已配置；留空保持不变。':'')+`<div class="credential-state">${badge(item.credentialConfigured?'已配置':'未配置',item.credentialConfigured?'good':'')}<span>保存后不回显凭据</span></div>`;
  }
  const refName=(kind,id)=>arr(data[kind]).find(x=>(x.id||x.name)===id)?.name||id||'未绑定';
  const editButton=(kind,id,label)=>`<button class="row-link" data-reg-kind="${kind}" data-reg-id="${esc(id)}">${icon(kind==='services'?'apps':kind==='models'?'models':'tools')}<span>${esc(label)}${label===id?'':`<span class="secondary mono">${esc(id)}</span>`}</span></button>`;
  const add=(kind,parent='')=>`<button class="button primary" data-reg-new="${kind}" data-parent="${esc(parent)}">＋ 新增${names[kind]}</button>`;
  function tabBar(){return `<div class="segment-bar" role="tablist">${['mcpServers','httpTools','skills','plugins'].map(k=>`<button role="tab" aria-selected="${section===k}" data-reg-tab="${k}">${names[k]} <span>${arr(data[k]).length}</span></button>`).join('')}</div>`;}
  const actions=(kind,item)=>`<div class="resource-actions"><button class="button compact" data-reg-kind="${kind}" data-reg-id="${esc(item.id||item.name)}">编辑</button><button class="button compact" data-reg-toggle="${kind}" data-reg-id="${esc(item.id||item.name)}">${item.enabled===false?'启用':'停用'}</button>${kind==='services'?'':`<button class="button compact subtle" data-reg-delete="${kind}" data-reg-id="${esc(item.id||item.name)}">删除</button>`}</div>`;
  function providersPage(){
    return providerTree.render();
  }
  function modelsPage(){
    const rows=arr(data.models);
    return heading('模型管理','',`<button class="button" data-start-import>导入模型</button>${add('models')}`)
      +`<div class="filters"><input aria-label="搜索模型" placeholder="搜索模型、别名或 API Key" value="${esc(resourceQuery)}" data-resource-search><select aria-label="模型状态" data-resource-status><option value="">全部状态</option><option value="true" ${resourceStatus==='true'?'selected':''}>已启用</option><option value="false" ${resourceStatus==='false'?'selected':''}>已停用</option></select></div>`
      +panel('模型配置',`<div class="table-wrap"><table class="models-table"><thead><tr><th>模型别名</th><th>上游模型 / Key</th><th>能力</th><th>价格 / 百万 Token</th><th>状态</th><th>操作</th></tr></thead><tbody>${rows.map(m=>`<tr data-resource-row data-enabled="${m.enabled!==false}" data-search="${esc([m.name,m.id,m.model,refName('providerKeys',m.keyId)].join(' ').toLowerCase())}"><td>${editButton('models',m.id,m.name||m.id)}</td><td>${esc(m.model)}<span class="secondary">${m.keyId?esc(refName('providerKeys',m.keyId)):'独立接口'}</span></td><td>${tags([m.capabilities?.vision?'视觉 + 文本':'文本',...(m.capabilities?.tools?['工具']:[])])}</td><td>${m.pricing?.inputPerMillion!=null?`${esc(m.pricing.currency)} ${esc(m.pricing.inputPerMillion)} / ${esc(m.pricing.outputPerMillion)}`:'未配置'}</td><td>${enabledBadge(m.enabled)}</td><td><div class="model-row-actions">${actions('models',m)}<button class="button small" data-model-test="${esc(m.id)}" ${m.enabled===false?'disabled':''}>测试连接</button></div></td></tr>`).join('')}</tbody></table></div><p class="filter-empty muted" data-resource-empty ${rows.length?'hidden':''}>暂无匹配模型</p>`);
  }
  function appsPage(){return heading('接入应用','配置 SaaS 与 CAD 的模型、能力域和准用策略')+panel('应用授权',`<div class="table-wrap"><table class="capability-table"><thead><tr><th>应用</th><th>能力域 / 模型</th><th>视觉路径</th><th>授权检查</th><th>状态</th><th>操作</th></tr></thead><tbody>${arr(data.services).map(s=>{const issues=capabilities.policyIssues(s,data);return `<tr><td>${editButton('services',s.id,s.name||s.id)}</td><td>${tags(s.domains)}<span class="secondary">${arr(s.modelProfiles).length} 个模型 · ${arr(s.mcpTools).length} 个 MCP 工具</span></td><td>${esc({ocr:'OCR 工具',direct:'Core 视觉模型',both:'两种可选'}[s.visionPolicy||'ocr'])}</td><td>${issues.length?badge(issues.length+' 项待检查','warn'):'未发现冲突'}</td><td>${enabledBadge(s.enabled)}</td><td>${actions('services',s)}</td></tr>`;}).join('')}</tbody></table></div>`);}
  function toolsPage(){const rows=arr(data[section]);return heading('能力管理','登记能力并维护调用范围',add(section))+tabBar()+`<div class="filters"><input aria-label="搜索能力" placeholder="搜索名称、标识、说明" value="${esc(resourceQuery)}" data-resource-search><select aria-label="能力状态" data-resource-status><option value="">全部状态</option><option value="true" ${resourceStatus==='true'?'selected':''}>已启用</option><option value="false" ${resourceStatus==='false'?'selected':''}>已停用</option></select></div>`+panel(names[section],`<div class="table-wrap"><table class="capability-table"><thead><tr><th>能力</th><th>类型 / 范围</th><th>配置 / 关联</th><th>状态</th><th>操作</th></tr></thead><tbody>${rows.map(e=>`<tr data-resource-row data-enabled="${e.enabled!==false}" data-search="${esc([e.name,e.id,e.description,e.pluginId].join(' ').toLowerCase())}"><td>${editButton(section,e.id||e.name,e.name||e.id)}</td><td>${esc(({read:'读取',analyze:'分析',compute:'计算',propose:'提案',write:'写入','streamable-http':'远端 MCP','http-bridge':'HTTP / OCR 桥接','rag-bridge':'RAG 桥接'})[section==='httpTools'?e.effect:e.transport]||(section==='plugins'?'能力分组':'指令技能'))}<span class="secondary">${arr(e.domains).join(' · ')}</span></td><td>${section==='mcpServers'?`${arr(e.tools).length} 项工具 · ${arr(e.tools).filter(t=>t.enabled!==false).length} 项启用`:section==='plugins'?`${['mcpServers','httpTools','skills'].flatMap(k=>arr(data[k])).filter(x=>x.pluginId===e.id).length} 项关联能力`:esc((e.description||e.instructions||'').slice(0,70))}${e.pluginId?`<span class="secondary">插件 · ${esc(refName('plugins',e.pluginId))}</span>`:''}</td><td>${enabledBadge(e.enabled)}</td><td>${actions(section,e)}</td></tr>`).join('')}</tbody></table></div><p class="filter-empty muted" data-resource-empty ${rows.length?'hidden':''}>暂无匹配能力</p>`);}
  const money=(value,currency)=>value==null?'未计价':`${esc(currency||'')} ${Number(value).toLocaleString('zh-CN',{maximumFractionDigits:6})}`;
  function usageFilters(){return `<form id="usage-filter" class="filters"><select name="caller" aria-label="费用调用方">${options([{id:'',name:'全部应用'},...arr(data.services)],usageFilter.caller||'',false)}</select><select name="profile" aria-label="费用模型">${options([{id:'',name:'全部模型'},...arr(data.models)],usageFilter.profile||'',false)}</select><input name="startDate" type="date" aria-label="开始日期" value="${esc(usageFilter.startDate||'')}"><input name="endDate" type="date" aria-label="结束日期" value="${esc(usageFilter.endDate||'')}"><button class="button" type="submit">查询</button></form>`;}
  function usagePath(){const q=new URLSearchParams();for(const k of ['caller','profile'])if(usageFilter[k])q.set(k,usageFilter[k]);if(usageFilter.startDate)q.set('start',Math.floor(new Date(usageFilter.startDate+'T00:00:00').getTime()/1000));if(usageFilter.endDate)q.set('end',Math.floor(new Date(usageFilter.endDate+'T00:00:00').getTime()/1000)+86400);return '/usage?'+q;}
  function usagePage(){const g=arr(usage.groups),recent=arr(usage.recent);return heading('用量与费用','Core 调用记录 · 按币种统计',`<button class="button" data-usage-export>导出最近 100 条</button>`)+usageFilters()+`<div class="metrics"><div class="metric"><div class="metric-label">记录调用</div><div class="metric-value">${num(g.reduce((n,x)=>n+x.requests,0))}</div></div><div class="metric"><div class="metric-label">已报告 Token</div><div class="metric-value">${num(g.reduce((n,x)=>n+(x.totalTokens||0),0))}</div></div><div class="metric"><div class="metric-label">用量未知</div><div class="metric-value">${num(g.reduce((n,x)=>n+x.unknownUsageRequests,0))}</div></div><div class="metric"><div class="metric-label">未计价调用 ${help('估算只使用已配置的输入和输出单价。缓存、图像附加费及未报告用量不等于零费用；最终费用以供应商账单为准。')}</div><div class="metric-value">${num(g.reduce((n,x)=>n+x.unknownCostRequests,0))}</div></div></div>`+panel('模型费用',`<div class="table-wrap"><table><thead><tr><th>模型 / 账号</th><th>调用</th><th>输入 / 输出 Token</th><th>已知部分估算</th><th>未计价</th></tr></thead><tbody>${g.map(x=>`<tr><td>${esc(x.profileId)}<span class="secondary">${esc(refName('providerAccounts',x.accountId))}</span></td><td>${num(x.requests)}</td><td>${x.promptTokens==null?'—':num(x.promptTokens)} / ${x.completionTokens==null?'—':num(x.completionTokens)}</td><td>${money(x.estimatedCost,x.currency)}</td><td>${num(x.unknownCostRequests)}</td></tr>`).join('')}</tbody></table></div>`)+panel('最近 100 次调用',`<div class="table-wrap"><table><thead><tr><th>时间</th><th>调用方</th><th>模型 / 来源</th><th>Token</th><th>估算</th><th>状态</th></tr></thead><tbody>${recent.map(x=>`<tr><td>${esc(date(x.created))}</td><td>${esc(x.caller)}</td><td>${esc(x.profile_id)}<span class="secondary">${esc(({gateway:'模型网关',agent:'Agent 执行'})[x.source]||x.source)}</span></td><td>${x.total_tokens==null?'未报告':num(x.total_tokens)}</td><td>${money(x.estimated_cost,x.currency)}</td><td>${esc(({completed:'完成',failed:'失败',disconnected:'连接中断'})[x.status]||x.status)}</td></tr>`).join('')}</tbody></table></div>`);}
  const pages={providers:providersPage,models:modelsPage,apps:appsPage,tools:toolsPage,usage:usagePage};
  function render(page){if(!pages[page])return false;$('content').innerHTML=pages[page]();if(['tools','models'].includes(page))filterResources();return true;}
  function editor(kind,id,defaults={}){
    version++;closeDetail();const existing=arr(data[kind]).find(x=>(x.id||x.name)===id);const item=existing||defaults;selection={kind,id,existing:!!existing,revision:data.revision};state.selected={kind:'registry',id:id||'new'};state.dirty=false;
    let fields=sectionFields('基本信息',input('name','名称',item.name||id||'')+input('id','ID',id||item.id||kind.replace(/s$/,'')+'-'+crypto.randomUUID().slice(0,8),'text',existing?'创建后不可修改':'')+bool('enabled','启用',item.enabled!==false));
    if(kind==='providers'){
      const accounts=arr(data.providerAccounts).filter(a=>a.providerId===id),keys=arr(data.providerKeys).filter(k=>accounts.some(a=>a.id===k.accountId));
      fields+=sectionFields('连接配置',(!existing?select('_preset','供应商模板','',[{id:'siliconflow',name:'SiliconFlow'},{id:'deepseek',name:'DeepSeek'},{id:'openai',name:'OpenAI'},{id:'custom',name:'自定义'}]):'')+select('providerType','API 类型',item.providerType||'OPENAI_COMPATIBLE',protocols)+input('baseUrl','Base URL',item.baseUrl||item.endpoint||'')+(keys.length?select('_credentialId','API Key 记录',keys[0].id,[{id:'',name:'新建 API Key'},...keys]):'')+credentialField(keys[0]||{}));
    }
    if(kind==='providerAccounts') fields+=select('providerId','供应商',item.providerId,data.providers||[])+`<details><summary>供应商账单配置</summary><div class="detail-inner">${select('billing.adapter','账单适配器',item.billing?.adapter||'',[{id:'openai-organization',name:'OpenAI Organization'}])}${input('billing.apiKeyEnv','账单管理密钥环境变量','','text','账单接口通常需要组织管理员凭证；留空保留既有引用。')}${input('billing.apiKeyFile','账单密钥文件引用')}</div></details>`+(existing?'<button type="button" class="button" data-billing-query>查询最近 7 天账单</button>':'');
    if(kind==='providerKeys')fields+=sectionFields('凭据',select('accountId','账号',item.accountId,data.providerAccounts||[])+credentialField(item));
    if(kind==='models'){
      const linked=!!item.keyId||(!existing&&arr(data.providerKeys).length>0),key=arr(data.providerKeys).find(k=>k.id===item.keyId);
      fields+=sectionFields('模型连接',input('model','Model ID',item.model||'')+select('_connectionMode','连接方式',linked?'provider':'direct',[{id:'provider',name:'供应商 API Key'},{id:'direct',name:'独立接口'}])+`<div id="model-provider-fields" class="full-field">${select('keyId','API Key 记录',item.keyId,arr(data.providerKeys).map(k=>({...k,name:refName('providerAccounts',k.accountId)+' / '+(k.name||k.id)})))}</div><div id="model-direct-fields" class="full-field editor-grid">${input('baseUrl','Base URL',item.baseUrl||'')}${select('providerType','API 类型',item.providerType||'OPENAI_COMPATIBLE',protocols)}</div>`+credentialField(key||item));
      fields+=sectionFields('模型能力',bool('capabilities.vision','Vision',item.capabilities?.vision===true)+bool('capabilities.tools','Tool Calling',item.capabilities?.tools===true)+bool('capabilities.reasoning','Reasoning',item.capabilities?.reasoning===true)+input('contextWindow','Context Window',item.contextWindow,'number')+input('maxOutputTokens','Max Output Tokens',item.maxOutputTokens,'number'))+`<details class="config-group"><summary>价格</summary><div class="detail-inner editor-grid">${input('pricing.inputPerMillion','输入价格 / 1M Tokens',item.pricing?.inputPerMillion,'number')}${input('pricing.outputPerMillion','输出价格 / 1M Tokens',item.pricing?.outputPerMillion,'number')}${input('pricing.currency','Currency',item.pricing?.currency||'')}</div></details>`;
    }
    fields+=capabilities.fields(kind,item,!!existing);
    if(kind==='models'&&existing)fields+=`<section class="editor-section"><h3>连接验证</h3><div class="probe-actions"><button type="button" class="button" data-run-probe="text">测试文本</button><button type="button" class="button" data-run-probe="tools">测试工具调用</button></div><p class="probe-result" role="status" aria-live="polite"></p></section>`;
    $('inspector').innerHTML=detailHeading(names[kind],existing?(item.name||id):`新增${names[kind]}`,existing?id:'')+`<div class="detail-body"><form id="registry-form" method="post">${fields}<p id="registry-error" class="form-error" role="alert" hidden></p><div id="registry-result"></div></form></div><div class="detail-footer"><button class="button" data-close-detail>取消</button><button class="button primary" type="submit" form="registry-form" ${data.writable===false?'disabled':''}>${existing?'保存更改':'创建'+names[kind]}</button></div>`;
    if(existing)$('reg-id').readOnly=true;
    $('reg-name').required=kind!=='httpTools';
    if(kind==='providers')$('reg-baseUrl').required=true;
    if(kind==='providers')$('reg-baseUrl').closest('.field').insertAdjacentHTML('afterend',input('proxyUrl','HTTP 代理地址',item.proxyUrl||'','url','留空直连；Docker 中访问本机代理，例如 http://host.docker.internal:10808'));
    if($('reg-baseUrl')){$('reg-baseUrl').placeholder='https://api.example.com/v1';$('reg-baseUrl').insertAdjacentHTML('afterend','<small class="field-hint">Chat Completions API 基础地址，例如 https://api.example.com/v1</small>');}
    if(kind==='providerKeys')$('reg-accountId').required=true;
    if(kind==='models'){$('reg-model').required=true;syncModelConnection();}
    if(kind==='httpTools'){$('reg-name').closest('.field').hidden=true;$('reg-id').required=true;}
    if(['mcpServers','httpTools','skills','plugins'].includes(kind)){$('reg-id').pattern=kind==='httpTools'?'[A-Za-z0-9_-]{1,64}':'[A-Za-z0-9_-]{1,48}';}
    capabilities.setup(kind,item);
    revealDetail();
    requestAnimationFrame(()=>{if(document.activeElement===$('inspector'))($('reg-name').closest('.field').hidden?$('reg-id'):$('reg-name')).focus({preventScroll:true});});
  }
  function syncModelConnection(){
    if(!$('model-provider-fields'))return;
    const linked=$('reg-_connectionMode').value==='provider';
    for(const [id,visible] of [['model-provider-fields',linked],['model-direct-fields',!linked]]){
      const host=$(id);host.hidden=!visible;host.querySelectorAll('input,select').forEach(el=>el.disabled=!visible);
    }
    $('reg-keyId').required=linked;$('reg-baseUrl').required=!linked;
    syncCredentialState();
  }
  function syncCredentialState(){
    const host=$('registry-form')?.querySelector('.credential-state');if(!host)return;
    let entry=arr(data[selection.kind]).find(row=>(row.id||row.name)===selection.id)||{};
    let shared='';
    if(selection.kind==='providers')entry=arr(data.providerKeys).find(k=>k.id===$('reg-_credentialId')?.value)||{};
    if(selection.kind==='models'&&$('reg-_connectionMode').value==='provider'){
      entry=arr(data.providerKeys).find(k=>k.id===$('reg-keyId').value)||{};
      const count=arr(data.models).filter(m=>m.keyId===entry.id).length;
      if(count)shared=`共享 API Key：${count} 个模型正在引用。修改密钥会同步生效。`;
    }
    host.innerHTML=badge(entry.credentialConfigured?'已配置':'未配置',entry.credentialConfigured?'good':'')+`<span>${esc(shared||'保存后不回显凭据')}</span>`;
  }
  function parseForm(form){const f=new FormData(form),kind=selection.kind,result={enabled:f.has('enabled')};const id=String(f.get('id')||'').trim();if(!id)throw new Error('请填写稳定标识');
    const set=(key,val)=>{const [p,c]=key.split('.');if(c)(result[p]??={})[c]=val;else result[p]=val;};
    for(const el of form.querySelectorAll('input,select,textarea')){const key=el.name;if(el.disabled||key?.startsWith('_')||!key||key==='id'||key==='enabled'||key==='secret'||key==='_preset'||el.type==='checkbox')continue;const val=el.value.trim();if(kind==='httpTools'&&key==='name')continue;
      if(['apiKeyEnv','apiKeyFile','tokenEnv','tokenFile','billing.apiKeyEnv','billing.apiKeyFile'].includes(key)&&!val)continue;
      if(['keyId','url','contextWindow','maxOutputTokens','billing.adapter'].includes(key)&&!val)continue;
      if(key.startsWith('pricing.')&&!val)continue;
      if(key==='outputSchema'&&!val){result.outputSchema={};continue;}
      if(key==='domains')set(key,val.split(',').map(v=>v.trim()).filter(Boolean));else if(key==='tools'&&kind==='mcpServers'||['inputSchema','outputSchema'].includes(key))set(key,JSON.parse(val));else set(key,el.type==='number'?Number(val):val);
    }
    if(kind==='models'){result.capabilities={vision:f.has('capabilities.vision'),tools:f.has('capabilities.tools'),reasoning:f.has('capabilities.reasoning')};result.pricing??={};if(result.pricing.currency)result.pricing.currency=result.pricing.currency.toUpperCase();}
    if(kind==='services')result.ragViaMcp=f.has('ragViaMcp');
    if(kind==='services')for(const key of ['modelProfiles','tools','mcpServers','mcpTools','skills','plugins'])result[key]=f.getAll(key);
    if(kind==='httpTools')result.version='1';
    capabilities.read(kind,form,result);
    if(kind==='mcpServers'&&result.transport!=='streamable-http'){delete result.url;delete result.tokenEnv;delete result.tokenFile;}
    if(kind==='models'&&f.get('_connectionMode')==='direct')result.keyId='';
    if(!selection.existing&&arr(data[kind]).some(x=>(x.id||x.name)===id))throw new Error('ID 已存在，请使用其他 ID。');
    return {revision:selection.revision,kind,id,changes:result};
  }
  function showDiscovery(result,keyId){discovery=result;selection=null;closeDetail();state.selected={kind:'registry',id:'discovery'};version++;
    const models=arr(result.models);$('inspector').innerHTML=detailHeading('上游发现',`${models.length} 个模型`,refName('providerKeys',keyId))+`<div class="detail-body"><p class="detail-subtitle">${help('供应商没有报告的上下文、价格或视觉能力保持未知。加入目录后仍需要在应用页授权。')} 选择模型以配置别名和价格。</p><input class="catalog-search" aria-label="搜索发现的模型" placeholder="搜索模型" data-catalog-search><div class="discovery-list">${models.map((m,i)=>`<button class="discovery-item catalog-item" data-import-model="${i}"><strong>${esc(m.model||m.id)}</strong><small>${m.contextWindow?num(m.contextWindow)+' 上下文':'上下文未报告'} · ${m.capabilities?.vision?'视觉':'视觉能力待确认'}</small><span class="import-label">导入配置</span></button>`).join('')||empty('未发现模型','检查供应商 /models 接口。')}</div></div>`;revealDetail();}
  function showPricing(r){closeDetail();selection=null;version++;state.selected={kind:'registry',id:'pricing'};
    const rows=Object.entries(r.models||{});$('inspector').innerHTML=detailHeading('供应商公开价格','价格参考',r.provider||'')+`<div class="detail-body"><p class="detail-subtitle">${esc({available:'官方参考价格，需在模型配置中填写适用单价。',stale:'官网暂不可用，显示上次缓存价格。',unavailable:'暂未取得官网价格，请稍后重试。',unsupported:'此供应商尚无公开价格适配。'}[r.status]||'价格参考')}</p><dl class="properties"><dt>状态</dt><dd>${esc({available:'已获取',stale:'缓存已过期',unavailable:'获取失败',unsupported:'暂无适配'}[r.status]||r.status)}</dd><dt>来源</dt><dd>${r.sourceUrl?`<a href="${esc(r.sourceUrl)}" target="_blank" rel="noopener noreferrer">供应商官方价格页 ↗</a>`:'—'}</dd><dt>获取时间</dt><dd>${esc(date(r.fetchedAt))}</dd></dl><input class="catalog-search" aria-label="搜索价格模型" placeholder="搜索模型" data-catalog-search>${rows.map(([model,prices])=>`<details class="catalog-item"><summary>${esc(model)}</summary><div class="detail-inner">${arr(prices).map(p=>`<p class="price-line">${esc(({input:'输入',output:'输出',cached_input:'缓存输入'})[p.kind]||p.kind)} · ${esc(r.currency||'')} ${esc(p.amount)} / ${esc(p.unit==='million_tokens'?'百万 Token':p.unit)}${p.condition?`<small>${esc(p.condition)}</small>`:''}${p.scenario?`<small>${esc(p.scenario)}</small>`:''}</p>`).join('')}</div></details>`).join('')}</div>`;revealDetail();
  }
  const managementError=ex=>({CONFIGURATION_ITEM_REFERENCED:'此资源仍被其他配置或调用方引用，请先解除关联或撤回授权。',INVALID_CONFIGURATION_CHANGE:'配置格式不正确，请检查必填项、标识和数值范围。',INVALID_MCP_TOOL:'请检查工具标识、说明与操作类型。',UNKNOWN_CAPABILITY_GRANT:'应用仍在引用已移除的能力，请先调整应用授权。',INVALID_SKILL_INSTRUCTIONS:'技能指令不能为空，且最多 16000 字符。',INVALID_MCP_HTTP_BRIDGE:'桥接目标必须存在，且操作类型与 HTTP 工具一致。',INVALID_MCP_SCHEMA:'工具输入参数必须是有效的对象 JSON Schema。',INVALID_MCP_RAG_BRIDGE:'RAG 桥接仅允许 recall、list_documents 只读工具。',ACTIVE_RUNS_CONFIGURATION_LOCKED:'任务执行中，请等待完成后保存。',ACTIVE_GATEWAY_CONFIGURATION_LOCKED:'模型调用中，请等待完成后保存。',CONFIGURATION_CONFLICT:'配置已更新，请刷新后重新编辑。',INVALID_CREDENTIAL_REFERENCE:'请填写有效的 API Key 或密钥引用。',INVALID_PROVIDER_SECRET:'API Key 格式不正确。',INVALID_MODEL_PRICING:'请填写输入、输出单价及三位币种，例如 CNY。'}[ex.code]||ex.message);
  function deletion(kind,id){const item=arr(data[kind]).find(x=>(x.id||x.name)===id);if(!item)return;closeDetail();version++;selection={kind,id};state.selected={kind:'registry',id};$('inspector').innerHTML=detailHeading('删除配置',item.name||id,id)+`<div class="detail-body"><p>确认从 Core 中删除此${names[kind]}？</p><p class="detail-subtitle">仍被引用的资源会被阻止删除。</p><p id="delete-error" class="form-error" hidden role="alert"></p></div><div class="detail-footer"><button class="button" data-close-detail>取消</button><button class="button danger" data-confirm-delete>确认删除</button></div>`;revealDetail();}
  function startImport(){closeDetail();version++;selection=null;state.selected={kind:'registry',id:'import'};$('inspector').innerHTML=detailHeading('导入模型','选择供应商密钥')+`<div class="detail-body"><form id="model-import-form">${select('keyId','API Key','',arr(data.providerKeys).map(k=>({...k,name:refName('providerAccounts',k.accountId)+' / '+(k.name||k.id)})))}<p id="import-error" class="form-error" hidden role="alert"></p></form></div><div class="detail-footer"><button class="button" data-close-detail>取消</button><button class="button primary" form="model-import-form" type="submit">发现模型</button></div>`;$('reg-keyId').required=true;revealDetail();}
  document.addEventListener('change',e=>{if(e.target.name!=='_preset')return;const presets={siliconflow:['SiliconFlow','https://api.siliconflow.cn/v1','SILICONFLOW'],deepseek:['DeepSeek','https://api.deepseek.com/v1','DEEPSEEK'],openai:['OpenAI','https://api.openai.com/v1','OPENAI']};const preset=presets[e.target.value];if(preset){$('reg-name').value=preset[0];$('reg-baseUrl').value=preset[1];$('reg-providerType').value=preset[2];}state.dirty=true;});
  document.addEventListener('submit',async e=>{if(e.target.getAttribute('id')!=='model-import-form')return;e.preventDefault();const key=new FormData(e.target).get('keyId'),v=version,b=$('inspector').querySelector('[type=submit]');b.disabled=true;try{const r=await api('/providers/'+encodeURIComponent(key)+'/discover',{method:'POST',body:'{}'});if(v===version)showDiscovery(r,key);}catch(ex){if(v===version){$('import-error').textContent=managementError(ex);$('import-error').hidden=false;b.disabled=false;}}});
  document.addEventListener('input',e=>{if(e.target.closest('#registry-form'))state.dirty=true;if(e.target.hasAttribute('data-catalog-search')){const q=e.target.value.toLowerCase();$('inspector').querySelectorAll('.catalog-item').forEach(el=>el.hidden=!el.textContent.toLowerCase().includes(q));}});
  document.addEventListener('submit',async e=>{
    if(e.target.getAttribute('id')==='usage-filter'){e.preventDefault();usageFilter=Object.fromEntries(new FormData(e.target));try{usage=await api(usagePath());render('usage');}catch(ex){showError(ex);}return;}
    if(e.target.getAttribute('id')!=='registry-form')return;e.preventDefault();
    const current=version,button=$('inspector').querySelector('[type=submit]'),err=$('registry-error');button.disabled=true;err.hidden=true;
    try{
      const payload=parseForm(e.target),form=new FormData(e.target),secret=String(form.get('secret')||'').trim();
      if(secret){
        const credentialId=payload.kind==='providers'?String(form.get('_credentialId')||''):'';
        data=await api('/connections',{method:'POST',body:JSON.stringify({...payload,apiKey:secret,...(credentialId?{credentialId}:{})})});
      }else{
        if(payload.kind==='providerKeys'&&!selection.existing)throw new Error('请填写 API Key。');
        if(payload.kind==='models'&&form.get('_connectionMode')==='direct'&&(!selection.existing||!arr(data.models).find(m=>m.id===payload.id)?.credentialConfigured))throw new Error('独立接口需要填写 API Key。');
        data=await api('/registry',{method:'PATCH',body:JSON.stringify(payload)});
      }
      if(current!==version)return;state.dirty=false;editor(payload.kind,payload.id);render(state.page);toast('配置已保存。');
    }catch(ex){if(current===version&&err.isConnected){err.textContent=managementError(ex);err.hidden=false;button.disabled=false;}}
  });
  document.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;try{
    if(b.dataset.modelTest){if(state.dirty){toast('请先保存当前配置。');return;}editor('models',b.dataset.modelTest);$('inspector').querySelector('[data-run-probe="text"]').click();return;}
    if(b.dataset.runProbe){
      if(state.dirty){toast('请先保存更改，再测试连接。');return;}
      const v=version,revision=data.revision,result=$('inspector').querySelector('.probe-result'),buttons=[...$('inspector').querySelectorAll('[data-run-probe]')];
      buttons.forEach(button=>button.disabled=true);result.textContent='正在验证…';result.dataset.status='running';result.scrollIntoView({block:'center'});
      try{
        const r=await api('/models/'+encodeURIComponent(selection.id)+'/test',{method:'POST',body:JSON.stringify({revision,mode:b.dataset.runProbe})});
        if(v!==version)return;
        const labels={INVALID_PROVIDER_ENDPOINT:'接口返回网页，请检查 Base URL 是否包含 /v1。',INVALID_RESPONSE_FORMAT:'接口未返回 SSE，请检查 Chat Completions 协议。',AUTHENTICATION:'认证失败，请检查 API Key 和模型权限。',CONNECT_TIMEOUT:'连接超时，请检查网络和接口地址。',TIMEOUT:'模型响应超时。',CONNECTION:'无法连接模型接口。',RATE_LIMIT:'供应商限流或额度不足。',MODEL_TOOL_TEST_FAILED:'未返回有效工具调用，请核对模型能力。',INCOMPLETE_RESPONSE:'模型流提前中断。',EMPTY_RESPONSE:'模型未返回正文。',UPSTREAM_STREAM_ERROR:'供应商在流中返回错误。'};
        result.dataset.status=r.status;
        result.textContent=r.stale?'测试期间配置发生变化，请重新验证。':`${r.status==='succeeded'?(r.mode==='tools'?'工具调用验证通过':'文本连接验证通过'):(labels[r.code]||'连接验证失败：'+r.code)} · ${(r.elapsedMs/1000).toFixed(1)} 秒${r.httpStatus?' · HTTP '+r.httpStatus:''}`;
      }catch(ex){if(v===version){result.dataset.status='failed';result.textContent=managementError(ex);}}
      finally{buttons.forEach(button=>button.disabled=false);}
      return;
    }
    if(b.dataset.toggleSecret){const input=$(b.dataset.toggleSecret),show=input.type==='password';input.type=show?'text':'password';b.innerHTML=icon(show?'eyeOff':'eye');b.title=show?'隐藏输入内容':'显示输入内容';b.setAttribute('aria-label',b.title);return;}
    if(b.dataset.providerKey){editor('providers',b.dataset.providerKey);$('reg-secret').focus();return;}
    if(b.dataset.newKeyModel){editor('models',null,{keyId:b.dataset.newKeyModel});return;}
    if(b.hasAttribute('data-start-import')){startImport();return;}
    if(b.dataset.regDelete){deletion(b.dataset.regDelete,b.dataset.regId);return;}
    if(b.hasAttribute('data-confirm-delete')){b.disabled=true;const selected={...selection},v=version;try{data=await api('/registry/'+selected.kind+'/'+encodeURIComponent(selected.id),{method:'DELETE',body:JSON.stringify({revision:data.revision})});if(v===version){closeDetail();render(state.page);}toast('配置已删除。');}catch(ex){if(v===version){$('delete-error').textContent=managementError(ex);$('delete-error').hidden=false;}}return;}
    if(b.dataset.regToggle){if(state.dirty){toast('请先保存或取消当前编辑。');return;}b.disabled=true;const kind=b.dataset.regToggle,id=b.dataset.regId,item=arr(data[kind]).find(x=>(x.id||x.name)===id);data=await api('/registry',{method:'PATCH',body:JSON.stringify({revision:data.revision,kind,id,changes:{enabled:item.enabled===false}})});closeDetail();render(state.page);toast(item.enabled===false?'已启用。':'已停用。');return;}
    if(b.dataset.regKind){editor(b.dataset.regKind,b.dataset.regId);return;}
    if(b.dataset.regNew){editor(b.dataset.regNew,null,b.dataset.regNew==='providerAccounts'?{providerId:b.dataset.parent}:b.dataset.regNew==='providerKeys'?{accountId:b.dataset.parent}:{});return;}
    if(b.dataset.regTab){section=b.dataset.regTab;closeDetail();render('tools');return;}
    if(b.dataset.pricingKey){b.disabled=true;const v=version,r=await api('/providers/'+encodeURIComponent(b.dataset.pricingKey)+'/pricing',{method:'POST',body:'{}'});if(v===version)showPricing(r);return;}
    if(b.dataset.discoverKey){b.disabled=true;const v=version;const r=await api('/providers/'+encodeURIComponent(b.dataset.discoverKey)+'/discover',{method:'POST',body:'{}'});if(v===version)showDiscovery(r,b.dataset.discoverKey);return;}
    if(b.dataset.importModel!=null){const m=discovery.models[Number(b.dataset.importModel)];editor('models',null,{...m,name:m.model,id:undefined,enabled:false});$('reg-id').value=m.model.replace(/[^a-zA-Z0-9_-]/g,'-').slice(0,100);return;}
    if(b.hasAttribute('data-discover-mcp')){if(state.dirty){toast('请先保存 MCP 配置，再发现工具。');return;}b.disabled=true;const v=version,r=await api('/mcp/'+encodeURIComponent(selection.id)+'/discover',{method:'POST',body:'{}'});if(v===version)capabilities.showDiscovery(r);return;}
    if(b.hasAttribute('data-billing-query')){b.disabled=true;const end=Math.floor(Date.now()/1000),v=version,r=await api('/accounts/'+encodeURIComponent(selection.id)+'/billing',{method:'POST',body:JSON.stringify({start:end-7*86400,end})});if(v===version)$('registry-result').innerHTML=`<details open><summary>供应商账单</summary><pre>${esc(JSON.stringify(r,null,2))}</pre></details>`;return;}
    if(b.hasAttribute('data-usage-export')){const rows=[['time','caller','model','source','tokens','estimated_cost','currency','status'],...arr(usage.recent).map(x=>[date(x.created),x.caller,x.profile_id,x.source,x.total_tokens,x.estimated_cost,x.currency,x.status])];const csv='\ufeff'+rows.map(row=>row.map(v=>'"'+String(v??'').replace(/^[=+@-]/,"'").replace(/"/g,'""')+'"').join(',')).join('\r\n');const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='core-usage-recent.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  }catch(ex){ex.message=managementError(ex);showError(ex);}finally{if(b.isConnected)b.disabled=false;}});
  function filterResources(){const q=document.querySelector('[data-resource-search]')?.value.toLowerCase()||'',status=document.querySelector('[data-resource-status]')?.value||'';let count=0;document.querySelectorAll('[data-resource-row]').forEach(row=>{row.hidden=!row.dataset.search.includes(q)||(status&&row.dataset.enabled!==status);if(!row.hidden)count++;});const empty=document.querySelector('[data-resource-empty]');if(empty)empty.hidden=count>0;}
  document.addEventListener('input',e=>{if(e.target.hasAttribute('data-resource-search')){resourceQuery=e.target.value;filterResources();}});
  document.addEventListener('change',e=>{if(e.target.hasAttribute('data-resource-status')){resourceStatus=e.target.value;filterResources();}});
  document.addEventListener('change',e=>{if(e.target.name==='_connectionMode')syncModelConnection();if(['_credentialId','keyId'].includes(e.target.name))syncCredentialState();});
  document.addEventListener('invalid',e=>{if(e.target.closest('#registry-form')){let parent=e.target.parentElement;while(parent){if(parent.tagName==='DETAILS')parent.open=true;parent=parent.parentElement;}}},true);
  document.addEventListener('click',e=>{if(!state.dirty)return;const target=e.target.closest('[data-page],[data-reg-kind],[data-reg-new],[data-reg-tab],[data-reg-delete],[data-start-import],[data-provider-key],[data-new-key-model],[data-object],.brand');if(target){e.preventDefault();e.stopImmediatePropagation();toast('有未保存的更改，请先保存或点击取消。');}},true);
  document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'&&$('registry-form')){e.preventDefault();$('registry-form').requestSubmit();}});
  return {render,edit:editor,usagePath,setData(registry,costs){data=registry;usage=costs||usage;},clear(){data={};usage={};usageFilter={};selection=null;discovery=null;providerTree.clear();version++;},invalidate(){version++;}};
};
