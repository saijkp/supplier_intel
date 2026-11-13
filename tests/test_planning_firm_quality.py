"""Quality regressions use only fake clients and the fixture database."""
import json
from types import SimpleNamespace as NS
import pytest
from tests.test_planning import service
from tests.test_planning_firms import seed
from planning.firm_trial import FirmTrial, classify, site_people, verify_site, sanitize_contacts


def test_exact_junk_and_generic_cases():
    html = '<article>People Partners Associate Directors Associates</article><article>Alex Smith Associate Director</article><a href="mailto:info@holdermathias.com">Email</a>'
    contacts = []
    people, _ = site_people(NS(get=lambda _: html), 'https://holdermathias.com/', 'Holder Mathias Architects', contacts)
    assert [p['name'] for p in people] == ['Alex Smith']
    assert people[0]['role'] == 'Associate Director'
    assert contacts[0]['email'] == 'info@holdermathias.com'
    data = dict(website='https://holdermathias.com/', website_verified=True, people=people + contacts + [{'name': 'People Partners', 'role': 'Associate'}, {'name': 'Holder Mathias Architects'}])
    sanitize_contacts(data, 'Holder Mathias Architects')
    assert len(data['people']) == 1 and len(data['generic_contacts']) == 1
    assert len(data['field_provenance']['rejected_people']) == 2


@pytest.mark.parametrize('text', ['JSA Design Boston Massachusetts 617-555-1234', 'Purcell Everett Architecture Seattle'])
def test_us_sites_rejected(text):
    company = 'JSA Design' if 'JSA' in text else 'Purcell Architecture'
    assert verify_site(NS(get=lambda _: text), 'jsainc.com', company) is None


@pytest.mark.parametrize('signal', ['Manchester', '+44 161 123 4567', '0161 123 4567', 'M7 1AL'])
def test_uk_evidence_required(signal):
    assert verify_site(NS(get=lambda _: 'JSA Design ' + signal), 'jsa.example', 'JSA Design')
    assert verify_site(NS(get=lambda _: 'Other Practice ' + signal), 'jsa.example', 'JSA Design') is None


def test_purcell_email_domain_candidate():
    data = dict(website='https://purcelleverett.com/', website_verified=True, people=[{'email': 'info@purcellarchitects.com', 'generic': True}])
    sanitize_contacts(data, 'Purcell Architecture')
    assert not data['people']
    assert 'email' not in data['generic_contacts'][0]
    assert data['generic_contacts'][0]['email_candidate'] == 'info@purcellarchitects.com'


@pytest.mark.parametrize('name', ['Formation design and build', 'Brecons Design & Build Ltd', 'ADL Design & Build Ltd'])
def test_sic_architect_protection(name):
    assert classify(name, NS(company_status='active', sic_codes=['41202'])) == 'design_build_contractor'
    assert classify(name, NS(company_status='active', sic_codes=['71111', '41202'])) == 'architect'


def test_retry_complete_rows_rechecks_and_preserves_evidence(service):
    seed(service)
    trial = FirmTrial(service, http=NS(get=lambda _: 'Studio Architects Boston'), apollo=NS(api_key=False), serp=NS(api_key=False))
    trial.run(max_reveals=0)
    key = 'ch:00123456'
    data = json.loads(service.repo.rows('SELECT payload FROM planning_firm_trials WHERE firm_key=?', (key,))[0]['payload'])
    data.update(website='https://jsainc.com/', website_verified=True, classification='design_build_contractor', people=[{'name': 'People Partners', 'role': 'Associate'}, {'email': 'info@jsainc.com'}])
    service.repo.execute('UPDATE planning_firm_trials SET payload=? WHERE firm_key=?', (json.dumps(data), key))
    trial.run(retry_incomplete=True, max_searches=0, max_reveals=0)
    saved = json.loads(service.repo.rows('SELECT payload FROM planning_firm_trials WHERE firm_key=?', (key,))[0]['payload'])
    assert saved['website'] == data['website'] and saved['website_verified'] is False
    assert saved['classification'] == 'architect'
    assert not saved['people'] and saved['generic_contacts'][0]['email_candidate'] == 'info@jsainc.com'
    assert saved['field_provenance']['website_candidate']
    assert saved['field_provenance']['website_reverification']


def test_postcode_preference(service):
    seed(service)
    trial = FirmTrial(service, http=NS(get=lambda url: 'Studio Architects London ' + ('M7 2AB' if 'near' in url else 'SW1A 1AA')), apollo=NS(api_key=False), serp=NS(api_key=True, scrape=lambda *a, **k: [NS(success=True, raw_data={'link': 'https://' + d}) for d in ['far.example', 'near.example']]))
    trial.run(max_searches=0, max_reveals=0)
    data = {}
    trial.serp_calls = 0
    site = trial.discover('ch:00123456', 'Studio Architects', data, NS(registered_office_address={'postal_code': 'M7 1AL'}), 1)
    assert site == 'https://near.example/' and data['website_evidence']['outward_match']


@pytest.mark.parametrize('change', ['none', 'unverified', 'no_ch', 'contractor', 'direct', 'org_mismatch', 'cap'])
def test_reveal_single_best_and_gates(service, monkeypatch, change):
    seed(service)
    reveals = []
    apollo = NS(api_key=False, search=lambda *a: [], reveal=lambda person: reveals.append(person) or {'name': 'Alex Smith', 'email': 'alex@studio.example'})
    trial = FirmTrial(service, http=NS(get=lambda _: 'Other'), apollo=apollo, serp=NS(api_key=False))
    trial.run(max_reveals=0)
    monkeypatch.setenv('OUTREACH_APOLLO_REVEAL_ENABLED', 'true')
    def person(role, i):
        return dict(name='Alex Smith', role=role, source='apollo', source_url='apollo-free:' + i, organization_domain='studio.example')
    data = dict(website='https://studio.example/', website_verified=True, CH_verified=True, classification='architect', people=[person(r, str(i)) for i, r in enumerate(['Architect', 'Associate', 'Associate Director', 'Founder'])])
    if change == 'unverified': data['website_verified'] = False
    if change == 'no_ch': data['CH_verified'] = False
    if change == 'contractor': data['classification'] = 'design_build_contractor'
    if change == 'direct': data['people'][0]['email'] = 'existing@studio.example'
    if change == 'org_mismatch':
        for p in data['people']: p['organization_domain'] = 'different.example'
    trial.reveal_best('ch:00123456', 'Studio Architects', data, 0 if change == 'cap' else 10)
    assert reveals == (['3'] if change == 'none' else [])
    if reveals:
        assert data['reveal_log'][0]['credit_used'] == 1
        assert data['reveal_log'][0]['firm'] == 'Studio Architects'
        trial.reveal_best('ch:00123456', 'Studio Architects', data, 10)
        assert reveals == ['3']


def test_reveal_run_cap_and_failure_reserved(service, monkeypatch):
    seed(service)
    trial = FirmTrial(service, http=NS(get=lambda _: 'Other'), apollo=NS(api_key=False), serp=NS(api_key=False))
    trial.run(max_reveals=0)
    monkeypatch.setenv('OUTREACH_APOLLO_REVEAL_ENABLED', 'true')
    def fail(_):
        raise RuntimeError('fake failure')
    trial.apollo = NS(reveal=fail)
    data = dict(website='https://studio.example/', website_verified=True, CH_verified=True, classification='architect', people=[dict(name='Alex Smith', role='Director', source='apollo', source_url='apollo-free:1', organization_domain='studio.example')])
    with pytest.raises(RuntimeError):
        trial.reveal_best('ch:00123456', 'Studio Architects', data, 1)
    saved = json.loads(service.repo.rows('SELECT payload FROM planning_firm_trials WHERE firm_key=?', ('ch:00123456',))[0]['payload'])
    assert saved['reveals_used'] == 1 and saved['reveal_log'][0]['status'] == 'RuntimeError'
    # A second firm cannot spend after the shared run cap, even after a failure.
    data['reveals_used'] = 0
    trial.reveal_best('ch:00123456', 'Other Architects', data, 1)
    assert trial.reveals_used == 1
