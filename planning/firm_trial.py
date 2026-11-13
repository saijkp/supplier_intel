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
    if profile and profile.company_status != 'active':
        return 'inactive'
    if profile and '71111' in profile.sic_codes:
        return 'architect'
    if re.search(r'\b(?:build|construction|contractors?)\b', company, re.I):
        return 'design_build_contractor'
    if re.search(r'\b(?:DP9|Turley|Savills|Firstplan|CMA|Newmark|Lichfields|planning|survey\w*|property|estates|agent)\b', company, re.I):
        return 'planning_consultant'
    if re.search(r'\b(?:architect\w*|interior\s+design\w*|design|studio)\b', company, re.I) or (profile and SICS.intersection(profile.sic_codes)):
        return 'architect'
    return 'unknown'


def site_people(http, website, company=None, generic_contacts=None):
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
            match = re.search(r'\b([A-Z][a-z]+(?:[ -][A-Z][a-z]+){1,3}?)\s*[,|:\u2013\u2014-]?\s+(Associate Director|(?:Design |Managing |Finance |Operations |Marketing |HR |IT |Business Development |Accounts )?Director|Partner(?: and [A-Za-z ]{1,40}Director)?|Principal|Founder|Owner|Associate|Architect|Interior Designer)\b', content)
            if not match or not human_name(match[1], company):
                continue
            emails = set(re.findall(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', content + ' ' + ' '.join(a['href'] for a in card.select('a[href^="mailto:"]'))))
            people.append({'name': match[1], 'role': match[2], 'email': next(iter(emails)) if len(emails) == 1 else None, 'source_url': target, 'evidence': content, 'source': 'site', 'generic': bool(emails and next(iter(emails)).split('@')[0].lower() in GENERIC)})
    unique = {}
    for person in people:
        unique.setdefault((person.get('name'), person.get('role'), person.get('email')), person)
    records = list(unique.values())
    if generic_contacts is not None:
        generic_contacts.extend(p for p in records if not p.get('name'))
    return [p for p in records if human_name(p.get('name'), company)], pages


GENERIC = {'info', 'studio', 'hello', 'office', 'contact', 'enquiries', 'admin', 'mail'}
EMAIL = r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'


def firm_name(value):
    tokens = re.findall(r'[a-z0-9]+', clean(value).casefold())
    return ' '.join('architect' if t in {'architects', 'architecture', 'architect'} else t
                    for t in tokens if t not in {'ltd', 'limited', 'llp', 'plc', 'and'})



def domain(value):
    return (urlsplit(value if '://' in clean(value) else 'https://' + clean(value)).hostname or '').lower().removeprefix('www.')


def human_name(value, company=None):
    tokens = re.findall(r"[^\W\d_]+(?:[-'][^\W\d_]+)*", clean(value), re.UNICODE)
    banned = {'people', 'partners', 'team', 'studio', 'contact', 'associates', 'directors', 'our', 'meet', 'about'}
    return len(tokens) >= 2 and not banned.intersection(t.casefold() for t in tokens) and (not company or firm_name(value) != firm_name(company)) and bool(re.fullmatch(r"[^\W\d_]+(?:[ ,.'-]+[^\W\d_]+)*\.?", clean(value), re.UNICODE))


def role_priority(person):
    role = clean(person.get('role')).casefold()
    if re.search(r'\b(?:financ\w*|operations?|hr|human resources|office manager|marketing|business development|accounts?|it|information technology|practice manager)\b', role):
        return 99
    if 'associate director' in role:
        return 1
    if any(t in role for t in ('director', 'partner', 'principal', 'founder', 'owner')):
        return 0
    return 2 if 'associate' in role else 3 if any(t in role for t in ('architect', 'interior designer')) else 99


def scoped(people, searched_domain):
    """Apollo free search omits the organisation domain on obfuscated hits.

    A search filtered by q_organization_domains_list only returns people at
    that domain, so stamp it where Apollo left it blank (never overwrite).
    """
    if not searched_domain:
        return people
    out = []
    for person in people or []:
        person = dict(person)
        if not (person.get('organization_domain') or (person.get('organization') or {}).get('primary_domain')):
            person['organization_domain'] = domain(searched_domain)
        out.append(person)
    return out


def reveal_candidate(person):
    person_id = clean(person.get('apollo_id') or person.get('id')) or clean(person.get('source_url')).removeprefix('apollo-free:')
    if not person_id:
        return None
    return {'apollo_id': person_id, 'first_name': clean(person.get('first_name')) or clean(person.get('name')).split(' ')[0],
            'role': person.get('title') or person.get('role'),
            'organization_domain': person.get('organization_domain') or (person.get('organization') or {}).get('primary_domain'),
            'source': 'apollo', 'source_url': 'apollo-free:' + person_id}


def collect_reveal_candidates(data, found=()):
    candidates = data.setdefault('reveal_candidates', [])
    legacy = data.get('people', []) + data.get('apollo_candidates', []) + data.get('field_provenance', {}).get('rejected_people', [])
    for person in list(found) + [p for p in legacy if p.get('source') == 'apollo']:
        candidate = reveal_candidate(person)
        if candidate and candidate not in candidates:
            candidates.append(candidate)


def sanitize_contacts(data, company):
    collect_reveal_candidates(data)
    non_design = next((p for p in data.get('people', [])
                       if p.get('email_evidence') and role_priority(p) == 99), None)
    if non_design:
        data['review_status'] = 'needs_review'
        note = ('revealed contact is finance role; consider design partner'
                if 'financ' in clean(non_design.get('role')).casefold()
                else 'revealed contact is non-design role; consider design decision-maker')
        data.setdefault('field_provenance', {})['role_review'] = note
    if data.get('review_status') == 'needs_review' and data.get('field_provenance', {}).get('identity_review'):
        candidates = data['field_provenance'].setdefault('people_candidates', [])
        for person in data.get('people', []):
            if person not in candidates:
                candidates.append(person)
        data['people'] = []
        data['website_verified'] = False
    people, generic = [], list(data.get('generic_contacts', []))
    rejected = data.setdefault('field_provenance', {}).setdefault('rejected_people', [])
    for original in data.get('people', []):
        person = dict(original)
        if (not human_name(person.get('name'), company)
                or (person.get('source') == 'apollo' and not person.get('email_evidence'))):
            if person.get('email') and person['email'].split('@')[0].lower() in GENERIC:
                person['generic'] = True
                if person not in generic:
                    generic.append(person)
            elif original not in rejected:
                rejected.append(original)
            continue
        people.append(person)
    for person in people + generic:
        email = person.get('email')
        if email and (not data.get('website_verified') or domain(email.split('@')[-1]) != domain(data.get('website'))):
            data.setdefault('field_provenance', {}).setdefault('email_candidates', []).append({'email': email, 'source_url': person.get('source_url'), 'reason': 'Email domain not verified for firm'})
            person['email_candidate'] = person.pop('email')
            person.setdefault('field_provenance', {})['email_candidate'] = email
    data.update(people=people, generic_contacts=generic)


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


def site_identity_conflict(text, profile):
    """Explicit legal identifiers override fuzzy name and UK signals."""
    number = clean(getattr(profile, 'company_number', None)).upper()
    numbers = re.findall(r'(?:company\s*(?:number|no\.?|registration number)|registered\s*(?:number|no\.?))\s*[:#]?\s*([A-Z]{2}\d{6}|\d{8})\b', text, re.I)
    if number and numbers and any(n.upper() != number for n in numbers):
        return True
    address = str(getattr(profile, 'registered_office_address', '') or '')
    offices = re.findall(r'registered office(?: address)?\s*:?\s*(.{0,220})', text, re.I)
    expected = re.findall(r'\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b', address, re.I)
    for office in offices:
        expected_street = re.search(r'\b(\d+[A-Za-z]?)\s+([A-Za-z]+)', address)
        actual_street = re.search(r'\b(\d+[A-Za-z]?)\s+([A-Za-z]+)', office)
        if expected_street and actual_street and expected_street.group(0).casefold() != actual_street.group(0).casefold():
            return True
        actual = re.findall(r'\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b', office, re.I)
        if actual and expected and not {re.sub(r'\s', '', p).upper() for p in actual}.intersection(re.sub(r'\s', '', p).upper() for p in expected):
            return True
    return False


def verify_site(http, url, company, profile=None, rejection=None):
    parts = urlsplit(url if '://' in url else 'https://' + url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        return None
    url = 'https://' + parts.netloc + '/'
    pages, queue = [], [url]
    for target in queue:
        try:
            soup = BeautifulSoup(http.get(target), 'html.parser')
        except Exception:
            if target == url:
                raise
            continue
        page_text = soup.get_text(' ', strip=True)
        if parking_page_reason(page_text):
            if target == url:
                return None
            continue
        pages.append({'source_url': target, 'text': page_text})
        for link in soup.select('a[href]'):
            href = urljoin(target, link['href']).split('#')[0]
            parts = urlsplit(href)
            if (parts.scheme == 'https' and not parts.username and not parts.password
                    and domain(href) == domain(url)
                    and re.search(r'contact|about|legal|terms|privacy', link.get_text(' ', strip=True) + ' ' + parts.path, re.I)
                    and href not in queue and len(queue) < 4):
                queue.append(href)
    text = ' '.join(p['text'] for p in pages)
    if site_identity_conflict(text, profile):
        if rejection is not None:
            rejection.update(reason='Site legal identity conflicts with Companies House match', pages=pages, company_number=getattr(profile, 'company_number', None), registered_office_address=getattr(profile, 'registered_office_address', None))
        return None
    if not firm_name(company) or not re.search(r'(?<!\w)' + re.escape(firm_name(company)) + r'(?!\w)', firm_name(text)):
        return None
    address = str(getattr(profile, 'registered_office_address', '') or '')
    postcodes = re.findall(r'\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b', address, re.I)
    postcode = any(re.sub(r'\s', '', p).lower() in re.sub(r'\s', '', text).lower() for p in postcodes)
    outward = any(re.search(r'(?<!\w)' + re.escape(p.split()[0] if ' ' in p else p[:-3]) + r'\s*\d[A-Z]{2}\b', text, re.I) for p in postcodes)
    uk = bool(re.search(r'\b(?:London|Manchester|Birmingham|Bristol|Leeds|Liverpool|Sheffield|Nottingham|Leicester|Cardiff|Edinburgh|Glasgow|Belfast|Newcastle|Oxford|Cambridge|(?<!New )York|Southampton|Brighton|United Kingdom|UK)\b|(?:\+44\s*(?:\(0\))?|\b0)[1-9][\d ()-]{8,12}\d|\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b', text, re.I))
    if not uk:
        return None
    return {'website': url, 'website_verified': True, 'website_evidence': {'source_url': url, 'pages': pages, 'name_match': True, 'uk_signal': uk, 'postcode_match': postcode, 'outward_match': outward, 'score': 80 + 10 * uk + 10 * postcode + 5 * outward}}


def firm_report(repo):
    counts = dict.fromkeys(('firms_seen', 'architect_firms', 'CH_verified', 'site_person', 'apollo_person', 'email_found', 'reveals_used'), 0)
    firms = []
    for row in repo.rows('SELECT * FROM planning_firm_trials ORDER BY firm_key'):
        data = json.loads(row['payload'])
        sanitize_contacts(data, row['company'])
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
            totals['generic_email' if generic else 'direct_email'] += len({p['email'] for p in d.get('people', []) + d.get('generic_contacts', []) if p.get('email') and bool(p.get('generic')) == generic})
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
        self.persist_progress(key, data)

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
        # Local repairs must not depend on network retry ranking or budget.
        from types import SimpleNamespace
        for row in self.repo.rows('SELECT * FROM planning_firm_trials'):
            data = json.loads(row['payload'])
            profile = SimpleNamespace(company_status='active', sic_codes=data.get('sic_codes', [])) if data.get('CH_verified') else None
            data['classification'] = classify(row['company'], profile)
            sanitize_contacts(data, row['company'])
            self.persist_progress(row['firm_key'], data)
        attempted = 0
        for company, projects in sorted(groups.items(), key=rank, reverse=True):
            alias = 'name:' + name_key(company)
            existing = self.repo.rows('SELECT * FROM planning_firm_trials WHERE firm_key=? OR company=? OR firm_key IN (SELECT firm_key FROM planning_firm_aliases WHERE name_key=?)', (alias, company, alias))
            if existing:
                key = existing[0]['firm_key']
                for p in projects:
                    self.repo.execute('INSERT OR IGNORE INTO planning_firm_projects VALUES(?,?)', (key, p['id']))
                old_data = json.loads(existing[0]['payload'])
                if retry_incomplete and attempted < limit:
                    attempted += 1
                    try:
                        self.retry(key, company, old_data, max_searches)
                        self.reveal_best(key, company, old_data, max_reveals)
                    except Exception as exc:
                        old_data['retry_reason'] = type(exc).__name__
                    self.persist_progress(key, old_data)
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
                    data.update(classification=classify(company, profile), CH_verified=profile.company_status == 'active', sic_codes=profile.sic_codes, ch_source=profile.source_url, registered_office_address=getattr(profile, 'registered_office_address', None))
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
                        people, data['pages'] = site_people(self.http, website, company, data.setdefault('generic_contacts', []))
                    except Exception as exc:
                        data['site_reason'] = type(exc).__name__
                found = []
                if data['classification'] == 'architect':
                    self.record_call(key, data, 'apollo', 'people_search')
                    found = scoped(self.apollo.search(company, urlsplit(website).hostname if website else None), urlsplit(website).hostname if website else None)
                    found = [p for p in found if firm_name(clean((p.get('organization') or {}).get('name'))) == firm_name(company) or (website and (p.get('organization') or {}).get('primary_domain') == urlsplit(website).hostname)]
                collect_reveal_candidates(data, found)
                data['people'] = people
                if not people and profile:
                    data['people'] = self.planning.companies_house.get_officers(profile.company_number)
                self.reveal_best(key, company, data, max_reveals)
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
        for field in ('people', 'generic_contacts', 'pages', 'search_calls', 'apollo_candidates', 'reveal_candidates'):
            merged[field] = list(old.get(field, []))
            for item in data.get(field, []):
                if item not in merged[field]:
                    merged[field].append(item)
            candidates.pop(field + '_candidate', None)
        if 'reveal_log' in data:
            merged['reveal_log'] = data['reveal_log']
        merged['reveals_used'] = max(old.get('reveals_used', 0), data.get('reveals_used', 0))
        # Classification is derived locally, not an extracted identity field.
        merged['classification'] = data.get('classification', old.get('classification'))
        merged['field_provenance'] = {**old.get('field_provenance', {}), **data.get('field_provenance', {}), **candidates}
        for field in ('website_verified', 'website_evidence', 'classification', 'review_status'):
            if field in data:
                merged[field] = data[field]
        sanitize_contacts(merged, self.repo.rows('SELECT company FROM planning_firm_trials WHERE firm_key=?', (key,))[0]['company'])
        self.repo.execute('UPDATE planning_firm_trials SET payload=? WHERE firm_key=?', (json.dumps(merged), key))

    def discover(self, key, company, data, profile, max_searches):
        if data.get('website_verified') and data.get('website'):
            return data['website']
        def attempt(url):
            if not url:
                return None
            try:
                rejection = {}
                verified = verify_site(self.http, url, company, profile, rejection)
                if rejection:
                    data['review_status'] = 'needs_review'
                    data.setdefault('field_provenance', {})['identity_review'] = rejection
            except Exception:
                verified = None
            if verified:
                if data.get('website') and data['website'] != verified['website']:
                    data.setdefault('field_provenance', {})['website_candidate'] = verified
                    return None
                data.update(verified)
                return verified['website']
            data.setdefault('field_provenance', {})['website_candidate'] = url
            data['review_status'] = 'needs_review'
            return None
        verified_candidates = []
        original_website = data.get('website')
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
                        verified_candidates.append(dict(website=website, website_verified=True, website_evidence=dict(data['website_evidence'])))
                        if original_website:
                            data['website'] = original_website
                        else:
                            data.pop('website', None)
                        data['website_verified'] = False
        if self.serp is None:
            from scrapers.google_search_scraper import GoogleSearchScraper
            self.serp = GoogleSearchScraper(enable_delays=False)
        if verified_candidates and (not getattr(profile, 'registered_office_address', None) or any(c['website_evidence']['outward_match'] for c in verified_candidates)):
            data.update(max(verified_candidates, key=lambda c: c['website_evidence']['score']))
            return data['website']
        if self.serp_calls >= max_searches or not getattr(self.serp, 'api_key', True):
            if verified_candidates:
                data.update(max(verified_candidates, key=lambda c: c['website_evidence']['score']))
                return data['website']
            return None
        query = company + ' architects London'
        self.serp_calls += 1
        self.record_call(key, data, 'serpapi', query)
        for result in self.serp.scrape(query, max_results=5):
            if result.success:
                website = attempt(result.raw_data.get('link'))
                if website:
                    verified_candidates.append(dict(website=website, website_verified=True, website_evidence=dict(data['website_evidence'])))
                    if original_website:
                        data['website'] = original_website
                    else:
                        data.pop('website', None)
                    data['website_verified'] = False
        if verified_candidates:
            data.update(max(verified_candidates, key=lambda c: c['website_evidence']['score']))
            return data['website']
        return None

    def retry(self, key, company, data, max_searches):
        from types import SimpleNamespace
        profile = SimpleNamespace(company_status='active', sic_codes=data.get('sic_codes', []), registered_office_address=data.get('registered_office_address'), company_number=key.removeprefix('ch:') if key.startswith('ch:') else None) if data.get('CH_verified') else None
        data['classification'] = classify(company, profile)
        sanitize_contacts(data, company)
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
        if profile and not getattr(profile, 'registered_office_address', None):
            number = self.repo.rows('SELECT company_number FROM planning_firm_trials WHERE firm_key=?', (key,))[0]['company_number']
            if number:
                profile = self.planning.companies_house.get_company_profile(number) or profile
        if profile and not getattr(profile, 'company_number', None):
            profile.company_number = self.repo.rows('SELECT company_number FROM planning_firm_trials WHERE firm_key=?', (key,))[0]['company_number']
        data['classification'] = classify(company, profile)
        if '71111' in data.get('sic_codes', []) and data['classification'] == 'design_build_contractor':
            data['classification'] = 'architect'
        if data.get('website'):
            try:
                rejection = {}
                verified = verify_site(self.http, data['website'], company, profile, rejection)
                if rejection:
                    data['review_status'] = 'needs_review'
                    data.setdefault('field_provenance', {})['identity_review'] = rejection
            except Exception:
                verified = None
            data['website_verified'] = bool(verified)
            if verified:
                data.update(verified)
            else:
                data.setdefault('field_provenance', {}).setdefault('website_reverification', []).append({'website': data['website'], 'reason': 'Failed name/UK re-verification', 'at': stamp(self.now())})
                data['field_provenance']['website_candidate'] = data['website']
        if data['classification'] in ('architect', 'design_build_contractor'):
            website = data.get('website') if data.get('website_verified') else self.discover(key, company, data, profile, max_searches)
            if website:
                try:
                    people, pages = site_people(self.http, website, company, data.setdefault('generic_contacts', []))
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
        collect_reveal_candidates(data)
        self.persist_progress(key, data)

    def reveal_best(self, key, company, data, max_reveals):
        collect_reveal_candidates(data)
        sanitize_contacts(data, company)
        if (os.getenv('OUTREACH_APOLLO_REVEAL_ENABLED', '').lower() != 'true'
                or self.reveals_used >= max_reveals or data.get('reveals_used')
                or not data.get('website_verified') or not data.get('CH_verified')
                or data.get('classification') != 'architect'
                or any(p.get('email') and not p.get('generic') for p in data['people'])):
            return
        candidates = data['reveal_candidates']
        if not any(domain(p.get('organization_domain')) == domain(data.get('website')) for p in candidates):
            self.record_call(key, data, 'apollo', 'people_search_for_reveal')
            collect_reveal_candidates(data, scoped(self.apollo.search(company, domain(data['website'])), domain(data['website'])))
        ranked = sorted((p for p in candidates if p.get('source') == 'apollo'
                         and domain(p.get('organization_domain')) == domain(data.get('website'))
                         and role_priority(p) < 99), key=role_priority)
        if not ranked:
            return
        person = ranked[0]
        self.reveals_used += 1
        data['reveals_used'] = data.get('reveals_used', 0) + 1
        entry = {'firm': company, 'person': person.get('first_name'), 'title': person.get('role'), 'person_id': person['apollo_id'], 'credit_used': 1, 'at': stamp(self.now()), 'status': 'reserved'}
        data.setdefault('reveal_log', []).append(entry)
        self.persist_progress(key, data)
        try:
            revealed = self.apollo.reveal(person['apollo_id'])
            entry['status'] = 'completed'
            email = clean(revealed.get('email'))
            name = clean(revealed.get('name')) or ' '.join(filter(None, [clean(revealed.get('first_name')), clean(revealed.get('last_name'))]))
            if human_name(name, company) and re.fullmatch(EMAIL, email):
                record = {**person, 'name': name, 'email': email, 'email_evidence': revealed}
                if record not in data['people']:
                    data['people'].append(record)
            else:
                entry['result_reason'] = 'Revealed full human name and email required'
        except Exception as exc:
            entry['status'] = type(exc).__name__
            raise
        finally:
            self.persist_progress(key, data)
