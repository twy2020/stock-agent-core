import unittest
from types import SimpleNamespace
from unittest.mock import patch
from agent_core.agent import stream_agent
from agent_core.security import CoreError


class StockAnswerTests(unittest.TestCase):
    def test_invalid_tool_parameter_returns_actionable_path_without_values(self):
        from agent_core.registry import validate
        runtime=self.runtime()
        def execute(*args):
            validate({'type':'object','properties':{'limit':{'type':'integer','maximum':20}}},{'limit':100})
        runtime.execute=execute
        with patch('agent_core.agent.OpenAICompatibleClient.stream',side_effect=[
            iter([{'type':'tool_call','index':0,'id':'q','function':{'name':'stock_query','arguments':'{}'}}]),
            iter([{'type':'delta','content':'<stock_answer>尚未取得查询结果。</stock_answer>'}]),
        ]):events=list(stream_agent({},[{'role':'user','content':'分析走势'}],runtime=runtime))
        failure=next(e['result'] for e in events if e['type']=='tool_result')
        self.assertEqual(failure['data']['validation'],{'path':'limit','rule':'maximum','expected':20})
    def test_repair_uses_question_and_evidence_without_failed_tool_transcript(self):
        with patch('agent_core.agent.OpenAICompatibleClient.stream',side_effect=[
            iter([{'type':'delta','content':'<DSML>stock_dataset</DSML>'}]),
            iter([{'type':'delta','content':'<stock_answer>资料不足，无法确认完整名单。</stock_answer>'}]),
        ]) as upstream:
            list(stream_agent({},[{'role':'user','content':'目前中国有哪些上市机器人公司'},
                                  {'role':'assistant','content':'旧的工具计划不能进入最终收尾上下文'}],runtime=self.runtime()))
        final_messages=upstream.call_args.args[1]
        self.assertEqual([m['role'] for m in final_messages],['system','user'])
        self.assertIn('上市机器人公司',final_messages[-1]['content'])
        self.assertNotIn('<DSML>',str(final_messages))
        self.assertNotIn('旧的工具计划',str(final_messages))

    def test_company_discovery_does_not_execute_market_history(self):
        runtime=self.runtime();runtime.execute=unittest.mock.Mock(return_value={'status':'succeeded','data':{}})
        runtime.tools=[{'function':{'name':'stock_query','parameters':{'properties':{},'required':['task']}}},
                       {'function':{'name':'stock_backtest'}}]
        with patch('agent_core.agent.OpenAICompatibleClient.stream',side_effect=[
            iter([{'type':'tool_call','index':0,'id':'q','function':{'name':'stock_query','arguments':'{"task":"analyze","universe":{"codes":["300024"]}}'}}]),
            iter([{'type':'delta','content':'<stock_answer>需要依据公司业务资料梳理，现有行情不能代表竞争实力。</stock_answer>'}]),
        ]) as upstream:
            events=list(stream_agent({},[{'role':'user','content':'目前中国还有哪些比较有实力的上市机器人公司'}],runtime=runtime))
        runtime.execute.assert_not_called()
        tools=upstream.call_args_list[0].kwargs['tools']
        self.assertNotIn('stock_backtest',[t['function']['name'] for t in tools])
        self.assertTrue(any(e.get('result',{}).get('error')=='COMPANY_METADATA_REQUIRED' for e in events))

    def test_ungrounded_indicator_reply_is_repaired_with_tools_enabled(self):
        runtime=self.runtime();runtime.execute=lambda *args:{'status':'succeeded','data':{'summary':{'BOLL_MID':12}}}
        with patch('agent_core.agent.OpenAICompatibleClient.stream',side_effect=[
            iter([{'type':'delta','content':'我已计算布林带，上轨12.102元。'}]),
            iter([{'type':'tool_call','index':0,'id':'boll','function':{'name':'stock_query','arguments':'{}'}}]),
            iter([{'type':'delta','content':'<stock_answer>已取得引擎计算的布林带结果。</stock_answer>'}]),
        ]) as upstream:
            events=list(stream_agent({},[{'role':'user','content':'布林带'}],runtime=runtime))
        self.assertEqual(upstream.call_args_list[1].kwargs['tool_choice'],'auto')
        self.assertEqual(upstream.call_count,3)
        self.assertNotIn('12.102',''.join(e.get('content','') for e in events if e['type']=='delta'))
        self.assertTrue(any(e.get('reason')=='MISSING_INDICATOR_EVIDENCE' for e in events))

    def test_repeated_ungrounded_indicator_values_fail_without_leaking(self):
        with patch('agent_core.agent.OpenAICompatibleClient.stream',side_effect=[
            iter([{'type':'delta','content':'<stock_answer>上轨12元。</stock_answer>'}]),
            iter([{'type':'delta','content':'<stock_answer>上轨13元。</stock_answer>'}]),
        ]):
            events=[]
            with self.assertRaisesRegex(CoreError,'STOCK_EVIDENCE_REQUIRED'):
                for event in stream_agent({},[{'role':'user','content':'布林带'}],runtime=self.runtime()):events.append(event)
        self.assertFalse(any(e['type']=='delta' for e in events))

    def test_indicator_definition_and_clarification_need_no_data_tool(self):
        for question,answer in [('布林带是什么','布林带由中轨和上下轨组成。'),('布林带','请问要分析哪只股票和哪个时间窗口？')]:
            with patch('agent_core.agent.OpenAICompatibleClient.stream',return_value=iter([{'type':'delta','content':f'<stock_answer>{answer}</stock_answer>'}])) as upstream:
                events=list(stream_agent({},[{'role':'user','content':question}],runtime=self.runtime()))
            self.assertEqual(upstream.call_count,1)
            self.assertIn(answer,''.join(e.get('content','') for e in events if e['type']=='delta'))

    def test_tool_preamble_only_appears_as_event_not_answer_delta(self):
        runtime=self.runtime()
        runtime.execute=lambda *args:{'status':'succeeded','data':{}}
        with patch('agent_core.agent.OpenAICompatibleClient.stream',side_effect=[iter([
            {'type':'delta','content':'字段写法需要调整，重新查询：'},
            {'type':'tool_call','index':0,'id':'q','function':{'name':'stock_query','arguments':'{}'}},
        ]),iter([{'type':'delta','content':'<stock_answer>这是根据真实结果给出的结论。</stock_answer>'}])]):
            events=list(stream_agent({},[{'role':'user','content':'分析走势'}],runtime=runtime))
        self.assertEqual(''.join(e['content'] for e in events if e['type']=='delta'),'这是根据真实结果给出的结论。')
        note=next(e for e in events if e.get('stage')=='MODEL_NOTE')
        self.assertIn('字段写法',note['content'])
        before=events[:events.index(note)]
        self.assertFalse(any(e.get('stage')=='RESPONSE_STREAMING' for e in before))

    def test_three_failed_queries_stop_further_external_attempts(self):
        runtime=self.runtime()
        runtime.execute=unittest.mock.Mock(return_value={'status':'failed','error':'INVALID_QUERY_SCHEMA'})
        calls=[{'type':'tool_call','index':i,'id':str(i),'function':{'name':'stock_query','arguments':'{}'}} for i in range(4)]
        with patch('agent_core.agent.OpenAICompatibleClient.stream',side_effect=[iter(calls),iter([{'type':'delta','content':'<stock_answer>目前无法核实，请检查查询参数。</stock_answer>'}])]) as upstream:
            events=list(stream_agent({},[{'role':'user','content':'分析走势'}],runtime=runtime))
        self.assertEqual(runtime.execute.call_count,3)
        self.assertEqual(upstream.call_args.kwargs['tool_choice'],'none')
        self.assertTrue(any(e.get('result',{}).get('error')=='STOCK_QUERY_FAILURE_LIMIT' for e in events))

    def runtime(self, stock=True):
        return SimpleNamespace(service={'agentType': 'stock' if stock else 'chat'}, tools=[], check=lambda: None,
                               max_output_tokens=1024, cancelled=lambda: False)

    def test_repairs_instruction_echo_once_without_more_tools(self):
        with patch('agent_core.agent.OpenAICompatibleClient.stream', side_effect=[
            iter([{'type':'delta','content':'请用中文回复。'}]),
            iter([{'type':'delta','content':'<stock_answer>当前数据不足以核实科技板块的完整筛选结果。</stock_answer>'}]),
        ]) as upstream:
            events=list(stream_agent({},[{'role':'user','content':'筛选科技股'}],runtime=self.runtime()))
        self.assertEqual(upstream.call_count,2)
        self.assertEqual(upstream.call_args.kwargs['tool_choice'],'none')
        reset=next(i for i,e in enumerate(events) if e['type']=='answer_reset')
        self.assertIn('当前数据不足', ''.join(e.get('content','') for e in events[reset+1:] if e['type']=='delta'))

    def test_repeated_echo_fails_instead_of_completing(self):
        with patch('agent_core.agent.OpenAICompatibleClient.stream', side_effect=[
            iter([{'type':'delta','content':'请用中文回复。'}]),iter([{'type':'delta','content':'请用中文回答。'}]),
        ]) as upstream:
            with self.assertRaises(CoreError) as raised:
                list(stream_agent({},[{'role':'user','content':'筛选科技股'}],runtime=self.runtime()))
        self.assertEqual(raised.exception.code,'INVALID_STOCK_ANSWER')
        self.assertEqual(upstream.call_count,2)

    def test_other_agents_are_unaffected(self):
        with patch('agent_core.agent.OpenAICompatibleClient.stream',return_value=iter([{'type':'delta','content':'请用中文回复。'}])) as upstream:
            list(stream_agent({},[{'role':'user','content':'hello'}],runtime=self.runtime(False)))
        self.assertEqual(upstream.call_count,1)

    def test_final_preamble_is_archived_even_without_a_tool_call(self):
        text='I have the data. Now I need to analyze it.\n<stock_answer>行情呈区间波动，需结合波动指标判断。</stock_answer>'
        with patch('agent_core.agent.OpenAICompatibleClient.stream',return_value=iter([{'type':'delta','content':text}])):
            events=list(stream_agent({},[{'role':'user','content':'分析行情'}],runtime=self.runtime()))
        self.assertEqual(''.join(e['content'] for e in events if e['type']=='delta'),'行情呈区间波动，需结合波动指标判断。')
        self.assertTrue(any(e.get('stage')=='MODEL_NOTE' and 'I have' in e.get('content','') for e in events))

    def test_missing_boundary_and_incomplete_answer_do_not_leak_or_complete(self):
        with patch('agent_core.agent.OpenAICompatibleClient.stream',side_effect=[
            iter([{'type':'delta','content':'I have the data. Now I need to analyze it.'}]),
            iter([{'type':'delta','content':'<stock_answer>现在</stock_answer>'}]),
        ]):
            events=[]
            with self.assertRaisesRegex(CoreError,'INVALID_STOCK_ANSWER'):
                for event in stream_agent({},[{'role':'user','content':'分析行情'}],runtime=self.runtime()):events.append(event)
        self.assertFalse(any(e['type']=='delta' for e in events))
