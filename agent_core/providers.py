"""Server-owned provider hierarchy, bounded discovery and usage accounting."""
import json
import math
import re
import time
import uuid

import httpx
from .security import CoreError, credential, endpoint, proxy_endpoint


def _items(config, group):
    values = config.get(group, [])
    if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
        raise CoreError('INVALID_PROVIDER_CONFIGURATION', 503)
    ids = [v.get('id') for v in values]
    if any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', v) for v in ids) or len(ids) != len(set(ids)):
        raise CoreError('INVALID_PROVIDER_CONFIGURATION', 503)
    for value in values:
        if 'enabled' in value and type(value['enabled']) is not bool:
            raise CoreError('INVALID_PROVIDER_CONFIGURATION', 503)
        if 'name' in value and (not isinstance(value['name'], str) or not 1 <= len(value['name']) <= 120):
            raise CoreError('INVALID_PROVIDER_CONFIGURATION', 503)
    return values


def validate_providers(config):
    providers = _items(config, 'providers'); accounts = _items(config, 'providerAccounts'); keys = _items(config, 'providerKeys')
    for item in providers:
        if set(item) - {'id', 'name', 'providerType', 'baseUrl', 'enabled','proxyUrl'}:
            raise CoreError('INVALID_PROVIDER_CONFIGURATION', 503)
        endpoint(item.get('baseUrl', ''))
        proxy_endpoint(item.get('proxyUrl'))
        if '?' in item['baseUrl'] or not isinstance(item.get('providerType', 'OPENAI'), str):
            raise CoreError('INVALID_PROVIDER_CONFIGURATION', 503)
    for item in accounts:
        if set(item) - {'id', 'name', 'providerId', 'enabled', 'billing'} or item.get('providerId') not in [v['id'] for v in providers]:
            raise CoreError('INVALID_PROVIDER_ACCOUNT', 503)
        billing = item.get('billing')
        if billing is not None:
            if not isinstance(billing, dict) or set(billing) - {'adapter', 'apiKeyEnv', 'apiKeyFile'} or billing.get('adapter') != 'openai-organization':
                raise CoreError('INVALID_BILLING_CONFIGURATION', 503)
            refs = [billing.get(k) for k in ('apiKeyFile', 'apiKeyEnv') if billing.get(k)]
            if len(refs) != 1 or not isinstance(refs[0], str) or not 1 <= len(refs[0]) <= 2048:
                raise CoreError('INVALID_CREDENTIAL_REFERENCE', 503)
            if billing.get('apiKeyEnv') and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', billing['apiKeyEnv']):
                raise CoreError('INVALID_CREDENTIAL_REFERENCE', 503)
    for item in keys:
        if set(item) - {'id', 'name', 'accountId', 'enabled', 'apiKeyFile', 'apiKeyEnv'} or item.get('accountId') not in [v['id'] for v in accounts]:
            raise CoreError('INVALID_PROVIDER_KEY', 503)
        refs = [item.get(k) for k in ('apiKeyFile', 'apiKeyEnv') if item.get(k)]
        if len(refs) != 1 or not isinstance(refs[0], str) or len(refs[0]) > 2048:
            raise CoreError('INVALID_CREDENTIAL_REFERENCE', 503)
        if item.get('apiKeyEnv') and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', item['apiKeyEnv']):
            raise CoreError('INVALID_CREDENTIAL_REFERENCE', 503)
    for item in config.get('models', []):
        if item.get('keyId'):
            if item['keyId'] not in [v['id'] for v in keys] or any(k in item for k in ('apiKey', 'apiKeyFile', 'apiKeyEnv', 'baseUrl')):
                raise CoreError('INVALID_MODEL_KEY', 503)
        caps = item.get('capabilities', {})
        if not isinstance(caps, dict) or set(caps) - {'vision', 'tools', 'reasoning'} or any(type(v) is not bool for v in caps.values()):
            raise CoreError('INVALID_MODEL_METADATA', 503)
        for field in ('contextWindow', 'maxOutputTokens'):
            if field in item and (type(item[field]) is not int or not 1 <= item[field] <= 100000000):
                raise CoreError('INVALID_MODEL_METADATA', 503)
        pricing = item.get('pricing', {})
        if not isinstance(pricing, dict) or set(pricing) - {'inputPerMillion', 'outputPerMillion', 'currency'}:
            raise CoreError('INVALID_MODEL_PRICING', 503)
        for key in ('inputPerMillion', 'outputPerMillion'):
            if key in pricing and (type(pricing[key]) not in (int, float) or not math.isfinite(pricing[key]) or not 0 <= pricing[key] <= 1000000):
                raise CoreError('INVALID_MODEL_PRICING', 503)
        if pricing and not re.fullmatch(r'[A-Z]{3}', str(pricing.get('currency', ''))):
            raise CoreError('INVALID_MODEL_PRICING', 503)


def resolve_key(config, key_id, require_enabled=True):
    key = next((v for v in config.get('providerKeys', []) if v['id'] == key_id), None)
    account = next((v for v in config.get('providerAccounts', []) if key and v['id'] == key['accountId']), None)
    provider = next((v for v in config.get('providers', []) if account and v['id'] == account['providerId']), None)
    if not all((key, account, provider)):
        raise CoreError('PROVIDER_KEY_NOT_FOUND', 404)
    if require_enabled and any(not v.get('enabled', True) for v in (key, account, provider)):
        raise CoreError('MODEL_UNAVAILABLE', 503)
    return {'keyId': key['id'], 'accountId': account['id'], 'providerId': provider['id'],
            'providerType': provider.get('providerType', 'OPENAI'), 'baseUrl': provider['baseUrl'],
            'proxyUrl':proxy_endpoint(provider.get('proxyUrl')),
            **{k: key[k] for k in ('apiKeyEnv', 'apiKeyFile') if key.get(k)}}


def resolve_profile(config, profile):
    if not profile.get('enabled', True):
        raise CoreError('MODEL_UNAVAILABLE', 503)
    return {**profile, **resolve_key(config, profile['keyId'])} if profile.get('keyId') else dict(profile)


def public_hierarchy(config):
    """Never expose credential references, values, or private endpoint queries."""
    from .admin_safety import public_endpoint
    result = {}
    for group, fields in (('providers', ('providerType',)), ('providerAccounts', ('providerId',)), ('providerKeys', ('accountId',))):
        result[group] = [{**{k: v[k] for k in ('id', *fields) if k in v}, 'name': v.get('name', v['id']), 'enabled': v.get('enabled', True),
                          **({'endpoint': public_endpoint(v['baseUrl'])} if group == 'providers' else {}),
                          **({'credentialConfigured': bool(v.get('apiKeyFile') or v.get('apiKeyEnv'))} if group == 'providerKeys' else {})}
                         for v in config.get(group, [])]
    return result


def bounded_json(response, limit=4 * 1024 * 1024):
    chunks = []; size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > limit:
            raise CoreError('UPSTREAM_RESPONSE_LIMIT', 502)
        chunks.append(chunk)
    try:
        value = json.loads(b''.join(chunks))
    except (ValueError, UnicodeError):
        raise CoreError('INVALID_UPSTREAM_RESPONSE', 502) from None
    if not isinstance(value, dict):
        raise CoreError('INVALID_UPSTREAM_RESPONSE', 502)
    return value


def upstream_status(status):
    if status != 200:
        raise CoreError('UPSTREAM_AUTHENTICATION' if status in (401, 403) else 'UPSTREAM_RATE_LIMIT' if status == 429 else 'UPSTREAM_ERROR', 502 if status != 429 else 429)


def discover_models(config, key_id):
    profile = resolve_key(config, key_id)
    try:
        with httpx.stream('GET', endpoint(profile['baseUrl']) + '/models', headers={'Authorization': 'Bearer ' + credential(profile, 'apiKey')},
                          timeout=httpx.Timeout(30, connect=10), follow_redirects=False, trust_env=False,proxy=profile.get('proxyUrl')) as response:
            upstream_status(response.status_code); value = bounded_json(response)
        if not isinstance(value.get('data'), list) or len(value['data']) > 5000:
            raise CoreError('INVALID_UPSTREAM_RESPONSE', 502)
        result = []
        for model in value['data']:
            if not isinstance(model, dict) or not isinstance(model.get('id'), str) or not 1 <= len(model['id']) <= 200:
                continue
            item = {'id': model['id'], 'model': model['id'], 'keyId': key_id, 'metadataSource': 'provider'}
            for field in ('description', 'owned_by'):
                if isinstance(model.get(field), str):
                    item[field] = model[field][:1000]
            if type(model.get('created')) is int and 0 <= model['created'] <= 10**12:
                item['created'] = model['created']
            for field, aliases in (('contextWindow', ('contextWindow', 'context_length', 'context_window')), ('maxOutputTokens', ('maxOutputTokens', 'max_output_tokens'))):
                candidate = next((model[k] for k in aliases if k in model), None)
                if type(candidate) is int and 0 < candidate <= 100000000:
                    item[field] = candidate
            caps = model.get('capabilities')
            if isinstance(caps, dict):
                item['capabilities'] = {k: caps[k] for k in ('vision', 'tools', 'reasoning') if type(caps.get(k)) is bool}
            result.append(item)
        return {'keyId': key_id, 'models': result, 'discoveredAt': time.time(), 'note': 'Unreported metadata is unknown; verify capabilities and pricing before enabling.'}
    except httpx.HTTPError:
        raise CoreError('PROVIDER_DISCOVERY_FAILED', 502) from None


def query_provider_billing(config, account_id, start, end):
    """Explicit organization billing adapter, never confused with token estimates."""
    account = next((v for v in config.get('providerAccounts', []) if v['id'] == account_id), None)
    if account is None:
        raise CoreError('PROVIDER_ACCOUNT_NOT_FOUND', 404)
    if type(start) is not int or type(end) is not int or not 0 <= start < end or end - start > 366 * 86400:
        raise CoreError('INVALID_BILLING_RANGE')
    billing = account.get('billing')
    if not billing:
        return {'supported': False, 'accountId': account_id, 'reason': 'BILLING_ADAPTER_NOT_CONFIGURED'}
    provider = next((v for v in config.get('providers', []) if v['id'] == account['providerId']), None)
    if not provider or not provider.get('enabled', True) or not account.get('enabled', True):
        raise CoreError('PROVIDER_UNAVAILABLE', 503)
    if billing.get('adapter') != 'openai-organization':
        return {'supported': False, 'accountId': account_id, 'reason': 'BILLING_ADAPTER_UNSUPPORTED'}
    headers = {'Authorization': 'Bearer ' + credential(billing, 'apiKey')}
    params = {'start_time': start, 'end_time': end, 'bucket_width': '1d', 'limit': 180}
    buckets = []; totals = {}; has_more = False; pages = set()
    try:
        for _ in range(5):
            with httpx.stream('GET', endpoint(provider['baseUrl']) + '/organization/costs', headers=headers, params=params,
                              timeout=httpx.Timeout(30, connect=10), follow_redirects=False, trust_env=False,proxy=proxy_endpoint(provider.get('proxyUrl'))) as response:
                upstream_status(response.status_code); value = bounded_json(response, 2 * 1024 * 1024)
            if not isinstance(value.get('data'), list) or len(value['data']) > 180:
                raise CoreError('INVALID_UPSTREAM_RESPONSE', 502)
            for bucket in value['data']:
                if not isinstance(bucket, dict) or not isinstance(bucket.get('results'), list) or len(bucket['results']) > 1000:
                    raise CoreError('INVALID_UPSTREAM_RESPONSE', 502)
                results = []
                for row in bucket['results']:
                    amount = row.get('amount') if isinstance(row, dict) else None
                    if not isinstance(amount, dict) or type(amount.get('value')) not in (int, float) or not math.isfinite(amount['value']) or not re.fullmatch(r'[A-Za-z]{3}', str(amount.get('currency', ''))):
                        raise CoreError('INVALID_UPSTREAM_RESPONSE', 502)
                    currency = amount['currency'].upper(); totals[currency] = totals.get(currency, 0) + amount['value']
                    results.append({'amount': {'value': amount['value'], 'currency': currency},
                                    **{k: row[k][:200] for k in ('line_item', 'project_id') if isinstance(row.get(k), str)}})
                buckets.append({'start': bucket.get('start_time'), 'end': bucket.get('end_time'), 'results': results})
            has_more = value.get('has_more') is True
            if not has_more:
                break
            cursor = value.get('next_page')
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048 or cursor in pages:
                raise CoreError('INVALID_UPSTREAM_RESPONSE', 502)
            pages.add(cursor); params['page'] = cursor
        return {'supported': True, 'adapter': 'openai-organization', 'accountId': account_id, 'start': start, 'end': end,
                'buckets': buckets, 'currencyTotals': totals, 'hasMore': has_more, 'costBasis': 'Provider organization billed cost; may include applications outside Agent Core.'}
    except httpx.HTTPError:
        raise CoreError('PROVIDER_BILLING_FAILED', 502) from None


def init_usage(store):
    with store.db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS provider_usage(id TEXT PRIMARY KEY, created REAL NOT NULL,
          caller TEXT NOT NULL, tenant TEXT NOT NULL, profile_id TEXT NOT NULL, provider_id TEXT, account_id TEXT, key_id TEXT,
          source TEXT NOT NULL, request_id TEXT, status TEXT NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER,
          total_tokens INTEGER, estimated_cost REAL, currency TEXT)''')
        db.execute('CREATE INDEX IF NOT EXISTS provider_usage_created ON provider_usage(created)')


def clean_usage(usage):
    usage = usage if isinstance(usage, dict) else {}
    result = {}
    for field, alternate in (('prompt_tokens', 'input_tokens'), ('completion_tokens', 'output_tokens'), ('total_tokens', 'total_tokens')):
        value = usage.get(field, usage.get(alternate))
        if type(value) is int and 0 <= value <= 10**12:
            result[field] = value
    if 'total_tokens' not in result and all(k in result for k in ('prompt_tokens', 'completion_tokens')):
        result['total_tokens'] = result['prompt_tokens'] + result['completion_tokens']
    return result


def record_usage(store, service, profile, usage, status, source='gateway', request_id=None):
    init_usage(store); usage = clean_usage(usage); pricing = profile.get('pricing', {}); cost = None
    if all(k in usage for k in ('prompt_tokens', 'completion_tokens')) and all(k in pricing for k in ('inputPerMillion', 'outputPerMillion', 'currency')):
        cost = (usage['prompt_tokens'] * pricing['inputPerMillion'] + usage['completion_tokens'] * pricing['outputPerMillion']) / 1000000
    with store.db() as db:
        db.execute('INSERT INTO provider_usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (str(uuid.uuid4()), time.time(), service['id'], service['tenantId'], profile['id'], profile.get('providerId'), profile.get('accountId'), profile.get('keyId'),
                    source, request_id, status, usage.get('prompt_tokens'), usage.get('completion_tokens'), usage.get('total_tokens'), cost, pricing.get('currency')))


def usage_summary(store, filters=None):
    init_usage(store)
    filters=filters or {}; clauses=[]; params=[]
    if set(filters)-{'caller','profile','start','end'}: raise CoreError('INVALID_USAGE_FILTER')
    for key,column in (('caller','caller'),('profile','profile_id')):
        if filters.get(key):
            if not isinstance(filters[key],str) or len(filters[key])>160: raise CoreError('INVALID_USAGE_FILTER')
            clauses.append(column+'=?');params.append(filters[key])
    bounds={}
    for key,operator in (('start','>='),('end','<')):
        if filters.get(key):
            try: bounds[key]=int(filters[key])
            except (TypeError,ValueError): raise CoreError('INVALID_USAGE_FILTER') from None
            if bounds[key]<0: raise CoreError('INVALID_USAGE_FILTER')
            clauses.append('created'+operator+'?');params.append(bounds[key])
    if 'start' in bounds and 'end' in bounds and bounds['start']>=bounds['end']: raise CoreError('INVALID_USAGE_FILTER')
    where=' WHERE '+' AND '.join(clauses) if clauses else '' 
    with store.db() as db:
        groups = [dict(row) for row in db.execute('''SELECT profile_id AS profileId, provider_id AS providerId, account_id AS accountId, key_id AS keyId,
            currency, COUNT(*) AS requests, SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
            SUM(prompt_tokens) AS promptTokens, SUM(completion_tokens) AS completionTokens, SUM(total_tokens) AS totalTokens,
            SUM(CASE WHEN total_tokens IS NULL THEN 1 ELSE 0 END) AS unknownUsageRequests,
            SUM(estimated_cost) AS estimatedCost, SUM(CASE WHEN estimated_cost IS NULL THEN 1 ELSE 0 END) AS unknownCostRequests
            FROM provider_usage''' + where + ' GROUP BY profile_id,provider_id,account_id,key_id,currency ORDER BY requests DESC LIMIT 1000',params)]
        recent = [dict(row) for row in db.execute('SELECT * FROM provider_usage'+where+' ORDER BY created DESC LIMIT 100',params)]
    return {'groups': groups, 'recent': recent, 'filters':filters, 'recentLimit':100, 'costBasis': 'Configured token rates; estimates exclude unreported provider charges. Null means unknown.'}
