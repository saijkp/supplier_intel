"""HTTP authorization and per-campaign control, with local test databases."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.auth
from api.rfq import router, get_rfq_repo
from api.rfq_workflow import router as workflow_router
from storage.database import initialise_schema
from storage.quote_workspace_repository import QuoteWorkspaceRepository
from storage.repository import SupplierRepository
from storage.rfq_repository import RFQRepository

AUTH={'Authorization':'Bearer workflow-test'}


@pytest.fixture
def http(tmp_path,monkeypatch):
    path=tmp_path/'http.db'; initialise_schema(path)
    monkeypatch.setattr(api.auth,'API_ACCESS_TOKEN','workflow-test')
    from config import settings
    monkeypatch.setattr(settings,'RFQ_SEND_ENABLED',False)
    monkeypatch.setattr(settings,'OPENAI_API_KEY',None)
    monkeypatch.setattr(settings,'RFQ_IMAP_HOST','')
    sid=SupplierRepository(path).create_golden_record({'canonical_name':'Test factory','primary_email':'factory@test.test'})
    app=FastAPI();app.include_router(workflow_router);app.include_router(router)
    app.dependency_overrides[get_rfq_repo]=lambda:QuoteWorkspaceRepository(path)
    client=TestClient(app)
    rfq=client.post('/rfqs',headers=AUTH,json={'title':'Lights','quantity':'100','unit':'pcs','mandatory_specs':{'material':'aluminium'}}).json()
    return client,rfq['id'],sid,path


@pytest.mark.parametrize('path', ['/rfqs/outreach/campaigns','/rfqs/nope/outreach'])
def test_workflow_requires_token(http,path):
    client,_,_,_=http
    assert client.get(path).status_code==401


def test_create_link_and_ingest_reply(http):
    client,rid,sid,path=http
    response=client.post(f'/rfqs/{rid}/outreach/create',headers=AUTH,json={'supplier_ids':[sid]})
    assert response.status_code==200,response.text
    view=response.json();assert view['linked']
    assert view['policy']['auto_send']==0
    assert 'lease_token' not in view['policy']
    mid=view['campaign']['messages'][0]['id']
    RFQRepository(path).update('messages',mid,status='sent',sent_at='2000-01-01 00:00:00')
    result=client.post(f'/rfqs/{rid}/outreach/messages/{mid}/reply',headers=AUTH,json={'text':'unit_price: 18\ncurrency: USD\nmaterial: aluminium'})
    assert result.status_code==200,result.text
    assert not result.json().get('error')
    q=client.get('/rfqs/'+rid,headers=AUTH).json()['quotes'][0]
    assert q['supplier_id']==sid and q['fields']['unit_price']=='18'
    assert q['review']['status']=='clarification_required'
    assert len(client.get(f'/rfqs/{rid}/outreach',headers=AUTH).json()['clarifications'])==1
    assert client.post(f'/rfqs/{rid}/outreach/messages/{mid}/send',headers=AUTH,json={}).status_code==422


def test_policy_requires_config_and_scope_and_has_hard_bounds(http):
    client,rid,sid,_=http
    client.post(f'/rfqs/{rid}/outreach/create',headers=AUTH,json={'supplier_ids':[sid]})
    assert client.post(f'/rfqs/{rid}/outreach/policy',headers=AUTH,json={'poll_enabled':True,'auto_send':True}).status_code==422
    assert client.post(f'/rfqs/{rid}/outreach/policy',headers=AUTH,json={'max_rounds':99}).status_code==422
    assert client.post(f'/rfqs/{rid}/outreach/policy',headers=AUTH,json={'followup_hours':0}).status_code==422
    assert client.post(f'/rfqs/{rid}/outreach/policy',headers=AUTH,json={'poll_enabled':True}).status_code==422
    assert client.post(f'/rfqs/{rid}/outreach/policy',headers=AUTH,json={'poll_enabled':False,'auto_send':False}).status_code==200


def test_cross_rfq_message_access_is_rejected(http):
    client,rid,sid,path=http
    view=client.post(f'/rfqs/{rid}/outreach/create',headers=AUTH,json={'supplier_ids':[sid]}).json()
    mid=view['campaign']['messages'][0]['id']
    other=client.post('/rfqs',headers=AUTH,json={'title':'Other','quantity':'100'}).json()['id']
    assert client.post(f'/rfqs/{other}/outreach/messages/{mid}/reply',headers=AUTH,json={'text':'moq: 10'}).status_code==422
    assert client.post(f'/rfqs/{other}/outreach/messages/{mid}/send',headers=AUTH,json={}).status_code==422
    assert client.post(f'/rfqs/{other}/quotes',headers=AUTH,json={'supplier_name':'Not linked','supplier_id':sid}).status_code==422
