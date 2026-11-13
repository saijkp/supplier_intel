"""Finder regression contracts: fake APIs and pages only."""
import json
from types import SimpleNamespace as NS
import pytest
from tests.test_planning import service, project
from tests.test_planning_firms import seed, Apollo
from planning.firm_trial import FirmTrial, firm_name, match_company, verify_site, classify, site_people


@pytest.mark.parametrize('name', ['Rodic Davidson Architects', 'Purcell Architecture', 'Holder Mathias Architects', 'Studio Serre Architects'])
def test_registry_legal_suffixes(name):
    profile = NS(company_number='123', company_name=name.replace('Architects', 'Architecture') + ' LLP.', company_status='active', sic_codes=['71111'])
    registry = NS(search_companies=lambda _: [NS(title=profile.company_name, company_number='123', company_status='active')], get_company_profile=lambda _: profile)
    matched, evidence = match_company(registry, name)
    assert matched is profile
    assert evidence['ch_matches'][0]['score'] == 100
    assert firm_name('A & B Limited') == firm_name('A and B PLC')


def test_registry_ambiguous_and_inactive():
    registry = NS(search_companies=lambda _: [NS(title='Studio Architects Ltd', company_number=n, company_status='active') for n in ('1', '2')], get_company_profile=lambda n: NS(company_number=n, company_name='Studio Architecture', company_status='active', sic_codes=['71111']))
    matched, evidence = match_company(registry, 'Studio Architects')
    assert matched is None and evidence['ch_match_status'] == 'needs_review'
    registry.get_company_profile = lambda n: NS(company_status='dissolved')
    assert match_company(registry, 'Studio Architects')[0] is None


@pytest.mark.parametrize('name', ['Formation design and build', 'Brecons Design & Build', 'ADL Design & Build', 'Alpha Construction', 'ABC Contractors'])
def test_contractor_class(name):
    assert classify(name) == 'design_build_contractor'


def test_verification_and_published_fallback():
    http = NS(get=lambda _: '<h1>Studio Architects</h1>London SW1A 1AA +44 20 12345678 <a href="/people">People</a><article>Alex Smith Director <a href="mailto:alex@example.org">Email</a></article><a href="mailto:studio@example.org">Contact</a>')
    verified = verify_site(http, 'studio.example', 'Studio Architects', NS(registered_office_address='SW1A 1AA'))
    assert verified['website_evidence']['postcode_match']
    contacts = []
    people, _ = site_people(http, verified['website'], generic_contacts=contacts)
    assert any(p.get('name') == 'Alex Smith' and p['email'] == 'alex@example.org' for p in people)
    assert any(p.get('generic') and p['email'] == 'studio@example.org' for p in contacts)
    assert verify_site(http, 'studio.example', 'Different Architects') is None
    assert verify_site(NS(get=lambda _: 'This domain is for sale'), 'studio.example', 'Studio Architects') is None


class Search:
    api_key = True
    def __init__(self):
        self.calls = []
    def scrape(self, query, **kwargs):
        self.calls.append(query)
        return [NS(success=True, raw_data={'link': 'https://studio.example/team'})]


class Organizations(Apollo):
    def __init__(self, domain=None):
        super().__init__()
        self.domain = domain
        self.org_calls = []
    def organizations(self, name):
        self.org_calls.append(name)
        return [{'name': name, 'primary_domain': self.domain}] if self.domain else []


def test_apollo_first_then_serp_and_retry_no_reveal(service, monkeypatch):
    seed(service)
    monkeypatch.setenv('OUTREACH_APOLLO_REVEAL_ENABLED', 'true')
    apollo, serp = Organizations(), Search()
    http = NS(get=lambda _: '<h1>Studio Architects London</h1><article>Alex Smith Director alex@studio.example</article>')
    trial = FirmTrial(service, http=http, apollo=apollo, serp=serp)
    trial.run(max_searches=0, max_reveals=1)
    assert len(apollo.reveals) == 0 and not serp.calls
    result = trial.run(retry_incomplete=True, max_searches=1)
    assert len(serp.calls) == 1 and len(apollo.reveals) == 0 and apollo.searches == 1
    assert result['website_verified'] == 1 and result['serp_calls'] == 1
    assert result['direct_email'] >= 1
    trial.run(retry_incomplete=True)
    assert len(serp.calls) == 1 and len(apollo.reveals) == 0


def test_org_verified_skips_serp(service):
    seed(service)
    apollo, serp = Organizations('studio.example'), Search()
    result = FirmTrial(service, http=NS(get=lambda _: '<h1>Studio Architects London</h1>'), apollo=apollo, serp=serp).run(max_reveals=0)
    assert result['website_verified'] == 1 and not serp.calls
    assert result['apollo_searches'] == 2


def test_unverified_not_extracted_and_cost_recorded(service):
    seed(service)
    serp = Search()
    trial = FirmTrial(service, http=NS(get=lambda _: '<h1>Other Practice</h1><article>Alex Smith Director bad@example.org</article>'), apollo=Organizations(), serp=serp)
    result = trial.run(max_reveals=0, max_searches=1)
    data = result['firm_records'][0]['payload']
    assert not data.get('website') and not result['website_verified']
    assert data['field_provenance']['website_candidate']
    assert result['serp_calls'] == 1 and result['direct_email'] == 0
    assert any(c['estimated_gbp'] == .01 for c in data['search_calls'])

def test_retry_preserves_website_disagreement(service):
    seed(service)
    apollo, serp = Organizations('studio.example'), Search()
    # The trusted (but now unverifiable) site no longer shows the firm name.
    pages = lambda url: '<h1>Unrelated Holding Page</h1>' if 'trusted.example' in url else '<h1>Studio Architects London</h1>'
    trial = FirmTrial(service, http=NS(get=pages), apollo=apollo, serp=serp)
    trial.run(max_searches=0, max_reveals=0)
    key = 'ch:00123456'
    data = json.loads(service.repo.rows('SELECT payload FROM planning_firm_trials WHERE firm_key=?', (key,))[0]['payload'])
    data['website'] = 'https://trusted.example/'
    data['website_verified'] = False
    service.repo.execute('UPDATE planning_firm_trials SET payload=? WHERE firm_key=?', (json.dumps(data), key))
    trial.run(retry_incomplete=True, max_searches=0)
    saved = json.loads(service.repo.rows('SELECT payload FROM planning_firm_trials WHERE firm_key=?', (key,))[0]['payload'])
    assert saved['website'] == 'https://trusted.example/'
    assert saved['field_provenance']['website_candidate']
    assert not saved['website_verified']


def test_search_run_cap_across_firms(service):
    for company in ('One Architects', 'Two Architects', 'Three Architects'):
        raw = project()
        raw.update(reference=company, agent_company=company, agent=company)
        service.store(raw)
    service.companies_house = NS(search_companies=lambda _: [])
    serp = Search()
    result = FirmTrial(service, http=NS(get=lambda _: 'Other practice'), apollo=Organizations(), serp=serp).run(limit=3, max_searches=1, max_reveals=0)
    assert len(serp.calls) == result['serp_calls'] == 1
    assert result['firms'] == 3
