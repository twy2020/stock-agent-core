"""Server-owned configuration. Requests never select endpoints or credentials."""
import json
from pathlib import Path
from .security import CoreError, endpoint
from .providers import validate_providers, resolve_profile


def load_config(path):
    value=json.loads(Path(path).read_text('utf-8-sig'))
    return validate_config(value)


def validate_config(value):
    if not isinstance(value,dict) or not value.get('database'): raise CoreError('INVALID_CONFIGURATION',503)
    services=value.get('services',[]); models=value.get('models',[]); entries=value.get('httpTools',[])
    for items in (services,models,entries):
        if not isinstance(items,list): raise CoreError('INVALID_CONFIGURATION',503)
        keys=[x.get('name') if items is entries else x.get('id') for x in items]
        if any(not isinstance(k,str) or not k for k in keys) or len(keys)!=len(set(keys)):
            raise CoreError('DUPLICATE_CONFIGURATION_ID',503)
    for item in models:
        if not item.get('keyId'): endpoint(item.get('baseUrl',''))
        if not item.get('model'): raise CoreError('INVALID_MODEL_CONFIGURATION',503)
    validate_providers(value)
    for item in entries:
        endpoint(item.get('url',''))
        if item.get('effect') not in ('read','analyze','compute','propose','write') or str(item.get('version','1'))!='1':
            raise CoreError('INVALID_TOOL_CONFIGURATION',503)
    for service in services:
        if 'ragEnabled' in service and type(service['ragEnabled']) is not bool: raise CoreError('INVALID_RAG_POLICY',503)
        if 'ragViaMcp' in service and type(service['ragViaMcp']) is not bool: raise CoreError('INVALID_RAG_POLICY',503)
        if service.get('visionPolicy','ocr') not in ('ocr','direct','both'): raise CoreError('INVALID_VISION_POLICY',503)
        if not service.get('tenantId') or len(service.get('tokenSha256',''))!=64: raise CoreError('INVALID_CALLER_CONFIGURATION',503)
        if service.get('ragCallback'): endpoint(service['ragCallback'].get('url',''))
        if set(service.get('modelProfiles',[]))-set(m['id'] for m in models): raise CoreError('UNKNOWN_MODEL_PROFILE',503)
        if set(service.get('tools',[]))-set(t['name'] for t in entries): raise CoreError('UNKNOWN_TOOL',503)
    return value


def model_profile(config,service,identifier=None):
    identifier=identifier or service.get('defaultModelProfile')
    if identifier not in service.get('modelProfiles',[]): raise CoreError('MODEL_FORBIDDEN',403)
    item=next((m for m in config.get('models',[]) if m['id']==identifier and m.get('enabled',True)),None)
    if not item: raise CoreError('MODEL_UNAVAILABLE',503)
    return resolve_profile(config,item)


def public_model_settings(config,service):
    from .model import thinking_control
    models=[]
    for identifier in service.get('modelProfiles',[]):
        try: profile=model_profile(config,service,identifier)
        except CoreError: continue
        models.append({'id':identifier,'name':profile.get('name',identifier),'model':profile['model'],
                       'providerType':profile.get('providerType','OPENAI_COMPATIBLE'),
                       'reasoningToggle':thinking_control(profile.get('providerType','OPENAI'),profile['model']) is not None,
                       'tools':profile.get('capabilities',{}).get('tools',True)})
    return {'defaultModelProfile':service.get('defaultModelProfile'),'models':models}
