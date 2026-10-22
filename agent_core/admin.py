"""Authenticated administration API. No credentials or arbitrary config writes."""
import copy
import hashlib
import json
import os
import tempfile
import threading
import time
from contextlib import nullcontext
from pathlib import Path

from flask import jsonify, request, send_from_directory
from .admin_safety import input_summary, public_endpoint, safe_data
from .config import validate_config
from .security import CoreError
from .management import configuration_state


RUN_FIELDS = ('id', 'caller', 'tenant', 'domain', 'status', 'created', 'expires')
ACTIVE = ('QUEUED', 'RUNNING')


def run_view(row):
    return {**{key: row[key] for key in RUN_FIELDS}, 'context': safe_data(json.loads(row['context']))}


def integer_arg(name, default, minimum=0, maximum=1000000):
    try:
        value = int(request.args.get(name, default))
    except (ValueError, TypeError):
        raise CoreError('INVALID_PAGINATION') from None
    if not minimum <= value <= maximum:
        raise CoreError('INVALID_PAGINATION')
    return value


def register_admin(app, config, store, config_path=None):
    static = Path(__file__).parent / 'admin_static'
    state = configuration_state(app, config, config_path)
    lock = state['lock']; path = state['path']
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS admin_audit(id INTEGER PRIMARY KEY, created REAL NOT NULL, kind TEXT NOT NULL, object_id TEXT NOT NULL, changes TEXT NOT NULL)')

    def configuration():
        with lock:
            callers = [{key: item.get(key, default) for key, default in (
                ('id', None), ('tenantId', None), ('enabled', True), ('domains', []), ('modelProfiles', []),
                ('defaultModelProfile', None), ('tools', []), ('projectIds', []), ('maxActiveRuns', 2), ('maxOutputTokens', 4096))}
                       for item in config.get('services', [])]
            models = [{key: item.get(key, default) for key, default in (
                ('id', None), ('name', item['id']), ('model', None), ('providerType', 'OPENAI'), ('enabled', True))}
                      for item in config.get('models', [])]
            tools = [{**{key: item.get(key, default) for key, default in (
                ('name', None), ('version', '1'), ('effect', None), ('transport', 'http'), ('description', ''), ('enabled', True))},
                'endpoint': public_endpoint(item.get('url', ''))} for item in config.get('httpTools', [])]
            with store.db() as db:
                audit = [{**dict(row), 'changes': json.loads(row['changes'])}
                         for row in db.execute('SELECT * FROM admin_audit ORDER BY id DESC LIMIT 20')]
            return {'revision': state['revision'], 'writable': state['writable'], 'callers': callers, 'models': models, 'tools': tools,
                    'settings': {'execution': 'single-process', 'retentionHours': 24,
                                 'temporaryRetrieval': {'defaultTtlSeconds': 3600, 'maxTtlSeconds': 86400, 'maxSpacesPerCaller': 32}},
                    'recentChanges': audit}

    def details(rid):
        row = store.run(rid)
        with store.db() as db:
            events = db.execute('SELECT seq,name,payload FROM events WHERE run_id=? ORDER BY seq', (rid,)).fetchall()
        result = {'run': run_view(row), 'input': {'available': False}, 'output': safe_data(row['content']),
                  'model': None, 'usage': None, 'toolCalls': [], 'timeline': [], 'outcome': None}
        calls = {}
        usages = []
        for event in events:
            payload = safe_data(json.loads(event['payload'])); name = event['name']; seq = event['seq']
            if name == 'input':
                result['input'] = payload
            elif name == 'model':
                result['model'] = payload
            elif name == 'usage':
                usages.append(payload)
            elif name == 'complete':
                result['outcome'] = payload.get('outcome')
            if name in ('tool_call', 'tool_result', 'tool_error') or name == 'progress' and payload.get('operationId') and payload.get('tool'):
                operation_id = payload.get('operationId')
                key = operation_id or 'event:' + str(seq)
                call = calls.setdefault(key, {'operationId': operation_id, 'tool': payload.get('tool'), 'status': None,
                                             'arguments': None, 'result': None, 'error': None, 'eventId': seq})
                call['tool'] = payload.get('tool', call['tool'])
                if 'arguments' in payload:
                    call['arguments'] = payload['arguments']
                if name == 'tool_result':
                    call['result'] = payload.get('result')
                    call['status'] = payload.get('status') or (call['result'].get('status') if isinstance(call['result'], dict) else None)
                    if isinstance(call['result'], dict):
                        call['error'] = call['result'].get('error')
                elif name == 'tool_error':
                    call['error'] = payload.get('error', payload.get('code', payload)); call['status'] = 'failed'
                else:
                    call['status'] = payload.get('status', 'called' if name == 'tool_call' else None)
                    if payload.get('code'):
                        call['error'] = payload['code']
            # Streaming deltas belong to the answer, not thousands of timeline rows.
            if name not in ('delta', 'reasoning', 'input'):
                result['timeline'].append({'id': seq, 'type': name, 'stage': payload.get('stage'), 'tool': payload.get('tool'),
                                           'status': payload.get('status'), 'message': payload.get('message', payload.get('code'))})
        result['toolCalls'] = list(calls.values())
        if usages:
            result['usage'] = {'records': usages}
            for key in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens', 'output_tokens'):
                values = [usage[key] for usage in usages if type(usage.get(key)) in (int, float)]
                if values:
                    result['usage'][key] = sum(values)
        if not result['input']['available'] and row['payload'] != '{}':
            result['input'] = input_summary(json.loads(row['payload']))
        return result

    @app.get('/admin')
    @app.get('/admin/')
    def admin_page():
        return send_from_directory(static, 'index.html')

    @app.get('/admin/assets/<path:filename>')
    def admin_asset(filename):
        # Windows MIME registry may classify .js as text/plain; nosniff then
        # correctly prevents execution. Serve our own assets deterministically.
        mime={'.js':'text/javascript','.css':'text/css'}.get(Path(filename).suffix)
        return send_from_directory(static, filename,mimetype=mime)

    @app.get('/admin/api/status')
    def admin_status():
        with store.db() as db:
            counts = {row['status']: row['n'] for row in db.execute('SELECT status,COUNT(*) n FROM runs GROUP BY status')}
            recent = [run_view(row) for row in db.execute('SELECT * FROM runs ORDER BY created DESC,id DESC LIMIT 100')]
            spaces = db.execute('SELECT COUNT(*) FROM spaces').fetchone()[0]
        conf = configuration()
        return jsonify({'service': 'agent-core', 'execution': 'single-process', 'runsByStatus': counts, 'spaces': spaces,
                        'callers': conf['callers'], 'models': conf['models'], 'tools': conf['tools'], 'recentRuns': recent,
                        'summary': {'counts': counts, 'totalRuns': sum(counts.values()), 'activeRuns': sum(counts.get(key, 0) for key in ACTIVE),
                                    'callers': len(conf['callers']), 'models': len(conf['models']), 'tools': len(conf['tools']), 'spaces': spaces}})

    @app.get('/admin/api/runs')
    def list_runs():
        offset = integer_arg('offset', 0); limit = integer_arg('limit', 25, 1, 100)
        clauses = []; params = []
        for key in ('status', 'caller'):
            value = request.args.get(key, '')
            if value:
                if len(value) > 160:
                    raise CoreError('INVALID_FILTER')
                clauses.append(key + '=?'); params.append(value)
        query = request.args.get('q', '').strip()
        if len(query) > 200:
            raise CoreError('INVALID_FILTER')
        if query:
            clauses.append('(instr(lower(id),?)>0 OR instr(lower(caller),?)>0 OR instr(lower(tenant),?)>0 OR instr(lower(context),?)>0)')
            params.extend([query.lower()] * 4)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with store.db() as db:
            total = db.execute('SELECT COUNT(*) FROM runs' + where, params).fetchone()[0]
            items = [run_view(row) for row in db.execute('SELECT * FROM runs' + where + ' ORDER BY created DESC,id DESC LIMIT ? OFFSET ?', [*params, limit, offset])]
        return jsonify({'items': items, 'total': total, 'offset': offset, 'limit': limit})

    @app.get('/admin/api/runs/<rid>')
    def run_detail(rid):
        return jsonify(details(rid))

    @app.get('/admin/api/runs/<rid>/events')
    def run_events(rid):
        store.run(rid); after = integer_arg('after', 0, maximum=2147483647); limit = integer_arg('limit', 50, 1, 200)
        with store.db() as db:
            rows = db.execute('SELECT seq,name,payload FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?', (rid, after, limit + 1)).fetchall()
        items = [{'id': row['seq'], 'type': row['name'], 'payload': safe_data(json.loads(row['payload']))} for row in rows[:limit]]
        return jsonify({'items': items, 'after': after, 'nextCursor': items[-1]['id'] if items else after, 'hasMore': len(rows) > limit, 'limit': limit})

    @app.post('/admin/api/runs/<rid>/cancel')
    def admin_cancel_run(rid):
        store.run(rid); store.cancel(rid)
        return jsonify(details(rid))

    @app.get('/admin/api/configuration')
    def get_configuration():
        return jsonify(configuration())

    @app.patch('/admin/api/configuration')
    def patch_configuration():
        value = request.get_json(silent=True)
        if not isinstance(value, dict) or set(value) != {'revision', 'kind', 'id', 'changes'}:
            raise CoreError('INVALID_CONFIGURATION_CHANGE')
        kind = value['kind']; changes = value['changes']
        allowed = {'callers': {'enabled', 'maxActiveRuns', 'maxOutputTokens'}, 'models': {'name', 'enabled'}, 'tools': {'enabled'}}
        if not isinstance(kind, str) or kind not in allowed or not isinstance(changes, dict) or not changes or set(changes) - allowed[kind]:
            raise CoreError('INVALID_CONFIGURATION_CHANGE')
        for key, item in changes.items():
            if key == 'enabled' and type(item) is not bool:
                raise CoreError('INVALID_CONFIGURATION_CHANGE')
            if key in ('maxActiveRuns', 'maxOutputTokens') and (type(item) is not int or not (1 <= item <= 8 if key == 'maxActiveRuns' else 256 <= item <= 8192)):
                raise CoreError('INVALID_CONFIGURATION_CHANGE')
            if key == 'name' and (not isinstance(item, str) or not 1 <= len(item.strip()) <= 120):
                raise CoreError('INVALID_CONFIGURATION_CHANGE')
        gateway = app.extensions.get('core_gateway_state', {})
        with lock, gateway.get('lock', nullcontext()):
            if any(gateway.get('active', {}).values()):
                raise CoreError('ACTIVE_GATEWAY_CONFIGURATION_LOCKED', 409)
            if not state['writable']:
                raise CoreError('CONFIGURATION_READ_ONLY', 409)
            if value['revision'] != state['revision']:
                raise CoreError('CONFIGURATION_CONFLICT', 409)
            group = {'callers': 'services', 'models': 'models', 'tools': 'httpTools'}[kind]
            identifier = 'name' if kind == 'tools' else 'id'
            original = next((item for item in config[group] if item[identifier] == value['id']), None)
            if original is None:
                raise CoreError('CONFIGURATION_ITEM_NOT_FOUND', 404)
            candidate = copy.deepcopy(config)
            next(item for item in candidate[group] if item[identifier] == value['id']).update(changes)
            validate_config(candidate)
            serialized = (json.dumps(candidate, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
            temporary = None
            try:
                with store.db() as db:
                    # Serialize with enqueue/claim/cancel; no model run can appear during this write.
                    db.execute('BEGIN IMMEDIATE')
                    if db.execute("SELECT 1 FROM runs WHERE status IN ('QUEUED','RUNNING') LIMIT 1").fetchone():
                        raise CoreError('ACTIVE_RUNS_CONFIGURATION_LOCKED', 409)
                    if hashlib.sha256(path.read_bytes()).hexdigest() != state['revision']:
                        raise CoreError('CONFIGURATION_FILE_CHANGED', 409)
                    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.core-config-', suffix='.tmp', delete=False) as output:
                        temporary = Path(output.name); output.write(serialized); output.flush(); os.fsync(output.fileno())
                    # Check again immediately before atomic replacement.
                    if hashlib.sha256(path.read_bytes()).hexdigest() != state['revision']:
                        raise CoreError('CONFIGURATION_FILE_CHANGED', 409)
                    db.execute('INSERT INTO admin_audit(created,kind,object_id,changes) VALUES(?,?,?,?)',
                               (time.time(), kind, value['id'], json.dumps(changes, ensure_ascii=False)))
                    os.replace(temporary, path); temporary = None
                    original.update(changes)
                    state['revision'] = hashlib.sha256(serialized).hexdigest()
            except OSError:
                raise CoreError('CONFIGURATION_WRITE_FAILED', 503) from None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return jsonify(configuration())
