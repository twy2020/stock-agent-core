"""Admin-only registry editing with optimistic concurrency and credential references."""
import copy
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

from flask import jsonify, request, g
from .admin_safety import public_endpoint
from .security import CoreError, endpoint


FIELDS = {
    'providers': {'name', 'enabled', 'baseUrl', 'providerType','proxyUrl'},
    'providerAccounts': {'name', 'enabled', 'providerId', 'billing'},
    'providerKeys': {'name', 'enabled', 'accountId', 'apiKeyEnv', 'apiKeyFile'},
    'models': {'name', 'enabled', 'keyId', 'model', 'baseUrl', 'providerType', 'apiKeyEnv', 'apiKeyFile',
               'capabilities', 'pricing', 'contextWindow', 'maxOutputTokens'},
    'services': {'name', 'enabled', 'domains', 'projectIds', 'modelProfiles', 'defaultModelProfile', 'tools',
                 'mcpServers', 'mcpTools', 'skills', 'plugins', 'visionPolicy', 'maxActiveRuns', 'maxOutputTokens', 'ragViaMcp', 'ragEnabled'},
    'mcpServers': {'name', 'description', 'enabled', 'transport', 'url', 'tokenEnv', 'tokenFile', 'domains',
                   'tools', 'pluginId', 'timeoutSeconds'},
    'skills': {'name', 'description', 'enabled', 'instructions', 'domains', 'pluginId'},
    'plugins': {'name', 'description', 'enabled'},
    'httpTools': {'description', 'enabled', 'version', 'effect', 'transport', 'url', 'tokenEnv', 'tokenFile',
                  'domains', 'inputSchema', 'outputSchema', 'timeoutSeconds', 'pluginId'},
}
MCP_TOOL_FIELDS = {'name', 'description', 'inputSchema', 'effect', 'enabled', 'httpTool'}
GRANTS = {'domains', 'projectIds', 'modelProfiles', 'tools', 'mcpServers', 'mcpTools', 'skills', 'plugins'}
ID = re.compile(r'[A-Za-z0-9_.:-]{1,120}')


def configuration_state(app, config, config_path=None):
    """Share this state with the legacy admin overview/editor."""
    if 'core_configuration_state' not in app.extensions:
        path = Path(config_path).resolve() if config_path is not None else None
        revision = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        writable = False
        if path is not None:
            try:
                raw = path.read_bytes()
                if json.loads(raw.decode('utf-8-sig')) == config:
                    revision = hashlib.sha256(raw).hexdigest(); writable = True
            except (OSError, ValueError, UnicodeError):
                pass
        app.extensions['core_configuration_state'] = {
            'lock': threading.RLock(), 'path': path, 'revision': revision, 'writable': writable}
    return app.extensions['core_configuration_state']


def _invalid():
    raise CoreError('INVALID_CONFIGURATION_CHANGE')


def _references(entry, prefix):
    refs = [key for key in (prefix + 'Env', prefix + 'File') if entry.get(key)]
    if len(refs) > 1:
        _invalid()
    for key in refs:
        value = entry[key]
        if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(ord(c) < 32 for c in value):
            _invalid()
        if key.endswith('Env') and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
            _invalid()


def _validate_changes(kind, changes):
    if not isinstance(changes, dict) or not changes or set(changes) - FIELDS[kind]:
        _invalid()
    try: json.dumps(changes, allow_nan=False)
    except (ValueError, TypeError): _invalid()
    for field, value in changes.items():
        if field=='proxyUrl':
            from .security import proxy_endpoint
            try: proxy_endpoint(value)
            except CoreError: _invalid()
        if kind=='models' and field=='keyId' and value=='':continue
        if field in ('enabled','ragViaMcp','ragEnabled') and type(value) is not bool:
            _invalid()
        if field == 'pluginId' and value == '':
            continue
        if field in ('name', 'providerType', 'model', 'providerId', 'accountId', 'keyId', 'pluginId'):
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= (200 if field == 'model' else 120):
                _invalid()
        if field in ('description', 'instructions') and (not isinstance(value, str) or len(value) > 16000):
            _invalid()
        if field in ('url', 'baseUrl'):
            if not isinstance(value, str) or len(value) > 4096 or '?' in value:
                _invalid()
            try: endpoint(value)
            except (CoreError, ValueError): _invalid()
        if field in ('apiKeyFile', 'apiKeyEnv', 'tokenFile', 'tokenEnv'):
            if not isinstance(value, str) or not value.strip():
                _invalid()
        if field in GRANTS and not (kind == 'mcpServers' and field == 'tools'):
            if not isinstance(value, list) or len(value) > 1024 or any(not isinstance(v, str) or not 1 <= len(v) <= 160 for v in value):
                _invalid()
            if len(value) != len(set(value)):
                _invalid()
        if field == 'domains' and set(value) - {'chat', 'cad'}:
            _invalid()
        if field == 'timeoutSeconds' and (type(value) not in (int, float) or not 1 <= value <= 120):
            _invalid()
    if kind == 'services':
        for key, low, high in (('maxActiveRuns', 1, 8), ('maxOutputTokens', 256, 8192)):
            if key in changes and (type(changes[key]) is not int or not low <= changes[key] <= high):
                _invalid()
        if 'defaultModelProfile' in changes and not isinstance(changes['defaultModelProfile'], str):
            _invalid()
    if kind == 'mcpServers' and 'tools' in changes:
        if not isinstance(changes['tools'], list) or any(not isinstance(v, dict) or set(v) - MCP_TOOL_FIELDS for v in changes['tools']):
            _invalid()
    if kind == 'httpTools':
        if 'transport' in changes and changes['transport'] not in ('http', 'ocr-vision-v1'):
            _invalid()
        from jsonschema import Draft202012Validator, SchemaError
        for field in ('inputSchema', 'outputSchema'):
            if field in changes:
                schema = changes[field]
                if not isinstance(schema, dict) or '"$ref"' in json.dumps(schema) or '"$dynamicRef"' in json.dumps(schema):
                    _invalid()
                try: Draft202012Validator.check_schema(schema)
                except SchemaError: _invalid()
    if 'billing' in changes:
        billing = changes['billing']
        if not isinstance(billing, dict) or set(billing) - {'adapter', 'apiKeyEnv', 'apiKeyFile'}:
            _invalid()
        _references(billing, 'apiKey')
    _references(changes, 'apiKey'); _references(changes, 'token')


def _validate_candidate(candidate):
    from .config import validate_config
    from .providers import validate_providers
    from .capabilities import validate_config as validate_capabilities
    try:
        validate_config(candidate)
        validate_providers(candidate)
        validate_capabilities(candidate)
        for service in candidate.get('services', []):
            if service.get('defaultModelProfile') and service['defaultModelProfile'] not in service.get('modelProfiles', []):
                raise CoreError('UNKNOWN_DEFAULT_MODEL_PROFILE')
    except CoreError as exc:
        raise CoreError(exc.code, 400) from None
    except (TypeError, ValueError, KeyError, AttributeError):
        _invalid()


def _public_item(kind, entry):
    identifier = 'name' if kind == 'httpTools' else 'id'
    value = {key: copy.deepcopy(entry[key]) for key in FIELDS[kind] | {identifier} if key in entry}
    value.setdefault('enabled', True)
    if kind == 'services':
        value.update({key: entry.get(key, default) for key, default in (('maxActiveRuns', 2), ('maxOutputTokens', 4096))})
    for key in ('url', 'baseUrl','proxyUrl'):
        if key in value:
            value[key] = public_endpoint(value[key])
    if kind == 'mcpServers' and 'tools' in value:
        value['tools'] = [{k: copy.deepcopy(v) for k, v in tool.items() if k in MCP_TOOL_FIELDS} for tool in value['tools']]
    if kind == 'providerAccounts' and 'billing' in value:
        value['billing'] = {k: v for k, v in value['billing'].items() if k in ('adapter', 'apiKeyEnv', 'apiKeyFile')}
    references=[key for key in ('apiKeyEnv','apiKeyFile','tokenEnv','tokenFile') if value.pop(key,None)]
    if kind in ('models','providerKeys','mcpServers','httpTools'):
        value['credentialConfigured']=bool(references)
    return value


def register_management(app, config, store, config_path=None):
    state = configuration_state(app, config, config_path)
    lock = state['lock']; path = state['path']
    probes = threading.BoundedSemaphore(2)
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS admin_audit(id INTEGER PRIMARY KEY, created REAL NOT NULL, kind TEXT NOT NULL, object_id TEXT NOT NULL, changes TEXT NOT NULL)')

    def registry():
        return {'revision': state['revision'], 'writable': state['writable'],
                **{kind: [_public_item(kind, entry) for entry in config.get(kind, [])] for kind in FIELDS}}

    @contextmanager
    def editing(revision):
        gateway = app.extensions.get('core_gateway_state', {})
        with lock, gateway.get('lock', nullcontext()):
            if any(gateway.get('active', {}).values()):
                raise CoreError('ACTIVE_GATEWAY_CONFIGURATION_LOCKED', 409)
            if not state['writable']:
                raise CoreError('CONFIGURATION_READ_ONLY', 409)
            if revision != state['revision']:
                raise CoreError('CONFIGURATION_CONFLICT', 409)
            yield

    def save(candidate, kind, identifier, fields, operation='update', staged=None, related=()):
        """Caller holds editing locks; DB lock also excludes newly enqueued runs."""
        _validate_candidate(candidate)
        serialized = (json.dumps(candidate, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
        temporary = None; credential_created = False; committed = False
        try:
            with store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute("SELECT 1 FROM runs WHERE status IN ('QUEUED','RUNNING') LIMIT 1").fetchone():
                    raise CoreError('ACTIVE_RUNS_CONFIGURATION_LOCKED', 409)
                if hashlib.sha256(path.read_bytes()).hexdigest() != state['revision']:
                    raise CoreError('CONFIGURATION_FILE_CHANGED', 409)
                if staged is not None:
                    credential_path, secret = staged
                    credential_path.parent.mkdir(mode=0o700, exist_ok=True)
                    if credential_path.parent.resolve() != path.parent / 'credentials':
                        raise CoreError('INVALID_CREDENTIAL_DIRECTORY', 503)
                    descriptor = os.open(credential_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    credential_created = True
                    with os.fdopen(descriptor, 'wb') as output:
                        output.write(secret.encode('utf-8')); output.flush(); os.fsync(output.fileno())
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.core-config-', suffix='.tmp', delete=False) as output:
                    temporary = Path(output.name); output.write(serialized); output.flush(); os.fsync(output.fileno())
                if hashlib.sha256(path.read_bytes()).hexdigest() != state['revision']:
                    raise CoreError('CONFIGURATION_FILE_CHANGED', 409)
                db.execute('INSERT INTO admin_audit(created,kind,object_id,changes) VALUES(?,?,?,?)',
                           (time.time(), kind, identifier, json.dumps({'operation': operation, 'fields': sorted(fields)})))
                os.replace(temporary, path); temporary = None; committed = True
                for group in dict.fromkeys((kind,*related)):
                    key = 'name' if group == 'httpTools' else 'id'
                    existing = {entry[key]: entry for entry in config.get(group, [])}
                    updated = []
                    for entry in candidate.get(group, []):
                        original = existing.get(entry[key])
                        if original is None:
                            updated.append(entry)
                        else:
                            removed = set(original) - set(entry)
                            original.update(entry)
                            for field in removed: original.pop(field, None)
                            updated.append(original)
                    config.setdefault(group, [])[:] = updated
                state['revision'] = hashlib.sha256(serialized).hexdigest()
        except OSError:
            raise CoreError('CONFIGURATION_WRITE_FAILED', 503) from None
        finally:
            cleanup = ([temporary] if temporary is not None else [])
            if credential_created and not committed:
                cleanup.append(staged[0])
            for item in cleanup:
                try: item.unlink(missing_ok=True)
                except OSError: pass

    @app.get('/v1/settings/model')
    def service_model_settings():
        from .config import public_model_settings
        with lock:
            return jsonify({**public_model_settings(config,g.service),'revision':state['revision'],'writable':state['writable']})

    @app.put('/v1/settings/model')
    def update_service_model():
        from .config import model_profile, public_model_settings
        value=request.get_json(silent=True)
        if not isinstance(value,dict) or set(value)!={'revision','modelProfile'} or not isinstance(value['modelProfile'],str):
            _invalid()
        with editing(value['revision']):
            # A caller may select only its existing grants; no registry or credentials cross this boundary.
            profile=model_profile(config,g.service,value['modelProfile'])
            if profile.get('capabilities',{}).get('tools') is False:
                raise CoreError('MODEL_TOOLS_REQUIRED',400)
            candidate=copy.deepcopy(config)
            service=next(s for s in candidate['services'] if s['id']==g.service['id'])
            service['defaultModelProfile']=value['modelProfile']
            save(candidate,'services',g.service['id'],['defaultModelProfile'])
            return jsonify({**public_model_settings(config,g.service),'revision':state['revision'],'writable':state['writable']})

    @app.get('/admin/api/registry')
    def get_registry():
        with lock:
            return jsonify(registry())

    @app.post('/admin/api/models/<model_id>/test')
    def test_model_connection(model_id):
        from .providers import resolve_profile
        from .model import OpenAICompatibleClient, ProviderError
        value=request.get_json(silent=True)
        if not isinstance(value,dict) or set(value)!={'revision','mode'} or value['mode'] not in ('text','tools'):
            _invalid()
        with lock:
            if value['revision']!=state['revision']: raise CoreError('CONFIGURATION_CONFLICT',409)
            entry=next((m for m in config.get('models',[]) if m['id']==model_id),None)
            if entry is None: raise CoreError('MODEL_UNAVAILABLE',404)
            profile=resolve_profile(copy.deepcopy(config),copy.deepcopy(entry))
            revision=state['revision']
        if not probes.acquire(blocking=False): raise CoreError('MODEL_TEST_BUSY',429)
        started=time.monotonic()
        result={'mode':value['mode'],'revision':revision,'status':'succeeded','code':'OK'}
        usage={}
        try:
            kwargs={'max_output_tokens':256,'timeout_seconds':15}
            prompt='Reply with only OK.'
            if value['mode']=='tools':
                prompt='Call connection_probe with value OK.'
                kwargs.update(tools=[{'type':'function','function':{'name':'connection_probe','description':'Connectivity check; no side effects.',
                    'parameters':{'type':'object','properties':{'value':{'type':'string','enum':['OK']}},'required':['value'],'additionalProperties':False}}}],
                    tool_choice={'type':'function','function':{'name':'connection_probe'}})
            events=[]
            for item in OpenAICompatibleClient().stream(profile,[{'role':'user','content':prompt}],**kwargs):
                if item['type']=='usage': usage.update({k:v for k,v in item.items() if k!='type'})
                else: events.append(item)
            if value['mode']=='tools':
                calls={}
                for item in events:
                    if item['type']!='tool_call': continue
                    call=calls.setdefault(item.get('index',0),{'name':'','arguments':''})
                    for key in ('name','arguments'): call[key]+=item.get('function',{}).get(key,'') or ''
                try: valid=len(calls)==1 and all(c['name']=='connection_probe' and json.loads(c['arguments'])=={'value':'OK'} for c in calls.values())
                except (ValueError,TypeError): valid=False
                if not valid: raise ProviderError('MODEL_TOOL_TEST_FAILED')
            elif not any(e['type']=='delta' and e.get('content','').strip() for e in events):
                raise ProviderError('EMPTY_RESPONSE')
        except ProviderError as exc:
            result.update(status='failed',code=exc.category)
            if exc.http_status is not None: result['httpStatus']=exc.http_status
        except CoreError as exc:
            result.update(status='failed',code=exc.code)
        finally:
            probes.release()
            from .providers import record_usage
            record_usage(store,{'id':'admin-diagnostics','tenantId':'core-admin'},profile,usage,
                         'completed' if result['status']=='succeeded' else 'failed',source='diagnostic')
        with lock: result['stale']=revision!=state['revision']
        result['elapsedMs']=int((time.monotonic()-started)*1000)
        return jsonify(result)

    @app.patch('/admin/api/registry')
    def patch_registry():
        value = request.get_json(silent=True)
        if not isinstance(value, dict) or set(value) != {'revision', 'kind', 'id', 'changes'}:
            _invalid()
        kind = value['kind']; identifier = value['id']; changes = value['changes']
        if not isinstance(kind, str) or kind not in FIELDS or not isinstance(identifier, str) or not ID.fullmatch(identifier):
            _invalid()
        _validate_changes(kind, changes)
        with editing(value['revision']):
            key = 'name' if kind == 'httpTools' else 'id'
            original = next((v for v in config.get(kind, []) if v[key] == identifier), None)
            if kind == 'services' and original is None:
                raise CoreError('CONFIGURATION_ITEM_NOT_FOUND', 404)
            candidate = copy.deepcopy(config)
            entries = candidate.setdefault(kind, [])
            entry = next((v for v in entries if v[key] == identifier), None)
            if entry is None:
                entry = {key: identifier}; entries.append(entry)
            changes = copy.deepcopy(changes)
            if 'billing' in changes:
                billing = {**entry.get('billing', {}), **changes['billing']}
                for suffix, opposite in (('File', 'Env'), ('Env', 'File')):
                    if 'apiKey' + suffix in changes['billing']:
                        billing.pop('apiKey' + opposite, None)
                changes['billing'] = billing
            for prefix in ('apiKey', 'token'):
                for suffix, opposite in (('File', 'Env'), ('Env', 'File')):
                    if prefix + suffix in changes:
                        entry.pop(prefix + opposite, None)
            # Migrating a legacy profile to a provider key discards its old private connection settings.
            if kind == 'models' and changes.get('keyId'):
                for field in ('baseUrl', 'apiKeyFile', 'apiKeyEnv', 'apiKey'):
                    entry.pop(field, None)
            if (kind == 'mcpServers' and changes.get('transport') in ('http-bridge', 'rag-bridge')
                    and not {'url', 'tokenFile', 'tokenEnv'} & set(changes)):
                for field in ('url', 'tokenFile', 'tokenEnv'):
                    entry.pop(field, None)
            entry.update(copy.deepcopy(changes))
            if kind=='models' and changes.get('keyId')=='':entry.pop('keyId',None)
            if changes.get('pluginId') == '':
                entry.pop('pluginId', None)
            save(candidate, kind, identifier, changes)
            return jsonify(registry())

    @app.post('/admin/api/provider-keys')
    def write_provider_key():
        value = request.get_json(silent=True)
        if (not isinstance(value, dict) or set(value) != {'revision', 'id', 'accountId', 'name', 'enabled', 'secret'}
                or not isinstance(value['id'], str) or not ID.fullmatch(value['id'])):
            _invalid()
        secret = value['secret']
        if not isinstance(secret, str) or not 1 <= len(secret) <= 16384 or secret != secret.strip() or any(ord(c) < 32 or ord(c) == 127 for c in secret):
            _invalid()
        try: secret.encode('utf-8')
        except UnicodeError: _invalid()
        changes = {field: value[field] for field in ('accountId', 'name', 'enabled')}
        _validate_changes('providerKeys', changes)
        with editing(value['revision']):
            credential_path = path.parent / 'credentials' / (secrets.token_hex(24) + '.token')
            candidate = copy.deepcopy(config)
            entries = candidate.setdefault('providerKeys', [])
            entry = next((row for row in entries if row['id'] == value['id']), None)
            if entry is None:
                entry = {'id': value['id']}; entries.append(entry)
            entry.pop('apiKeyEnv', None)
            entry.update(changes); entry['apiKeyFile'] = str(credential_path)
            save(candidate, 'providerKeys', value['id'], [*changes, 'credential'], staged=(credential_path, secret))
            return jsonify(registry())

    @app.post('/admin/api/connections')
    def write_connection():
        value=request.get_json(silent=True)
        allowed={'revision','kind','id','changes','apiKey','credentialId'}
        if not isinstance(value,dict) or set(value)-allowed or not {'revision','kind','id','changes','apiKey'}<=set(value):_invalid()
        kind=value['kind'];identifier=value['id'];secret=value['apiKey'];changes=value['changes']
        if kind not in ('providers','providerKeys','models','mcpServers','httpTools') or not isinstance(identifier,str) or not ID.fullmatch(identifier):_invalid()
        if not isinstance(secret,str) or not 1<=len(secret)<=16384 or secret!=secret.strip() or any(ord(c)<32 or ord(c)==127 for c in secret):_invalid()
        try:secret.encode('utf-8')
        except UnicodeError:_invalid()
        _validate_changes(kind,changes)
        if {'apiKeyFile','apiKeyEnv','tokenFile','tokenEnv'}&set(changes):_invalid()
        if 'credentialId' in value and (kind!='providers' or not isinstance(value['credentialId'],str)): _invalid()
        with editing(value['revision']):
            candidate=copy.deepcopy(config)
            identity='name' if kind=='httpTools' else 'id'
            entries=candidate.setdefault(kind,[])
            entry=next((row for row in entries if row[identity]==identifier),None)
            if entry is None:
                entry={identity:identifier};entries.append(entry)
            if kind=='models' and changes.get('keyId'):
                for field in ('baseUrl','apiKeyFile','apiKeyEnv','apiKey'):entry.pop(field,None)
            entry.update(copy.deepcopy(changes))
            if kind=='models' and changes.get('keyId')=='':entry.pop('keyId',None)
            credential_entry=entry;prefix='token' if kind in ('mcpServers','httpTools') else 'apiKey'
            if kind=='providers':
                accounts=candidate.setdefault('providerAccounts',[]);keys=candidate.setdefault('providerKeys',[])
                if value.get('credentialId'):
                    credential_entry=next((k for k in keys if k['id']==value['credentialId']),None)
                    account=next((a for a in accounts if credential_entry and a['id']==credential_entry['accountId']),None)
                    if not account or account['providerId']!=identifier:_invalid()
                else:
                    account=next((a for a in accounts if a['providerId']==identifier),None)
                    if account is None:
                        account={'id':'account-'+secrets.token_hex(8),'providerId':identifier,'name':'Default','enabled':True};accounts.append(account)
                    credential_entry={'id':'key-'+secrets.token_hex(8),'accountId':account['id'],'name':'Default','enabled':True};keys.append(credential_entry)
            elif kind=='models' and entry.get('keyId'):
                credential_entry=next((k for k in candidate.get('providerKeys',[]) if k['id']==entry['keyId']),None)
                if credential_entry is None:_invalid()
            if kind=='mcpServers' and entry.get('transport')!='streamable-http':_invalid()
            credential_path=path.parent/'credentials'/(secrets.token_hex(24)+'.token')
            credential_entry.pop(prefix+'Env',None);credential_entry[prefix+'File']=str(credential_path)
            related=('providerAccounts','providerKeys') if kind=='providers' else ('providerKeys',) if kind=='models' and entry.get('keyId') else ()
            save(candidate,kind,identifier,[*changes,'credential'],staged=(credential_path,secret),related=related)
            return jsonify(registry())

    @app.delete('/admin/api/registry/<kind>/<identifier>')
    def delete_registry(kind, identifier):
        value = request.get_json(silent=True)
        if not isinstance(value, dict) or set(value) != {'revision'} or kind not in FIELDS or not ID.fullmatch(identifier):
            _invalid()
        if kind == 'services':
            raise CoreError('CALLER_DELETION_NOT_ALLOWED')
        with editing(value['revision']):
            key = 'name' if kind == 'httpTools' else 'id'
            if not any(entry[key] == identifier for entry in config.get(kind, [])):
                raise CoreError('CONFIGURATION_ITEM_NOT_FOUND', 404)
            candidate = copy.deepcopy(config)
            candidate[kind] = [entry for entry in candidate[kind] if entry[key] != identifier]
            try: _validate_candidate(candidate)
            except CoreError:
                raise CoreError('CONFIGURATION_ITEM_REFERENCED', 409) from None
            save(candidate, kind, identifier, [], operation='delete')
            return jsonify(registry())

    @app.post('/admin/api/providers/<key_id>/discover')
    def provider_discover(key_id):
        from .providers import discover_models
        with lock: snapshot = copy.deepcopy(config)
        return jsonify(discover_models(snapshot, key_id))

    @app.post('/admin/api/mcp/<server_id>/discover')
    def mcp_discover(server_id):
        from .capabilities import discovery
        with lock: snapshot = copy.deepcopy(config)
        return jsonify(discovery(snapshot, server_id))

    @app.get('/admin/api/usage')
    def get_usage():
        from .providers import usage_summary
        return jsonify(usage_summary(store,dict(request.args)))

    @app.post('/admin/api/accounts/<account_id>/billing')
    def account_billing(account_id):
        from .providers import query_provider_billing
        value = request.get_json(silent=True)
        if not isinstance(value, dict) or set(value) != {'start', 'end'}:
            _invalid()
        with lock: snapshot = copy.deepcopy(config)
        return jsonify(query_provider_billing(snapshot, account_id, value['start'], value['end']))
