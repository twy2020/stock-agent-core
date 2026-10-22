const test=require('node:test');
const assert=require('node:assert/strict');
const {mergeTools,policyIssues}=require('../agent_core/admin_static/capability-data.js');

test('discovery requires an explicit effect and never trusts upstream grants or annotations',()=>{
  const remote={name:'extract',description:'Extract',inputSchema:{type:'object'},annotations:{readOnlyHint:true},enabled:true,httpTool:'private'};
  assert.throws(()=>mergeTools([], [remote], 'streamable-http', []), /操作类型/);
  const merged=mergeTools([], [{...remote,effect:'analyze'}], 'streamable-http', []);
  assert.deepEqual(merged,[{name:'extract',description:'Extract',inputSchema:{type:'object'},effect:'analyze',enabled:false}]);
});
test('updating discovered schema preserves the approved effect and disabled status of other tools',()=>{
  const before=[{name:'old',effect:'read',enabled:false,description:'Keep',inputSchema:{type:'object'}},
    {name:'extract',effect:'analyze',enabled:true,description:'Earlier',inputSchema:{type:'object'}}];
  const rows=mergeTools(before,[{name:'extract',effect:'analyze',description:'New',inputSchema:{type:'object',properties:{q:{type:'string'}}}}],'streamable-http',[]);
  assert.deepEqual(rows[0],before[0]);assert.equal(rows[1].enabled,true);assert.equal(rows[1].description,'New');
  assert.equal(before[1].description,'Earlier');
});
test('bridge import requires a registered target with the same effect',()=>{
  const row={name:'lookup',description:'Lookup',effect:'write',httpTool:'reader'};
  assert.throws(()=>mergeTools([],[row],'http-bridge',[{name:'reader',effect:'read'}]),/一致/);
});
test('policy diagnostics identify missing MCP service, HTTP bridge and plugin grants without changing policy',()=>{
  const data={plugins:[{id:'infra',name:'Infrastructure'}],httpTools:[{name:'ocr',pluginId:'infra',effect:'analyze',domains:['cad']}],
    mcpServers:[{id:'vision',name:'Vision',transport:'http-bridge',pluginId:'infra',domains:['cad'],tools:[{name:'analyze',httpTool:'ocr',effect:'analyze'}]}]};
  const service={domains:['cad'],mcpTools:['mcp__vision__analyze'],mcpServers:[],plugins:[],tools:[]};
  const before=JSON.stringify(service);const issues=policyIssues(service,data);
  assert.ok(issues.some(s=>s.includes('MCP 服务')));assert.ok(issues.some(s=>s.includes('HTTP 工具')));assert.ok(issues.some(s=>s.includes('插件')));
  assert.equal(JSON.stringify(service),before);
  assert.deepEqual(policyIssues({...service,mcpServers:['vision'],plugins:['infra'],tools:['ocr']},data),[]);
});
test('policy diagnostics include disabled provider ancestors and invalid default models',()=>{
  const data={providers:[{id:'p',enabled:false}],providerAccounts:[{id:'a',providerId:'p'}],providerKeys:[{id:'k',accountId:'a'}],models:[{id:'m',keyId:'k'}]};
  const issues=policyIssues({modelProfiles:['m'],defaultModelProfile:'other'},data);
  assert.ok(issues.some(s=>s.includes('供应商')));assert.ok(issues.some(s=>s.includes('默认模型')));
});
