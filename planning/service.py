"""Planning ingestion and project-bound draft enrolment. No sending or enrichment."""
import json
import re
from datetime import timedelta
from planning.planit_client import PlanItClient, Inconclusive
from planning.relevance import fit
from outreach.service import OutreachService, stamp, domain
from storage.database import connection_scope

LONDON = ('Barking and Dagenham|Barnet|Bexley|Brent|Bromley|Camden|City of London|Croydon|Ealing|Enfield|Greenwich|Hackney|Hammersmith and Fulham|Haringey|Harrow|Havering|Hillingdon|Hounslow|Islington|Kensington and Chelsea|Kingston upon Thames|Lambeth|Lewisham|Merton|Newham|Redbridge|Richmond upon Thames|Southwark|Sutton|Tower Hamlets|Waltham Forest|Wandsworth|Westminster').split('|')
MANCHESTER = 'Bolton|Bury|Manchester|Oldham|Rochdale|Salford|Stockport|Tameside|Trafford|Wigan'.split('|')


def name_key(value):
    return re.sub(r'\W+', '', value.casefold())


class PlanningService:
    def __init__(self, outreach=None, client=None):
        self.outreach = outreach or OutreachService()
        self.repo = self.outreach.repo
        self.client = client or PlanItClient()

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
                return matches[0]['id']
        matches = [a for a in accounts if name_key(a['company']) == name_key(name)]
        if len(matches) == 1:
            return matches[0]['id']
        # Ambiguous names are never silently assigned to an existing company.
        if matches:
            return None
        self.outreach.import_rows([{'company': name, 'segment': role, **details}], 'planning_register', source_url)
        return self.repo.rows('SELECT id FROM prospect_accounts ORDER BY id DESC LIMIT 1')[0]['id']

    def store(self, raw):
        authority = str(raw.get('authority') or '').strip()
        reference = str(raw.get('reference') or '').strip()
        if not authority or not reference:
            raise ValueError('Authority and reference required')
        for key in ('address','postcode','description','application_type','status','validated_date','decided_date','applicant','agent','source_url'):
            if raw.get(key) is not None and not isinstance(raw[key], str):
                raise ValueError('Malformed register field: '+key)
        description = raw.get('description') or ''
        score, reasons, units = fit(description, raw.get('application_type') or '')
        agent = self.account(raw.get('agent'), 'architect', raw.get('source_url'), raw.get('agent_details'))
        applicant = self.account(raw.get('applicant'), 'developer', raw.get('source_url'), raw.get('applicant_details'))
        fields = ['authority','reference','address','postcode','description','application_type','units','status','validated_date','decided_date','applicant','agent','source_url','fit_score','fit_reasons','applicant_account_id','agent_account_id','first_seen_at']
        values = {k: raw.get(k) for k in fields}
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
                    values.update(fit_score=existing['fit_score'],fit_reasons=existing['fit_reasons'],units=existing['units'])
                for key in fields:
                    if existing[key] is not None and existing[key] != '':
                        values[key] = existing[key]
            c.execute('INSERT INTO planning_projects ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+') ON CONFLICT(authority,reference) DO UPDATE SET '+','.join(k+'=excluded.'+k for k in fields if k not in ('authority','reference','first_seen_at')), [values[k] for k in fields])
            pid = c.execute('SELECT id FROM planning_projects WHERE authority=? AND reference=?',(authority,reference)).fetchone()['id']
            c.execute('INSERT OR IGNORE INTO planning_project_observations(project_id,payload,observed_at) VALUES(?,?,?)',
                      (pid,json.dumps(raw,sort_keys=True),stamp(self.outreach.now())))
            return pid

    def sync_recent(self, areas=None, days=90):
        if not 1 <= days <= 180:
            raise ValueError('days must be between 1 and 180')
        areas = areas if areas is not None else LONDON + MANCHESTER
        run = self.repo.execute('INSERT INTO planning_sync_runs(started_at) VALUES(?)',(stamp(self.outreach.now()),))
        end = self.outreach.now().date()
        result = {'stored': 0, 'errors': {}, 'complete': True}
        for area in areas:
            try:
                records = self.client.recent(area, end-timedelta(days=days-1), end)
                for r in records:
                    if not isinstance(r, dict):
                        raise Inconclusive('Malformed PlanIt application')
                    other = r.get('other_fields') or {}
                    if not isinstance(other, dict):
                        raise Inconclusive('Malformed PlanIt other_fields')
                    self.store({'authority': area, 'reference': r.get('reference') or r.get('uid'),
                                'address': r.get('address'), 'postcode': r.get('postcode'),
                                'description': r.get('description'), 'application_type': r.get('application_type') or other.get('application_type') or r.get('app_type'),
                                'status': r.get('status') or other.get('status'), 'validated_date': r.get('date_validated') or other.get('date_validated'),
                                'decided_date': r.get('decided_date') or r.get('date_decided'), 'applicant': r.get('applicant') or other.get('applicant_company'),
                                'agent': r.get('agent') or other.get('agent_company'), 'source_url': r.get('url') or r.get('source_url')})
                    result['stored'] += 1
            except (Inconclusive, ValueError) as exc:
                result['errors'][area] = str(exc)
                result['complete'] = False
        self.repo.execute('UPDATE planning_sync_runs SET completed_at=?,result=? WHERE id=?',(stamp(self.outreach.now()),json.dumps(result),run))
        return result

    def projects(self, days=7):
        rows = self.repo.rows('''SELECT p.*,a.company AS architect,d.company AS developer FROM planning_projects p
            LEFT JOIN prospect_accounts a ON a.id=p.agent_account_id
            LEFT JOIN prospect_accounts d ON d.id=p.applicant_account_id
            WHERE fit_score>0 AND first_seen_at>=? ORDER BY first_seen_at DESC,p.id''', (stamp(self.outreach.now()-timedelta(days=days)),))
        for p in rows:
            p['fit_reasons'] = json.loads(p['fit_reasons'])
            p['contacts'] = self.repo.rows('SELECT id,name,email,email_status FROM prospect_contacts WHERE account_id=?',(p['agent_account_id'],))
        return rows

    def enrol(self, project_id, contact_id, mailbox, role='architect'):
        if role not in ('architect', 'developer'):
            raise ValueError('Invalid project role')
        rows = self.repo.rows('SELECT * FROM planning_projects WHERE id=?',(project_id,))
        if not rows or not rows[0]['fit_score'] or not rows[0]['source_url']:
            raise ValueError('A fit project with a register source is required')
        p = rows[0]
        contact = self.repo.get('prospect_contacts',contact_id)
        aid = p['agent_account_id' if role == 'architect' else 'applicant_account_id']
        if not aid or contact['account_id'] != aid:
            raise ValueError('Contact does not belong to this project role')
        account = self.repo.get('prospect_accounts',aid)
        identifiers = [contact['email'] or '', (contact['email'] or '').rpartition('@')[2], account['domain'] or '', name_key(account['company'])]
        if any(self.repo.rows('SELECT 1 FROM planning_first_touch_exclusions WHERE identifier=?',(v,)) for v in identifiers if v):
            raise ValueError('Already contacted in historical planning outreach; review follow-up manually')
        existing = self.repo.rows('''SELECT e.sequence_id FROM planning_enrolment_projects l JOIN outreach_enrolments e ON e.id=l.enrolment_id WHERE l.project_id=? AND e.contact_id=?''',(project_id,contact_id))
        if existing:
            return self.outreach.sequence(existing[0]['sequence_id'])
        seq = self.outreach.save_sequence('Live planning '+role+': '+p['reference'],role,[{
            'channel':'email','day_offset':0,'subject':'Planning {project_ref} at {project_address}',
            'template':'Hello {first_name}.\nI saw your '+('application' if role == 'developer' else 'work on application')+' {project_ref} at {project_address}.\nThe register describes: {project_summary}\nCould we price joinery, kitchens, wardrobes, doors or FF&E against your drawings for this scheme?'}])
        self.outreach.enrol(seq['id'],[contact_id],mailbox)
        eid = self.repo.rows('SELECT id FROM outreach_enrolments WHERE sequence_id=? AND contact_id=?',(seq['id'],contact_id))[0]['id']
        self.repo.execute('INSERT INTO planning_enrolment_projects VALUES(?,?,?)',(eid,project_id,json.dumps(p)))
        return self.outreach.sequence(seq['id'])
