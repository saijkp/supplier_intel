"""Offline firm route contracts. No network, Playwright, credits or sends."""
from types import SimpleNamespace
import httpx
import pytest
from tests.test_planning import service, project
from planning.firm_trial import FirmTrial, classify, clean, site_people
from planning.portal_agents import TrialHTTP, Unavailable, merge_trusted
from planning.planit_client import PlanItClient, RateLimited


class Registry:
    def search_companies(self, name):
        return [SimpleNamespace(title=name, company_number='00123456')]

    def get_company_profile(self, number):
        return SimpleNamespace(company_number=number, company_name='Studio Architects', company_status='active', sic_codes=['71111'], source_url='https://registry.example/00123456')

    def get_officers(self, number):
        return [{'name': 'SMITH, Alex', 'role': 'director', 'source': 'companies_house'}]


class Apollo:
    def __init__(self):
        self.searches = 0
        self.reveals = []

    def search(self, company, domain=None):
        self.searches += 1
        return [{'id': str(i), 'first_name': 'Alex', 'title': 'Director', 'organization': {'name': company}} for i in range(4)]

    def reveal(self, person):
        self.reveals.append(person)
        return {'email': 'alex@studio.example'}


def seed(service):
    # Ingestion with a no-op registry creates no paid calls.
    for reference in ('A', 'B'):
        raw = project()
        raw['reference'] = reference
        service.store(raw)
    service.companies_house = Registry()


def test_placeholder_and_classifier(service):
    raw = project()
    raw.update(agent_name=' See source ', agent_company='See source', agent='See source', applicant='See source')
    pid = service.store(raw)
    row = service.repo.rows('SELECT * FROM planning_projects WHERE id=?', (pid,))[0]
    assert not row['agent_name'] and not row['agent_company']
    assert clean(' SEE SOURCE ') == ''
    assert classify('DP9') == classify('Firstplan') == classify('CMA Planning') == 'planning_consultant'
    assert classify('Property Design Studio') == 'planning_consultant'
    assert classify('Studio Architects') == 'architect'
    assert classify('Plain Ltd', SimpleNamespace(company_status='active', sic_codes=['74901'])) == 'architect'
    assert classify('Studio Architects', SimpleNamespace(company_status='dissolved', sic_codes=['71111'])) == 'inactive'


def test_site_robots_prevents_page_request(service):
    urls = []
    def get(request):
        urls.append(str(request.url))
        return httpx.Response(200, text='User-agent: *\nDisallow: /')
    http = TrialHTTP(service.repo, service.outreach.now, client=httpx.Client(transport=httpx.MockTransport(get)), sleep=lambda _: None, public_url=lambda _: True)
    with pytest.raises(Unavailable):
        site_people(http, 'https://studio.example/')
    assert urls == ['https://studio.example/robots.txt']


def test_site_people_published_only_and_parking():
    page = '<article>Alex Smith Director <a href="mailto:alex@studio.example">Email</a></article>'
    people, _ = site_people(SimpleNamespace(get=lambda _: page), 'https://studio.example/')
    assert people[0]['name'] == 'Alex Smith' and people[0]['email'] == 'alex@studio.example'
    people, _ = site_people(SimpleNamespace(get=lambda _: '<article>Alex Smith Director</article>'), 'https://studio.example/')
    assert people[0]['email'] is None


@pytest.mark.parametrize('enabled,cap,expected', [('false', 10, 0), ('true', 1, 1), ('true', 10, 2), ('true', 0, 0)])
def test_reveal_gate_cap_idempotency_no_send(service, monkeypatch, enabled, cap, expected):
    seed(service)
    monkeypatch.setenv('OUTREACH_APOLLO_REVEAL_ENABLED', enabled)
    apollo = Apollo()
    trial = FirmTrial(service, apollo=apollo)
    before = {table: service.repo.rows('SELECT * FROM ' + table) for table in ('prospect_accounts', 'outreach_enrolments', 'rfq_messages')}
    result = trial.run(3, max_reveals=cap)
    assert result['firms_seen'] == result['CH_verified'] == 1
    assert len(apollo.reveals) == result['run_reveals_used'] == expected
    assert len(service.repo.rows('SELECT * FROM planning_firm_projects')) == 2
    trial.run(3, max_reveals=cap)
    assert apollo.searches == 1 and len(apollo.reveals) == expected
    for table, rows in before.items():
        assert service.repo.rows('SELECT * FROM ' + table) == rows


def test_trusted_guard():
    merged, candidates = merge_trusted({'email': 'trusted@example.org'}, {'email': 'new@example.org'})
    assert merged['email'] == 'trusted@example.org'
    assert candidates['email_candidate'] == 'new@example.org'


def test_council_disabled(service, monkeypatch):
    from planning.council import CouncilClient
    monkeypatch.delenv('PLANNING_COUNCIL_FETCH_ENABLED', raising=False)
    client = CouncilClient(service.repo, service.outreach.now, client=SimpleNamespace(get=lambda *a, **k: pytest.fail('council fetch')))
    assert 'disabled' in client.fetch('https://council.gov.uk/')['error']


def test_planit_global_retry_after(monkeypatch):
    monkeypatch.setattr(PlanItClient, '_retry_at', 0)
    client = PlanItClient(httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={'Retry-After': '600'}))))
    with pytest.raises(RateLimited, match='600'):
        client._get({})
    second = PlanItClient(SimpleNamespace(get=lambda *a, **k: pytest.fail('global stop violated')))
    with pytest.raises(RateLimited, match='cooldown'):
        second._get({})


def test_negative_attempt_marker(service):
    raw = project()
    raw['agent_company'] = 'DP9'
    raw['agent'] = 'DP9'
    service.store(raw)
    calls = []
    service.companies_house = SimpleNamespace(search_companies=lambda name: calls.append(name) or [])
    trial = FirmTrial(service, apollo=SimpleNamespace(search=lambda *args: pytest.fail('consultant targeted')))
    assert trial.run()['firms_seen'] == 1
    trial.run()
    assert calls == ['DP9']


def test_trial_preserves_trusted_fields(service):
    seed(service)
    trial = FirmTrial(service, apollo=Apollo())
    trial.run(max_reveals=0)
    key = 'ch:00123456'
    trial.save(key, {'website': 'https://trusted.example'})
    trial.save(key, {'website': 'https://candidate.example'})
    import json
    data = json.loads(service.repo.rows('SELECT payload FROM planning_firm_trials WHERE firm_key=?', (key,))[0]['payload'])
    assert data['website'] == 'https://trusted.example'
    assert data['field_provenance']['website_candidate'] == 'https://candidate.example'
