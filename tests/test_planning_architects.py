"""Offline regressions for the planning-led architect workflow."""
import json
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest

from planning.council import CouncilClient, parse, role_for
from planning.planit_client import PlanItClient, Inconclusive, RateLimited
from planning.relevance import fit
from planning.service import in_window
from tests.test_planning import service, project, eligible


@pytest.mark.parametrize('text,units', [
    ('Erection of 6 x flats', 6), ('6 x 1-bed flats', 6), ('4 self-contained units', 4), ('6no. flats', 6),
    ('4 self-contained flats', 4), ('2 houses, 2 flats', 4),
    ('2 x 1-bed, 3 x 2-bed flats', 5),
])
def test_units(text, units):
    assert fit(text)[0] and fit(text)[2] == units
    assert not fit(text, min_units=units+1)[0]
    assert all(r['evidence'] in text for r in fit(text)[1])


@pytest.mark.parametrize('text', [
    'Discharge of Condition 20 (diesel generators) for 800 student bedrooms',
    'Request for scoping opinion for student accommodation',
    'Change of secondary glazing at hotel', 'Hotel roof plant replacement',
])
def test_scope_junk(text):
    assert not fit(text)[0]


@pytest.mark.parametrize('name', ['Newmark', 'Lichfields', 'DP9', 'Turley', 'Savills planning', 'Q Square Group Ltd'])
def test_consultants_never_architects(name):
    assert role_for(name) == 'planning consultant'


def test_unproven_brand_remains_unknown():
    assert role_for('Morris+Company') == 'unknown'
    assert role_for('Morris+Company', 'Architect') == 'architect'
    assert role_for('Mr Smith') == 'unknown'


@pytest.mark.parametrize('html', [
    '<table><tr><th>Agent Name</th><td>Alex Smith</td></tr><tr><th>Agent Company</th><td>Example Architects Ltd</td></tr><tr><th>Agent Address</th><td>1 Test Road</td></tr></table>',
    '<dl><dt>Agent Name:</dt><dd>Alex Smith</dd><dt>Agent Company:</dt><dd>Example Architects Ltd</dd><dt>Agent Address:</dt><dd>1 Test Road</dd></dl>',
    '<div><span>Agent Name</span><span>Alex Smith</span><span>Agent Company</span><span>Example Architects Ltd</span><span>Agent Address</span><span>1 Test Road</span></div>',
])
def test_idox_nec_labelled_fields(html):
    record, _ = parse(html, 'https://council.gov.uk/details')
    assert record['agent_company'] == 'Example Architects Ltd'
    assert record['agent_name'] == 'Alex Smith'
    assert record['agent_address'] == '1 Test Road'
    assert record['agent_role'] == 'architect'
    assert all(e['value'] in html for e in record['evidence'])


def test_further_information_cache_and_failure(service):
    requests = []; sleeps = []
    def respond(request):
        requests.append(str(request.url))
        if request.url.path == '/bad':
            return httpx.Response(403)
        if request.url.path == '/summary':
            return httpx.Response(200, text='<a href="/details">Further Information</a>')
        return httpx.Response(200, text='<dl><dt>Agent Company</dt><dd>Test Architects Ltd</dd></dl>')
    client = CouncilClient(service.repo, service.outreach.now,
                           httpx.Client(transport=httpx.MockTransport(respond)), sleeps.append)
    first = client.fetch('https://council.gov.uk/summary')
    assert first['agent_company'] == 'Test Architects Ltd'
    assert client.fetch('https://council.gov.uk/summary') == first
    assert len(requests) == 2 and any(s > 0 for s in sleeps)
    assert client.fetch('https://council.gov.uk/bad')['error']
    assert client.fetch('https://localhost/')['error']
    assert len(requests) == 3


@pytest.mark.parametrize('payload', [{'records': [], 'total': None}, {'records': []}])
def test_null_or_missing_total_is_empty(payload):
    client = PlanItClient(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))))
    assert client.recent('Camden', date(2026, 11, 1), date(2026, 11, 2)) == []


def test_error_even_with_empty_records_is_inconclusive():
    client = PlanItClient(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'records': [], 'error': 'timeout'}))))
    with pytest.raises(Inconclusive):
        client.recent('Camden', date(2026, 11, 1), date(2026, 11, 2))


def test_rate_limit_stops_batch_without_sleep(service):
    calls = []; sleeps = []
    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={'Retry-After': '300'})
    service.client = PlanItClient(httpx.Client(transport=httpx.MockTransport(respond)), sleeps.append)
    result = service.sync_recent(['Camden', 'Islington'])
    assert len(calls) == 1 and not sleeps and not result['complete']
    assert 'Islington' not in result['errors']


def test_union_decision_window_and_dedup():
    requests = []
    def respond(request):
        requests.append(dict(request.url.params))
        rows = [{'uid': 'shared'}]
        if 'decided_start' in request.url.params:
            rows.append({'uid': 'old-validation-new-decision'})
        return httpx.Response(200, json={'records': rows, 'total': len(rows)})
    client = PlanItClient(httpx.Client(transport=httpx.MockTransport(respond)))
    assert len(client.recent('Camden', date(2026,11,1), date(2026,11,2))) == 2
    assert 'start_date' in requests[0] and 'start_date' not in requests[1]


def test_validation_or_decision_and_status():
    row = {'validated_date': '2020-01-01', 'decided_date': '2026-11-01', 'status': 'Granted'}
    assert in_window(row, date(2026,11,1), date(2026,11,2), ['decided'])
    assert not in_window(row, date(2026,11,1), date(2026,11,2), ['validated'])


def test_consultant_stored_without_outreach_account(service):
    pid = service.store({**project(), 'agent_company': 'Newmark', 'agent': 'Newmark'})
    row = service.repo.rows('SELECT * FROM planning_projects WHERE id=?', (pid,))[0]
    assert row['agent_company'] == 'Newmark' and row['agent_role'] == 'planning consultant'
    assert row['agent_account_id'] is None


def test_corporate_registry_verification_and_hook(service):
    class Registry:
        def search_companies(self, name):
            return [SimpleNamespace(title=name, company_number='00123456')]
        def get_company_profile(self, number):
            return SimpleNamespace(company_name='Test Architects Ltd', company_status='active', company_number=number, source_url='https://find-and-update.company-information.service.gov.uk/company/'+number)
    service.companies_house = Registry()
    pid = service.store({**project(), 'agent_company': 'Test Architects Ltd', 'agent_details': {'website': 'https://test.example'}})
    p = service.repo.rows('SELECT * FROM planning_projects WHERE id=?', (pid,))[0]
    account = service.outreach.account(p['agent_account_id'])
    assert account['subscriber_type'] == 'corporate'
    assert account['project_hook']['reference'] == '26/100'
    assert account['project_hook']['source_url'] == project()['source_url']


def test_enrol_requires_verified_domain_and_registry(service):
    pid = service.store(project())
    service.outreach.import_rows([{'company':'Studio Architects','email':'alex@studio.example'}], 'manual')
    cid = service.repo.rows('SELECT id FROM prospect_contacts')[0]['id']
    with pytest.raises(ValueError, match='flagged|corporate-domain'):
        service.enrol(pid, cid, 'sj@ikeng.online')
    eligible(service, cid)
    service.repo.execute("UPDATE prospect_accounts SET domain='wrong.example'")
    with pytest.raises(ValueError, match='corporate-domain'):
        service.enrol(pid, cid, 'sj@ikeng.online')
    with pytest.raises(ValueError, match='role=architect'):
        service.enrol(pid, cid, 'sj@ikeng.online', 'developer')


def test_watchdog_expires_old_run_not_recent(service):
    service.repo.execute("INSERT INTO planning_sync_runs(id,started_at) VALUES(3,'2026-11-10 10:00:00')")
    recent = service.start_run()
    old = service.repo.rows('SELECT * FROM planning_sync_runs WHERE id=3')[0]
    assert old['completed_at'] and json.loads(old['result'])['status'] == 'failed'
    assert service.repo.rows('SELECT completed_at FROM planning_sync_runs WHERE id=?', (recent,))[0]['completed_at'] is None


def test_identity_conflict_preserved_in_observation(service):
    pid = service.store({**project(), 'agent_name': 'First', 'agent_address': '1 Road'})
    service.store({**project(), 'agent_name': 'Second', 'agent_address': '2 Road'})
    row = service.repo.rows('SELECT * FROM planning_projects WHERE id=?', (pid,))[0]
    assert row['agent_name'] == 'First' and row['agent_address'] == '1 Road'
    assert any(json.loads(r['payload'])['agent_name'] == 'Second' for r in service.repo.rows('SELECT payload FROM planning_project_observations'))


def test_identity_import_is_grounded_and_preview_does_not_enrol(service):
    pid = service.store(project())
    with pytest.raises(ValueError, match='verbatim'):
        service.import_identity(pid, 'https://studio.example/about', 'Architects', {'agent_name': 'Invented'})
    service.import_identity(pid, 'https://studio.example/about', 'Alex Smith\n1 Road', {'agent_name': 'Alex Smith', 'agent_address': '1 Road'})
    service.outreach.import_rows([{'company':'Studio Architects','contact_name':'Alex','email':'alex@studio.example'}], 'manual')
    cid = service.repo.rows('SELECT id FROM prospect_contacts')[0]['id']
    eligible(service, cid)
    preview = service.preview(pid, cid)
    assert preview['words'] <= 120 and not preview['approved']
    assert '26/100' in preview['body'] and 'X Road' in preview['body']
    assert not service.repo.rows('SELECT * FROM outreach_sequences')
    assert not service.repo.rows('SELECT * FROM outreach_enrolments')


def test_council_exception_does_not_lose_next_project(service):
    class Council:
        def fetch(self, url): raise RuntimeError('offline')
    class Client:
        def recent(self, *args):
            return [{'uid': str(i), 'description': '6 flats', 'url': 'https://council.gov.uk/'+str(i)} for i in range(2)]
    service.council, service.client = Council(), Client()
    result = service.sync_recent(['Camden'])
    assert result['stored'] == 2 and len(result['identity_errors']) == 2


def test_background_api_returns_job_and_records_failure(service, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.planning import router
    from api.outreach import get_service
    from planning.service import PlanningService
    import api.auth
    monkeypatch.setattr(api.auth, 'API_ACCESS_TOKEN', 'planning-test')
    monkeypatch.setattr(PlanningService, 'sync_recent', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('unexpected')))
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[get_service] = lambda: service.outreach
    client = TestClient(app, headers={'Authorization': 'Bearer planning-test'})
    response = client.post('/planning/sync', json={'areas': ['Camden'], 'statuses': ['validated', 'decided'], 'min_units': 4})
    assert response.status_code == 202
    job = client.get(response.json()['status_url']).json()
    assert job['completed_at'] and json.loads(job['result'])['status'] == 'failed'
    assert client.get('/planning/runs/99999').status_code == 404
