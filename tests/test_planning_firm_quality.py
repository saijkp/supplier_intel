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
    if change == 'direct':
        data['people'][0].update(email='existing@studio.example', email_evidence={'name': 'Alex Smith', 'email': 'existing@studio.example'})
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


@pytest.mark.parametrize('enabled,cap,expected', [('true', 10, ['founder']), ('false', 10, []), ('true', 0, [])])
def test_first_name_only_search_candidates(service, monkeypatch, enabled, cap, expected):
    seed(service)
    monkeypatch.setenv('OUTREACH_APOLLO_REVEAL_ENABLED', enabled)
    hits = [dict(id=ident, first_name=first, last_name_obfuscated='S***', title=title,
                 organization={'name': 'Studio Architects', 'primary_domain': 'studio.example'})
            for ident, first, title in [('architect', 'Paolo', 'Architect'), ('associate', 'Oliver', 'Associate'),
                                        ('ad', 'Neil', 'Associate Director'), ('founder', 'David', 'Founder')]]
    reveals = []
    apollo = NS(api_key=True, organizations=lambda _: [{'name': 'Studio Architects', 'primary_domain': 'studio.example'}],
                search=lambda *a: hits, reveal=lambda ident: reveals.append(ident) or {'name': 'David Smith', 'email': 'david@studio.example'})
    result = FirmTrial(service, http=NS(get=lambda _: 'Studio Architects London'), apollo=apollo,
                       serp=NS(api_key=False)).run(max_reveals=cap)
    data = result['firm_records'][0]['payload']
    assert reveals == expected
    assert {p['first_name'] for p in data['reveal_candidates']} == {'Paolo', 'Oliver', 'Neil', 'David'}
    assert {p['apollo_id'] for p in data['reveal_candidates']} == {'architect', 'associate', 'ad', 'founder'}
    assert not any('name' in p or 'email' in p for p in data['reveal_candidates'])
    assert [p['name'] for p in data['people'] if p.get('source') == 'apollo'] == (['David Smith'] if expected else [])
    assert result['run_reveals_used'] == len(expected)


@pytest.mark.parametrize('revealed', [{'name': 'Dilan', 'email': 'dilan@studio.example'}, {'name': 'Dilan Smith'}, {}])
def test_reveal_requires_full_name_and_email(service, monkeypatch, revealed):
    seed(service)
    trial = FirmTrial(service, http=NS(get=lambda _: 'Other'), apollo=NS(api_key=False), serp=NS(api_key=False))
    trial.run(max_reveals=0)
    monkeypatch.setenv('OUTREACH_APOLLO_REVEAL_ENABLED', 'true')
    calls = []
    trial.apollo = NS(reveal=lambda ident: calls.append(ident) or revealed)
    data = dict(website='https://studio.example/', website_verified=True, CH_verified=True, classification='architect',
                people=[], field_provenance={'rejected_people': [dict(name='Dilan', role='Director', source='apollo',
                    source_url='apollo-free:dilan', organization_domain='studio.example')]})
    trial.reveal_best('ch:00123456', 'Studio Architects', data, 1)
    assert calls == ['dilan'] and not data['people']
    assert data['reveal_log'][0]['status'] == 'completed'
    saved = json.loads(service.repo.rows('SELECT payload FROM planning_firm_trials WHERE firm_key=?', ('ch:00123456',))[0]['payload'])
    assert not saved['people'] and saved['reveals_used'] == 1


def test_cottrell_contact_page_uk_evidence():
    company = 'Cottrell and Vermeulen Architecture'
    pages = {'https://cv-arch.co.uk/': '<h1>Cottrell & Vermeulen Architecture</h1><a href="/contact/">Contact</a>',
             'https://cv-arch.co.uk/contact/': 'Contact 1B Iliffe St London SE17 3LJ Telephone +44 (0) 20 7708 2567'}
    result = verify_site(NS(get=pages.__getitem__), 'cv-arch.co.uk', company)
    assert result['website_verified'] and result['website_evidence']['uk_signal']
    assert result['website_evidence']['pages'][1]['source_url'].endswith('/contact/')
    assert verify_site(NS(get=pages.__getitem__), 'cv-arch.co.uk', 'Other Architects') is None
    pages['https://cv-arch.co.uk/contact/'] = 'Contact us'
    assert verify_site(NS(get=pages.__getitem__), 'cv-arch.co.uk', company) is None


@pytest.mark.parametrize('href', ['https://other.example/contact', 'http://cv-arch.co.uk/contact', 'https://user@cv-arch.co.uk/contact'])
def test_contact_evidence_cannot_cross_domain_or_scheme(href):
    calls = []
    def get(url):
        calls.append(url)
        return '<h1>Cottrell & Vermeulen Architecture</h1><a href="' + href + '">Contact</a>'
    assert verify_site(NS(get=get), 'cv-arch.co.uk', 'Cottrell and Vermeulen Architecture') is None
    assert calls == ['https://cv-arch.co.uk/']


def test_local_cleanup_not_starved_by_retry_limit_or_registry_failure(service):
    seed(service)
    trial = FirmTrial(service, http=NS(get=lambda _: 'Other'), apollo=NS(api_key=False), serp=NS(api_key=False))
    trial.run(max_reveals=0)
    # These lower-ranked rows used to remain unchanged when architects consumed the budget.
    for i, company in enumerate(['Formation design and build', 'Brecons Design & Build Ltd']):
        payload = dict(classification='architect', CH_verified=bool(i), sic_codes=['41100', '41201', '41202', '70229'],
                       people=[dict(name='Dilan', role='Architecture', source='apollo', source_url='apollo-free:dilan')])
        service.repo.execute('INSERT INTO planning_firm_trials(firm_key,company,payload,attempted_at) VALUES(?,?,?,?)',
                             ('name:contractor' + str(i), company, json.dumps(payload), '2026-11-13'))
    def fail(*a):
        raise RuntimeError('fake registry failure')
    service.companies_house = NS(search_companies=fail, get_company_profile=fail)
    trial.run(limit=1, retry_incomplete=True, max_searches=0, max_reveals=0)
    for row in service.repo.rows("SELECT * FROM planning_firm_trials WHERE firm_key LIKE 'name:contractor%'"):
        data = json.loads(row['payload'])
        assert data['classification'] == 'design_build_contractor' and not data['people']
        assert data['reveal_candidates'][0]['first_name'] == 'Dilan'
    # Repeated persistence must not resurrect legacy single-token people.
    trial.run(limit=1, retry_incomplete=True, max_searches=0, max_reveals=0)
    assert all(not json.loads(r['payload'])['people'] for r in service.repo.rows("SELECT payload FROM planning_firm_trials WHERE firm_key LIKE 'name:contractor%'"))


def test_scoped_search_stamps_missing_domain_only():
    from planning.firm_trial import scoped
    people = scoped([{'id': 'a1', 'first_name': 'Graham'}, {'id': 'b2', 'organization_domain': 'other.example'}], 'www.grahamford.example')
    assert people[0]['organization_domain'] == 'grahamford.example'
    assert people[1]['organization_domain'] == 'other.example'
    assert scoped([{'id': 'c3'}], None) == [{'id': 'c3'}]
