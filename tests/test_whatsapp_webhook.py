import hashlib
import hmac
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
import api.auth
from api.whatsapp import router, configuration, get_repo, MAX_BODY
from storage.database import initialise_schema, connection_scope
from storage.whatsapp_repository import WhatsAppRepository


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path/'test.db'
    initialise_schema(path)
    config = dict(enabled=True,secret='test-secret',verify='test-verify',waba='1713595540331361',phone='1383583781512437')
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[configuration] = lambda: config
    repo = WhatsAppRepository(path)
    app.dependency_overrides[get_repo] = lambda: repo
    monkeypatch.setattr(api.auth,'API_ACCESS_TOKEN','test-admin')
    return TestClient(app), config, repo, path


def payload(kind='text'):
    return {'object':'whatsapp_business_account','entry':[{'id':'1713595540331361','changes':[{'field':'messages','value':{'messaging_product':'whatsapp','metadata':{'phone_number_id':'1383583781512437'},'messages':[{'id':'wamid.test','from':'8613812345678','timestamp':'1700000000','type':kind,'text':{'body':'Untrusted quotation <script>alert(1)</script>'}}]}}]}]}


def send(c, value, raw=None, secret='test-secret'):
    raw = raw if raw is not None else json.dumps(value).encode()
    sig = 'sha256='+hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()
    return c.post('/webhooks/whatsapp',content=raw,headers={'Content-Type':'application/json','X-Hub-Signature-256':sig})


AUTH = {'Authorization':'Bearer test-admin'}


def test_verification_and_off(setup):
    c, config, repo, _ = setup
    params={'hub.mode':'subscribe','hub.verify_token':'test-verify','hub.challenge':'123456'}
    assert c.get('/webhooks/whatsapp',params=params).text=='123456'
    params['hub.verify_token']='wrong'
    assert c.get('/webhooks/whatsapp',params=params).status_code==403
    config['enabled']=False
    assert send(c,payload()).status_code==404
    assert c.get('/webhooks/whatsapp',params=params).status_code==404
    assert c.get('/whatsapp/inbound',headers=AUTH).status_code==404
    assert repo.list()==[]


def test_raw_signature_forgery_and_replay(setup,caplog):
    c, _, repo, _ = setup
    caplog.set_level('INFO', logger='api.whatsapp')
    assert send(c,payload(),secret='forged').status_code==401
    assert c.post('/webhooks/whatsapp',json=payload()).status_code==401
    raw=json.dumps(payload(),indent=2).encode()
    assert send(c,None,raw).json()=={'accepted':1}
    assert send(c,None,raw).json()=={'accepted':0}
    assert len(repo.list())==1
    assert repo.list()[0]['supplier_id'] is None
    assert 'Untrusted' not in caplog.text and '8613812345678' not in caplog.text


@pytest.mark.parametrize('mutation', ['account','phone','object','text','sender','messages','type'])
def test_strict_validation_atomic(setup,mutation):
    c, _, repo, _ = setup
    p=payload()
    value=p['entry'][0]['changes'][0]['value']
    if mutation=='account': p['entry'][0]['id']='disabled-account'
    if mutation=='phone': value['metadata']['phone_number_id']='447767990007'
    if mutation=='object': p['object']='other'
    if mutation=='text': value['messages'][0]['text']['body']=False
    if mutation=='sender': value['messages'][0]['from']='not-a-number'
    if mutation=='messages': value['messages']={}
    if mutation=='type': value['messages'][0]['timestamp']=123
    assert send(c,p).status_code==400
    assert repo.list()==[]


def test_size_json_and_unconfigured(setup):
    c,config,repo,_=setup
    assert send(c,None,b'x'*(MAX_BODY+1)).status_code==413
    assert send(c,None,b'{bad').status_code==400
    config['secret']=''
    assert send(c,payload()).status_code==503
    assert repo.list()==[]


def test_status_and_media_are_safe(setup):
    c,_,repo,_=setup
    p=payload('image')
    assert send(c,p).status_code==200
    assert repo.list()[0]['body'] is None
    p['entry'][0]['changes'][0]['value']['messages']=[]
    p['entry'][0]['changes'][0]['value']['statuses']=[{'id':'outbound','status':'delivered'}]
    assert send(c,p).json()=={'accepted':0}


def test_manual_association_and_retention(setup):
    c,_,repo,path=setup
    send(c,payload())
    assert c.get('/whatsapp/inbound').status_code==401
    assert c.patch('/whatsapp/inbound/1',json={'supplier_id':1,'rfq_id':'rfq'}).status_code==401
    with connection_scope(path) as conn:
        conn.execute("INSERT INTO suppliers(id,canonical_name) VALUES (1,'Factory')")
        conn.execute("INSERT INTO rfqs(id,payload) VALUES ('rfq','{}')")
    assert c.patch('/whatsapp/inbound/1',headers=AUTH,json={'supplier_id':2,'rfq_id':'rfq'}).status_code==404
    assert c.patch('/whatsapp/inbound/1',headers=AUTH,json={'supplier_id':1,'rfq_id':'rfq'}).status_code==200
    assert repo.list()[0]['rfq_id']=='rfq'
    with connection_scope(path) as conn:
        conn.execute("UPDATE whatsapp_inbound SET received_at=datetime('now','-31 days')")
    assert repo.list()==[]
    assert send(c,payload()).json()=={'accepted':0}
    with connection_scope(path) as conn:
        row=conn.execute('SELECT * FROM whatsapp_inbound').fetchone()
        assert row['sender'] is None and row['body'] is None and row['rfq_id'] is None


def test_database_failure_not_acknowledged(setup, monkeypatch):
    c,_,repo,_=setup
    def fail(messages):
        raise RuntimeError('storage unavailable')
    monkeypatch.setattr(repo,'store',fail)
    with pytest.raises(RuntimeError):
        send(c,payload())



def test_mixed_batch_rejected_before_writes(setup):
    c,_,repo,_=setup
    p=payload()
    p['entry'].append({'id':'unapproved','changes':p['entry'][0]['changes']})
    assert send(c,p).status_code==400
    assert repo.list()==[]


def test_shared_sender_remains_unmatched(setup):
    c,_,repo,path=setup
    with connection_scope(path) as conn:
        conn.execute("INSERT INTO suppliers(id,canonical_name,whatsapp) VALUES (1,'A','+8613812345678')")
        conn.execute("INSERT INTO suppliers(id,canonical_name,whatsapp) VALUES (2,'B','+8613812345678')")
    assert send(c,payload()).status_code==200
    assert repo.list()[0]['supplier_id'] is None
    assert repo.list()[0]['rfq_id'] is None


def test_migration_idempotent_existing_database(setup):
    _,_,_,path=setup
    with connection_scope(path) as conn:
        conn.execute('DROP TABLE whatsapp_inbound')
        conn.execute('DELETE FROM schema_migrations WHERE version=37')
    initialise_schema(path)
    initialise_schema(path)
    with connection_scope(path) as conn:
        assert conn.execute('SELECT count(*) FROM schema_migrations WHERE version=37').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM whatsapp_inbound').fetchone()[0]==0
