"""Offline planning integration tests: no paid calls, production DB or real sends."""
import json
from datetime import date, datetime, timezone
import httpx
import pytest
from planning.planit_client import PlanItClient, Inconclusive, RateLimited
from planning.relevance import fit
from planning.service import PlanningService
from planning.import_batch import import_batch
from planning.worker import due
from outreach.repository import OutreachRepository
from outreach.service import OutreachService
from storage.database import initialise_schema


@pytest.fixture
def service(tmp_path):
    path=tmp_path/'planning.db'
    initialise_schema(path)
    class Council:
        def fetch(self, url):
            return {'evidence': [], 'agent_role': 'unknown'}
    class Registry:
        def search_companies(self, name): return []
    return PlanningService(OutreachService(OutreachRepository(path),now=lambda:datetime(2026,11,11,9,tzinfo=timezone.utc),send_enabled=False), council=Council(), companies_house=Registry())


def project(**kw):
    return dict(authority='Salford',reference='26/100',address='X Road',description='Erection of 24 flats',agent='Studio Architects',agent_company='Studio Architects',applicant='Developer Ltd',source_url='https://council.example/26/100',**kw)


def eligible(service, cid):
    contact = service.repo.get('prospect_contacts', cid)
    aid = contact['account_id']
    service.repo.execute("UPDATE prospect_accounts SET domain='studio.example', companies_house_number='00123456', subscriber_type='corporate', flagged=0 WHERE id=?", (aid,))
    service.outreach.review_contact(cid, 'corporate_verified')
    service.outreach.event(aid, 'planning corporate verification', {'company_number': '00123456', 'source_url': 'https://find-and-update.company-information.service.gov.uk/company/00123456'})


@pytest.mark.parametrize('text,expected',[
    ('Erection of 24 flats',True),('Erection of 4 houses',True),
    ('Householder extension of dwelling',False),('Discharge of conditions for 24 flats',False),
    ('Conversion to HMO',True),('New student accommodation',True),
    ('New hotel',True),('New care home',True),('Office fit-out',True),
    ('Luxury house with full fit-out',True),('Luxury house extension',False),
    ('Signage for hotel',False),('Telecoms installation',False),
])
def test_fit_evidence(text,expected):
    score,reasons,units=fit(text)
    assert bool(score)==expected
    assert all(r['evidence'] in text for r in reasons)


def test_sync_is_idempotent_and_errors_are_inconclusive(service):
    class Fake:
        def recent(self,area,start,end):
            if area=='Broken':raise Inconclusive('timeout')
            return [{'uid':'26/100','description':'24 flats','address':'X Road','agent':'Studio','applicant':'Builder','url':'https://council.example'}]
    service.client=Fake()
    result=service.sync_recent(['Salford','Broken'],180)
    assert not result['complete'] and result['errors']['Broken']=='timeout'
    service.sync_recent(['Salford'],180)
    assert len(service.projects())==1
    assert len(service.repo.rows('SELECT * FROM prospect_accounts'))==1
    assert not due(service)


def test_client_rate_limit_pagination_and_bounded_windows():
    requests=[]; sleeps=[]
    def respond(request):
        requests.append(request)
        offset=int(request.url.params['index'])
        return httpx.Response(200,json={'records':[{'uid':str(len(requests))}], 'total':2 if offset==0 else 2})
    client=PlanItClient(httpx.Client(transport=httpx.MockTransport(respond)),sleeps.append)
    assert len(client.recent('Salford',date(2026,1,1),date(2026,6,29)))==8
    assert sleeps==[]
    assert requests[0].url.params['end_date']=='2026-03-31'


@pytest.mark.parametrize('mode',['timeout','truncated','malformed','429'])
def test_failures_never_return_empty_success(mode):
    def respond(request):
        if mode=='timeout':raise httpx.ReadTimeout('timeout')
        if mode=='429':return httpx.Response(429)
        return httpx.Response(200,json={'records':[],'total':1} if mode=='truncated' else {})
    with pytest.raises(Inconclusive):
        PlanItClient(httpx.Client(transport=httpx.MockTransport(respond)),retries=0).recent('Salford',date(2026,1,1),date(2026,1,1))


def test_identity_priority_and_preserves_trusted_values(service):
    service.outreach.import_rows([{'company':'Existing Studio','number':'00123456','website':'https://studio.example','postcode':'M1 1AA'}],'manual')
    aid=service.repo.rows('SELECT id FROM prospect_accounts')[0]['id']
    assert service.account('Different Label','architect','https://register',{'companies_house_number':'00123456'})==aid
    assert service.account('Another Label','architect','https://register',{'website':'https://studio.example'})==aid
    assert service.account('Existing Studio','architect','https://register')==aid
    assert service.repo.get('prospect_accounts',aid)['company']=='Existing Studio'


def test_project_enrolment_snapshot_and_approval_gate(service):
    pid=service.store(project())
    service.outreach.import_rows([{'company':'Studio Architects','contact_name':'Alex','email':'alex@studio.example'}],'manual','https://studio.example/team')
    cid=service.repo.rows('SELECT id FROM prospect_contacts')[0]['id']
    eligible(service, cid)
    seq=service.enrol(pid,cid,'sj@ikeng.online')
    assert seq['approved_at'] is None
    assert service.enrol(pid,cid,'sj@ikeng.online')['id']==seq['id']
    eid=seq['contacts'][0]['id']
    service.store({**project(),'description':'Erection of 30 flats'})
    row={'id':eid,'name':'Alex','source_url':'https://team','company':'Studio Architects','account_source':'planning','area':None,'project_hint':None,'project_hint_source':None}
    assert service.outreach._values(row)['project_summary']=='Erection of 24 flats'
    assert service.outreach.run_due()['sent']==0
    other=service.repo.rows('SELECT id FROM prospect_accounts WHERE segment=?',('developer',))[0]['id']
    service.repo.execute('UPDATE prospect_contacts SET account_id=? WHERE id=?',(other,cid))
    with pytest.raises(ValueError,match='does not belong'):service.enrol(pid,cid,'sj@ikeng.online')


def test_historical_first_touch_block_cannot_be_bypassed(service):
    pid=service.store(project())
    service.outreach.import_rows([{'company':'Studio Architects','email':'alex@studio.example'}],'manual')
    cid=service.repo.rows('SELECT id FROM prospect_contacts')[0]['id']
    eligible(service, cid)
    service.repo.execute('INSERT INTO planning_first_touch_exclusions VALUES(?,?)',('alex@studio.example','sent log'))
    with pytest.raises(ValueError,match='Already contacted'):service.enrol(pid,cid,'sj@ikeng.online')
    seq=service.outreach.save_sequence('Generic','architect',[{'channel':'email','day_offset':0,'subject':'Hello','template':'Hello'}])
    with pytest.raises(ValueError,match='Historical'):service.outreach.enrol(seq['id'],[cid],'sj@ikeng.online')


def test_first_batch_import_is_local_idempotent_unreviewed(service):
    # Synthetic batch: the real one holds contact emails and stays outside the public repo.
    batch={'provenance':'test','first_touch_exclusions':['sent@prp.example','prp.example'],
           'projects':[{'authority':'Brent','reference':f'26/{n}','applicant':f'Applicant {n} Ltd','agent':f'Agent {n} Architects',
                        'address':f'{n} Test Road','description':'Erection of 12 flats','status':'Application Received',
                        'validated_date':'2026-10-01','decided_date':None,'source_url':f'https://planning.example/{n}',
                        'contact':{'role':'architect','name':f'Alex {n}','email':f'alex{n}@agent{n}.example','source_url':f'https://agent{n}.example'}}
                       for n in range(1,4)]}
    import_batch(service,batch)
    first=service.repo.rows('SELECT * FROM planning_projects')
    import_batch(service,batch)
    assert len(first)==len(service.repo.rows('SELECT * FROM planning_projects'))==3
    contacts=service.repo.rows('SELECT * FROM prospect_contacts')
    assert contacts and all(c['email_status']=='unreviewed' for c in contacts)
    assert not service.repo.rows('SELECT * FROM outreach_enrolments')
    assert service.repo.rows('SELECT 1 FROM planning_first_touch_exclusions WHERE identifier=?',('sent@prp.example',))

def test_v45_upgrade_and_candidate_changes_preserve_trusted_fields(service):
    from storage.database import connection_scope, get_schema_version
    pid=service.store(project())
    with connection_scope(service.repo.db_path) as c:
        c.execute('DELETE FROM schema_migrations WHERE version=46')
    initialise_schema(service.repo.db_path)
    assert get_schema_version(service.repo.db_path)==51
    service.store({**project(),'description':'30 flats','address':'Changed address'})
    row=service.repo.rows('SELECT * FROM planning_projects WHERE id=?',(pid,))[0]
    assert row['description']=='Erection of 24 flats' and row['address']=='X Road'
    assert len(service.repo.rows('SELECT * FROM planning_project_observations'))==2


def test_planning_api_auth_bounds_and_drafts(service,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.planning import router
    from api.outreach import get_service
    import api.auth
    monkeypatch.setattr(api.auth,'API_ACCESS_TOKEN','planning-test')
    app=FastAPI();app.include_router(router)
    app.dependency_overrides[get_service]=lambda:service.outreach
    client=TestClient(app)
    assert client.get('/planning/projects').status_code==401
    client.headers['Authorization']='Bearer planning-test'
    pid=service.store(project())
    service.outreach.import_rows([{'company':'Studio Architects','email':'hello@studio.example'}],'manual')
    cid=service.repo.rows('SELECT id FROM prospect_contacts')[0]['id']
    assert client.get('/planning/projects').json()[0]['reference']=='26/100'
    assert client.get('/planning/projects?days=0').status_code==422
    assert client.post('/planning/sync',json={'areas':['Salford'],'days':181}).status_code==422
    eligible(service, cid)
    seq=client.post(f'/planning/projects/{pid}/enrol',json={'contact_id':cid}).json()
    assert seq['approved_at'] is None and seq['contacts']
    assert not service.repo.rows('SELECT * FROM outreach_events WHERE kind=?',('email',))

def test_documented_planit_nested_company_and_dates(service):
    class Fake:
        def recent(self,*args):
            return [{'uid':'26/100','description':'24 flats','url':'https://council.example/26/100',
                     'decided_date':'2026-11-01', 'other_fields':{'agent_company':'Studio',
                     'applicant_company':'Builder','agent_name':'Available in source',
                     'date_validated':'2026-09-10','application_type':'Full','status':'Granted'}}]
    service.client=Fake()
    assert service.sync_recent(['Salford'])['complete']
    row=service.repo.rows('SELECT * FROM planning_projects')[0]
    assert row['agent']=='Studio' and row['applicant']=='Builder'
    assert row['validated_date']=='2026-09-10' and row['decided_date']=='2026-11-01'
    assert row['status']=='Granted'

def test_imported_history_blocks_an_already_active_enrolment(service):
    service.outreach.import_rows([{'company':'Studio','contact_name':'Alex','email':'alex@studio.example','number':'00123456'}],'manual')
    cid=service.repo.rows('SELECT id FROM prospect_contacts')[0]['id']
    service.outreach.review_contact(cid,'corporate_verified')
    seq=service.outreach.save_sequence('Existing','architect',[{'channel':'email','day_offset':0,'subject':'Hello','template':'Hello'}])
    service.outreach.enrol(seq['id'],[cid],'sj@ikeng.online')
    service.outreach.approve(seq['id'],'Sai','Reviewed professional relevance')
    service.repo.execute('INSERT INTO planning_first_touch_exclusions VALUES(?,?)',('studio.example','sent log imported later'))
    service.outreach.run_due()
    assert service.repo.rows('SELECT status FROM outreach_enrolments')[0]['status']=='stopped'
    assert not service.repo.rows('SELECT * FROM outreach_events WHERE kind=?',('email',))


@pytest.mark.parametrize('text',['12 units for machinery storage','Installation of 8 units'])
def test_nonresidential_unit_counts_do_not_qualify(text):
    assert fit(text)[0]==0

@pytest.mark.parametrize('text',['Erection of 24 residential flats with removal of trees', 'Erection of 4 houses and 6 flats', 'Erection of 6 no. residential dwellings'])
def test_residential_counts_and_incidental_tree_work(text):
    assert fit(text)[0]>0
    assert all(r['evidence'] in text for r in fit(text)[1])


@pytest.fixture(autouse=True)
def reset_planit_cooldown(monkeypatch):
    monkeypatch.setattr(PlanItClient, '_retry_at', 0)
