"""OpenAI-compatible SSE client extracted from SaaS, without DB or cipher imports."""
import json
import time
from contextlib import ExitStack
import httpx
from .security import credential, endpoint, proxy_endpoint


class ProviderError(RuntimeError):
    def __init__(self,category,retryable=False,http_status=None):
        super().__init__(category)
        self.category,self.retryable,self.http_status=category,retryable,http_status


def thinking_control(provider_type,model):
    name=model.lower()
    if provider_type=='DEEPSEEK' and name in {'deepseek-flash','deepseek-pro'}: return 'thinking'
    if provider_type=='SILICONFLOW' and name.removeprefix('pro/').startswith(('deepseek-ai/deepseek-v4','deepseek-ai/deepseek-v3.1','deepseek-ai/deepseek-v3.2')): return 'enable_thinking'
    return None


class OpenAICompatibleClient:
    def stream(self,profile,messages,reasoning_enabled=None,tools=None,tool_choice='auto',max_output_tokens=4096,cancelled=lambda:False,timeout_seconds=None):
        key=credential(profile,'apiKey')
        base=endpoint(profile['baseUrl'])
        payload={'model':profile['model'],'messages':messages,'stream':True,'stream_options':{'include_usage':True},
                 'max_tokens':max(256,min(8192,int(max_output_tokens)))}
        if tools: payload.update(tools=tools,tool_choice=tool_choice,parallel_tool_calls=False)
        control=thinking_control(profile.get('providerType','OPENAI'),profile['model'])
        if reasoning_enabled is not None and control:
            payload[control]={'type':'enabled' if reasoning_enabled else 'disabled'} if control=='thinking' else reasoning_enabled
        elif reasoning_enabled is True: raise ProviderError('THINKING_UNSUPPORTED')
        seen=False;finished=None;had_tools=False
        deadline=time.monotonic()+timeout_seconds if timeout_seconds is not None else None
        try:
            with ExitStack() as stack:
                for attempt in range(2):
                    try:
                        response=stack.enter_context(httpx.stream('POST',base+'/chat/completions',headers={'Authorization':f'Bearer {key}','Accept':'text/event-stream'},
                            json=payload,timeout=httpx.Timeout(timeout_seconds or 120,connect=min(timeout_seconds or 10,10)),follow_redirects=False,trust_env=False,proxy=proxy_endpoint(profile.get('proxyUrl'))))
                        break
                    except (httpx.ConnectTimeout,httpx.ConnectError):
                        if timeout_seconds is not None or attempt: raise
                        if cancelled(): raise ProviderError('CANCELLED')
                        # Connection setup failed before sending HTTP request bytes.
                        yield {'type':'progress','stage':'MODEL_CONNECT_RETRY'}
                if response.status_code!=200:
                    raise ProviderError('AUTHENTICATION' if response.status_code in (401,403) else 'RATE_LIMIT' if response.status_code==429 else 'UPSTREAM',False,response.status_code)
                content_type=response.headers.get('content-type','').split(';',1)[0].strip().lower()
                if content_type in ('text/html','application/xhtml+xml'):
                    raise ProviderError('INVALID_PROVIDER_ENDPOINT',http_status=response.status_code)
                if content_type!='text/event-stream':
                    raise ProviderError('INVALID_RESPONSE_FORMAT',http_status=response.status_code)
                usage={}
                for line in response.iter_lines():
                    if deadline is not None and time.monotonic()>deadline: raise ProviderError('TIMEOUT')
                    if cancelled(): raise ProviderError('CANCELLED')
                    if not line.startswith('data:'): continue
                    data=line[5:].strip()
                    if not data: continue
                    if data=='[DONE]': break
                    if len(data)>2*1024*1024: raise ProviderError('UPSTREAM_RESPONSE_LIMIT')
                    value=json.loads(data)
                    if isinstance(value,dict) and value.get('error'): raise ProviderError('UPSTREAM_STREAM_ERROR')
                    choice=(value.get('choices') or [{}])[0]; delta=choice.get('delta') or {}
                    if finished is not None and (delta.get('content') or delta.get('reasoning_content') or delta.get('tool_calls')):
                        raise ProviderError('INVALID_STREAM_AFTER_FINISH')
                    for call in delta.get('tool_calls') or []:
                        seen=True;had_tools=True;yield {**call,'type':'tool_call'}
                    if isinstance(delta.get('reasoning_content'),str) and delta['reasoning_content']:
                        seen=True;yield {'type':'reasoning','content':delta['reasoning_content']}
                    if isinstance(delta.get('content'),str) and delta['content']:
                        seen=True;yield {'type':'delta','content':delta['content']}
                    if value.get('usage'): usage.update({k:v for k,v in value['usage'].items() if v is not None})
                    reason=choice.get('finish_reason')
                    if reason is not None:
                        if reason=='length':
                            if usage: yield {'type':'usage',**usage}
                            raise ProviderError('OUTPUT_LIMIT')
                        if reason=='content_filter': raise ProviderError('CONTENT_FILTER')
                        if reason not in ('stop','tool_calls') or (reason=='tool_calls')!=had_tools:
                            raise ProviderError('INVALID_FINISH_REASON')
                        if finished is not None and finished!=reason: raise ProviderError('INVALID_FINISH_REASON')
                        finished=reason
                if usage: yield {'type':'usage',**usage}
                # A clean socket EOF or [DONE] alone does not confirm the model
                # completed its answer/tool call. Never execute partial tools.
                if finished is None: raise ProviderError('INCOMPLETE_RESPONSE')
                if not seen: raise ProviderError('EMPTY_RESPONSE')
        except ProviderError: raise
        except httpx.ConnectTimeout as exc: raise ProviderError('CONNECT_TIMEOUT') from exc
        except httpx.TimeoutException as exc: raise ProviderError('TIMEOUT') from exc
        except httpx.ConnectError as exc: raise ProviderError('CONNECTION') from exc
        except (httpx.HTTPError,ValueError,KeyError,TypeError,AttributeError) as exc: raise ProviderError('UPSTREAM') from exc
