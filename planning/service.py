"""Planning ingestion and project-bound draft enrolment. No sending or enrichment."""
import json
import re
import threading
import time
from datetime import timedelta
from planning.planit_client import PlanItClient, Inconclusive, RateLimited
from planning.council import CouncilClient, role_for
from verification.companies_house_client import CompaniesHouseClient
from planning.relevance import fit
from outreach.service import OutreachService, stamp, domain
from storage.database import connection_scope

SYNC_GATE = threading.Lock()

LONDON = ('Barking and Dagenham|Barnet|Bexley|Brent|Bromley|Camden|City of London|Croydon|Ealing|Enfield|Greenwich|Hackney|Hammersmith and Fulham|Haringey|Harrow|Havering|Hillingdon|Hounslow|Islington|Kensington and Chelsea|Kingston upon Thames|Lambeth|Lewisham|Merton|Newham|Redbridge|Richmond upon Thames|Southwark|Sutton|Tower Hamlets|Waltham Forest|Wandsworth|Westminster').split('|')
MANCHESTER = 'Bolton|Bury|Manchester|Oldham|Rochdale|Salford|Stockport|Tameside|Trafford|Wigan'.split('|')


def name_key(value):
    return re.sub(r'\W+', '', value.casefold())


class PlanningService:
    def __init__(self, outreach=None, client=None, council=None, companies_house=None):
        self.outreach = outreach or OutreachService()
        self.repo = self.outreach.repo
        self.client = client or PlanItClient()
        self.council = council or CouncilClient(self.repo, self.outreach.now)
        self.companies_house = companies_house or CompaniesHouseClient()

    def account(self, name, role, source_url, details=None):
        if not name or not name.strip():
            return None
        details = details or {}
        number = details.get('companies_house_number')
        host = domain(details['website']) if details.get('website') else None
        accounts = self.repo.rows('SELECT * FROM prospect_accounts ORDER BY id')
        for field, value in [('companies_house_number', number), ('domain', host)]:
            matches = [a for a in accounts if value and a[field] == value]
            if matches:
                return self.fill_account(matches[0], details)
        matches = [a for a in accounts if name_key(a['company']) == name_key(name)]
        if len(matches) == 1:
            return self.fill_account(matches[0], details)
        # Ambiguous names are never silently assigned to an existing company.
        if matches:
            return None
        self.outreach.import_rows([{'company': name, 'segment': role, **details}], 'planning_register', source_url)
        return self.repo.rows('SELECT id FROM prospect_accounts ORDER BY id DESC LIMIT 1')[0]['id']

    def fill_account(self, account, details):
        values = {k: v for k,v in details.items() if k in ('website', 'address', 'postcode', 'companies_house_number') and v}
        if values.get('website'):
            values['domain'] = domain(values['website'])
        for key, value in values.items():
            self.repo.execute(f"UPDATE prospect_accounts SET {key}=? WHERE id=? AND ({key} IS NULL OR {key}='')", (value, account['id']))
        if values:
            self.outreach.event(account['id'], 'planning account candidate', values)
        return account['id']

    def corporate_details(self, name, details):
        details = dict(details or {})
        # A caller-supplied number alone is not verification. Require an exact,
        # unambiguous registry match and an active Ltd/LLP profile.
        matches = [m for m in self.companies_house.search_companies(name)
                   if name_key(m.title) == name_key(name)]
        if len(matches) == 1:
            profile = self.companies_house.get_company_profile(matches[0].company_number)
            if profile and profile.company_status == 'active' and name_key(profile.company_name) == name_key(name) and re.search(r'\b(?:limited|ltd|llp)\.?$', profile.company_name, re.I):
                details.update(companies_house_number=profile.company_number, subscriber_type='corporate')
                details['fit_notes'] = 'Companies House verified: ' + profile.source_url
                return details, {'company_number': profile.company_number, 'source_url': profile.source_url, 'name': profile.company_name}
        details.pop('companies_house_number', None)
        details['subscriber_type'] = 'unknown'
        return details, None

    def store(self, raw, min_units=None):
        authority = str(raw.get('authority') or '').strip()
        reference = str(raw.get('reference') or '').strip()
        if not authority or not reference:
            raise ValueError('Authority and reference required')
        for key in ('agent_name','agent_company','agent_address','role_evidence','agent_website','address','postcode','description','application_type','status','validated_date','decided_date','applicant','agent','source_url'):
            if raw.get(key) is not None and not isinstance(raw[key], str):
                raise ValueError('Malformed register field: '+key)
        candidate = dict(raw)
        raw = dict(raw)
        trusted = self.repo.rows('SELECT * FROM planning_projects WHERE authority=? AND reference=?', (authority, reference))
        if trusted:
            for key in ('agent_company', 'agent_name', 'agent_address', 'description', 'application_type', 'source_url', 'applicant'):
                if trusted[0][key]:
                    raw[key] = trusted[0][key]
        description = raw.get('description') or ''
        score, reasons, units = fit(description, raw.get('application_type') or '', min_units)
        raw = dict(raw)
        if not raw.get('agent_company') and role_for(raw.get('agent')) != 'unknown':
            raw['agent_company'] = raw['agent']
        firm = raw.get('agent_company')
        role = role_for(firm, raw.get('role_evidence'))
        raw['agent_role'] = role if role != 'unknown' else None
        agent = None
        if firm and role == 'architect' and score:
            details, registry = self.corporate_details(firm, {**(raw.get('agent_details') or {}), **({'website': raw['agent_website']} if raw.get('agent_website') else {}), **({'address': raw['agent_address']} if raw.get('agent_address') else {})})
            agent = self.account(firm, 'architect', raw.get('source_url'), details)
            if registry:
                self.outreach.event(agent, 'planning corporate verification', registry)
        # Consultants and unknown agents stay on the register record, never targets.
        applicant = self.account(raw.get('applicant'), 'developer', raw.get('source_url'), raw.get('applicant_details'))
        fields = ['authority','reference','address','postcode','description','application_type','units','status','validated_date','decided_date','applicant','agent','source_url','fit_score','fit_reasons','applicant_account_id','agent_account_id','first_seen_at','agent_name','agent_company','agent_address','agent_role','identity_evidence']
        values = {k: raw.get(k) for k in fields}
        values['identity_evidence'] = json.dumps(raw['identity_evidence']) if raw.get('identity_evidence') else None
        values.update(authority=authority, reference=reference, description=description, units=units,
                      fit_score=score, fit_reasons=json.dumps(reasons), applicant_account_id=applicant,
                      agent_account_id=agent, first_seen_at=stamp(self.outreach.now()))
        with connection_scope(self.repo.db_path) as c:
            c.execute('BEGIN IMMEDIATE')
            existing = c.execute('SELECT * FROM planning_projects WHERE authority=? AND reference=?',(authority,reference)).fetchone()
            if existing:
                # Preserve trusted non-empty register fields. Changes remain candidates
                # in observations; they never silently rewrite an approved hook.
                if existing['description']:
                    values.update(fit_score=existing['fit_score'],fit_reasons=existing['fit_reasons'])
                for key in fields:
                    if existing[key] is not None and existing[key] != '':
                        values[key] = existing[key]
            c.execute('INSERT INTO planning_projects ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+') ON CONFLICT(authority,reference) DO UPDATE SET '+','.join(k+'=excluded.'+k for k in fields if k not in ('authority','reference','first_seen_at')), [values[k] for k in fields])
            pid = c.execute('SELECT id FROM planning_projects WHERE authority=? AND reference=?',(authority,reference)).fetchone()['id']
            c.execute('INSERT OR IGNORE INTO planning_project_observations(project_id,payload,observed_at) VALUES(?,?,?)',
                      (pid,json.dumps(candidate,sort_keys=True),stamp(self.outreach.now())))
            return pid

    def sync_recent(self, areas=None, days=90, statuses=None, min_units=4, run_id=None):
        if not 1 <= days <= 180:
            raise ValueError('days must be between 1 and 180')
        areas = areas if areas is not None else LONDON + MANCHESTER
        run = run_id or self.start_run()
        end = self.outreach.now().date()
        result = {'stored': 0, 'errors': {}, 'complete': True, 'identity_errors': {}}
        deadline = time.monotonic() + 2400
        if isinstance(self.client, PlanItClient):
            self.client.deadline = deadline
        for area in areas:
            try:
                records = self.client.recent(area, end-timedelta(days=days-1), end)
                for r in records:
                    if time.monotonic() > deadline:
                        raise RateLimited('Planning job budget exceeded; batch stopped')
                    if not isinstance(r, dict):
                        raise Inconclusive('Malformed PlanIt application')
                    other = r.get('other_fields')
                    if other is None:
                        other = {}
                    if not isinstance(other, dict):
                        raise Inconclusive('Malformed PlanIt other_fields')
                    raw = {'authority': area, 'reference': r.get('reference') or r.get('uid'),
                                'address': r.get('address'), 'postcode': r.get('postcode'),
                                'description': r.get('description'), 'application_type': r.get('application_type') or other.get('application_type') or r.get('app_type'),
                                'status': r.get('status') or other.get('status'), 'validated_date': r.get('date_validated') or other.get('date_validated'),
                                'decided_date': r.get('decided_date') or r.get('date_decided') or other.get('date_decided'), 'applicant': r.get('applicant') or other.get('applicant_company'),
                                'agent': r.get('agent') or other.get('agent_company'), 'source_url': r.get('url') or r.get('source_url')}
                    raw['register_payload'] = r
                    raw['agent_name'] = other.get('agent_name')
                    raw['agent_company'] = other.get('agent_company')
                    if statuses or raw['validated_date'] or raw['decided_date']:
                        if not in_window(raw, end-timedelta(days=days-1), end, statuses):
                            continue
                    if fit(raw.get('description'), raw.get('application_type') or '', min_units)[0] and raw.get('source_url'):
                        try:
                            identity = self.council.fetch(raw['source_url'])
                        except Exception as exc:
                            identity = {'error': 'Council fetch inconclusive: ' + type(exc).__name__}
                        raw['identity_evidence'] = identity.get('evidence', [])
                        for key in ('agent_name', 'agent_company', 'agent_address', 'agent_website', 'role_evidence', 'applicant'):
                            if identity.get(key) and not raw.get(key):
                                raw[key] = identity[key]
                        if identity.get('error'):
                            result['identity_errors'][raw['reference']] = identity['error']
                    self.store(raw, min_units)
                    result['stored'] += 1
            except Exception as exc:
                result['errors'][area] = str(exc)
                result['complete'] = False
                if isinstance(exc, RateLimited):
                    break
        result['status'] = 'completed' if result['complete'] else 'failed'
        self.repo.execute('UPDATE planning_sync_runs SET completed_at=?,result=? WHERE id=? AND completed_at IS NULL',(stamp(self.outreach.now()),json.dumps(result),run))
        return result

    def import_identity(self, project_id, source_url, source_text, values):
        from urllib.parse import urlsplit
        if urlsplit(source_url).scheme != 'https' or not source_text.strip():
            raise ValueError('HTTPS provenance and exact source text required')
        allowed = {'agent_name', 'agent_company', 'agent_address', 'agent_website', 'role_evidence'}
        if not values or set(values)-allowed or any(not isinstance(v, str) or not v.strip() or v not in source_text for v in values.values()):
            raise ValueError('Every identity value must occur verbatim in source_text')
        rows = self.repo.rows('SELECT * FROM planning_projects WHERE id=?', (project_id,))
        if not rows:
            raise ValueError('Project not found')
        raw = dict(rows[0])
        raw['identity_evidence'] = [{'field': k, 'value': v, 'source_url': source_url} for k,v in values.items()]
        raw.update(values)
        raw['identity_import_source'] = {'source_url': source_url, 'source_text': source_text}
        self.store(raw)
        return self.repo.rows('SELECT * FROM planning_projects WHERE id=?', (project_id,))[0]

    def corporate_verified(self, account):
        if not account['companies_house_number']:
            return False
        events = self.repo.rows("SELECT payload FROM outreach_events WHERE account_id=? AND kind='planning corporate verification'", (account['id'],))
        return any(json.loads(e['payload']).get('company_number') == account['companies_house_number'] for e in events)

    def search_contacts(self, account_id):
        from outreach.apollo import ProspectContactService
        account = self.repo.get('prospect_accounts', account_id)
        projects = self.repo.rows("SELECT description,application_type FROM planning_projects WHERE agent_account_id=? AND agent_role='architect' AND agent_company IS NOT NULL", (account_id,))
        if not any(fit(p['description'], p['application_type'] or '')[0] for p in projects) or not account['domain'] or account['subscriber_type'] != 'corporate' or account['flagged']:
            raise ValueError('Verified corporate architect firm, domain and fit project required')
        identifiers = (account['domain'], name_key(account['company']))
        if any(self.repo.rows('SELECT 1 FROM planning_first_touch_exclusions WHERE identifier=?', (v,)) or self.repo.rows('SELECT 1 FROM suppression_list WHERE identifier=?', (v,)) for v in identifiers):
            raise ValueError('Historical or suppressed planning firm requires manual follow-up review')
        if not self.corporate_verified(account):
            raise ValueError('Companies House corporate verification required')
        if account['contact_search_attempted_at']:
            return {'skipped': True, 'reason': 'Already attempted; review stored contacts'}
        return ProspectContactService(self.outreach).search(account_id, titles=['director', 'associate'])

    def start_run(self):
        self.expire_stalled()
        return self.repo.execute('INSERT INTO planning_sync_runs(started_at) VALUES(?)', (stamp(self.outreach.now()),))

    def expire_stalled(self, minutes=45):
        cutoff = stamp(self.outreach.now()-timedelta(minutes=minutes))
        self.repo.execute("UPDATE planning_sync_runs SET completed_at=?, result=? WHERE completed_at IS NULL AND started_at<?",
                          (stamp(self.outreach.now()), json.dumps({'complete': False, 'status': 'failed', 'errors': {'watchdog': 'Stalled planning run exceeded 45 minutes'}}), cutoff))

    def run_background(self, run_id, **options):
        if not SYNC_GATE.acquire(blocking=False):
            self.repo.execute('UPDATE planning_sync_runs SET completed_at=?,result=? WHERE id=?',
                              (stamp(self.outreach.now()), json.dumps({'complete': False, 'status': 'failed', 'error': 'Another planning sync is running'}), run_id))
            return
        try:
            self.sync_recent(run_id=run_id, **options)
        except Exception as exc:
            self.repo.execute('UPDATE planning_sync_runs SET completed_at=?,result=? WHERE id=?',
                              (stamp(self.outreach.now()), json.dumps({'complete': False, 'status': 'failed', 'error': type(exc).__name__}), run_id))

        finally:
            SYNC_GATE.release()

    def projects(self, days=7):
        rows = self.repo.rows('''SELECT p.*,a.company AS architect,d.company AS developer FROM planning_projects p
            LEFT JOIN prospect_accounts a ON a.id=p.agent_account_id
            LEFT JOIN prospect_accounts d ON d.id=p.applicant_account_id
            WHERE fit_score>0 AND first_seen_at>=? ORDER BY first_seen_at DESC,p.id''', (stamp(self.outreach.now()-timedelta(days=days)),))
        rows = [p for p in rows if fit(p['description'], p['application_type'] or '')[0]]
        for p in rows:
            p['fit_score'], p['fit_reasons'], parsed_units = fit(p['description'], p['application_type'] or '')
            p['units'] = p['units'] if p['units'] is not None else parsed_units
            p['agent_role'] = p['agent_role'] or 'unknown'
            p['applicant_role'] = 'applicant' if p['applicant'] else 'unknown'
            if p['agent_role'] != 'architect':
                p['architect'] = None
            p['project_hook'] = {k: p[k] for k in ('address','description','units','reference','status','source_url')}
            p['identity_evidence'] = json.loads(p['identity_evidence'] or '[]')
            p['contacts'] = self.repo.rows('SELECT id,name,email,email_status FROM prospect_contacts WHERE account_id=?',(p['agent_account_id'],))
        return rows

    def eligible_project(self, project_id, contact_id, role='architect'):
        if role != 'architect':
            raise ValueError('Only a role=architect firm may be enrolled')
        rows = self.repo.rows('SELECT * FROM planning_projects WHERE id=?',(project_id,))
        if not rows or not rows[0]['fit_score'] or not rows[0]['source_url']:
            raise ValueError('A fit project with a register source is required')
        p = rows[0]
        if p.get('agent_lookup_attempted_at'):
            raise ValueError('Council agent trial requires Sai review')
        if not fit(p['description'], p['application_type'] or '')[0]:
            raise ValueError('Project no longer fits current classification')
        contact = self.repo.get('prospect_contacts',contact_id)
        aid = p['agent_account_id' if role == 'architect' else 'applicant_account_id']
        if not aid or contact['account_id'] != aid:
            raise ValueError('Contact does not belong to this project role')
        account = self.repo.get('prospect_accounts',aid)
        if p['agent_role'] != 'architect' or not p['agent_company'] or account['segment'] != 'architect':
            raise ValueError('A grounded role=architect firm is required')
        if account['flagged'] or contact['opted_out_at'] or contact['bounced_at']:
            raise ValueError('Contact or account is flagged or opted out')
        if contact['email_status'] != 'corporate_verified' or not account['domain'] or domain('https://' + (contact['email'] or '').rpartition('@')[2]) != account['domain'] or account['subscriber_type'] != 'corporate' or account['domain'] in {'gmail.com','hotmail.com','outlook.com','yahoo.com','icloud.com','live.com','aol.com','proton.me'}:
            raise ValueError('A verified corporate-domain email is required')
        if not self.corporate_verified(account):
            raise ValueError('Companies House corporate verification required')
        identifiers = [contact['email'] or '', (contact['email'] or '').rpartition('@')[2], account['domain'] or '', name_key(account['company'])]
        if any(self.repo.rows('SELECT 1 FROM planning_first_touch_exclusions WHERE identifier=?',(v,)) for v in identifiers if v):
            raise ValueError('Already contacted in historical planning outreach; review follow-up manually')
        return p, contact, account

    def preview(self, project_id, contact_id):
        from outreach.service import FOOTER
        p, contact, account = self.eligible_project(project_id, contact_id)
        if not contact['name'] or not p['address']:
            raise ValueError('Named contact and project address required for preview')
        body = ('Hello ' + contact['name'].split()[0] + ',\nRegarding ' + p['reference'] + ' at ' + p['address'] +
                ': could we quote kitchens, wardrobes, doors or bespoke joinery against your drawings?')
        body += FOOTER.format(source=contact['source_url'] or p['source_url'])
        if len(body.split()) > 120:
            raise ValueError('Rendered message exceeds 120 words; review wording')
        return {'recipient': contact['email'], 'subject': 'Joinery for '+p['reference'], 'body': body,
                'words': len(body.split()), 'project_hook': p, 'approved': False}

    def enrol(self, project_id, contact_id, mailbox, role='architect'):
        p, contact, account = self.eligible_project(project_id, contact_id, role)
        existing = self.repo.rows('''SELECT e.sequence_id FROM planning_enrolment_projects l JOIN outreach_enrolments e ON e.id=l.enrolment_id WHERE l.project_id=? AND e.contact_id=?''',(project_id,contact_id))
        if existing:
            return self.outreach.sequence(existing[0]['sequence_id'])
        seq = self.outreach.save_sequence('Live planning '+role+': '+p['reference'],role,[{
            'channel':'email','day_offset':0,'subject':'Planning {project_ref} at {project_address}',
            'template':'Hello {first_name},\nRegarding {project_ref} at {project_address}: could we quote kitchens, wardrobes, doors or bespoke joinery against your drawings?'}])
        self.outreach.enrol(seq['id'],[contact_id],mailbox)
        eid = self.repo.rows('SELECT id FROM outreach_enrolments WHERE sequence_id=? AND contact_id=?',(seq['id'],contact_id))[0]['id']
        self.repo.execute('INSERT INTO planning_enrolment_projects VALUES(?,?,?)',(eid,project_id,json.dumps(p)))
        return self.outreach.sequence(seq['id'])


def in_window(raw, start, end, statuses=None):
    from datetime import date
    dates = []
    for field in ('validated_date', 'decided_date'):
        try:
            dates.append(date.fromisoformat((raw.get(field) or '')[:10]))
        except ValueError:
            pass
    if not any(start <= day <= end for day in dates):
        return False
    status = (raw.get('status') or '').casefold()
    return not statuses or any((s == 'validated' and ('register' in status or 'validat' in status or 'received' in status)) or
                               (s == 'decided' and any(w in status for w in ('decid', 'grant', 'refus', 'approv', 'permission'))) for s in statuses)
