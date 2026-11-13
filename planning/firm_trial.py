"""PlanIt firm-first research. All writes are isolated trial_review records."""
import json
import os
import re
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from outreach.apollo import FirmTrialApollo
from outreach.service import stamp
from planning.service import name_key
from planning.portal_agents import TrialHTTP, merge_trusted
from planning.relevance import fit
from storage.database import connection_scope
from verification.website_contact_extractor import parking_page_reason

SICS = {'71111', '71112', '74100', '74901'}


def clean(value):
    return '' if not value or str(value).strip().casefold() == 'see source' else str(value).strip()


def classify(company, profile=None):
    company = clean(company)
    if not company:
        return 'unknown'
    if re.search(r'\b(?:build|construction|contractors?)\b', company, re.I):
        return 'design_build_contractor'
    if re.search(r'\b(?:DP9|Turley|Savills|Firstplan|CMA|Newmark|Lichfields|planning|survey\w*|property|estates|agent)\b', company, re.I):
        return 'planning_consultant'
    if profile and profile.company_status != 'active':
        return 'inactive'
    if re.search(r'\b(?:architect\w*|interior\s+design\w*|design|studio)\b', company, re.I) or (profile and SICS.intersection(profile.sic_codes)):
        return 'architect'
    return 'unknown'


def site_people(http, website):
    """Extract literal name/role pairs; email is tied to a small person card."""
    people, pages, queue = [], [], [website] if website else []
    for target in queue:
        if len(pages) >= 4:
            break
        try:
            soup = BeautifulSoup(http.get(target), 'html.parser')
        except Exception:
            if target == website:
                raise
            continue
        text = soup.get_text(' ', strip=True)
        if parking_page_reason(text):
            continue
        published = text + ' ' + ' '.join(a['href'] for a in soup.select('a[href^="mailto:"]'))
        for email in sorted(set(re.findall(EMAIL, published))):
            if email.split('@')[0].lower() in GENERIC:
                people.append({'email': email, 'generic': True, 'source': 'site', 'source_url': target, 'evidence': email})
        pages.append({'source_url': target, 'text': text})
        for a in soup.select('a[href]'):
            link = urljoin(target, a['href'])
            if urlsplit(link).netloc == urlsplit(website).netloc and re.search(r'team|about|people|contact', a.get_text(' ', strip=True) + ' ' + a['href'], re.I) and link not in queue and len(queue) < 4:
                queue.append(link)
        for card in soup.select('article, li, .team-member, .person') + [h.parent for h in soup.select('h2, h3, h4')]:
            content = card.get_text(' ', strip=True)
            if len(content) > 700:
                continue
            match = re.search(r'\b([A-Z][a-z]+(?:[ -][A-Z][a-z]+){1,3})\s*[,|:–—-]?\s+(Director|Partner|Principal|Associate|Architect|Interior Designer)\b', content)
            if not match:
                continue
            emails = set(re.findall(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', content + ' ' + ' '.join(a['href'] for a in card.select('a[href^="mailto:"]'))))
            people.append({'name': match[1], 'role': match[2], 'email': next(iter(emails)) if len(emails) == 1 else None, 'source_url': target, 'evidence': content, 'source': 'site', 'generic': bool(emails and next(iter(emails)).split('@')[0].lower() in GENERIC)})
    unique = {}
    for person in people:
        unique.setdefault((person.get('name'), person.get('role'), person.get('email')), person)
    return list(unique.values()), pages


GENERIC = {'info', 'studio', 'hello', 'office', 'contact', 'enquiries', 'admin', 'mail'}
EMAIL = r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'


def firm_name(value):
    tokens = re.findall(r'[a-z0-9]+', clean(value).casefold())
    return ' '.join('architect' if t in {'architects', 'architecture', 'architect'} else t
                    for t in tokens if t not in {'ltd', 'limited', 'llp', 'plc', 'and'})


def match_company(registry, company):
    candidates = {}
    queries = [company]
    if firm_name(company) != company.casefold():
        queries.append(firm_name(company))
    for query in queries:
        for hit in registry.search_companies(query)[:10]:
            if getattr(hit, 'company_status', None) not in (None, 'active'):
                continue
            if firm_name(hit.title) != firm_name(company):
                continue
            profile = registry.get_company_profile(hit.company_number)
            if profile and profile.company_status == 'active' and firm_name(profile.company_name) == firm_name(company):
                candidates[profile.company_number] = profile
    evidence = [{'company_number': p.company_number, 'score': 100 if SICS.intersection(p.sic_codes) else 90,
                 'reason': 'Active; normalized name exact' + ('; relevant SIC' if SICS.intersection(p.sic_codes) else '')}
                for p in candidates.values()]
    return (next(iter(candidates.values())) if len(candidates) == 1 else None,
            {'ch_match_status': 'verified' if len(candidates) == 1 else 'needs_review' if candidates else 'unmatched',
             'ch_matches': evidence})


def verify_site(http, url, company, profile=None):
    parts = urlsplit(url if '://' in url else 'https://' + url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        return None
    url = 'https://' + parts.netloc + '/'
    text = BeautifulSoup(http.get(url), 'html.parser').get_text(' ', strip=True)
    if parking_page_reason(text) or not firm_name(company) or not re.search(r'(?<!\w)' + re.escape(firm_name(company)) + r'(?!\w)', firm_name(text)):
        return None
    address = str(getattr(profile, 'registered_office_address', '') or '')
    postcodes = re.findall(r'\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b', address, re.I)
    postcode = any(re.sub(r'\s', '', p).lower() in re.sub(r'\s', '', text).lower() for p in postcodes)
    uk = bool(re.search(r'\b(?:London|United Kingdom|UK)\b|(?:\+44|020)\s*\d', text, re.I))
    return {'website': url, 'website_verified': True, 'website_evidence': {'source_url': url, 'name_match': True, 'uk_signal': uk, 'postcode_match': postcode, 'score': 80 + 10 * uk + 10 * postcode}}


def firm_report(repo):
    counts = dict.fromkeys(('firms_seen', 'architect_firms', 'CH_verified', 'site_person', 'apollo_person', 'email_found', 'reveals_used'), 0)
    firms = []
    for row in repo.rows('SELECT * FROM planning_firm_trials ORDER BY firm_key'):
        data = json.loads(row['payload'])
        counts['firms_seen'] += 1
        counts['architect_firms'] += data.get('classification') == 'architect'
        counts['CH_verified'] += bool(data.get('CH_verified'))
        for source in ('site', 'apollo'):
            counts[source + '_person'] += any(p.get('source') == source for p in data.get('people', []))
        counts['email_found'] += any(p.get('email') for p in data.get('people', []))
        counts['reveals_used'] += data.get('reveals_used', 0)
        firms.append({**row, 'payload': data})
    totals = {k: 0 for k in ('firms', 'architect', 'design_build', 'website_verified', 'direct_email', 'generic_email', 'serp_calls', 'apollo_searches', 'reveals')}
    for firm in firms:
        d = firm['payload']
        totals['firms'] += 1
        totals['architect'] += d.get('classification') == 'architect'
        totals['design_build'] += d.get('classification') == 'design_build_contractor'
        totals['website_verified'] += bool(d.get('website_verified'))
        for generic in (False, True):
            totals['generic_email' if generic else 'direct_email'] += len({p['email'] for p in d.get('people', []) if p.get('email') and bool(p.get('generic')) == generic})
        for field in ('serp_calls', 'apollo_searches'):
            totals[field] += sum(c.get('provider') == ('serpapi' if field == 'serp_calls' else 'apollo') for c in d.get('search_calls', []))
        totals['reveals'] += d.get('reveals_used', 0)
    counts['site_person'] = sum(sum(bool(p.get('name')) and p.get('source') == 'site' for p in f['payload'].get('people', [])) for f in firms)
    counts['apollo_person'] = sum(sum(p.get('source') == 'apollo' for p in f['payload'].get('people', [])) for f in firms)
    return {**counts, **totals, 'firms': totals['firms'], 'firm_records': firms, 'design_build_firms': [f for f in firms if f['payload'].get('classification') == 'design_build_contractor'], 'status': 'trial_review'}


class FirmTrial:
    def __init__(self, planning, http=None, apollo=None, serp=None):
        self.planning, self.repo = planning, planning.repo
        self.now = planning.outreach.now
        self.http = http or TrialHTTP(self.repo, self.now)
        self.apollo = apollo or FirmTrialApollo()
        self.serp = serp
        self.reveals_used = 0

    def save(self, key, data):
        old = self.repo.rows('SELECT payload FROM planning_firm_trials WHERE firm_key=?', (key,))
        merged, candidates = merge_trusted(json.loads(old[0]['payload']), data)
        merged.setdefault('field_provenance', {}).update(candidates)
        self.repo.execute('UPDATE planning_firm_trials SET payload=? WHERE firm_key=?', (json.dumps(merged), key))

    def run(self, limit=3, authorities=(), max_reveals=10, max_searches=25, retry_incomplete=False):
        if not 1 <= limit <= 500 or not 0 <= max_reveals <= 100:
            raise ValueError('Invalid firm/reveal limit')
        self.reveals_used = 0
        self.serp_calls = 0
        if not 0 <= max_searches <= 500:
            raise ValueError('Invalid search limit')
        groups = {}
        allowed = {a.casefold() for a in authorities}
        for project in self.repo.rows('SELECT * FROM planning_projects'):
            company = clean(project['agent_company'])
            if company:
                groups.setdefault(company, []).append(project)
        groups = {company: projects for company, projects in groups.items() if not allowed or any(p['authority'].casefold() in allowed for p in projects)}
        def rank(item):
            company, projects = item
            live = [p for p in projects if not re.search(r'refus|withdraw|reject|complete', p.get('status') or '', re.I)]
            return (classify(company) == 'architect', max((fit(p['description'], p['application_type'] or '')[0] for p in live), default=0), max((p.get('validated_date') or '' for p in live), default=''), len(live))
        attempted = 0
        for company, projects in sorted(groups.items(), key=rank, reverse=True):
            alias = 'name:' + name_key(company)
            existing = self.repo.rows('SELECT * FROM planning_firm_trials WHERE firm_key=? OR company=? OR firm_key IN (SELECT firm_key FROM planning_firm_aliases WHERE name_key=?)', (alias, company, alias))
            if existing:
                key = existing[0]['firm_key']
                for p in projects:
                    self.repo.execute('INSERT OR IGNORE INTO planning_firm_projects VALUES(?,?)', (key, p['id']))
                old_data = json.loads(existing[0]['payload'])
                if retry_incomplete and attempted < limit and (not old_data.get('website_verified') or not old_data.get('CH_verified')):
                    attempted += 1
                    self.retry(key, company, old_data, max_searches)
                continue
            if attempted >= limit:
                break
            attempted += 1
            # Reserve a name attempt before registry calls, including negative results.
            with connection_scope(self.repo.db_path) as db:
                claimed = db.execute('INSERT OR IGNORE INTO planning_firm_trials(firm_key,company,attempted_at) VALUES(?,?,?)', (alias, company, stamp(self.now()))).rowcount
            if not claimed:
                continue
            key = alias
            data = {'classification': classify(company), 'people': [], 'reveals_used': 0}
            try:
                profile, matching = match_company(self.planning.companies_house, company)
                data.update(matching)
                if profile:
                    key = 'ch:' + profile.company_number
                    prior = self.repo.rows('SELECT * FROM planning_firm_trials WHERE firm_key=?', (key,))
                    if prior:
                        self.repo.execute('DELETE FROM planning_firm_trials WHERE firm_key=?', (alias,))
                    else:
                        self.repo.execute('UPDATE planning_firm_trials SET firm_key=?, company_number=? WHERE firm_key=?', (key, profile.company_number, alias))
                    data.update(classification=classify(company, profile), CH_verified=profile.company_status == 'active', sic_codes=profile.sic_codes, ch_source=profile.source_url)
                    self.repo.execute('INSERT OR REPLACE INTO planning_firm_aliases VALUES(?,?)', (alias, key))
                    if prior:
                        for p in projects:
                            self.repo.execute('INSERT OR IGNORE INTO planning_firm_projects VALUES(?,?)', (key, p['id']))
                        continue
                for p in projects:
                    self.repo.execute('INSERT OR IGNORE INTO planning_firm_projects VALUES(?,?)', (key, p['id']))
                if data['classification'] not in ('architect', 'design_build_contractor'):
                    self.save(key, data)
                    continue
                website = self.discover(key, company, data, profile, max_searches)
                people = []
                if website:
                    try:
                        people, data['pages'] = site_people(self.http, website)
                    except Exception as exc:
                        data['site_reason'] = type(exc).__name__
                found = []
                if data['classification'] == 'architect':
                    self.record_call(key, data, 'apollo', 'people_search')
                    found = self.apollo.search(company, urlsplit(website).hostname if website else None)
                    found = [p for p in found if firm_name(clean((p.get('organization') or {}).get('name'))) == firm_name(company) or (website and (p.get('organization') or {}).get('primary_domain') == urlsplit(website).hostname)]
                for person in found:
                    if not person.get('id'):
                        continue
                    # Free search surnames may be obfuscated: retain only supplied first name.
                    people.append({'name': clean(person.get('first_name')), 'role': person.get('title'), 'source': 'apollo', 'source_url': 'apollo-free:' + str(person['id'])})
                data['people'] = people
                if not people and profile:
                    data['people'] = self.planning.companies_house.get_officers(profile.company_number)
                # Reserve and persist each paid attempt BEFORE calling, never retry.
                if os.getenv('OUTREACH_APOLLO_REVEAL_ENABLED', '').lower() == 'true' and not any(p.get('email') for p in people):
                    ranked = sorted([p for p in people if p.get('source') == 'apollo'], key=lambda p: next((i for i, title in enumerate(('director', 'partner', 'principal', 'associate', 'architect', 'interior designer')) if title in (p.get('role') or '').casefold()), 99))
                    for person in ranked[:2]:
                        if self.reveals_used >= max_reveals:
                            break
                        self.reveals_used += 1
                        data['reveals_used'] += 1
                        self.repo.execute('UPDATE planning_firm_trials SET payload=? WHERE firm_key=?', (json.dumps({k: v for k, v in data.items() if k != 'people'}), key))
                        revealed = self.apollo.reveal(person['source_url'].removeprefix('apollo-free:'))
                        email = clean(revealed.get('email'))
                        if re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', email):
                            person['email'] = email
                            person['email_evidence'] = revealed
            except Exception as exc:
                data['reason'] = type(exc).__name__
            self.save(key, data)
        return {**firm_report(self.repo), 'run_reveals_used': self.reveals_used}

    def record_call(self, key, data, provider, query):
        data.setdefault('search_calls', []).append({'provider': provider, 'query': query, 'at': stamp(self.now()), 'estimated_gbp': 0.01 if provider == 'serpapi' else 0})
        # Reserve cost before invoking any client, including failed calls.
        self.persist_progress(key, data)

    def persist_progress(self, key, data):
        old = json.loads(self.repo.rows('SELECT payload FROM planning_firm_trials WHERE firm_key=?', (key,))[0]['payload'])
        merged, candidates = merge_trusted(old, data)
        for field in ('people', 'pages', 'search_calls'):
            merged[field] = list(old.get(field, []))
            for item in data.get(field, []):
                if item not in merged[field]:
                    merged[field].append(item)
            candidates.pop(field + '_candidate', None)
        # Classification is derived locally, not an extracted identity field.
        merged['classification'] = data.get('classification', old.get('classification'))
        merged['field_provenance'] = {**old.get('field_provenance', {}), **data.get('field_provenance', {}), **candidates}
        self.repo.execute('UPDATE planning_firm_trials SET payload=? WHERE firm_key=?', (json.dumps(merged), key))

    def discover(self, key, company, data, profile, max_searches):
        if data.get('website_verified') and data.get('website'):
            return data['website']
        def attempt(url):
            if not url:
                return None
            try:
                verified = verify_site(self.http, url, company, profile)
            except Exception:
                verified = None
            if verified:
                if data.get('website') and data['website'] != verified['website']:
                    data.setdefault('field_provenance', {})['website_candidate'] = verified
                    return None
                data.update(verified)
                return verified['website']
            data.setdefault('field_provenance', {})['website_candidate'] = url
            return None
        if hasattr(self.apollo, 'organizations') and getattr(self.apollo, 'api_key', True):
            self.record_call(key, data, 'apollo', 'organization_search: ' + company)
            try:
                organizations = self.apollo.organizations(company)
            except Exception as exc:
                data['apollo_org_error'] = type(exc).__name__
                organizations = []
            for org in organizations:
                if firm_name(org.get('name')) == firm_name(company):
                    website = attempt(org.get('primary_domain') or org.get('website_url'))
                    if website:
                        return website
        if self.serp is None:
            from scrapers.google_search_scraper import GoogleSearchScraper
            self.serp = GoogleSearchScraper(enable_delays=False)
        if self.serp_calls >= max_searches or not getattr(self.serp, 'api_key', True):
            return None
        query = company + ' architects London'
        self.serp_calls += 1
        self.record_call(key, data, 'serpapi', query)
        for result in self.serp.scrape(query, max_results=5):
            if result.success:
                website = attempt(result.raw_data.get('link'))
                if website:
                    return website
        return None

    def retry(self, key, company, data, max_searches):
        profile = None
        if not data.get('CH_verified'):
            profile, matching = match_company(self.planning.companies_house, company)
            # Negative/review outcomes are operational state, not trusted identity.
            data.update(matching)
            if profile:
                data.update(CH_verified=True, ch_source=profile.source_url, sic_codes=profile.sic_codes)
                current = self.repo.rows('SELECT company_number FROM planning_firm_trials WHERE firm_key=?', (key,))[0]['company_number']
                if current and current != profile.company_number:
                    data.setdefault('field_provenance', {})['company_number_candidate'] = profile.company_number
                if not self.repo.rows('SELECT 1 FROM planning_firm_trials WHERE company_number=? AND firm_key<>?', (profile.company_number, key)):
                    self.repo.execute('UPDATE planning_firm_trials SET company_number=? WHERE firm_key=? AND company_number IS NULL', (profile.company_number, key))
        data['classification'] = classify(company, profile)
        if data['classification'] in ('architect', 'design_build_contractor') and not data.get('website_verified'):
            website = self.discover(key, company, data, profile, max_searches)
            if website:
                try:
                    people, pages = site_people(self.http, website)
                    existing = data.setdefault('people', [])
                    for person in people:
                        prior = next((p for p in existing if p.get('name') and p.get('name') == person.get('name') and p.get('source') == person.get('source')), None)
                        if prior:
                            merged, candidates = merge_trusted(prior, person)
                            prior.update(merged)
                            if candidates:
                                prior.setdefault('field_provenance', {}).update(candidates)
                        elif person not in existing:
                            existing.append(person)
                    data.setdefault('pages', []).extend(pages)
                except Exception as exc:
                    data['site_reason'] = type(exc).__name__
        self.persist_progress(key, data)
