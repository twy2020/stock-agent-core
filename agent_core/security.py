import hashlib
import hmac
import os
from pathlib import Path
from urllib.parse import urlsplit


class CoreError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


def credential(entry, prefix='token'):
    try:
        value = (Path(entry[prefix+'File']).read_text('utf-8-sig').strip()
                 if entry.get(prefix+'File') else os.getenv(entry.get(prefix+'Env',''),''))
    except OSError:
        raise CoreError('CREDENTIAL_UNAVAILABLE',503) from None
    if not value: raise CoreError('CREDENTIAL_UNAVAILABLE',503)
    return value


def endpoint(value):
    parsed=urlsplit(value)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise CoreError('INVALID_ENDPOINT_CONFIGURATION',503)
    return value.rstrip('/')


def proxy_endpoint(value):
    if value in (None,''): return None
    if not isinstance(value,str) or len(value)>2048: raise CoreError('INVALID_PROXY_CONFIGURATION',400)
    try:
        endpoint(value)
        parsed=urlsplit(value)
        if parsed.path not in ('','/') or parsed.query or parsed.port is None: raise ValueError()
    except (CoreError,ValueError): raise CoreError('INVALID_PROXY_CONFIGURATION',400) from None
    return value.rstrip('/')


def authenticate(config, header):
    token=header[7:] if header.startswith('Bearer ') else ''
    digest=hashlib.sha256(token.encode()).hexdigest()
    for service in config.get('services',[]):
        if token and service.get('enabled',True) and hmac.compare_digest(digest,service.get('tokenSha256','')):
            return service
    raise CoreError('SERVICE_AUTHENTICATION_REQUIRED',401)


def authorize(service, context, domain):
    if domain not in service.get('domains',[]): raise CoreError('DOMAIN_FORBIDDEN',403)
    if not isinstance(context,dict): raise CoreError('INVALID_CONTEXT')
    if domain=='cad':
        keys=('project_id','drawing_id','revision')
        if set(context)!=set(keys): raise CoreError('INVALID_CONTEXT')
        if any(not isinstance(context.get(k),str) or not 1<=len(context[k])<=160 for k in keys): raise CoreError('INVALID_CONTEXT')
        if context['project_id'] not in service.get('projectIds',[]): raise CoreError('PROJECT_FORBIDDEN',403)
    else:
        keys={'conversation_id','knowledge_base_id','actor_id','tenant_id'}
        if set(context)!=keys: raise CoreError('INVALID_CONTEXT')
        if any(not isinstance(context.get(k),str) or not 1<=len(context[k])<=160 for k in ('conversation_id','actor_id','tenant_id')):
            raise CoreError('INVALID_CONTEXT')
        kb=context.get('knowledge_base_id')
        if kb is not None and (not isinstance(kb,str) or not 1<=len(kb)<=160): raise CoreError('INVALID_CONTEXT')
        if context['tenant_id']!=service['tenantId']: raise CoreError('TENANT_FORBIDDEN',403)
    return dict(context)
