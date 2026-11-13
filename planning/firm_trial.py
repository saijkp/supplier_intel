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
        soup = BeautifulSoup(http.get(target), 'html.parser')
        text = soup.get_text(' ', strip=True)
        if parking_page_reason(text):
            continue
        pages.append({'source_url': target, 'text': text})
        for a in soup.select('a[href]'):
            link = urljoin(target, a['href'])
            if urlsplit(link).netloc == urlsplit(website).netloc and re.search(r'team|about|contact', a.get_text(' ', strip=True) + ' ' + a['href'], re.I) and link not in queue and len(queue) < 4:
                queue.append(link)
        for card in soup.select('article, li, .team-member, .person') + [h.parent for h in soup.select('h2, h3, h4')]:
            content = card.get_text(' ', strip=True)
            if len(content) > 700:
                continue
            match = re.search(r'\b([A-Z][a-z]+(?:[ -][A-Z][a-z]+){1,3})\s*[,|:–—-]?\s+(Director|Partner|Principal|Associate|Architect|Interior Designer)\b', content)
            if not match:
                continue
            emails = set(re.findall(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', content + ' ' + ' '.join(a['href'] for a in card.select('a[href^="mailto:"]'))))
            people.append({'name': match[1], 'role': match[2], 'email': next(iter(emails)) if len(emails) == 1 else None, 'source_url': target, 'evidence': content, 'source': 'site'})
    return people, pages


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
    return {**counts, 'firms': firms, 'status': 'trial_review'}


class FirmTrial:
    def __init__(self, planning, http=None, apollo=None):
        self.planning, self.repo = planning, planning.repo
        self.now = planning.outreach.now
        self.http = http or TrialHTTP(self.repo, self.now)
        self.apollo = apollo or FirmTrialApollo()
        self.reveals_used = 0

    def save(self, key, data):
        old = self.repo.rows('SELECT payload FROM planning_firm_trials WHERE firm_key=?', (key,))
        merged, candidates = merge_trusted(json.loads(old[0]['payload']), data)
        merged.setdefault('field_provenance', {}).update(candidates)
        self.repo.execute('UPDATE planning_firm_trials SET payload=? WHERE firm_key=?', (json.dumps(merged), key))

    def run(self, limit=3, authorities=(), max_reveals=10):
        if not 1 <= limit <= 500 or not 0 <= max_reveals <= 100:
            raise ValueError('Invalid firm/reveal limit')
        self.reveals_used = 0
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
                matches = [m for m in self.planning.companies_house.search_companies(company) if name_key(m.title) == name_key(company)]
                profile = self.planning.companies_house.get_company_profile(matches[0].company_number) if len(matches) == 1 else None
                if profile and name_key(profile.company_name) == name_key(company):
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
                if data['classification'] != 'architect' or not data.get('CH_verified'):
                    self.save(key, data)
                    continue
                accounts = self.repo.rows('SELECT * FROM prospect_accounts WHERE companies_house_number=?', (profile.company_number,))
                website = next((a['website'] or ('https://' + a['domain'] if a['domain'] else None) for a in accounts if a['website'] or a['domain']), None)
                people = []
                if website:
                    try:
                        people, data['pages'] = site_people(self.http, website)
                    except Exception as exc:
                        data['site_reason'] = str(exc)
                # Free search can supply a source-backed organisation domain when no account exists.
                found = [] if any(p.get('email') for p in people) else self.apollo.search(company, urlsplit(website).hostname if website else None)
                found = [p for p in found if name_key(clean((p.get('organization') or {}).get('name'))) == name_key(company) or (website and urlsplit((p.get('organization') or {}).get('website_url') or '').hostname == urlsplit(website).hostname)]
                if not website:
                    domains = {(p.get('organization') or {}).get('website_url') or ('https://' + (p.get('organization') or {})['primary_domain'] if (p.get('organization') or {}).get('primary_domain') else None) for p in found}
                    domains.discard(None)
                    domains.discard('')
                    if len(domains) == 1:
                        website = domains.pop()
                        try:
                            people, data['pages'] = site_people(self.http, website)
                        except Exception as exc:
                            data['site_reason'] = str(exc)
                data['website'] = website
                for person in found:
                    if not person.get('id'):
                        continue
                    # Free search surnames may be obfuscated: retain only supplied first name.
                    people.append({'name': clean(person.get('first_name')), 'role': person.get('title'), 'source': 'apollo', 'source_url': 'apollo-free:' + str(person['id'])})
                data['people'] = people
                if not people:
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
