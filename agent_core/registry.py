"""Config-approved HTTP tools. No arbitrary installations, caller URLs or retries."""
import json
import uuid
import httpx
from jsonschema import Draft202012Validator, ValidationError, SchemaError
from .security import CoreError, credential, endpoint


def http_catalog(config,service,domain,agent=True):
    from .capabilities import plugin_allowed
    if domain not in service.get('domains',[]): raise CoreError('DOMAIN_FORBIDDEN',403)
    allowed=set(service.get('tools',[]))
    return {e['name']:e for e in config.get('httpTools',[]) if e['name'] in allowed and e.get('enabled',True)
            and domain in e.get('domains',['cad','chat']) and (not agent or e.get('effect')!='write')
            and not (service.get('visionPolicy','ocr')=='direct' and e.get('transport')=='ocr-vision-v1')
            and plugin_allowed(config,service,e)}


def catalog(config,service,domain,agent=True):
    from . import mcp
    return {**http_catalog(config,service,domain,agent),**mcp.catalog(config,service,domain,agent)}


def validate(schema,value):
    try:
        if '"$ref"' in json.dumps(schema) or '"$dynamicRef"' in json.dumps(schema): raise CoreError('SCHEMA_REFERENCES_UNSUPPORTED')
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(value)
    except ValidationError as error:
        failure=CoreError('SCHEMA_VALIDATION_FAILED')
        hint={'path':'.'.join(str(p) for p in error.absolute_path) or '$','rule':error.validator}
        if error.validator in ('type','enum','required','minimum','maximum','minItems','maxItems','pattern'):
            hint['expected']=error.validator_value
        if error.validator=='additionalProperties':hint['allowedFields']=list(error.schema.get('properties',{}))
        failure.details={'data':{'validation':hint}}
        raise failure from None
    except (SchemaError,TypeError): raise CoreError('SCHEMA_VALIDATION_FAILED') from None


def json_request(method,url,entry,payload=None,request_id=None,limit=16*1024*1024):
    headers={'Authorization':'Bearer '+credential(entry)}
    if request_id: headers['Idempotency-Key']=request_id
    try:
        with httpx.stream(method,endpoint(url),headers=headers,json=payload,follow_redirects=False,trust_env=False,
                          timeout=httpx.Timeout(min(120,max(1,entry.get('timeoutSeconds',60))),connect=10)) as response:
            if response.status_code not in (200,201,202): raise CoreError('TOOL_UPSTREAM_ERROR',502)
            content=bytearray()
            for block in response.iter_bytes():
                content.extend(block)
                if len(content)>limit: raise CoreError('TOOL_RESPONSE_TOO_LARGE',502)
        value=json.loads(content)
        if not isinstance(value,dict): raise CoreError('TOOL_INVALID_RESPONSE',502)
        return value
    except (httpx.HTTPError,ValueError) as exc:
        if isinstance(exc,CoreError): raise
        raise CoreError('TOOL_UPSTREAM_ERROR',502) from None


def execute(config,service,context,domain,name,arguments,request_id=None,agent=True):
    entry=catalog(config,service,domain,agent).get(name)
    if entry is None: raise CoreError('TOOL_FORBIDDEN',403)
    if entry.get('transport')=='ocr-vision-v1': raise CoreError('VISION_REQUIRES_ANALYSIS_RUN')
    if entry.get('transport')=='mcp':
        from . import mcp
        return mcp.execute(config,service,context,domain,entry,arguments,request_id,agent)
    validate(entry.get('inputSchema',{'type':'object'}),arguments)
    identity={**context,'service_id':service['id'],'caller_id':service['id'],'tenant_id':service['tenantId']}
    rid=request_id or str(uuid.uuid4())
    value=json_request('POST',entry['url'],entry,{'contractVersion':'1','requestId':rid,'context':identity,'tool':name,'arguments':arguments},rid)
    if value.get('contractVersion')!='1' or value.get('status')!='succeeded':
        if entry.get('pluginId') in ('stock-data','stock-web') and value.get('contractVersion')=='1' and value.get('status')=='failed' and isinstance(value.get('error'),str):
            return value
        raise CoreError('TOOL_EXECUTION_FAILED',502)
    validate(entry.get('outputSchema',{'type':'object'}),value)
    return value
