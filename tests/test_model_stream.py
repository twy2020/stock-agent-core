import json
import os
import unittest
import httpx
from unittest.mock import patch
from agent_core.model import OpenAICompatibleClient, ProviderError
from agent_core.agent import stream_agent


class Response:
    status_code=200
    def __init__(self,lines,content_type='text/event-stream'):
        self.lines=lines;self.headers={'content-type':content_type}
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def iter_lines(self):yield from self.lines


def frame(delta=None,finish=None):
    return 'data: '+json.dumps({'choices':[{'delta':delta or {},'finish_reason':finish}]})


PROFILE={'id':'test','model':'test','baseUrl':'http://127.0.0.1:1/v1','apiKeyEnv':'CORE_PROTOCOL_TEST_KEY'}


class ModelStreamTests(unittest.TestCase):
    def test_only_pre_request_connection_failures_are_retried_once(self):
        with patch.dict(os.environ,{'CORE_PROTOCOL_TEST_KEY':'test'}),patch('agent_core.model.httpx.stream',side_effect=[httpx.ConnectTimeout('private'),Response([frame({'content':'OK'}),frame(finish='stop')])]) as stream:
            events=list(OpenAICompatibleClient().stream(PROFILE,[]))
        self.assertEqual(events,[{'type':'progress','stage':'MODEL_CONNECT_RETRY'},{'type':'delta','content':'OK'}])
        self.assertEqual(stream.call_count,2)
        with patch.dict(os.environ,{'CORE_PROTOCOL_TEST_KEY':'test'}),patch('agent_core.model.httpx.stream',side_effect=httpx.ConnectTimeout('private')) as stream:
            with self.assertRaisesRegex(ProviderError,'CONNECT_TIMEOUT'):list(OpenAICompatibleClient().stream(PROFILE,[]))
        self.assertEqual(stream.call_count,2)

    def test_admin_probes_and_read_timeouts_never_auto_retry(self):
        for error,kwargs in [(httpx.ConnectTimeout('private'),{'timeout_seconds':15}),(httpx.ReadTimeout('private'),{})]:
            with self.subTest(error=type(error).__name__),patch.dict(os.environ,{'CORE_PROTOCOL_TEST_KEY':'test'}),patch('agent_core.model.httpx.stream',side_effect=error) as stream:
                with self.assertRaises(ProviderError):list(OpenAICompatibleClient().stream(PROFILE,[],**kwargs))
            self.assertEqual(stream.call_count,1)

    def test_failure_after_response_started_is_never_replayed(self):
        class Interrupted(Response):
            def iter_lines(self):
                yield frame({'content':'partial'})
                raise httpx.ConnectError('private upstream error')
        events=[]
        with patch.dict(os.environ,{'CORE_PROTOCOL_TEST_KEY':'test'}),patch('agent_core.model.httpx.stream',return_value=Interrupted([])) as stream:
            with self.assertRaisesRegex(ProviderError,'CONNECTION'):
                for event in OpenAICompatibleClient().stream(PROFILE,[]):events.append(event)
        self.assertEqual(events,[{'type':'delta','content':'partial'}])
        self.assertEqual(stream.call_count,1)

    def test_explicit_provider_proxy_is_used_without_environment_proxy_inheritance(self):
        profile={**PROFILE,'proxyUrl':'http://host.docker.internal:10808'}
        with patch.dict(os.environ,{'CORE_PROTOCOL_TEST_KEY':'test'}),patch('agent_core.model.httpx.stream',return_value=Response([frame({'content':'OK'}),frame(finish='stop')])) as stream:
            list(OpenAICompatibleClient().stream(profile,[]))
        self.assertEqual(stream.call_args.kwargs['proxy'],profile['proxyUrl'])
        self.assertFalse(stream.call_args.kwargs['trust_env'])

    def test_html_success_status_is_an_endpoint_error(self):
        with patch.dict(os.environ,{'CORE_PROTOCOL_TEST_KEY':'test'}),patch('agent_core.model.httpx.stream',return_value=Response(['<html>site</html>'],'text/html; charset=utf-8')):
            with self.assertRaisesRegex(ProviderError,'INVALID_PROVIDER_ENDPOINT'):
                list(OpenAICompatibleClient().stream(PROFILE,[]))

    def test_non_sse_response_is_rejected(self):
        with patch.dict(os.environ,{'CORE_PROTOCOL_TEST_KEY':'test'}),patch('agent_core.model.httpx.stream',return_value=Response(['{}'],'application/json')):
            with self.assertRaisesRegex(ProviderError,'INVALID_RESPONSE_FORMAT'):
                list(OpenAICompatibleClient().stream(PROFILE,[]))

    def test_stream_error_envelope_is_not_an_empty_response(self):
        with self.assertRaisesRegex(ProviderError,'UPSTREAM_STREAM_ERROR'):
            self.stream(['data: '+json.dumps({'error':{'message':'private provider details'}}),'data: [DONE]'])

    def stream(self,lines):
        with patch.dict(os.environ,{'CORE_PROTOCOL_TEST_KEY':'test'}),patch('agent_core.model.httpx.stream',return_value=Response(lines)):
            return list(OpenAICompatibleClient().stream(PROFILE,[{'role':'user','content':'hi'}]))

    def test_partial_delta_eof_is_failure_not_success(self):
        with self.assertRaisesRegex(ProviderError,'INCOMPLETE_RESPONSE'):
            self.stream([frame({'content':'partial'})])

    def test_done_without_explicit_finish_reason_is_rejected(self):
        with self.assertRaisesRegex(ProviderError,'INCOMPLETE_RESPONSE'):
            self.stream([frame({'content':'partial'}),'data: [DONE]'])

    def test_stop_and_tool_calls_are_completed_protocol_responses(self):
        events=self.stream([frame({'content':'answer'}),frame(finish='stop'),'data: [DONE]'])
        self.assertEqual(events,[{'type':'delta','content':'answer'}])
        events=self.stream([frame({'tool_calls':[{'index':0,'id':'call','function':{'name':'current_time','arguments':'{}'}}]}),frame(finish='tool_calls'),'data: [DONE]'])
        self.assertEqual(events[0]['type'],'tool_call')

    def test_length_filter_unknown_and_tool_finish_mismatch_are_rejected(self):
        for finish in ('length','content_filter','unexpected','tool_calls'):
            with self.subTest(finish=finish),self.assertRaises(ProviderError):
                self.stream([frame({'content':'partial'}),frame(finish=finish),'data: [DONE]'])

    def test_truncated_tool_call_cannot_execute(self):
        class Runtime:
            tools=[];max_output_tokens=256;called=False
            def cancelled(self):return False
            def check(self):pass
            def execute(self,*args):self.called=True;return {}
        runtime=Runtime()
        partial=[frame({'tool_calls':[{'index':0,'id':'call','function':{'name':'current_time','arguments':'{}'}}]})]
        with patch.dict(os.environ,{'CORE_PROTOCOL_TEST_KEY':'test'}),patch('agent_core.model.httpx.stream',return_value=Response(partial)):
            with self.assertRaisesRegex(ProviderError,'INCOMPLETE_RESPONSE'):
                list(stream_agent(PROFILE,[{'role':'user','content':'hi'}],runtime=runtime))
        self.assertFalse(runtime.called)

    def test_length_failure_preserves_provider_usage_without_accepting_partial_answer(self):
        lines=[frame({'content':'partial'}), 'data: '+json.dumps({
            'choices':[{'delta':{},'finish_reason':'length'}],
            'usage':{'prompt_tokens':100,'completion_tokens':4096}})]
        events=[]
        with patch.dict(os.environ,{'CORE_PROTOCOL_TEST_KEY':'test'}),patch('agent_core.model.httpx.stream',return_value=Response(lines)):
            with self.assertRaisesRegex(ProviderError,'OUTPUT_LIMIT'):
                for event in OpenAICompatibleClient().stream(PROFILE,[{'role':'user','content':'hi'}]):events.append(event)
        self.assertEqual(events[-1],{'type':'usage','prompt_tokens':100,'completion_tokens':4096})


if __name__=='__main__':unittest.main()
