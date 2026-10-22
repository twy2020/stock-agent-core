"""SQLite owns only Core execution and temporary evidence, never business DBs."""
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from .security import CoreError, authorize
from .admin_safety import input_summary


def encoded(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


class Store:
    def __init__(self,path):
        self.path=str(path)
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        with self.db() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, caller TEXT NOT NULL, tenant TEXT NOT NULL,
                request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, domain TEXT NOT NULL, context TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
                cancelled INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, expires REAL NOT NULL,
                UNIQUE(caller,tenant,request_id));
              CREATE TABLE IF NOT EXISTS events(run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL, name TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id,seq));
              CREATE TABLE IF NOT EXISTS spaces(id TEXT PRIMARY KEY, caller TEXT NOT NULL, tenant TEXT NOT NULL,
                context TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'temporary', name TEXT NOT NULL DEFAULT '',
                expires REAL, promotion_key TEXT UNIQUE);
              CREATE TABLE IF NOT EXISTS documents(space_id TEXT REFERENCES spaces(id) ON DELETE CASCADE,
                id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(space_id,id));
            ''')

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally: db.close()

    def _event(self,db,rid,name,payload):
        seq=db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE run_id=?',(rid,)).fetchone()[0]
        db.execute('INSERT INTO events VALUES(?,?,?,?)',(rid,seq,name,encoded(payload)))

    def recover(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute("SELECT id FROM runs WHERE status='RUNNING'").fetchall():
                db.execute("UPDATE runs SET status='FAILED',payload='{}' WHERE id=?",(row['id'],))
                self._event(db,row['id'],'error',{'code':'WORKER_RESTARTED','retryable':False,'status':'FAILED'})

    def enqueue(self,service,domain,body,request_id):
        fingerprint=hashlib.sha256(encoded(body).encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not service.get('enabled',True): raise CoreError('SERVICE_REVOKED',403)
            old=db.execute('SELECT * FROM runs WHERE caller=? AND tenant=? AND request_id=?',(service['id'],service['tenantId'],request_id)).fetchone()
            if old:
                if old['fingerprint']!=fingerprint: raise CoreError('IDEMPOTENCY_CONFLICT',409)
                return dict(old)
            count=db.execute("SELECT COUNT(*) FROM runs WHERE caller=? AND tenant=? AND status IN ('QUEUED','RUNNING')",(service['id'],service['tenantId'])).fetchone()[0]
            if count>=min(8,max(1,service.get('maxActiveRuns',2))): raise CoreError('RUN_LIMIT',429)
            rid=str(uuid.uuid4());now=time.time()
            db.execute('INSERT INTO runs(id,caller,tenant,request_id,fingerprint,domain,context,payload,status,created,expires) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (rid,service['id'],service['tenantId'],request_id,fingerprint,domain,encoded(body['context']),encoded(body),'QUEUED',now,now+86400))
            self._event(db,rid,'progress',{'stage':'QUEUED','status':'QUEUED','elapsedMs':0})
            self._event(db,rid,'input',input_summary(body))
            return dict(db.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone())

    def run(self,rid,service=None):
        with self.db() as db: row=db.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
        if not row or row['expires']<=time.time() or service and (row['caller']!=service['id'] or row['tenant']!=service['tenantId']):
            raise CoreError('RUN_NOT_FOUND',404)
        value=dict(row)
        if service: authorize(service,json.loads(value['context']),value['domain'])
        return value

    def claim(self,rid):
        with self.db() as db:
            return db.execute("UPDATE runs SET status='RUNNING' WHERE id=? AND status='QUEUED' AND cancelled=0",(rid,)).rowcount==1

    def append(self,rid,name,payload):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT cancelled,status,content FROM runs WHERE id=?',(rid,)).fetchone()
            if not row or row['cancelled']: raise CoreError('CANCELLED')
            if row['status']!='RUNNING': raise CoreError('RUN_NOT_ACTIVE',409)
            content='' if name=='answer_reset' else row['content']+payload.get('content','') if name=='delta' else row['content']
            if len(content)>200000: raise CoreError('OUTPUT_LIMIT')
            db.execute('UPDATE runs SET content=? WHERE id=?',(content,rid))
            self._event(db,rid,name,payload)

    def finish(self,rid,code=None,outcome=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
            if not row or row['status'] not in ('RUNNING','QUEUED'): return
            if row['cancelled']: code='CANCELLED'
            if not code and not row['content'].strip(): code='EMPTY_RESPONSE'
            status='CANCELLED' if code=='CANCELLED' else 'FAILED' if code else 'COMPLETED'
            db.execute("UPDATE runs SET status=?,payload='{}' WHERE id=?",(status,rid))
            self._event(db,rid,'error' if code else 'complete',{'code':code,'retryable':False,'status':status} if code else {'content':row['content'],'status':status,**({'outcome':outcome} if outcome else {})})

    def cancel(self,rid):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT status FROM runs WHERE id=?',(rid,)).fetchone()
            if row['status'] in ('QUEUED','RUNNING'):
                db.execute("UPDATE runs SET cancelled=1,status='CANCELLED',payload='{}' WHERE id=?",(rid,))
                self._event(db,rid,'error',{'code':'CANCELLED','status':'CANCELLED','retryable':False})

    def events(self,rid,cursor):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 200',(rid,cursor))]

    def result(self,rid,operation_id):
        return self.result_record(rid,operation_id)['result']

    def result_record(self,rid,operation_id):
        with self.db() as db:
            row=db.execute("SELECT payload FROM events WHERE run_id=? AND name='tool_result' AND json_extract(payload,'$.operationId')=? ORDER BY seq DESC LIMIT 1",(rid,operation_id)).fetchone()
        if not row:raise CoreError('RESULT_NOT_FOUND',404)
        return json.loads(row['payload'])

    def queued(self):
        with self.db() as db: return [r[0] for r in db.execute("SELECT id FROM runs WHERE status='QUEUED' ORDER BY created LIMIT 2")]

    def purge(self):
        with self.db() as db:
            db.execute("DELETE FROM runs WHERE expires<? AND status NOT IN ('RUNNING','QUEUED')",(time.time(),))
            db.execute('DELETE FROM spaces WHERE expires IS NOT NULL AND expires<?',(time.time(),))

    def space(self,service,sid):
        with self.db() as db: row=db.execute('SELECT * FROM spaces WHERE id=? AND caller=? AND tenant=?',(sid,service['id'],service['tenantId'])).fetchone()
        if not row or row['expires'] is not None and row['expires']<=time.time(): raise CoreError('SPACE_NOT_FOUND',404)
        value=dict(row);value['context']=json.loads(value['context'])
        authorize(service,value['context'],'cad')
        return value

    def create_space(self,service,body):
        context=authorize(service,body.get('context'),'cad')
        ttl=body.get('ttlSeconds',3600)
        if type(ttl) is not int or not 60<=ttl<=86400: raise CoreError('INVALID_TTL')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT COUNT(*) FROM spaces WHERE caller=? AND tenant=? AND (expires IS NULL OR expires>?)',(service['id'],service['tenantId'],time.time())).fetchone()[0]>=32: raise CoreError('SPACE_LIMIT',429)
            sid=str(uuid.uuid4())
            db.execute('INSERT INTO spaces(id,caller,tenant,context,expires) VALUES(?,?,?,?,?)',(sid,service['id'],service['tenantId'],encoded(context),time.time()+ttl))
        return self.space(service,sid)

    def documents(self,sid):
        with self.db() as db: return [{'id':r['id'],**json.loads(r['data'])} for r in db.execute('SELECT * FROM documents WHERE space_id=? ORDER BY id',(sid,))]
