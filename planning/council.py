"""Bounded, cached public council HTML reads. No bypass of access controls.

Only labelled values are extracted; an agent is not automatically an architect.
"""
import json
import re
import threading
import time
from datetime import timedelta
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from outreach.service import stamp
from verification.website_contact_extractor import parking_page_reason

_LOCK = threading.Lock()
_LAST = {}
_PAUSED = {}


def role_for(company, evidence=''):
    text = ' '.join((company or '', evidence or ''))
    if re.search(r'\b(?:Newmark|Lichfields|DP9|Turley|Savills|Q Square|planning consult\w*)\b', text, re.I):
        return 'planning consultant'
    if re.search(r'\b(?:architects?|architecture|architectural|interior design(?:ers?)?)\b', text, re.I):
        return 'architect'
    return 'unknown'


LABELS = {
    'agent': 'agent_name', 'agent name': 'agent_name',
    'agent company': 'agent_company', 'agent company name': 'agent_company',
    'agent organisation': 'agent_company', 'agent organization': 'agent_company',
    'agent website': 'agent_website', 'agent address': 'agent_address', 'applicant': 'applicant',
    'applicant name': 'applicant', 'applicant company': 'applicant_company', 'applicant company name': 'applicant_company',
    'architect': 'agent_company', 'architect name': 'agent_company',
    'agent role': 'role_evidence', 'agent profession': 'role_evidence',
}


def parse(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    if parking_page_reason(soup.get_text(' ', strip=True)):
        raise ValueError('Council returned a parking or default page')
    result = {'evidence': []}
    pairs = []
    for row in soup.select('tr'):
        cells = row.find_all(['th', 'td'], recursive=False)
        if len(cells) == 2:
            pairs.append((cells[0].get_text(' ', strip=True), cells[1].get_text(' ', strip=True)))
    for label in soup.select('dt, label, .label, .field-label'):
        value = label.find_next_sibling()
        if value:
            pairs.append((label.get_text(' ', strip=True), value.get_text(' ', strip=True)))
    # NEC/Northgate pages also render labelled values as separate text lines.
    lines = [s.strip() for s in soup.get_text('\n', strip=True).splitlines() if s.strip()]
    pairs.extend(zip(lines, lines[1:]))
    for line in lines:
        if ':' in line:
            pairs.append(line.split(':', 1))
    for label, value in pairs:
        value = value.strip()
        normal = label.strip().rstrip(':').casefold()
        key = LABELS.get(normal)
        if key and value and value.casefold() not in ('see source', 'see company name', 'not available', 'n/a', 'redacted') and value.rstrip(':').casefold() not in LABELS:
            result.setdefault(key, value)
            result['evidence'].append({'field': key, 'label': label, 'value': value, 'source_url': url})
            if normal.startswith('architect'):
                result['role_evidence'] = label
    if not result.get('agent_company') and role_for(result.get('agent_name')) != 'unknown':
        result['agent_company'] = result['agent_name']
    result['agent_role'] = role_for(result.get('agent_company'), result.get('role_evidence'))
    return result, [urljoin(url, a['href']) for a in soup.select('a[href]')
                    if re.search(r'further information|details|application details', a.get_text(' ', strip=True), re.I)]


class CouncilClient:
    def __init__(self, repo, now, client=None, sleep=time.sleep, interval=2):
        self.repo, self.now = repo, now
        self.client = client or httpx.Client(timeout=20, headers={'User-Agent': 'IKEngPlanning/1.0 (+https://ikeng.co.uk)'})
        self.sleep, self.interval = sleep, interval

    @staticmethod
    def allowed(url):
        parsed = urlsplit(url)
        return parsed.scheme == 'https' and (parsed.hostname or '').endswith('.gov.uk') and not parsed.username and parsed.port in (None, 443)

    def _get(self, url):
        for _ in range(5):
            if not self.allowed(url):
                raise ValueError('Council URL must be public HTTPS gov.uk')
            host = urlsplit(url).hostname
            with _LOCK:
                if _PAUSED.get(host, 0) > time.monotonic():
                    raise ValueError('Council rate-limit cooldown; no request made')
                self.sleep(max(0, self.interval - (time.monotonic() - _LAST.get(host, 0))))
                _LAST[host] = time.monotonic()
            response = self.client.get(url, follow_redirects=False)
            if response.is_redirect:
                url = urljoin(url, response.headers['location'])
                continue
            if response.status_code == 429:
                with _LOCK:
                    _PAUSED[host] = time.monotonic() + 300
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise ValueError('Council page too large')
            return response.text, url
        raise ValueError('Council redirect limit')

    def fetch(self, url):
        cached = self.repo.rows('SELECT * FROM planning_council_cache WHERE url=? AND fetched_at>=?',
                                (url, stamp(self.now()-timedelta(days=7))))
        if cached:
            return json.loads(cached[0]['payload'])
        result = {'evidence': [], 'agent_role': 'unknown'}
        try:
            html, final = self._get(url)
            result, links = parse(html, final)
            # Idox exposes agent fields on Further Information, not the summary.
            for link in list(dict.fromkeys(links))[:2]:
                if urlsplit(link).hostname != urlsplit(final).hostname or link == final:
                    continue
                page, target = self._get(link)
                details, _ = parse(page, target)
                result['evidence'].extend(details.pop('evidence'))
                for key, value in details.items():
                    if value and (not result.get(key) or result[key] == 'unknown'):
                        result[key] = value
            result['agent_role'] = role_for(result.get('agent_company'), result.get('role_evidence'))
            if not result.get('agent_company') and not result.get('agent_name'):
                result['error'] = 'No labelled agent fields; manual review required'
        except Exception as exc:
            result['error'] = 'Council fetch inconclusive: ' + type(exc).__name__
        self.repo.execute('INSERT INTO planning_council_cache(url,payload,fetched_at) VALUES(?,?,?) ON CONFLICT(url) DO UPDATE SET payload=excluded.payload,fetched_at=excluded.fetched_at',
                          (url, json.dumps(result), stamp(self.now())))
        return result
