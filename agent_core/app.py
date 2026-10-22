import copy
import json
import time
from flask import Flask, Response, g, jsonify, request, stream_with_context
from werkzeug.exceptions import HTTPException
from . import knowledge, registry, runs
from .config import validate_config, model_profile, public_model_settings
from .model import thinking_control
from .security import CoreError, authenticate, authorize
from .store import Store
from .admin import register_admin
from .gateway import register_gateway
from .management import register_management
from .admin_auth import register_admin_auth
from . import capabilities as capability_registry, mcp

def create_app(config,start_worker=True,config_path=None):
    config=validate_config(config)
    capability_registry.validate_config(config)
    app=Flask(__name__);app.config['MAX_CONTENT_LENGTH']=48*1024*1024
    store=Store(config['database']);store.recover()
    app.extensions['core_store']=store;app.extensions['core_config']=config
    admin_authenticate=register_admin_auth(app,store)
    worker=runs.Worker(config,store,app.logger);app.extensions['core_worker']=worker
    if start_worker:worker.start()

    @app.before_request
    def auth():
        if request.path in ('/admin','/admin/') or request.path.startswith('/admin/assets/'):return
        if request.path.startswith('/admin/'):
            admin_authenticate()
            return
        if request.headers.get('Origin'): raise CoreError('SERVICE_BROWSER_ACCESS_FORBIDDEN',403)
        g.service=authenticate(config,request.headers.get('Authorization',''))

    @app.after_request
    def headers(response):
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        if request.path.startswith('/admin'):
            response.headers['Content-Security-Policy']="default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.errorhandler(CoreError)
    def error(exc):
        if request.path in ('/v1/models','/v1/chat/completions'):
            return jsonify({'error':{'message':exc.code,'type':'invalid_request_error' if exc.status<500 else 'upstream_error','code':exc.code}}),exc.status
        return jsonify({'code':exc.code,'message':exc.code,'retryable':False}),exc.status

    @app.errorhandler(HTTPException)
    def http_error(exc):return jsonify({'code':exc.name.upper().replace(' ','_'),'retryable':False}),exc.code

    def body():
        value=request.get_json(silent=True)
        if not isinstance(value,dict):raise CoreError('INVALID_REQUEST')
        return value

    @app.get('/health')
    def health():return jsonify({'status':'ok','service':'agent-core','storage':'sqlite','execution':'single-process','version':'1'})

    @app.get('/v1/capabilities')
    @app.get('/api/service/v1/capabilities')
    def capabilities():
        domains=['cad'] if request.path.startswith('/api/') else g.service.get('domains',[])
        entries={}
        for domain in domains:entries.update(registry.catalog(config,g.service,domain,agent=False))
        profile=model_profile(config,g.service)
        return jsonify({'contractVersion':'1','domains':domains,
                        **public_model_settings(config,g.service),
                        'reasoningToggle':thinking_control(profile.get('providerType','OPENAI'),profile['model']) is not None,
                        'tools':[{k:v for k,v in e.items() if k in ('name','version','description','effect','inputSchema','outputSchema')} for e in entries.values()]})

    @app.post('/api/service/v1/tools/execute')
    def execute_tool():
        value=body()
        if value.get('contractVersion','1')!='1':raise CoreError('UNSUPPORTED_CONTRACT_VERSION')
        context=authorize(g.service,value.get('context'),'cad')
        return jsonify(registry.execute(config,g.service,context,'cad',value.get('tool'),value.get('arguments'),value.get('requestId'),agent=False))

    @app.post('/api/service/v1/spaces')
    def create_space():return jsonify(knowledge.view(store.create_space(g.service,body()))),201

    @app.get('/api/service/v1/spaces/<sid>')
    @app.get('/api/service/v1/spaces/<sid>/snapshot')
    def space(sid):
        value=store.space(g.service,sid)
        return jsonify({**knowledge.view(value),'documents':[{k:d[k] for k in ('id','title','revision')} | {'chunkCount':len(d['chunks'])} for d in store.documents(sid)]})

    @app.delete('/api/service/v1/spaces/<sid>')
    def delete_space(sid):
        store.space(g.service,sid)
        with store.db() as db:db.execute('DELETE FROM spaces WHERE id=?',(sid,))
        return '',204

    @app.put('/api/service/v1/spaces/<sid>/documents/<doc_id>')
    def put_document(sid,doc_id):return jsonify(knowledge.put(store,store.space(g.service,sid),doc_id,body()))

    @app.delete('/api/service/v1/spaces/<sid>/documents/<doc_id>')
    def delete_document(sid,doc_id):
        value=store.space(g.service,sid)
        if value['kind']!='temporary':raise CoreError('PERSISTENT_SPACE_READ_ONLY',403)
        with store.db() as db:db.execute('DELETE FROM documents WHERE space_id=? AND id=?',(sid,doc_id))
        return '',204

    @app.post('/api/service/v1/spaces/<sid>/search')
    def search(sid):return jsonify(knowledge.search(store,store.space(g.service,sid),body()))

    @app.get('/api/service/v1/spaces/<sid>/package')
    def package(sid):return jsonify(knowledge.package(store,store.space(g.service,sid)))

    @app.post('/api/service/v1/spaces/<sid>/promote')
    def promote(sid):return jsonify(knowledge.view(knowledge.promote(store,g.service,store.space(g.service,sid),body()))),201

    @app.post('/v1/chat/runs')
    @app.post('/api/service/v1/runs')
    def create_run():
        domain='chat' if request.path=='/v1/chat/runs' else 'cad'
        value,request_id=runs.prepare(config,store,g.service,domain,body())
        row=store.enqueue(g.service,domain,value,request_id)
        return jsonify(runs.view(row)),202

    @app.get('/v1/runs/<rid>')
    @app.get('/api/service/v1/runs/<rid>')
    def get_run(rid):return jsonify(runs.view(store.run(rid,g.service)))

    @app.get('/v1/runs/<rid>/results/<path:operation_id>')
    @app.get('/api/service/v1/runs/<rid>/results/<path:operation_id>')
    def get_result(rid,operation_id):
        row=store.run(rid,g.service);record=store.result_record(rid,operation_id)
        builtins={'current_time','recall','list_documents','session_recall','session_list_documents','session_load_document','session_unload_document','session_read_result'}
        authorities=record.get('sourceTools',[record['tool']] if record['tool'] not in builtins else [])
        allowed=registry.catalog(config,g.service,row['domain'])
        if any(name not in allowed for name in authorities):raise CoreError('TOOL_REVOKED',403)
        return jsonify(record['result'])

    @app.post('/v1/runs/<rid>/cancel')
    @app.post('/api/service/v1/runs/<rid>/cancel')
    def cancel_run(rid):
        store.run(rid,g.service);store.cancel(rid)
        return jsonify(runs.view(store.run(rid,g.service)))

    @app.get('/v1/runs/<rid>/events')
    @app.get('/api/service/v1/runs/<rid>/events')
    def events(rid):
        service=g.service;store.run(rid,service)
        try:cursor=int(request.args.get('after',request.headers.get('Last-Event-ID','0')))
        except ValueError:raise CoreError('INVALID_EVENT_CURSOR') from None
        if cursor<0:raise CoreError('INVALID_EVENT_CURSOR')
        def stream():
            nonlocal cursor
            ping=0
            while True:
                try:
                    if not service.get('enabled',True):raise CoreError('SERVICE_REVOKED',403)
                    row=store.run(rid,service);events=store.events(rid,cursor)
                except CoreError as exc:
                    yield 'event: error\ndata: '+json.dumps({'code':exc.code,'retryable':False})+'\n\n';return
                for event in events:
                    cursor=event['seq'];payload=json.loads(event['payload'])
                    yield f"id: {cursor}\nevent: {event['name']}\ndata: "+json.dumps({**payload,'eventId':cursor},ensure_ascii=False)+'\n\n'
                if not events and row['status'] not in ('QUEUED','RUNNING'):return
                if row['status'] in ('QUEUED','RUNNING') and time.monotonic()-ping>10:
                    yield ': keepalive\n\n';ping=time.monotonic()
                time.sleep(.05)
        return Response(stream_with_context(stream()),mimetype='text/event-stream',headers={'X-Accel-Buffering':'no'})

    @app.post('/mcp/<server_id>')
    def mcp_endpoint(server_id):
        result,status=mcp.handle(config,g.service,server_id,body())
        return (jsonify(result),status) if result is not None else ('',status)

    @app.post('/admin/api/providers/<key_id>/pricing')
    def provider_pricing(key_id):
        from .provider_pricing import discover_pricing
        with app.extensions['core_configuration_state']['lock']:
            snapshot=copy.deepcopy(config)
        return jsonify(discover_pricing(snapshot,key_id))

    register_management(app,config,store,config_path)
    register_admin(app,config,store,config_path)
    register_gateway(app,config,store)
    return app
