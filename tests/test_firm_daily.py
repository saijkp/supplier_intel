"""No network: scheduled research, identity and role regression checks."""
import json
from datetime import datetime
from types import SimpleNamespace as NS
import pytest
from tests.test_planning import service
from planning.firm_trial import role_priority, verify_site, sanitize_contacts
from planning.firm_daily import tick, health, authorities, export_contacts
from customers.mailboxes import UK

@pytest.mark.parametrize('role', ['Partner and Finance Director','Operations Director','HR Director','Marketing Partner','Business Development Director','IT Director','Practice Manager','Office Manager','Accounts Director'])
def test_non_design_excluded(role):
    assert role_priority({'role': role}) == 99


def test_role_order():
    roles = ['Design Director','Managing Director','Director','Partner','Principal','Founder','Owner','Associate Director','Associate','Architect','Interior Designer']
    assert [role_priority({'role': r}) for r in roles] == [0]*7+[1,2,3,3]

@pytest.mark.parametrize('legal', ['Company number 87654321', 'Registered office: 8 Wrong Road, SW1A 1AA'])
def test_legal_mismatch_rejected(legal):
    profile = NS(company_number='12345678',registered_office_address='1 Right Road M7 1AL')
    assert verify_site(NS(get=lambda _: 'Studio Architects London '+legal), 'studio.example','Studio Architects',profile) is None


def test_matching_legal_identity():
    assert verify_site(NS(get=lambda _: 'Studio Architects London Registered no. 12345678 Registered office: M7 1AL'), 'studio.example','Studio Architects', NS(company_number='12345678',registered_office_address='M7 1AL'))


def test_finance_review_retains_credit():
    d={'reveals_used':1,'people':[{'name':'Kevin Hobbs','role':'Partner and Finance Director','email_evidence':{'email':'kevin@example.com'}}]}
    sanitize_contacts(d,'Holder Mathias')
    assert d['review_status']=='needs_review' and d['reveals_used']==1
    assert d['field_provenance']['role_review']=='revealed contact is finance role; consider design partner'


def test_rotation():
    from planning.firm_daily import all_areas
    selected=[authorities(datetime(2026,11,9+i).date()) for i in range(5)]
    assert sorted(a for group in selected for a in group)==sorted(all_areas())
    assert all(selected)


def test_daily_disabled_due_and_claim(service,monkeypatch):
    calls=[]
    service.outreach.now=lambda: datetime(2026,11,13,8,29,tzinfo=UK)
    assert tick(service)['status']=='disabled'
    monkeypatch.setenv('PLANNING_FIRM_TRIAL_SCHEDULE_ENABLED','true')
    assert tick(service)['status']=='outside_window'
    service.outreach.now=lambda: datetime(2026,11,13,8,30,tzinfo=UK)
    service.sync_recent=lambda **kw: calls.append(kw) or {'complete':False,'errors':{'Stockport':'Retry-After'}}
    class FakeTrial:
        def __init__(self,p): pass
        def run(self,**kw): calls.append(kw)
    assert tick(service,FakeTrial)['status']=='partial'
    assert calls[0]['trial_only'] is True
    assert calls[1]=={'limit':100,'max_searches':40,'max_reveals':20}
    assert tick(service,FakeTrial)['status']=='already_attempted'
    assert health(service.repo,service.outreach.now())['people_target']==100


def test_export_candidates(service,tmp_path):
    service.repo.execute("INSERT INTO planning_firm_trials(firm_key,company,payload) VALUES(?,?,?)",('name:example','Example',json.dumps({'review_status':'needs_review','field_provenance':{'website_candidate':'https://example.com/','people_candidates':[{'name':'Alex Smith','role':'Partner','email':'alex@example.com'}]}})))
    output=tmp_path/'contacts.csv'; export_contacts(service.repo,output)
    assert 'Alex Smith' in output.read_text(encoding='utf-8-sig') and 'needs_review' in output.read_text(encoding='utf-8-sig')


def test_trial_sync_never_creates_accounts_or_enrols(service):
    from tests.test_planning import project
    pid=service.store(project(),trial_only=True)
    assert service.repo.rows('SELECT * FROM prospect_accounts') == []
    row=service.repo.rows('SELECT * FROM planning_projects WHERE id=?',(pid,))[0]
    assert row['agent_lookup_attempted_at'] and row['agent_lookup_status']=='trial_review'
    from outreach.always_on import AlwaysOnService
    loop=AlwaysOnService(service.outreach,planning=service)
    loop.identity_pending(); loop.queue_projects()
    assert service.repo.rows('SELECT * FROM outreach_project_queue') == []


def test_planit_429_stops_rotation(service,monkeypatch):
    from planning.planit_client import RateLimited
    calls=[]
    class Fake:
        def recent(self,*args):
            calls.append(args[0]); raise RateLimited('Retry-After: 300')
    service.client=Fake()
    result=service.sync_recent(['Westminster','Lambeth'],trial_only=True)
    assert not result['complete'] and calls==['Westminster']


def test_new_york_is_not_uk():
    assert verify_site(NS(get=lambda _: 'Purcell Architects New York NY 10018 United States'), 'purcelleverett.com','Purcell Architects') is None


def test_daily_failure_is_durable(service,monkeypatch):
    monkeypatch.setenv('PLANNING_FIRM_TRIAL_SCHEDULE_ENABLED','true')
    service.outreach.now=lambda: datetime(2026,11,13,9,tzinfo=UK)
    def fail(**kw): raise RuntimeError('fake')
    service.sync_recent=fail
    assert tick(service)['status']=='failed'
    assert health(service.repo,service.outreach.now())['today']['error']=='RuntimeError'
    assert tick(service)['status']=='already_attempted'


def test_weekend_and_summer_time(service,monkeypatch):
    monkeypatch.setenv('PLANNING_FIRM_TRIAL_SCHEDULE_ENABLED','true')
    service.outreach.now=lambda: datetime(2026,11,14,9,tzinfo=UK)
    assert tick(service)['status']=='outside_window'
    from datetime import timezone
    service.outreach.now=lambda: datetime(2026,7,13,7,29,tzinfo=timezone.utc)
    assert tick(service)['status']=='outside_window'


def test_five_day_totals(service):
    for day in ['2026-11-09','2026-11-13']:
        service.repo.execute("INSERT INTO planning_firm_daily(day,started_at,status,payload) VALUES(?,?,'completed',?)",(day,day,json.dumps({'counters':{'named_people':12,'reveals':6}})))
    h=health(service.repo,datetime(2026,11,13,9,tzinfo=UK))
    assert h['five_day_totals']['named_people_any']==24 and h['people_remaining']==100
    assert h['five_day_totals']['verified_people']==0
    assert h['today']['counters']['reveals']==6


def test_resume_authorities_and_durable_cooldown(service,monkeypatch):
    from planning.planit_client import PlanItClient
    monkeypatch.setattr(PlanItClient,'_retry_at',0)
    monkeypatch.setenv('PLANNING_FIRM_TRIAL_SCHEDULE_ENABLED','true')
    service.outreach.now=lambda: datetime(2026,11,13,9,tzinfo=UK)
    previous={'authorities':['Westminster','Lambeth','Barnet'],'sync':{'errors':{'Lambeth':'429'}},'planit_retry_at':0}
    service.repo.execute("INSERT INTO planning_firm_daily(day,started_at,status,payload) VALUES('2026-11-12','2026-11-12','partial',?)",(json.dumps(previous),))
    calls=[]
    service.sync_recent=lambda **kw: calls.append(kw) or {'complete':True}
    fake=lambda p: NS(run=lambda **kw: None)
    tick(service,fake)
    assert calls[0]['areas'][:2]==['Lambeth','Barnet']
    assert 'Westminster' not in calls[0]['areas']
    service.outreach.now=lambda: datetime(2026,11,16,9,tzinfo=UK)
    service.repo.execute("UPDATE planning_firm_daily SET payload=? WHERE day='2026-11-13'",(json.dumps({'planit_retry_at':service.outreach.now().timestamp()+3600}),))
    assert tick(service,fake)['status']=='planit_cooldown'


def test_identity_rejection_has_page_evidence():
    evidence={}
    assert verify_site(NS(get=lambda _: 'Studio Architects London Registered no. 87654321'),'studio.example','Studio Architects',NS(company_number='12345678'),evidence) is None
    assert evidence['pages'][0]['source_url']=='https://studio.example/'
    assert '87654321' in evidence['pages'][0]['text']


def test_holder_not_revealed_again(service,monkeypatch):
    from planning.firm_trial import FirmTrial
    monkeypatch.setenv('OUTREACH_APOLLO_REVEAL_ENABLED','true')
    def forbidden(*a,**kw): raise AssertionError('No additional credit or search')
    trial=FirmTrial(service,apollo=NS(search=forbidden,reveal=forbidden))
    data={'reveals_used':1,'website_verified':True,'CH_verified':True,'website':'https://holdermathias.com/','classification':'architect','people':[{'name':'Kevin Hobbs','role':'Partner and Finance Director','email_evidence':{'email':'kevin@holdermathias.com'}}]}
    trial.reveal_best('holder','Holder Mathias',data,20)
    assert trial.reveals_used==0 and data['review_status']=='needs_review'


def test_site_preserves_full_design_and_finance_titles():
    from planning.firm_trial import site_people
    people,_=site_people(NS(get=lambda _: '<article>Alex Smith Design Director</article><article>Kevin Hobbs Partner and Finance Director</article>'),'https://example.com/')
    assert people[0]['name']=='Alex Smith' and people[0]['role']=='Design Director'
    assert people[1]['role']=='Partner and Finance Director' and role_priority(people[1])==99


def test_schema_51_upgrade(service):
    from storage.database import initialise_schema,get_schema_version
    service.repo.execute('DROP TABLE planning_firm_daily')
    service.repo.execute('DELETE FROM schema_migrations WHERE version=52')
    initialise_schema(service.repo.db_path)
    initialise_schema(service.repo.db_path)
    assert get_schema_version(service.repo.db_path)==52
    assert service.repo.rows('SELECT * FROM planning_firm_daily')==[]


def target_firm(service, key='ch:12345678', company_number='12345678', **changes):
    data = dict(classification='architect', CH_verified=True, website_verified=True,
                website='https://studio.example/', people=[dict(name='Alex Smith', role='Design Director', email='alex@studio.example', source='site')])
    data.update(changes)
    service.repo.execute("INSERT INTO planning_firm_trials(firm_key,company,company_number,attempted_at,payload) VALUES(?,?,?,?,?)",
                         (key, 'Studio Architects', company_number, '2026-11-13T09:00:00+00:00', json.dumps(data)))


@pytest.mark.parametrize('changes', [
    {'classification':'design_build_contractor'}, {'CH_verified':False}, {'website_verified':False},
    {'field_provenance':{'identity_review':{'reason':'mismatch'}}},
    *[{'people':[dict(name=name, role=role, email=email, source='site')]} for name,role,email in [
        ('Alex Smith','Finance Director','alex@studio.example'), ('Alex Smith','Operations Director','alex@studio.example'),
        ('Alex Smith','Architect','info@studio.example'), ('Alex Smith','Architect','alex@other.example'),
        ('Alex Smith','Architect',None), ('Our Team','Architect','team@studio.example')]],
    {'people':[dict(name='SMITH, Alex',role='director',source='companies_house')]},
])
def test_target_requires_every_gate(service, changes):
    target_firm(service, **changes)
    h=health(service.repo, datetime(2026,11,13,10,tzinfo=UK))
    assert h['people_remaining']==100
    assert h['five_day_totals']['verified_people']==0


def test_target_unique_and_export(service,tmp_path):
    import csv
    target_firm(service)
    # A name-keyed alias row cannot repeat the CH number (UNIQUE column).
    target_firm(service, 'name:alias', company_number=None, people=[dict(name=' ALEX  SMITH ',role='Partner',email='alex@studio.example',source='site')])
    h=health(service.repo,datetime(2026,11,13,10,tzinfo=UK))
    assert h['people_remaining']==99
    assert h['today']['counters']['verified_people']==1
    assert h['current_inventory']['verified_people']==1
    output=tmp_path/'target.csv'; export_contacts(service.repo,output)
    with output.open(encoding='utf-8-sig') as stream:
        flags=[r['counts_toward_target'] for r in csv.DictReader(stream)]
    # The same person via an alias row counts once toward the target.
    assert flags.count('True')==1


def test_late_enable_survives_restart_until_monday(service,monkeypatch):
    monkeypatch.setenv('PLANNING_FIRM_TRIAL_SCHEDULE_ENABLED','true')
    calls=[]
    service.sync_recent=lambda **kw: calls.append(kw) or {'complete':True}
    fake=lambda p: NS(run=lambda **kw: None)
    service.outreach.now=lambda: datetime(2026,11,13,12,1,tzinfo=UK)
    assert tick(service,fake)['first_run_at']=='2026-11-16T08:30:00+00:00'
    service.outreach.now=lambda: datetime(2026,11,13,16,tzinfo=UK)
    assert tick(service,fake)['status']=='outside_window'
    assert not calls
    service.outreach.now=lambda: datetime(2026,11,16,8,30,tzinfo=UK)
    assert tick(service,fake)['status']=='completed'
    assert tick(service,fake)['status']=='already_attempted'


def test_qualification_improvement_dedup_across_days(service):
    target_firm(service)
    for day in ['2026-11-12','2026-11-13']:
        service.repo.execute("INSERT INTO planning_firm_daily(day,started_at,status,payload) VALUES(?,?,'completed',?)",
                             (day,day,json.dumps({'verified_people_keys':['12345678:alex smith'], 'counters':{'serp_calls':3,'reveals':2}})))
    h=health(service.repo,datetime(2026,11,13,10,tzinfo=UK))
    assert h['five_day_totals']['verified_people']==1
    assert h['five_day_totals']['est_cost_gbp']==0.06
    assert h['five_day_totals']['apollo_credits']==4
