"use strict";
window.CoreProviderTree=function(ctx){
  const {esc,icon,heading,empty,enabledBadge,getData}=ctx;
  const arr=x=>Array.isArray(x)?x:[];
  const collapsed=new Set();let query='';
  const names={providers:'供应商',providerAccounts:'账号',providerKeys:'API Key',models:'模型'};
  const control=(symbol,title,attrs)=>`<button type="button" class="icon-button" title="${esc(title)}" aria-label="${esc(title)}" ${attrs}>${icon(symbol)}</button>`;
  function nodes(){
    const data=getData(),models=arr(data.models);
    const roots=arr(data.providers).map(p=>({kind:'providers',item:p,children:arr(data.providerAccounts).filter(a=>a.providerId===p.id).map(a=>({kind:'providerAccounts',item:a,children:arr(data.providerKeys).filter(k=>k.accountId===a.id).map(k=>({kind:'providerKeys',item:k,children:models.filter(m=>m.keyId===k.id).map(m=>({kind:'models',item:m,children:[]}))}))}))}));
    const standalone=models.filter(m=>!m.keyId);
    if(standalone.length)roots.push({kind:'standalone',item:{id:'standalone',name:'独立模型',enabled:true},children:standalone.map(m=>({kind:'models',item:m,children:[]}))});
    return roots;
  }
  function render(){
    const roots=nodes(),data=getData(),term=query.trim().toLowerCase();
    const matches=node=>[node.item.id,node.item.name,node.item.baseUrl,node.item.model,node.item.providerType].join(' ').toLowerCase().includes(term);
    const includes=node=>matches(node)||node.children.some(includes);
    function rows(node,level=1,parentMatch=false){
      if(term&&!parentMatch&&!includes(node))return '';
      const {kind,item,children}=node,key=kind+':'+item.id,open=term||!collapsed.has(key),virtual=kind==='standalone';
      const connected=kind==='providers'?(item.baseUrl||item.endpoint||''):kind==='models'?item.model:kind==='providerKeys'?(item.credentialConfigured?'已配置':'未配置'):`${children.length} ${kind==='providerAccounts'?'个 API Key':'个模型'}`;
      const symbol={providers:'providers',providerAccounts:'apps',providerKeys:'key',models:'models',standalone:'folder'}[kind];
      const label=item.name||item.id;
      let actions=virtual?'':control('edit','编辑'+label,`data-reg-kind="${kind}" data-reg-id="${esc(item.id)}"`);
      if(kind==='providers')actions+=control('key','配置'+label+' API Key',`data-provider-key="${esc(item.id)}"`)+control('add','新增'+label+'账号',`data-reg-new="providerAccounts" data-parent="${esc(item.id)}"`);
      if(kind==='providerAccounts')actions+=control('add','新增'+label+' API Key',`data-reg-new="providerKeys" data-parent="${esc(item.id)}"`);
      if(kind==='providerKeys')actions+=control('search','发现'+label+'模型',`data-discover-key="${esc(item.id)}"`)+control('add','新增'+label+'模型',`data-new-key-model="${esc(item.id)}"`);
      if(!virtual)actions+=`<details class="tree-menu"><summary aria-label="${esc(label)}更多操作" title="更多操作">${icon('more')}</summary><div><button type="button" data-reg-toggle="${kind}" data-reg-id="${esc(item.id)}">${item.enabled===false?'启用':'停用'}</button>${kind==='providerKeys'?`<button type="button" data-pricing-key="${esc(item.id)}">价格参考</button>`:''}<button type="button" class="danger" data-reg-delete="${kind}" data-reg-id="${esc(item.id)}">删除</button></div></details>`;
      const row=`<tr role="row" aria-level="${level}" ${children.length?`aria-expanded="${!!open}"`:''} data-provider-node="${esc(key)}">
        <td role="gridcell"><div class="tree-name">${children.length?control('chevron',open?'收起'+label:'展开'+label,`data-tree-toggle="${esc(key)}" aria-expanded="${!!open}"`):'<span class="tree-spacer"></span>'}${icon(symbol)}${virtual?`<strong>${esc(label)}</strong>`:`<button class="tree-label" data-reg-kind="${kind}" data-reg-id="${esc(item.id)}"><strong>${esc(label)}</strong><small>${esc(item.id)}</small></button>`}<span class="tree-count">${children.length||''}</span></div></td>
        <td role="gridcell" class="tree-type">${names[kind]||'独立连接'}</td><td role="gridcell" class="tree-connection mono" title="${esc(connected)}">${esc(connected)}</td><td role="gridcell">${virtual?'':enabledBadge(item.enabled)}</td><td role="gridcell"><div class="tree-actions">${actions}</div></td></tr>`;
      return row+(open?children.map(child=>rows(child,level+1,parentMatch||(term&&matches(node)))).join(''):'');
    }
    const body=roots.map(root=>rows(root)).join('');
    return heading('供应商管理','',`<button class="button primary" data-reg-new="providers">${icon('add')}新增供应商</button>`)+`<div class="provider-toolbar"><label class="search-field">${icon('search')}<input data-provider-search aria-label="搜索供应商、账号、API Key 或模型" placeholder="搜索供应商、账号、API Key 或模型" value="${esc(query)}"></label><span class="muted">${arr(data.providers).length} 供应商 · ${arr(data.providerKeys).length} API Key · ${arr(data.models).length} 模型</span><div>${control('expand','展开全部','data-tree-expand')}${control('collapse','收起全部','data-tree-collapse')}</div></div><div class="provider-tree-wrap"><table class="provider-tree" role="treegrid" aria-label="供应商、账号、API Key 和模型"><thead><tr><th>名称 / ID</th><th>类型</th><th>连接 / Model ID</th><th>状态</th><th>操作</th></tr></thead><tbody>${body||`<tr><td colspan="5">${empty(term?'没有匹配的配置':'暂无供应商',term?'':'')}</td></tr>`}</tbody></table></div>`;
  }
  function refresh(){
    const active=document.activeElement,key=active?.closest('[data-provider-node]')?.dataset.providerNode,toggle=!!active?.dataset.treeToggle;
    document.getElementById('content').innerHTML=render();
    if(key)document.querySelector(`[data-provider-node="${CSS.escape(key)}"] ${toggle?'[data-tree-toggle]':'.tree-label'}`)?.focus({preventScroll:true});
  }
  document.addEventListener('click',event=>{
    const button=event.target.closest('button');if(!button)return;
    if(button.dataset.treeToggle){const key=button.dataset.treeToggle;collapsed.has(key)?collapsed.delete(key):collapsed.add(key);refresh();}
    if(button.hasAttribute('data-tree-expand')){collapsed.clear();refresh();}
    if(button.hasAttribute('data-tree-collapse')){function collapse(node){collapsed.add(node.kind+':'+node.item.id);node.children.forEach(collapse);}nodes().forEach(collapse);refresh();}
  });
  document.addEventListener('input',event=>{
    if(!event.target.hasAttribute('data-provider-search'))return;
    const start=event.target.selectionStart,end=event.target.selectionEnd;query=event.target.value;refresh();
    const input=document.querySelector('[data-provider-search]');input.focus();input.setSelectionRange(start,end);
  });
  document.addEventListener('keydown',event=>{
    const row=event.target.closest('[data-provider-node]');if(!row||!['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(event.key))return;
    if(!event.target.matches('.tree-label,[data-tree-toggle]'))return;
    event.preventDefault();const rows=[...document.querySelectorAll('[data-provider-node]')],index=rows.indexOf(row),level=Number(row.getAttribute('aria-level'));
    let target;
    if(event.key==='ArrowUp')target=rows[index-1];
    if(event.key==='ArrowDown')target=rows[index+1];
    if(event.key==='ArrowRight'){
      if(row.getAttribute('aria-expanded')==='false'){collapsed.delete(row.dataset.providerNode);refresh();return;}
      if(Number(rows[index+1]?.getAttribute('aria-level'))>level)target=rows[index+1];
    }
    if(event.key==='ArrowLeft'){
      if(row.getAttribute('aria-expanded')==='true'){collapsed.add(row.dataset.providerNode);refresh();return;}
      target=rows.slice(0,index).reverse().find(r=>Number(r.getAttribute('aria-level'))<level);
    }
    (target?.querySelector('.tree-label')||target?.querySelector('[data-tree-toggle]'))?.focus();
  });
  return {render,clear(){query='';collapsed.clear();}};
};
