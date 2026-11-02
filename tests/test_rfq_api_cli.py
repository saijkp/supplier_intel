"""API and CLI RFQ integration tests with in-process transports only."""
import json
import pytest
from fastapi.testclient import TestClient
from click.testing import CliRunner
from tests.test_rfq_service import env

@pytest.fixture
def client(env,monkeypatch):
    import api.app as mod
    import api.auth
    monkeypatch.setattr(api.auth,'API_ACCESS_TOKEN','rfq-test')
    monkeypatch.setattr(mod,'get_repo',lambda:env.suppliers)
    monkeypatch.setattr(mod,'_rfq_service',lambda:env.s)
    def job(job_id,action,options):
        env.suppliers.mark_pipeline_job_running(job_id)
        result=env.s.send_approved(**options) if action=='send' else env.s.poll_inbox(**options)
        env.suppliers.mark_pipeline_job_completed(job_id,stats=result)
    monkeypatch.setattr(mod,'run_rfq_job',job)
    with TestClient(mod.app) as test_client:
        test_client.headers['Authorization']='Bearer rfq-test'
        yield test_client

@pytest.mark.parametrize('method,path,body',[
    ('GET','/rfq/status',None),('GET','/rfq/campaigns',None),('POST','/rfq/campaigns',{'title':'a','product':'b'}),
    ('GET','/rfq/campaigns/1',None),('PATCH','/rfq/messages/1',{'body':'text'}),
    ('POST','/rfq/campaigns/1/approve',{}),('POST','/rfq/campaigns/1/send',{}),('POST','/rfq/poll',{}),
    ('POST','/rfq/messages/1/mark-sent',{}),('POST','/rfq/messages/1/reply',{'text':'reply'}),
    ('POST','/rfq/campaigns/1/followups',{}),('GET','/rfq/campaigns/1/compare',None)])
def test_every_endpoint_requires_token(client,method,path,body):
    response=client.request(method,path,json=body,headers={'Authorization':'Bearer wrong'})
    assert response.status_code==401


def test_api_campaign_draft_edit_approve_send_poll_compare(client,env):
    response=client.post('/rfq/campaigns',json={'title':'New RFQ','product':'wheel','supplier_ids':[env.sid],'channels':['email']})
    assert response.status_code==200
    cid=response.json()['id'];assert response.json()['drafting']['drafted']==1
    assert client.get('/rfq/campaigns').json()[-1]['id']==cid
    message=client.get(f'/rfq/campaigns/{cid}').json()['messages'][0]
    mid=message['id']
    assert client.patch(f'/rfq/messages/{mid}',json={'body':'Edited request'}).status_code==200
    assert client.post(f'/rfq/campaigns/{cid}/approve',json={'message_ids':[mid]}).json()['approved']==1
    response=client.post(f'/rfq/campaigns/{cid}/send',json={})
    assert response.status_code==202
    job=env.suppliers.get_pipeline_job(response.json()['job_id'])
    assert job['status']=='completed'
    assert len(env.sender.messages)==1
    assert client.post('/rfq/poll',json={}).status_code==202
    assert client.post(f'/rfq/messages/{mid}/reply',json={'text':'No quote yet'}).json()['parse_status']=='no_quote'
    assert client.post(f'/rfq/campaigns/{cid}/followups',json={}).json()['drafted']==0
    assert client.get(f'/rfq/campaigns/{cid}/compare').json()['currencies']=={}
    assert cfg_secret() not in client.get('/rfq/status').text


def cfg_secret():
    from config import settings
    return settings.RFQ_EMAIL_PASSWORD


def test_api_bad_requests_and_assisted_sent(client,env):
    assert client.get('/rfq/campaigns/9999').status_code==400
    assert client.post('/rfq/campaigns',json={'product':'wheel'}).status_code==422
    assert client.post('/rfq/poll',json={'limit':0}).status_code==422
    assert client.patch(f'/rfq/messages/{env.mid}',json={'body':None}).status_code==400
    env.s.draft_messages(env.cid,supplier_ids=[env.sid],channels=['alibaba'])
    mid=env.repo.rows('messages',"channel='alibaba'")[0]['id']
    assert client.post(f'/rfq/messages/{mid}/mark-sent').status_code==200


def test_cli_confirmation_and_all_commands(env,monkeypatch):
    import rfq.cli as mod
    monkeypatch.setattr(mod,'service',lambda:env.s)
    runner=CliRunner()
    for command in ('create','list','show','approve','send','followups','poll','reply','mark-sent','compare','check','test-email'):
        assert runner.invoke(mod.rfq,[command,'--help']).exit_code==0
    env.s.approve(env.cid)
    result=runner.invoke(mod.rfq,['send',str(env.cid)],input='n\n')
    assert result.exit_code==0
    assert 'sales@factory.test' in result.output and '25' in result.output
    assert not env.sender.messages
    result=runner.invoke(mod.rfq,['send',str(env.cid),'--yes'])
    assert result.exit_code==0 and len(env.sender.messages)==1
    assert runner.invoke(mod.rfq,['check']).exit_code==0
    assert len(env.sender.messages)==1


def test_rfq_background_failure_does_not_expose_password(env,monkeypatch):
    import api.jobs as jobs
    import rfq.rfq_service as service_module
    monkeypatch.setattr(jobs,'SupplierRepository',lambda:env.suppliers)
    monkeypatch.setattr(service_module,'RFQService',lambda:env.s)
    env.s.poll_inbox=lambda **kw:(_ for _ in ()).throw(RuntimeError(cfg_secret()))
    env.suppliers.create_pipeline_job(job_id='rfq-failure',query='test',options={})
    jobs.run_rfq_job('rfq-failure','poll',{})
    job=env.suppliers.get_pipeline_job('rfq-failure')
    assert job['status']=='failed' and cfg_secret() not in json.dumps(job)
