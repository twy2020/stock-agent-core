"use strict";
// Shared by the console and its policy/merge regression tests. No network or state mutation.
(() => {
  const list=x=>Array.isArray(x)?x:[], effects=['read','analyze','compute','propose','write'];
  function mergeTools(existing, selected, transport, httpTools){
    const merged=list(existing).map(t=>structuredClone(t));
    for(const source of selected){
      if(!/^[A-Za-z0-9_-]{1,48}$/.test(source.name||''))throw new Error('工具标识只允许字母、数字、下划线和连字符，最多 48 位。');
      if(!effects.includes(source.effect))throw new Error('请为选中的工具指定操作类型。');
      const index=merged.findIndex(t=>t.name===source.name);
      const item={name:source.name,description:source.description||'',inputSchema:structuredClone(source.inputSchema||{type:'object'}),effect:source.effect,enabled:index<0?false:merged[index].enabled!==false};
      if(transport==='http-bridge'){
        const target=list(httpTools).find(t=>t.name===source.httpTool);
        if(!target||target.effect!==source.effect)throw new Error('桥接工具与 HTTP 工具的操作类型必须一致。');
        item.httpTool=source.httpTool;
      }
      if(transport==='rag-bridge'&&(!['recall','list_documents'].includes(item.name)||item.effect!=='read'))throw new Error('RAG 桥接仅支持 recall 和 list_documents 只读工具。');
      if(index<0)merged.push(item);else merged[index]=item;
    }
    return merged;
  }
  function policyIssues(service,data){
    const issues=[],find=(kind,id)=>list(data[kind]).find(x=>(x.id||x.name)===id),granted=(kind,id)=>list(service[kind]).includes(id);
    const label=x=>x?.name||x?.id||'未知对象';
    function plugin(entry){if(!entry?.pluginId)return;const p=find('plugins',entry.pluginId);if(!granted('plugins',entry.pluginId))issues.push(`${label(entry)}：尚未授权插件 ${label(p)||entry.pluginId}`);if(p?.enabled===false)issues.push(`插件 ${label(p)} 已停用。`);}
    function available(entry,type){if(!entry)return;if(entry.enabled===false)issues.push(`${type} ${label(entry)} 已停用。`);if(entry.domains&&!list(service.domains).some(d=>entry.domains.includes(d)))issues.push(`${type} ${label(entry)} 与应用能力域不匹配。`);plugin(entry);}
    if(service.defaultModelProfile&&!granted('modelProfiles',service.defaultModelProfile))issues.push('默认模型不在允许模型中。');
    for(const id of list(service.modelProfiles)){
      const m=find('models',id);available(m,'模型');
      if(m?.keyId){const k=find('providerKeys',m.keyId),a=find('providerAccounts',k?.accountId),p=find('providers',a?.providerId);if([k,a,p].some(x=>!x||x.enabled===false))issues.push(`模型 ${label(m)} 的供应商、账号或 API Key 未启用。`);}
    }
    for(const [kind,type] of [['httpTools','HTTP 工具'],['skills','Skill'],['mcpServers','MCP 服务'],['plugins','插件']]){
      for(const id of list(service[kind==='httpTools'?'tools':kind]))available(find(kind,id),type);
    }
    for(const id of list(service.mcpTools)){
      const server=list(data.mcpServers).find(s=>list(s.tools).some(t=>`mcp__${s.id}__${t.name}`===id));
      if(!server){issues.push(`MCP 工具 ${id} 已不存在。`);continue;}
      const tool=server.tools.find(t=>`mcp__${server.id}__${t.name}`===id);
      if(!granted('mcpServers',server.id))issues.push(`${tool.name}：尚未授权 MCP 服务 ${label(server)}。`);
      available(server,'MCP 服务');available(tool,'MCP 工具');
      if(server.transport==='http-bridge'){
        if(!granted('tools',tool.httpTool))issues.push(`${tool.name}：尚未授权 HTTP 工具 ${tool.httpTool}。`);
        available(find('httpTools',tool.httpTool),'HTTP 工具');
      }
    }
    if(service.visionPolicy==='direct'&&list(service.modelProfiles).length&&!list(service.modelProfiles).some(id=>find('models',id)?.capabilities?.vision))issues.push('只允许直连视觉，但没有获准的视觉模型。');
    return [...new Set(issues)];
  }
  const api={mergeTools,policyIssues};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  else window.CoreCapabilityData=api;
})();
