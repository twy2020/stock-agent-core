"""Bounded lexical session knowledge, extracted from the SaaS plugin."""
import hashlib
import json
import math
import re
import time
import uuid
from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from .security import CoreError
from .store import encoded


class Chunk(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    id: str=Field(min_length=1,max_length=160)
    text: str=Field(min_length=1,max_length=12000)
    source: dict=Field(default_factory=dict)


class Document(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    title: str=Field(min_length=1,max_length=500)
    revision: str=Field(min_length=1,max_length=160)
    chunks: list[Chunk]=Field(min_length=1,max_length=1000)


def view(space):
    return {k:space[k] for k in ('id','context','kind','name')} | {
        'expiresAt':datetime.fromtimestamp(space['expires'],timezone.utc).isoformat() if space['expires'] else None}


def put(store,space,doc_id,body):
    if not isinstance(doc_id,str) or not 1<=len(doc_id)<=160: raise CoreError('INVALID_DOCUMENT_ID')
    if space['kind']!='temporary': raise CoreError('PERSISTENT_SPACE_READ_ONLY',403)
    try: value=Document.model_validate(body).model_dump()
    except ValidationError: raise CoreError('INVALID_DOCUMENT') from None
    if len({c['id'] for c in value['chunks']})!=len(value['chunks']): raise CoreError('DUPLICATE_CHUNK_ID')
    if any(len(encoded(c['source']).encode())>8192 for c in value['chunks']): raise CoreError('SOURCE_METADATA_LIMIT',413)
    if len(encoded(value).encode())>4*1024*1024: raise CoreError('DOCUMENT_SIZE_LIMIT',413)
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        others=[json.loads(r[0]) for r in db.execute('SELECT data FROM documents WHERE space_id=? AND id!=?',(space['id'],doc_id))]
        if len(others)>=100 or sum(len(c['text']) for d in [*others,value] for c in d['chunks'])>2_000_000: raise CoreError('CORPUS_LIMIT',413)
        db.execute('INSERT INTO documents VALUES(?,?,?) ON CONFLICT(space_id,id) DO UPDATE SET data=excluded.data',(space['id'],doc_id,encoded(value)))
    return {'id':doc_id,'chunkCount':len(value['chunks']),'revision':value['revision']}


def tokens(text):
    words=re.findall(r'[a-z0-9]+|[\u3400-\u9fff]+',text.lower())
    return [part for word in words for part in ([word] if word.isascii() or len(word)==1 else [word[i:i+2] for i in range(len(word)-1)])]


def search(store,space,body):
    question,top_k,ids=body.get('question'),body.get('topK',8),body.get('documentIds',[])
    if not isinstance(question,str) or not 1<=len(question.strip())<=8192 or type(top_k) is not int or not 1<=top_k<=20: raise CoreError('INVALID_SEARCH')
    if not isinstance(ids,list) or len(ids)>100 or any(not isinstance(i,str) for i in ids): raise CoreError('INVALID_DOCUMENT_IDS')
    query=set(tokens(question)); results=[]
    for doc in store.documents(space['id']):
        if ids and doc['id'] not in ids: continue
        for chunk in doc['chunks']:
            terms=tokens(chunk['text'])
            score=sum(1+math.log(terms.count(t)) for t in query if t in terms)/math.sqrt(len(terms)) if terms else 0
            if score: results.append({'documentId':doc['id'],'revision':doc['revision'],'title':doc['title'],
                                     'chunkId':chunk['id'],'text':chunk['text'],'source':chunk['source'],'score':score})
    results.sort(key=lambda r:(-r['score'],r['documentId'],r['chunkId']))
    return {'spaceId':space['id'],'mode':'lexical','results':results[:top_k]}


def package(store,space):
    return {'contractVersion':'1','format':'session-knowledge-v1','space':view(space),'documents':store.documents(space['id'])}


def promote(store,service,space,body):
    try: request_id=str(uuid.UUID(body['requestId']))
    except (ValueError,KeyError,TypeError,AttributeError): raise CoreError('INVALID_REQUEST_ID') from None
    name=body.get('name','')
    if not isinstance(name,str) or not 1<=len(name.strip())<=200: raise CoreError('INVALID_NAME')
    key=hashlib.sha256(f"{service['id']}:{service['tenantId']}:{request_id}".encode()).hexdigest()
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT * FROM spaces WHERE promotion_key=?',(key,)).fetchone()
        if old:
            if old['context']!=encoded(space['context']) or old['name']!=name: raise CoreError('IDEMPOTENCY_CONFLICT',409)
            sid=old['id']
        else:
            sid=str(uuid.uuid4())
            db.execute('INSERT INTO spaces(id,caller,tenant,context,kind,name,promotion_key) VALUES(?,?,?,?,?,?,?)',
                       (sid,service['id'],service['tenantId'],encoded(space['context']),'persistent',name,key))
            db.execute('INSERT INTO documents SELECT ?,id,data FROM documents WHERE space_id=?',(sid,space['id']))
    return store.space(service,sid)
