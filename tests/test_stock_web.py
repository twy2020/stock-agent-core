import unittest
import uuid
import tempfile
from pathlib import Path
from agent_core.web import select_tools, initial_search_tool
from types import SimpleNamespace


class WebPolicyTests(unittest.TestCase):
    def test_general_news_stops_failed_web_retries_even_without_forced_search(self):
        from unittest.mock import patch,Mock
        from agent_core.agent import stream_agent
        execute=Mock(return_value={'status':'failed','error':'WEB_SEARCH_RATE_LIMITED'})
        runtime=SimpleNamespace(service={'agentType':'stock'},body={'web_enabled':True},
            tools=[{'function':{'name':'stock_web_search'}},{'function':{'name':'stock_query'}}],
            max_output_tokens=4096,check=lambda:None,cancelled=lambda:False,execute=execute)
        calls=[{'type':'tool_call','index':i,'id':str(i),'function':{'name':'stock_web_search','arguments':'{}'}} for i in range(3)]
        with patch('agent_core.agent.OpenAICompatibleClient.stream',side_effect=[
            iter(calls),iter([{'type':'delta','content':'<stock_answer>搜索被限流，现有资料无法核实最新消息。</stock_answer>'}]),
        ]) as upstream:
            events=list(stream_agent({},[{'role':'user','content':'宇树科技最近的股票情况'}],runtime=runtime))
        self.assertEqual(execute.call_count,2)
        self.assertEqual([t['function']['name'] for t in upstream.call_args_list[1].kwargs['tools']],['stock_query'])
        self.assertTrue(any(e.get('code')=='WEB_RESEARCH_BUDGET' for e in events))
    def test_current_quote_search_requires_user_opt_in_and_authorized_tool(self):
        runtime=SimpleNamespace(body={'web_enabled':True},tools=[{'function':{'name':'stock_web_search'}}])
        messages=[{'role':'user','content':'帮我查一下宇树科技股票当前价'}]
        self.assertEqual(initial_search_tool(runtime,messages),'stock_web_search')
        runtime.body['web_enabled']=False
        self.assertIsNone(initial_search_tool(runtime,messages))
        runtime.body['web_enabled']=True;runtime.tools=[]
        self.assertIsNone(initial_search_tool(runtime,messages))
        runtime.tools=[{'function':{'name':'stock_web_search'}}]
        self.assertIsNone(initial_search_tool(runtime,[{'role':'user','content':'绘制平安银行最近300天布林带'}]))

    def test_current_quote_first_round_calls_search_then_returns_to_auto(self):
        from unittest.mock import patch
        from agent_core.agent import stream_agent
        runtime=SimpleNamespace(service={'agentType':'stock'},body={'web_enabled':True},
            tools=[{'function':{'name':'stock_web_search'}}],max_output_tokens=4096,
            check=lambda:None,cancelled=lambda:False,
            execute=lambda *args:{'status':'succeeded','data':{'result':{'results':[]}}})
        with patch('agent_core.agent.OpenAICompatibleClient.stream',side_effect=[
            iter([{'type':'tool_call','index':0,'id':'search','function':{'name':'stock_web_search','arguments':'{"query":"宇树科技 股票 当前价"}'}}]),
            iter([{'type':'delta','content':'<stock_answer>未取得可核实的最新报价，不能提供实时价格。</stock_answer>'}]),
        ]) as upstream:
            list(stream_agent({},[{'role':'user','content':'宇树科技股票当前价'}],runtime=runtime))
        self.assertEqual(upstream.call_args_list[0].kwargs['tool_choice'],{'type':'function','function':{'name':'stock_web_search'}})
        self.assertEqual(upstream.call_args_list[1].kwargs['tool_choice'],'auto')
        for call in upstream.call_args_list:
            messages=call.args[1]
            self.assertEqual(sum(m['role']=='system' for m in messages),1)
            self.assertEqual(messages[0]['role'],'system')
            self.assertNotEqual(messages[-1]['role'],'system')

    def test_realtime_quote_stops_external_calls_at_four_even_in_a_batch(self):
        from unittest.mock import patch,Mock
        from agent_core.agent import stream_agent
        execute=Mock(return_value={'status':'succeeded','data':{'result':{'content':'source'}}})
        runtime=SimpleNamespace(service={'agentType':'stock'},body={'web_enabled':True},
            tools=[{'function':{'name':'stock_web_search'}}],max_output_tokens=4096,
            check=lambda:None,cancelled=lambda:False,execute=execute)
        calls=[{'type':'tool_call','index':i,'id':str(i),'function':{'name':'stock_web_search' if i==0 else 'stock_web_fetch','arguments':'{}'}} for i in range(5)]
        with patch('agent_core.agent.OpenAICompatibleClient.stream',side_effect=[
            iter(calls),iter([{'type':'delta','content':'<stock_answer>已有来源缺少行情时间，无法核实当前价格。</stock_answer>'}]),
        ]) as upstream:
            events=list(stream_agent({},[{'role':'user','content':'宇树科技股票当前价'}],runtime=runtime))
        self.assertEqual(execute.call_count,4)
        self.assertEqual(upstream.call_args_list[1].kwargs['tool_choice'],'none')
        self.assertIsNone(upstream.call_args_list[1].kwargs['tools'])
        self.assertEqual([m['role'] for m in upstream.call_args_list[1].args[1]],['system','user'])
        self.assertTrue(any(e.get('code')=='WEB_RESEARCH_BUDGET' for e in events))

    def test_successful_quote_search_is_followed_by_a_real_source_read(self):
        from unittest.mock import patch
        from agent_core.agent import stream_agent
        runtime=SimpleNamespace(service={'agentType':'stock'},body={'web_enabled':True},
            tools=[{'function':{'name':name}} for name in ('stock_web_search','stock_web_fetch')],max_output_tokens=4096,
            check=lambda:None,cancelled=lambda:False,
            execute=lambda *args:{'status':'succeeded','data':{'result':{'results':[{'url':'https://example.com'}]}}})
        with patch('agent_core.agent.OpenAICompatibleClient.stream',side_effect=[
            iter([{'type':'tool_call','index':0,'id':'s','function':{'name':'stock_web_search','arguments':'{}'}}]),
            iter([{'type':'tool_call','index':0,'id':'f','function':{'name':'stock_web_fetch','arguments':'{}'}}]),
            iter([{'type':'delta','content':'<stock_answer>已核对来源，页面未提供有行情时间的最新报价。</stock_answer>'}]),
        ]) as upstream:
            list(stream_agent({},[{'role':'user','content':'宇树科技股票当前价'}],runtime=runtime))
        self.assertEqual(upstream.call_args_list[1].kwargs['tool_choice'],{'type':'function','function':{'name':'stock_web_fetch'}})
        self.assertEqual(upstream.call_args_list[2].kwargs['tool_choice'],'auto')
    def test_web_off_excludes_http_and_mcp_tools_but_keeps_data_engine(self):
        entries={'stock_query':{'pluginId':'stock-data'}, 'stock_web_fetch':{'pluginId':'stock-web'},
                 'mcp__dhole__fetch':{'_mcpServer':{'pluginId':'stock-web'}}}
        self.assertEqual(select_tools(entries,list(entries),False),['stock_query'])
        self.assertEqual(select_tools(entries,list(entries),True),list(entries))

    def test_stock_run_opt_in_is_enforced_in_prepare_and_generic_agents_keep_tools(self):
        from test_core import configuration
        from agent_core.runs import prepare
        from agent_core.security import CoreError
        with tempfile.TemporaryDirectory() as directory:
            config=configuration(Path(directory)/'test.db');service=config['services'][0]
            service.update(agentType='stock',tools=['stock_web_fetch'])
            config['plugins']=[{'id':'stock-web'}];service['plugins']=['stock-web']
            config['httpTools']=[{'name':'stock_web_fetch','description':'web','effect':'read','pluginId':'stock-web'}]
            body={'request_id':str(uuid.uuid4()),'messages':[{'role':'user','content':'read'}],
                  'context':{'conversation_id':'c','actor_id':'u','tenant_id':'tenant','knowledge_base_id':None}}
            self.assertEqual(prepare(config,None,service,'chat',body)[0]['tools'],[])
            self.assertEqual(prepare(config,None,service,'chat',{**body,'web_enabled':True})[0]['tools'],['stock_web_fetch'])
            with self.assertRaises(CoreError):prepare(config,None,service,'chat',{**body,'web_enabled':'true'})
            service['agentType']='chat'
            self.assertEqual(prepare(config,None,service,'chat',body)[0]['tools'],['stock_web_fetch'])
