"""Offline fixtures only. Claude runs this with the full suite."""
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import httpx
import pytest
from storage.database import initialise_schema
from outreach.repository import OutreachRepository
from outreach.service import OutreachService
from planning.service import PlanningService
from planning.council import parse
from planning.portal_agents import AgentTrial, TrialHTTP, Unavailable, classify, merge_trusted, site_identity, trial_report

FIXTURES = Path(__file__).parent/'fixtures'/'council_agents'

@pytest.fixture
def planning(tmp_path):
    path = tmp_path/'trial.db'
    initialise_schema(path)
    registry = SimpleNamespace(search_companies=lambda name: [], get_company_profile=lambda number: None)
    return PlanningService(OutreachService(OutreachRepository(path), now=lambda:datetime(2026,11,13,10,tzinfo=timezone.utc), send_enabled=False), companies_house=registry)


def insert(planning, authority='Westminster', name=None):
    return planning.repo.execute("INSERT INTO planning_projects(authority,reference,fit_score,fit_reasons,first_seen_at,source_url,agent_name) VALUES(?,?,1,'[]',?,?,?)", (authority,str(len(planning.repo.rows('SELECT * FROM planning_projects'))), '2026-11-13', 'https://planning.westminster.gov.uk/applicationDetails.do?activeTab=summary&keyVal=123',name))


@pytest.mark.parametrize('fixture', ['idox_details.html','hackney.html'])
def test_labelled_parsers(fixture):
    result,_ = parse((FIXTURES/fixture).read_text(), 'https://council.gov.uk/')
    assert result['agent_name']=='Jane Example'
    assert result['agent_company']=='Example Architects Ltd'
    assert result['agent_address']=='10 Example Road'
    assert result['evidence']


def test_summary_and_missing_fields():
    result,links=parse((FIXTURES/'idox_summary.html').read_text(), 'https://council.gov.uk/')
    assert 'agent_name' not in result
    assert links
    assert not parse('<p>Agent Name: See source</p>', 'https://council.gov.uk')[0].get('agent_name')


@pytest.mark.parametrize('name,expected', [('Example Architects Ltd',True),('Example Design Ltd',True),('Tree Design Ltd',False),('University Estates Architects',False),('John Smith',False),('Planning Consultant Designs Ltd',False),('Construction Architects Ltd',False)])
def test_classification(name,expected):
    assert classify(name)==expected


def test_trusted_values():
    merged,candidates=merge_trusted({'agent_name':'Trusted'}, {'agent_name':'New','agent_address':'Published'})
    assert merged=={'agent_name':'Trusted','agent_address':'Published'}
    assert candidates=={'agent_name_candidate':'New'}


@pytest.mark.parametrize('code',[403,429])
def test_host_stop_and_retry_after(planning,code):
    calls=[]
    def handle(request):
        calls.append(str(request.url))
        if request.url.path=='/robots.txt':
            return httpx.Response(200,text='User-agent: *\nAllow: /')
        return httpx.Response(code,headers={'Retry-After':'172800'})
    sleeps=[]
    http=TrialHTTP(planning.repo,planning.outreach.now,client=httpx.Client(transport=httpx.MockTransport(handle)),sleep=sleeps.append,public_url=lambda u:True)
    for _ in range(2):
        with pytest.raises(Unavailable): http.get('https://council.gov.uk/application')
    assert len(calls)==2
    assert max(sleeps)>=2
    assert planning.repo.rows('SELECT retry_at FROM planning_agent_http')[0]['retry_at']>=planning.outreach.now().timestamp()+172800


def test_robots_and_daily_cap(planning):
    calls=[]
    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(200,text='User-agent: *\nDisallow: /private')
    http=TrialHTTP(planning.repo,planning.outreach.now,client=httpx.Client(transport=httpx.MockTransport(handle)),sleep=lambda n:None,cap=1,public_url=lambda u:True)
    with pytest.raises(Unavailable): http.get('https://council.gov.uk/private')
    with pytest.raises(Unavailable): http.get('https://council.gov.uk/public')
    assert calls==['/robots.txt']


def test_trial_idempotent_and_never_enrols(planning):
    insert(planning, name='Existing Person')
    calls=[]
    def get(url):
        calls.append(url)
        return (FIXTURES/('idox_details.html' if 'activeTab=details' in url else 'idox_summary.html')).read_text()
    trial=AgentTrial(planning,http=SimpleNamespace(get=get))
    trial.run(10)
    trial.run(10)
    assert len(calls)==2
    p=planning.repo.rows('SELECT * FROM planning_projects')[0]
    assert p['agent_lookup_status']=='found'
    assert p['agent_name']=='Existing Person'
    data=json.loads(planning.repo.rows('SELECT payload FROM planning_agent_trials')[0]['payload'])
    assert data['field_provenance']['agent_name_candidate']=='Jane Example'
    assert data['status']=='trial_review'
    assert planning.repo.rows('SELECT * FROM outreach_enrolments')==[]
    from outreach.always_on import AlwaysOnService
    loop=AlwaysOnService(planning.outreach,planning=planning,contacts=object())
    loop.queue_projects()
    with pytest.raises(ValueError,match='Sai review'): loop.quality({'id':p['id']},{},{})
    assert not planning.repo.rows('SELECT * FROM outreach_project_queue')


@pytest.mark.parametrize('authority', ['Camden','Islington','Manchester','Salford','Southwark'])
def test_unsupported_never_fetches(planning,authority):
    insert(planning,authority)
    def forbidden(url): raise AssertionError('No HTTP allowed')
    AgentTrial(planning,http=SimpleNamespace(get=forbidden)).run()
    assert planning.repo.rows('SELECT agent_lookup_status FROM planning_projects')[0]['agent_lookup_status']=='portal_unavailable'


def test_site_requires_entity_binding_and_published_person():
    page='<p>Example Architects Ltd Company number 00123456</p><article>Jane Example Director <a href="mailto:jane@practice.example">jane@practice.example</a></article>'
    http=SimpleNamespace(get=lambda url:page)
    result=site_identity(http,'https://practice.example','Example Architects Ltd','Jane Example','00123456')
    assert result['person_on_site'] and result['email']=='jane@practice.example'
    assert not result['generic_fallback']
    assert not site_identity(http,'https://practice.example','Different Ltd','Jane Example','00123456')['person_on_site']
    generic=SimpleNamespace(get=lambda url:'<p>Example Architects Ltd 00123456 Jane Example Director info@practice.example</p>')
    assert site_identity(generic,'https://practice.example','Example Architects Ltd','Jane Example','00123456')['generic_fallback']


def test_parking_page_untrusted():
    http=SimpleNamespace(get=lambda url:'<h1>This domain is for sale</h1><p>Example Architects Ltd 00123456 Jane Example Director info@practice.example</p>')
    assert not site_identity(http,'https://practice.example','Example Architects Ltd','Jane Example','00123456')['email_found']


@pytest.mark.parametrize('active,sics,verified', [('active',['71111'],True),('dissolved',['71111'],False),('active',['43210'],False)])
def test_registry_gate_and_handoff(planning,active,sics,verified):
    insert(planning)
    planning.companies_house = SimpleNamespace(
        search_companies=lambda name:[SimpleNamespace(title='Example Architects Ltd',company_number='00123456')],
        get_company_profile=lambda number:SimpleNamespace(company_name='Example Architects Ltd',company_number=number,company_status=active,sic_codes=sics,source_url='https://registry.example/00123456'))
    def get(url):
        if 'practice.example' in url:
            return '<p>Example Architects Ltd 00123456 Jane Example Director info@practice.example</p>'
        return (FIXTURES/('idox_details.html' if 'activeTab=details' in url else 'idox_summary.html')).read_text()
    result=AgentTrial(planning,http=SimpleNamespace(get=get)).run()
    data=json.loads(planning.repo.rows('SELECT payload FROM planning_agent_trials')[0]['payload'])
    assert bool(data.get('CH_verified'))==verified
    assert result['unique_verified_people']==int(verified)
    from outreach.always_on import AlwaysOnService
    loop=AlwaysOnService(planning.outreach,planning=planning,contacts=object())
    assert len(loop.trial_decision_makers())==int(verified)
    assert result['unique_direct_email_people']==0
    assert not planning.repo.rows('SELECT * FROM prospect_contacts')


def test_error_attempt_is_not_repeated(planning):
    insert(planning)
    def broken(url): raise RuntimeError('offline')
    trial=AgentTrial(planning,http=SimpleNamespace(get=broken))
    trial.run(); trial.run()
    assert planning.repo.rows('SELECT agent_lookup_status FROM planning_projects')[0]['agent_lookup_status']=='error'
    assert len(planning.repo.rows('SELECT * FROM planning_agent_trials'))==1


@pytest.fixture(autouse=True)
def opt_in_legacy_portal_tests(monkeypatch):
    monkeypatch.setenv('PLANNING_COUNCIL_FETCH_ENABLED', 'true')
