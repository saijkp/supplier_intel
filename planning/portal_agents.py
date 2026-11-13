"""Free, HTTPS-only council identity trial. No enrolment, reveal or sending APIs.

A run is one TrialHTTP instance. Host stops survive all projects in that run;
request budgets and Retry-After survive process restarts. Robots failures close access.
"""
import json
import os
import re
import threading
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urljoin, parse_qs, urlencode, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from outreach.service import stamp
from storage.database import connection_scope
from planning.council import parse, CouncilClient
from planning.service import name_key
from verification.website_contact_extractor import parking_page_reason

UA = 'IKEngCouncilIdentityTrial/1.0 (+https://ikeng.co.uk; sai@ikeng.online)'
SICS = {'71111', '71112', '74100'}
SUPPORTED = {'westminster', 'lambeth', 'barnet', 'brent', 'tower hamlets', 'trafford', 'bolton', 'stockport', 'hackney'}
_LOCK = threading.Lock()


class Unavailable(Exception):
    pass


class TrialHTTP:
    def __init__(self, repo, now, client=None, sleep=time.sleep, cap=None, public_url=None):
        self.repo, self.now = repo, now
        self.client = client or httpx.Client(timeout=20)
        self.sleep = sleep
        self.cap = int(cap if cap is not None else os.getenv('PLANNING_AGENT_HOST_DAILY_CAP', '120'))
        self.stopped, self.robots = set(), {}
        if public_url is None:
            from outreach.always_on import AlwaysOnService
            public_url = AlwaysOnService._public_url
        self.public_url = public_url

    def raw(self, url):
        if not self.public_url(url):
            raise Unavailable('Non-public HTTPS URL')
        host = urlsplit(url).hostname
        day = self.now().date().isoformat()
        with _LOCK:
            with connection_scope(self.repo.db_path) as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT OR IGNORE INTO planning_agent_http(host,day) VALUES(?,?)', (host, day))
                row = db.execute('SELECT * FROM planning_agent_http WHERE host=? AND day=?', (host, day)).fetchone()
                retry = db.execute('SELECT max(retry_at) FROM planning_agent_http WHERE host=?', (host,)).fetchone()[0] or 0
                if host in self.stopped or retry > self.now().timestamp() or row['requests'] >= self.cap:
                    raise Unavailable('Host stopped, cooling down or daily cap reached')
                self.sleep(max(0, 2 - (self.now().timestamp() - row['last_at'])))
                db.execute('UPDATE planning_agent_http SET requests=requests+1,last_at=? WHERE host=? AND day=?', (self.now().timestamp(), host, day))
            response = self.client.get(url, headers={'User-Agent': UA}, follow_redirects=False)
            if response.status_code in (403, 429):
                self.stopped.add(host)
                delay = 86400 if response.status_code == 403 else 300
                value = response.headers.get('Retry-After', '')
                try:
                    delay = max(delay, int(value))
                except ValueError:
                    try:
                        delay = max(delay, parsedate_to_datetime(value).timestamp() - self.now().timestamp())
                    except (ValueError, TypeError, OverflowError):
                        pass
                self.repo.execute('UPDATE planning_agent_http SET retry_at=? WHERE host=? AND day=?', (self.now().timestamp()+delay, host, day))
                raise Unavailable('Host returned ' + str(response.status_code))
        if response.status_code != 200 or len(response.content) > 2_000_000:
            raise Unavailable('HTTP unavailable, redirect or oversized page')
        return response.text

    def get(self, url):
        root = urlunsplit((*urlsplit(url)[:2], '/robots.txt', '', ''))
        host = urlsplit(url).hostname
        if host not in self.robots:
            try:
                rules = RobotFileParser()
                rules.parse(self.raw(root).splitlines())
                self.robots[host] = rules
            except Exception:
                self.stopped.add(host)
                raise Unavailable('robots.txt unavailable')
        rules = self.robots[host]
        if not rules.can_fetch(UA, url):
            raise Unavailable('robots.txt disallows request')
        delay = rules.crawl_delay(UA) or rules.crawl_delay('*') or 0
        if delay > 2:
            self.sleep(delay)
        return self.raw(url)


def classify(company):
    if re.search(r'\b(tree|arbor|surgeon|contractor|construction|estates|university|hospital|planning consultant)\w*', company or '', re.I):
        return False
    return bool(re.search(r'\b(architect\w*|design\w*)\b', company or '', re.I))


def merge_trusted(original, extracted):
    result, candidates = {k: (None if isinstance(v, str) and v.strip().casefold() == 'see source' else v) for k, v in original.items()}, {}
    for key, value in extracted.items():
        if not value or key == 'evidence' or (isinstance(value, str) and value.strip().casefold() == 'see source'):
            continue
        if result.get(key) and result[key] != value:
            candidates[key + '_candidate'] = value
        else:
            result[key] = value
    return result, candidates


def site_identity(http, url, company, person, number):
    """Only source-linked sites; exact legal name AND registry number bind domain.

    Emails associated with a person must occur in the same small team card;
    generic inboxes are explicitly labelled and never counted as personal emails.
    """
    out = {'website': url, 'person_on_site': False, 'email_found': False, 'pages': []}
    if not url:
        out['cost_flag'] = 'website_missing: paid search disabled; supply a published practice URL'
        return out
    queue = [url]
    for target in queue:
        if len(out['pages']) >= 4:
            break
        soup = BeautifulSoup(http.get(target), 'html.parser')
        text = soup.get_text(' ', strip=True)
        if parking_page_reason(text):
            continue
        out['pages'].append({'source_url': target, 'text': text, 'html': str(soup)})
        for a in soup.select('a[href]'):
            link = urljoin(target, a['href'])
            if urlsplit(link).hostname == urlsplit(url).hostname and re.search(r'team|about|contact', a.get_text(' ', strip=True), re.I) and link not in queue and len(queue)<4:
                queue.append(link)
    bound = any(name_key(company) in name_key(p['text']) and re.search(r'(?<!\d)' + re.escape(number) + r'(?!\d)', p['text']) for p in out['pages'])
    if not bound or not person or len(person.split()) < 2 or name_key(person) == name_key(company):
        for page in out['pages']:
            page.pop('html', None)
        return out
    for page in out['pages']:
        soup = BeautifulSoup(page['html'], 'html.parser')
        # Text is retained as evidence, but a whole page never binds a personal inbox.
        text = soup.get_text(' ', strip=True)
        match = re.search(re.escape(person) + r'[, |\u2013\u2014:-]{0,8}(principal|director|partner|architect|designer)\b', text, re.I)
        if match:
            out.update(person_on_site=True, name=person, role=match.group(1), person_source=page['source_url'], person_evidence=match.group(0))
        for card in soup.select('article, li, .team-member, .person'):
            card_text = card.get_text(' ', strip=True)
            if person.casefold() not in card_text.casefold() or len(card_text)>700:
                continue
            emails = re.findall(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', card_text + ' ' + ' '.join(a['href'] for a in card.select('a[href^="mailto:"]')))
            personal = [e for e in set(emails) if e.split('@')[1].lower()==urlsplit(url).hostname.removeprefix('www.') and e.split('@')[0].lower() not in {'info','hello','contact','enquiries','office'}]
            if len(personal)==1 and out['person_on_site']:
                out.update(email=personal[0], email_found=True, generic_fallback=False, email_source=page['source_url'], email_evidence=card_text)
        generic = re.search(r'\b(?:info|hello|contact|enquiries|office)@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', text, re.I)
        if not out.get('email_found') and generic and generic.group().split('@')[1].lower() == urlsplit(url).hostname.removeprefix('www.'):
            out.update(email=generic.group(), email_found=True, generic_fallback=True, email_source=page['source_url'], email_evidence=generic.group())
    for page in out['pages']:
        page.pop('html', None)
    return out


class AgentTrial:
    def __init__(self, planning, http=None, website_finder=None):
        self.planning, self.repo = planning, planning.repo
        self.now = planning.outreach.now
        self.http = http or TrialHTTP(self.repo, self.now)
        # Optional free evidence adapter returns a source-backed URL, never a guess.
        self.website_finder = website_finder

    def attempt(self, project):
        if os.getenv('PLANNING_COUNCIL_FETCH_ENABLED', '').lower() != 'true':
            return {'status': 'trial_review', 'reason': 'Council fetching disabled'}
        with connection_scope(self.repo.db_path) as db:
            changed = db.execute("UPDATE planning_projects SET agent_lookup_attempted_at=?,agent_lookup_status='error' WHERE id=? AND agent_lookup_attempted_at IS NULL", (stamp(self.now()), project['id'])).rowcount
        if not changed:
            return None
        data = {'status': 'trial_review'}
        status = 'error'
        try:
            if project['authority'].casefold() not in SUPPORTED or not CouncilClient.allowed(project['source_url'] or ''):
                raise Unavailable('Unsupported authority or portal')
            url = project['source_url']
            extracted, links = parse(self.http.get(url), url)
            # Idox tab is explicit even when its anchor text is absent.
            parts = urlsplit(url)
            query = parse_qs(parts.query)
            if 'activeTab' in query:
                query['activeTab'] = ['details']
                links.insert(0, urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), '')))
            for link in list(dict.fromkeys(links))[:1]:
                if link != url and urlsplit(link).hostname == parts.hostname:
                    detail, _ = parse(self.http.get(link), link)
                    extracted['evidence'].extend(detail.pop('evidence', []))
                    for k, v in detail.items():
                        if v and not extracted.get(k):
                            extracted[k] = v
            data, candidates = merge_trusted(dict(project), extracted)
            data.update(status='trial_review', field_provenance=candidates, portal_evidence=extracted['evidence'])
            status = 'found' if extracted.get('agent_name') or extracted.get('agent_company') else 'absent'
            data['agent_found'] = status == 'found'
            company = data.get('agent_company') or ''
            data['architect_practice'] = classify(company)
            if data['architect_practice']:
                matches = [m for m in self.planning.companies_house.search_companies(company) if name_key(m.title) == name_key(company)]
                profile = self.planning.companies_house.get_company_profile(matches[0].company_number) if len(matches)==1 else None
                if profile and name_key(profile.company_name)==name_key(company) and profile.company_status=='active' and SICS.intersection(profile.sic_codes):
                    data.update(CH_verified=True, company_number=profile.company_number, company_status=profile.company_status, sic_codes=profile.sic_codes, ch_source=profile.source_url)
                    website = extracted.get('agent_website')
                    if not website:
                        accounts = self.repo.rows('SELECT website FROM prospect_accounts WHERE companies_house_number=?', (profile.company_number,))
                        website = accounts[0]['website'] if accounts else None
                    if not website and self.website_finder:
                        website = self.website_finder(company)
                    try:
                        data.update(site_identity(self.http, website, company, data.get('agent_name'), profile.company_number))
                    except Exception as exc:
                        data['website_reason'] = type(exc).__name__
        except Unavailable as exc:
            status = 'portal_unavailable'
            data['reason'] = str(exc)
        except Exception as exc:
            data['reason'] = type(exc).__name__
        self.repo.execute('INSERT INTO planning_agent_trials(project_id,payload,observed_at) VALUES(?,?,?)', (project['id'], json.dumps(data), stamp(self.now())))
        self.repo.execute('UPDATE planning_projects SET agent_lookup_status=? WHERE id=?', (status, project['id']))
        return data

    def run(self, limit=20, authorities=()):
        rows = self.repo.rows('SELECT * FROM planning_projects WHERE agent_lookup_attempted_at IS NULL AND fit_score>0 ORDER BY id')
        selected = [p for p in rows if not authorities or p['authority'].casefold() in {a.casefold() for a in authorities}]
        for p in selected[:limit]:
            self.attempt(p)
        return trial_report(self.repo)


def trial_report(repo):
    report = {}
    unique = set()
    direct = set()
    for row in repo.rows('SELECT p.authority,p.agent_lookup_status,t.payload FROM planning_projects p LEFT JOIN planning_agent_trials t ON t.project_id=p.id WHERE p.agent_lookup_attempted_at IS NOT NULL'):
        counts = report.setdefault(row['authority'], dict.fromkeys(('attempted','portal_unavailable','agent_found','architect_practice','CH_verified','person_on_site','email_found'), 0))
        counts['attempted'] += 1
        counts['portal_unavailable'] += row['agent_lookup_status']=='portal_unavailable'
        data = json.loads(row['payload'] or '{}')
        for k in ('agent_found','architect_practice','CH_verified','person_on_site','email_found'):
            counts[k] += bool(data.get(k))
        if data.get('CH_verified') and data.get('person_on_site') and data.get('email_found'):
            key = (data['company_number'], name_key(data['name']))
            unique.add(key)
            if not data.get('generic_fallback', True):
                direct.add(key)
    from planning.firm_trial import firm_report
    return {**firm_report(repo), 'authorities': report, 'unique_verified_people': len(unique), 'unique_direct_email_people': len(direct), 'status': 'trial_review'}
