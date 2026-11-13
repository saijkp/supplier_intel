"""Deterministic, injectable outreach engine. Never automates LinkedIn.

An event claimed before send is a durable no-retry marker, even after a crash.
The shared mailbox lock covers Sent inspection, reservation and verification.
"""
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone, date
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from threading import Semaphore
from urllib.parse import quote, urlparse

from customers.mailboxes import Mailbox, SEND_LOCK, UK, sent_time, recipients
from outreach.repository import OutreachRepository
from storage.database import connection_scope

STAGES = ('prospect', 'contacted', 'replied', 'meeting', 'drawings received', 'quoted', 'won', 'lost')
SEGMENTS = ('architect', 'interior_designer', 'showroom_dealer', 'uk_brand', 'distributor', 'developer')
SOURCES = ('apollo_free_search', 'website', 'business_card', 'manual')
FIELDS = {'first_name', 'company', 'area', 'project_hint', 'project_ref', 'project_address', 'project_summary'}
GATE = Semaphore(1)
FOOTER = ('\n\nSai Jyothis | IK Eng Ltd\n#1, 125 Great Clowes Street, Salford, M7 1AL, UK\n'
          'Relevant professional sourcing introduction; source: {source}.\n'
          'We use limited business contact data for this introduction on legitimate interests.\n'
          'For privacy information, access or deletion requests, reply to this email.\n'
          'To opt out of further outreach, reply "opt out".\n'
          'Any introducer fee must be agreed in writing and disclosed to the end client.')


def utcnow():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def domain(value):
    return (urlparse(value if '://' in value else 'https://' + value).hostname or '').lower().removeprefix('www.')


def validate_template(text):
    if set(re.findall(r'\{([^{}]+)\}', text)) - FIELDS or '<' in text or '>' in text:
        raise ValueError('Only plain text and approved merge fields are allowed')
    if re.search(r'supplier\s+(?:name|cost|price)|\bmargin\b|bit\.ly|tinyurl|tracking', text, re.I):
        raise ValueError('Private commercial details or tracking are not allowed')
    # Specific company identities/claims need Sai review; no AI-generated text enters here.


def render(text, values):
    """Drop entire sentences containing missing evidence; never infer fields."""
    output = []
    for line in text.splitlines():
        sentences = re.split(r'(?<=[.!?])\s+', line)
        keep = []
        for sentence in sentences:
            keys = re.findall(r'\{([^{}]+)\}', sentence)
            if any(not values.get(k) for k in keys):
                continue
            for k in keys:
                sentence = sentence.replace('{' + k + '}', str(values[k]))
            keep.append(sentence)
        if keep:
            output.append(' '.join(keep))
    return '\n'.join(output)


def classify(text):
    text = text.lower()
    if re.search(r'opt[ -]?out|unsubscribe|remove me|do not (?:email|contact)|stop (?:emailing|contacting)', text):
        return 'opt-out'
    if re.search(r'undeliver|delivery (?:status notification|fail)|mail delivery subsystem|address not found', text):
        return 'bounce'
    if re.search(r'out of (?:the )?office|automatic reply|auto.?reply|on annual leave', text):
        return 'out-of-office'
    if re.search(r'not interested|not relevant|no requirement', text):
        return 'not relevant'
    if re.search(r'not now|next (?:year|month)|later in|in a few months', text):
        return 'not now'
    if re.search(r'please (?:quote|price)|send (?:a |your )?(?:quote|quotation)|attached.*drawing|book a meeting|interested in', text):
        return 'interested'
    return 'needs Sai'


class OutreachService:
    def __init__(self, repo=None, mailboxes=None, now=None, send_enabled=None, warmup_start=None):
        self.repo = repo or OutreachRepository()
        self._boxes = mailboxes
        self.now = now or utcnow
        self.enabled = (os.getenv('OUTREACH_SEND_ENABLED', '').lower() == 'true') if send_enabled is None else send_enabled
        self.warmup_start = warmup_start or os.getenv('OUTREACH_WARMUP_START', '')

    def mailboxes(self):
        if self._boxes is None:
            from rfq.zoho_mail import ZohoClient, ZohoSender, ZohoInbox
            self._boxes = {}
            for address, prefix in [('sai@ikeng.online', 'RFQ_ZOHO_'), ('sj@ikeng.online', 'OUTREACH_ZOHO_')]:
                client = ZohoClient(prefix=prefix)
                self._boxes[address] = Mailbox(address, ZohoSender(client, address), ZohoInbox(client))
        return self._boxes

    def event(self, account_id, kind, payload=None, enrolment_id=None, external_id=None, status='done'):
        now = stamp(self.now())
        return self.repo.execute('INSERT INTO outreach_events(account_id,enrolment_id,kind,payload,external_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                                 (account_id, enrolment_id, kind, json.dumps(payload or {}), external_id, status, now, now))

    def import_rows(self, rows, source, source_url=''):
        if not source or re.search(r'purchased|bought|rented list',source,re.I):
            raise ValueError('Non-purchased provenance is required')
        result = {'created': 0, 'merged': 0, 'contacts': 0}
        for raw in rows:
            company = (raw.get('company') or raw.get('name') or '').strip()
            if not company:
                continue
            website = (raw.get('website') or raw.get('web') or '').strip()
            number = str(raw.get('companies_house_number') or raw.get('number') or '').strip() or None
            postcode = (raw.get('postcode') or '').strip().upper()
            if not postcode:
                explicit = re.search(r'\b(?:GIR\s?0AA|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2})\b',raw.get('address') or '',re.I)
                if explicit:
                    postcode=explicit[0].upper()
            key = re.sub(r'\W+', '', company.casefold()) + '|' + re.sub(r'\s+', '', postcode)
            host = domain(website) if website else None
            segment = raw.get('segment') or ('architect' if number else 'showroom_dealer')
            if segment not in SEGMENTS:
                raise ValueError('Invalid segment')
            subtype = raw.get('subscriber_type') or ('corporate' if number else 'unknown')
            if subtype not in ('corporate', 'sole_trader', 'partnership', 'unknown'):
                raise ValueError('Invalid subscriber type')
            desc = raw.get('fit_notes') or raw.get('desc') or ''
            priority = 'high' if segment in ('architect', 'interior_designer') else 'low'
            if segment in ('uk_brand', 'distributor') or re.search(r'\bresell|\bdealer|\bdistribut|\bUK brand|\bBritish brand', desc, re.I):
                priority = 'high'
            now = stamp(self.now())
            with connection_scope(self.repo.db_path) as c:
                c.execute('BEGIN IMMEDIATE')
                found = None
                for sql, args in [('companies_house_number=?', (number,)), ('domain=?', (host,)), ('name_key=?', (key,))]:
                    if args[0]:
                        found = c.execute('SELECT id FROM prospect_accounts WHERE ' + sql, args).fetchone()
                        if found:
                            break
                if found:
                    aid = found['id']; result['merged'] += 1
                    # Preserve trusted fields; the raw candidate stays in an event.
                else:
                    aid = c.execute('INSERT INTO prospect_accounts(company,segment,website,domain,address,postcode,area,companies_house_number,name_key,subscriber_type,source,source_url,fit_notes,priority,flagged,flag_reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (company, segment, website, host, raw.get('address'), postcode, raw.get('area'), number, key, subtype, source,
                         raw.get('source_url') or source_url, desc, priority, int(subtype != 'corporate'),
                         'Subscriber status requires review' if subtype != 'corporate' else None, now, now)).lastrowid
                    result['created'] += 1
                email = (raw.get('email') or '').strip().lower() or None
                name = raw.get('contact_name') or (raw.get('name') if raw.get('company') else None)
                if email and not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', email):
                    raise ValueError('Invalid email')
                if email or name or raw.get('linkedin_url'):
                    existing = c.execute('SELECT id FROM prospect_contacts WHERE account_id=? AND (email=? OR (email IS NULL AND coalesce(name,\'\')=? AND coalesce(linkedin_url,\'\')=?))',
                                         (aid, email, name or '', raw.get('linkedin_url') or '')).fetchone()
                    if not existing:
                        contact_source = source if source in SOURCES else 'manual'
                        c.execute('INSERT INTO prospect_contacts(account_id,name,title,email,linkedin_url,phone,source,source_url,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                                  (aid, name, raw.get('title'), email, raw.get('linkedin_url'), raw.get('phone'), contact_source, raw.get('source_url') or source_url or source, now))
                        result['contacts'] += 1
            self.event(aid, 'import', {'source': source, 'candidate': raw})
        return result

    def update_account(self, aid, **values):
        allowed = {'segment', 'subscriber_type', 'fit_notes', 'priority', 'owner', 'flagged', 'flag_reason', 'area'}
        if set(values) - allowed:
            raise ValueError('Unsupported account fields')
        self.repo.get('prospect_accounts', aid)
        if values.get('segment', 'architect') not in SEGMENTS or values.get('subscriber_type', 'unknown') not in ('corporate','sole_trader','partnership','unknown'):
            raise ValueError('Invalid account classification')
        with SEND_LOCK, connection_scope(self.repo.db_path) as c:
            for key, value in values.items():
                c.execute(f'UPDATE prospect_accounts SET {key}=?,updated_at=? WHERE id=?', (value, stamp(self.now()), aid))
            c.execute('UPDATE outreach_sequences SET approved_at=NULL,approved_by=NULL WHERE id IN (SELECT sequence_id FROM outreach_enrolments JOIN prospect_contacts ON contact_id=prospect_contacts.id WHERE account_id=?)', (aid,))
        self.event(aid, 'note', values)
        return self.account(aid)

    def review_contact(self, cid, email_status, project_hint=None, project_hint_source=None):
        if email_status not in ('unreviewed','corporate_verified','invalid'):
            raise ValueError('Invalid email review')
        if project_hint and not project_hint_source:
            raise ValueError('Project hint needs a source')
        with SEND_LOCK, connection_scope(self.repo.db_path) as c:
            c.execute('UPDATE prospect_contacts SET email_status=?,project_hint=?,project_hint_source=? WHERE id=?', (email_status,project_hint,project_hint_source,cid))
            c.execute('UPDATE outreach_sequences SET approved_at=NULL,approved_by=NULL WHERE id IN (SELECT sequence_id FROM outreach_enrolments WHERE contact_id=?)', (cid,))
        return self.repo.get('prospect_contacts', cid)

    def save_sequence(self, title, segment, steps, sequence_id=None):
        if segment not in SEGMENTS or not steps or len(steps) > 20:
            raise ValueError('Invalid segment or steps')
        previous = -1
        for step in steps:
            if step['channel'] not in ('email','linkedin_task','visit_task','call_task') or not isinstance(step['day_offset'], int) or step['day_offset'] < previous:
                raise ValueError('Steps must have valid channels and increasing nonnegative offsets')
            previous = step['day_offset']
            validate_template(step.get('subject', '')); validate_template(step['template'])
            if not step['template'].strip():
                raise ValueError('Empty template')
        with SEND_LOCK, connection_scope(self.repo.db_path) as c:
            if sequence_id:
                old = c.execute('SELECT * FROM outreach_steps WHERE sequence_id=? ORDER BY position',(sequence_id,)).fetchall()
                if not old:
                    raise ValueError('Sequence not found')
                used = c.execute('SELECT 1 FROM outreach_events WHERE step_id IN (SELECT id FROM outreach_steps WHERE sequence_id=?)', (sequence_id,)).fetchone()
                if used:
                    if len(old)!=len(steps) or any(r['channel']!=v['channel'] or r['day_offset']!=v['day_offset'] for r,v in zip(old,steps)):
                        raise ValueError('Create a revised sequence to change the structure of a used cadence')
                    c.execute('UPDATE outreach_sequences SET title=?,segment=?,approved_at=NULL,approved_by=NULL WHERE id=?',(title,segment,sequence_id))
                    for row,step in zip(old,steps):
                        c.execute('UPDATE outreach_steps SET subject=?,template=? WHERE id=?',(step.get('subject',''),step['template'],row['id']))
                    # Historic rendered content remains in immutable event payloads.
                    return_id=sequence_id
                else:
                    return_id=None
                c.execute('UPDATE outreach_sequences SET title=?,segment=?,approved_at=NULL,approved_by=NULL WHERE id=?', (title,segment,sequence_id))
                if not used:
                    c.execute('DELETE FROM outreach_steps WHERE sequence_id=?', (sequence_id,))
            else:
                return_id=None
                sequence_id = c.execute('INSERT INTO outreach_sequences(title,segment,created_at) VALUES(?,?,?)', (title,segment,stamp(self.now()))).lastrowid
            for i, step in enumerate([] if return_id else steps):
                c.execute('INSERT INTO outreach_steps(sequence_id,position,channel,day_offset,subject,template) VALUES(?,?,?,?,?,?)',
                          (sequence_id,i,step['channel'],step['day_offset'],step.get('subject',''),step['template']))
        return self.sequence(sequence_id)

    def sequence(self, sid):
        seq = self.repo.get('outreach_sequences', sid)
        seq['steps'] = self.repo.rows('SELECT * FROM outreach_steps WHERE sequence_id=? ORDER BY position', (sid,))
        seq['contacts'] = self.repo.rows('SELECT e.*,c.name,c.email,a.company,a.subscriber_type,c.email_status FROM outreach_enrolments e JOIN prospect_contacts c ON c.id=e.contact_id JOIN prospect_accounts a ON a.id=c.account_id WHERE sequence_id=?', (sid,))
        for contact in seq['contacts']:
            linked = self.repo.rows('SELECT snapshot FROM planning_enrolment_projects WHERE enrolment_id=?',(contact['id'],))
            contact['project_hook'] = json.loads(linked[0]['snapshot']) if linked else None
        return seq

    def enrol(self, sid, contact_ids, mailbox):
        if mailbox not in ('sai@ikeng.online','sj@ikeng.online'):
            raise ValueError('Unsupported mailbox')
        self.sequence(sid)
        with SEND_LOCK, connection_scope(self.repo.db_path) as c:
            for cid in set(contact_ids):
                if not c.execute('SELECT 1 FROM prospect_contacts WHERE id=?',(cid,)).fetchone():
                    raise ValueError('Contact not found')
                contact = dict(c.execute('SELECT c.email,a.domain,a.company FROM prospect_contacts c JOIN prospect_accounts a ON a.id=c.account_id WHERE c.id=?',(cid,)).fetchone())
                identifiers = [contact['email'] or '', (contact['email'] or '').rpartition('@')[2], contact['domain'] or '', re.sub(r'\W+', '', contact['company'].casefold())]
                if any(c.execute('SELECT 1 FROM planning_first_touch_exclusions WHERE identifier=?',(v.lower(),)).fetchone() for v in identifiers if v):
                    raise ValueError('Historical planning recipient requires manual follow-up review')
                other=c.execute("SELECT 1 FROM outreach_enrolments WHERE contact_id=? AND status='active' AND sequence_id!=?",(cid,sid)).fetchone()
                if other:
                    raise ValueError('Contact already has an active sequence; pause it first')
                c.execute('INSERT OR IGNORE INTO outreach_enrolments(contact_id,sequence_id,mailbox,next_step_at,started_at) VALUES(?,?,?,?,?)', (cid,sid,mailbox,stamp(self.now()),stamp(self.now())))
            c.execute('UPDATE outreach_sequences SET approved_at=NULL,approved_by=NULL WHERE id=?', (sid,))
        return self.sequence(sid)

    def approve(self, sid, approved_by, legitimate_interest_note):
        if approved_by != 'Sai' or not legitimate_interest_note.strip():
            raise ValueError('Sai and a legitimate interest assessment are required')
        seq = self.sequence(sid)
        if not seq['contacts']:
            raise ValueError('Approve an exact contact list, not an empty sequence')
        with SEND_LOCK:
            self.repo.execute('UPDATE outreach_sequences SET approved_at=?,approved_by=?,legitimate_interest_note=? WHERE id=?', (stamp(self.now()),approved_by,legitimate_interest_note,sid))
        return self.sequence(sid)

    def suppress(self, identifier, reason):
        identifier = identifier.strip().lower().removeprefix('@')
        if not identifier or not reason.strip():
            raise ValueError('Identifier and reason required')
        with SEND_LOCK, connection_scope(self.repo.db_path) as c:
            c.execute('INSERT OR IGNORE INTO suppression_list VALUES(?,?,?)', (identifier,reason,stamp(self.now())))
            contacts = c.execute('SELECT id,account_id,email FROM prospect_contacts').fetchall()
            for contact in contacts:
                email = contact['email'] or ''
                if email == identifier or email.rpartition('@')[2] == identifier:
                    c.execute('UPDATE prospect_contacts SET opted_out_at=? WHERE id=?', (stamp(self.now()), contact['id']))
                    c.execute("UPDATE outreach_enrolments SET status='stopped' WHERE contact_id=?", (contact['id'],))
                    c.execute("UPDATE outreach_events SET status='cancelled' WHERE status='pending' AND enrolment_id IN (SELECT id FROM outreach_enrolments WHERE contact_id=?)", (contact['id'],))

    def blocked(self, row):
        email = row.get('email') or ''
        suppressed = self.repo.rows('SELECT 1 FROM suppression_list WHERE identifier IN (?,?)', (email,email.rpartition('@')[2]))
        historical = any(self.repo.rows('SELECT 1 FROM planning_first_touch_exclusions WHERE identifier=?',(v,))
                         for v in (email, email.rpartition('@')[2], re.sub(r'\W+', '', row.get('company','').casefold())) if v)
        # RFQ objections must apply to prospect outreach too.
        rfq = self.repo.rows('SELECT 1 FROM rfq_suppressions WHERE lower(email)=?', (email,))
        return bool(historical or suppressed or rfq or row['opted_out_at'] or row['bounced_at'] or row['flagged'])

    def _values(self, row):
        projects = self.repo.rows('SELECT snapshot FROM planning_enrolment_projects WHERE enrolment_id=?', (row.get('id'),))
        project = json.loads(projects[0]['snapshot']) if projects else {}
        return {'project_ref': project.get('reference') if project.get('source_url') else '',
                'project_address': project.get('address') if project.get('source_url') else '',
                'project_summary': project.get('description') if project.get('source_url') else '',
                'first_name': (row['name'] or '').split()[0] if row['name'] and row['source_url'] else '',
                'company': row['company'] if row['account_source'] else '',
                'area': row['area'] if row['account_source'] else '',
                'project_hint': row['project_hint'] if row['project_hint_source'] else ''}

    def _advance(self, eid):
        row = self.repo.get('outreach_enrolments', eid)
        next_steps = self.repo.rows('SELECT s.* FROM outreach_steps s WHERE sequence_id=? AND NOT EXISTS(SELECT 1 FROM outreach_events v WHERE v.enrolment_id=? AND v.step_id=s.id) ORDER BY position LIMIT 1', (row['sequence_id'],eid))
        if next_steps:
            start = datetime.fromisoformat(row['started_at']).replace(tzinfo=timezone.utc)
            self.repo.execute('UPDATE outreach_enrolments SET next_step_at=? WHERE id=?', (stamp(start+timedelta(days=next_steps[0]['day_offset'])),eid))
        else:
            self.repo.execute("UPDATE outreach_enrolments SET status='finished' WHERE id=?", (eid,))
            # Finishing a cold cadence is not a business outcome. Keep an action
            # visible until Sai chooses a new next step or closes the prospect.
            if self.repo.rows("SELECT 1 FROM outreach_steps WHERE sequence_id=? AND channel='email'",(row['sequence_id'],)):
                contact=self.repo.get('prospect_contacts',row['contact_id'])
                key='outreach-finished:'+str(eid)
                if not self.repo.rows('SELECT 1 FROM outreach_events WHERE external_id=?',(key,)):
                    self.event(contact['account_id'],'needs Sai',{'reason':'Cadence finished. Review replies, schedule a dated next action or explicitly close won/lost; no automatic closure.'},eid,external_id=key,status='pending')

    def _cap(self, mailbox):
        if mailbox != 'sj@ikeng.online':
            return 30
        try:
            age = (self.now().astimezone(UK).date() - date.fromisoformat(self.warmup_start)).days
        except ValueError:
            raise ValueError('Configure OUTREACH_WARMUP_START before sending from sj') from None
        if age < 0:
            return 0
        return (5,10,20,30)[min(age//7,3)]

    def run_due(self, limit=10):
        if not 1 <= limit <= 100:
            raise ValueError('Limit must be 1–100')
        if not GATE.acquire(blocking=False):
            return {'busy': True}
        try:
            return self._run_due(limit)
        finally:
            GATE.release()

    def _run_due(self, limit):
        stats = {'sent':0,'tasks':0,'skipped':0,'failed':0}
        deadline = time.monotonic()+45
        for _ in range(limit):
            if time.monotonic() > deadline:
                break
            rows = self.repo.rows('''SELECT e.*,c.name,c.email,c.email_status,c.source,c.source_url,c.project_hint,c.project_hint_source,
                c.linkedin_url,c.opted_out_at,c.bounced_at,a.id account_id,a.company,a.area,a.address,a.source account_source,
                a.subscriber_type,a.flagged FROM outreach_enrolments e JOIN prospect_contacts c ON c.id=e.contact_id
                JOIN prospect_accounts a ON a.id=c.account_id JOIN outreach_sequences q ON q.id=e.sequence_id
                WHERE e.status='active' AND e.next_step_at<=? AND q.approved_at IS NOT NULL ORDER BY e.next_step_at,e.id''', (stamp(self.now()),))
            progressed = False
            for row in rows:
                eid = row['id']
                steps = self.repo.rows('SELECT s.* FROM outreach_steps s WHERE sequence_id=? AND NOT EXISTS(SELECT 1 FROM outreach_events v WHERE v.enrolment_id=? AND v.step_id=s.id) ORDER BY position LIMIT 1', (row['sequence_id'],eid))
                if not steps:
                    self._advance(eid); continue
                step = steps[0]
                start = datetime.fromisoformat(row['started_at']).replace(tzinfo=timezone.utc)
                if start+timedelta(days=step['day_offset']) > self.now():
                    self._advance(eid); continue
                if self.blocked(row):
                    self.repo.execute("UPDATE outreach_enrolments SET status='stopped' WHERE id=?", (eid,)); continue
                values = self._values(row)
                payload = {'subject':render(step['subject'],values),'body':render(step['template'],values),
                           'recipient':row['email'],'mailbox':row['mailbox']}
                if step['channel'] == 'email':
                    if not self.enabled or row['subscriber_type'] != 'corporate' or row['email_status'] != 'corporate_verified' or not row['email']:
                        stats['skipped'] += 1; continue
                    if not payload['body'] or not payload['subject'] or re.search(r'[\r\n]',payload['subject']):
                        stats['skipped'] += 1; continue
                    payload['subject'] += ' [IKE-PROSPECT-'+str(eid)+']'
                    payload['body'] += FOOTER.format(source=row['source_url'] or row['source'])
                    try:
                        sent = self._send(row, step, payload)
                    except Exception:
                        self.repo.execute("UPDATE outreach_enrolments SET status='paused' WHERE id=? AND status='active'",(eid,))
                        if not self.repo.rows("SELECT 1 FROM outreach_events WHERE enrolment_id=? AND kind='needs Sai' AND status='pending'",(eid,)):
                            self.event(row['account_id'],'needs Sai',{'reason':'Delivery safety check blocked sending. Review Sent, cap and configuration.'},eid,status='pending')
                        stats['failed'] += 1
                        return stats
                    if not sent:
                        continue
                    stats['sent'] += 1
                    self.stage(row['account_id'],'contacted')
                else:
                    payload.update({'connection_note': render('Hello {first_name}. Could we connect about sourcing to drawing for {company}?',values)[:299],
                                    'action_url': row['linkedin_url'] if (row['linkedin_url'] or '').startswith('https://www.linkedin.com/') else 'https://www.linkedin.com/sales/search/people?keywords='+quote((row['name'] or '')+' '+row['company']),
                                    'address':row['address'],'area':row['area']})
                    with SEND_LOCK, connection_scope(self.repo.db_path) as c:
                        # Reply/approval may have changed while fetching data.
                        eligible = c.execute("SELECT 1 FROM outreach_enrolments e JOIN outreach_sequences q ON q.id=e.sequence_id WHERE e.id=? AND e.status='active' AND q.approved_at IS NOT NULL", (eid,)).fetchone()
                        if not eligible:
                            continue
                        try:
                            c.execute('INSERT INTO outreach_events(enrolment_id,account_id,step_id,kind,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)', (eid,row['account_id'],step['id'],step['channel'],'pending',json.dumps(payload),stamp(self.now()),stamp(self.now())))
                        except sqlite3.IntegrityError:
                            continue
                    stats['tasks'] += 1
                self._advance(eid); progressed=True
                break
            if not progressed:
                break
        return stats

    def _send(self, row, step, payload):
        with SEND_LOCK:
            # BEGIN IMMEDIATE claim below plus unique ledger is cross-process safe.
            box = self.mailboxes().get(row['mailbox'])
            if not box:
                raise ValueError('Mailbox not configured')
            sent = box.sent(2)
            today = self.now().astimezone(UK).date()
            today_sent = [m for m in sent if sent_time(m).astimezone(UK).date()==today]
            if len(today_sent) >= self._cap(row['mailbox']):
                return False
            box.check_send(payload,sent,self.now())
            if sent and (self.now()-max(sent_time(m) for m in sent)).total_seconds() < 90:
                return False
            # Ledger reservations count even when Zoho has not exposed a send yet.
            recent = self.repo.rows("SELECT created_at,status FROM outreach_events WHERE kind='email' AND json_extract(payload,'$.mailbox')=? AND status IN ('sending','failed','sent') ORDER BY id DESC", (row['mailbox'],))
            if any(v['status'] in ('sending','failed') for v in recent):
                raise ValueError('Uncertain mailbox send requires human review')
            if recent and (self.now()-datetime.fromisoformat(recent[0]['created_at']).replace(tzinfo=timezone.utc)).total_seconds()<90:
                return False
            now = stamp(self.now())
            with connection_scope(self.repo.db_path) as c:
                c.execute('BEGIN IMMEDIATE')
                # Re-read identity/suppression/approval inside the reservation transaction.
                eligible = c.execute("SELECT 1 FROM outreach_enrolments e JOIN outreach_sequences q ON q.id=e.sequence_id JOIN prospect_contacts p ON p.id=e.contact_id JOIN prospect_accounts a ON a.id=p.account_id WHERE e.id=? AND e.status='active' AND q.approved_at IS NOT NULL AND p.opted_out_at IS NULL AND p.bounced_at IS NULL AND a.flagged=0 AND a.subscriber_type='corporate' AND p.email_status='corporate_verified' AND NOT EXISTS(SELECT 1 FROM suppression_list s WHERE s.identifier=p.email OR s.identifier=substr(p.email,instr(p.email,'@')+1))", (row['id'],)).fetchone()
                if not eligible:
                    return False
                # Only one mailbox reservation may be outstanding across replicas.
                if c.execute("SELECT 1 FROM outreach_events WHERE kind='email' AND status IN ('sending','failed') AND json_extract(payload,'$.mailbox')=?", (row['mailbox'],)).fetchone():
                    return False
                last = c.execute("SELECT created_at FROM outreach_events WHERE kind='email' AND json_extract(payload,'$.mailbox')=? ORDER BY id DESC LIMIT 1", (row['mailbox'],)).fetchone()
                if last and (self.now()-datetime.fromisoformat(last['created_at']).replace(tzinfo=timezone.utc)).total_seconds()<90:
                    return False
                try:
                    event_id = c.execute('INSERT INTO outreach_events(enrolment_id,account_id,step_id,kind,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)', (row['id'],row['account_id'],step['id'],'email','sending',json.dumps(payload),now,now)).lastrowid
                except sqlite3.IntegrityError:
                    return False
            try:
                provider = box.sender.send(payload)
                self.repo.execute('UPDATE outreach_events SET provider_id=? WHERE id=?',(provider,event_id))
                provider_id = str(provider).removeprefix('zoho:')
                verified = box.sent(2)
                if not any(str(m.get('messageId'))==provider_id and payload['recipient'].lower() in recipients(m) for m in verified):
                    raise ValueError('Sent verification failed')
                self.repo.execute("UPDATE outreach_events SET status='sent',provider_id=?,updated_at=? WHERE id=?", (provider,stamp(self.now()),event_id))
                return True
            except Exception:
                self.repo.execute("UPDATE outreach_events SET status='failed',updated_at=? WHERE id=?", (stamp(self.now()),event_id))
                self.repo.execute("UPDATE outreach_enrolments SET status='paused' WHERE id=?", (row['id'],))
                self.event(row['account_id'],'needs Sai',{'reason':'Send uncertain; inspect Zoho and ledger. Never retry blindly.'},row['id'],status='pending')
                raise

    def stage(self, aid, stage):
        if stage not in STAGES or stage in ('won','lost'):
            raise ValueError('Use linked customer enquiry closure for won/lost')
        account = self.repo.get('prospect_accounts',aid)
        if STAGES.index(stage) < STAGES.index(account['stage']):
            return
        self.repo.execute('UPDATE prospect_accounts SET stage=?,updated_at=? WHERE id=?', (stage,stamp(self.now()),aid))
        self.event(aid,'stage change',{'stage':stage})

    def task_done(self, event_id, outcome):
        event = self.repo.get('outreach_events',event_id)
        if event['status'] != 'pending' or event['kind'] not in ('linkedin_task','visit_task','call_task','needs Sai'):
            raise ValueError('Task is not pending')
        if not outcome.strip():
            raise ValueError('Outcome required')
        with SEND_LOCK:
            self.repo.execute("UPDATE outreach_events SET status='done',updated_at=? WHERE id=? AND status='pending'", (stamp(self.now()),event_id))
            self.event(event['account_id'],'task done',{'task_id':event_id,'outcome':outcome},event['enrolment_id'])
        return {'done': True}

    def pause(self, eid):
        row=self.repo.get('outreach_enrolments',eid)
        with SEND_LOCK, connection_scope(self.repo.db_path) as c:
            c.execute("UPDATE outreach_enrolments SET status='paused' WHERE id=?",(eid,))
            c.execute('UPDATE outreach_sequences SET approved_at=NULL,approved_by=NULL WHERE id=?',(row['sequence_id'],))
            c.execute("UPDATE outreach_events SET status='cancelled' WHERE enrolment_id=? AND status='pending'",(eid,))
        return {'paused':True}

    def manual_task(self,eid,channel,text,due_at):
        if channel not in ('linkedin_task','visit_task','call_task','needs Sai') or not text.strip():
            raise ValueError('Choose a manual task channel and grounded text')
        try:
            when=datetime.fromisoformat(due_at.replace('Z','+00:00'))
            if when.tzinfo is None:
                raise ValueError('Timezone required')
        except ValueError:
            raise ValueError('Use an ISO date/time with timezone') from None
        enrolment=self.repo.get('outreach_enrolments',eid)
        contact=self.repo.get('prospect_contacts',enrolment['contact_id'])
        account=self.repo.get('prospect_accounts',contact['account_id'])
        if self.blocked({**account,**contact,'flagged':account['flagged']}):
            raise ValueError('Contact is suppressed, bounced or flagged')
        event_id=self.event(contact['account_id'],channel,{'body':text,'action_url':contact['linkedin_url'] or 'https://www.linkedin.com/sales/search/people?keywords='+quote((contact['name'] or '')+' '+account['company'])},eid,status='pending')
        self.repo.execute('UPDATE outreach_events SET due_at=? WHERE id=?',(stamp(when),event_id))
        return {'event_id':event_id}

    def reconcile(self,event_id,confirmed_by,note):
        if confirmed_by!='Sai' or not note.strip():
            raise ValueError('Sai must record the mailbox review outcome')
        event=self.repo.get('outreach_events',event_id)
        if event['kind']!='email' or event['status'] not in ('sending','failed'):
            raise ValueError('Only uncertain email ledger entries can be reviewed')
        payload=json.loads(event['payload'])
        with SEND_LOCK:
            sent=self.mailboxes()[payload['mailbox']].sent(14)
            matches=[m for m in sent if payload['recipient'] in recipients(m) and m.get('subject')==payload['subject']]
            status='sent' if matches else 'reviewed_no_retry'
            self.repo.execute('UPDATE outreach_events SET status=?,updated_at=? WHERE id=?',(status,stamp(self.now()),event_id))
            # This step stays consumed even if no Sent match is found. A new
            # explicitly approved sequence is required to send anything further.
            self.repo.execute("UPDATE outreach_enrolments SET status='paused' WHERE id=?",(event['enrolment_id'],))
            self.event(event['account_id'],'delivery review',{'event_id':event_id,'note':note,'sent_matches':len(matches)},event['enrolment_id'])
        return {'status':status,'retry_allowed':False}

    def receive(self, eid, text, external_id, subject=''):
        if not text.strip() or not external_id:
            raise ValueError('Reply text and external id required')
        enrolment = self.repo.get('outreach_enrolments',eid)
        contact = self.repo.get('prospect_contacts',enrolment['contact_id'])
        # Strip quoted history to avoid classifying our own opt-out footer.
        from rfq.quote_parser import strip_history
        category = classify(subject+'\n'+strip_history(text))
        with SEND_LOCK, connection_scope(self.repo.db_path) as c:
            if c.execute('SELECT 1 FROM outreach_events WHERE external_id=?', (external_id,)).fetchone():
                return {'duplicate':True}
            now = stamp(self.now())
            c.execute('INSERT INTO outreach_events(enrolment_id,account_id,kind,payload,external_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)', (eid,contact['account_id'],'reply',json.dumps({'text':text,'classification':category}),external_id,now,now))
            c.execute("UPDATE outreach_enrolments SET status='replied' WHERE contact_id=? AND status IN ('active','finished','paused')", (contact['id'],))
            c.execute("UPDATE outreach_events SET status='cancelled' WHERE status='pending' AND enrolment_id IN (SELECT id FROM outreach_enrolments WHERE contact_id=?)", (contact['id'],))
            if category == 'bounce':
                c.execute('UPDATE prospect_contacts SET bounced_at=? WHERE id=?', (now,contact['id']))
            if category in ('opt-out','bounce') and contact['email']:
                c.execute('INSERT OR IGNORE INTO suppression_list VALUES(?,?,?)',(contact['email'],category,now))
                c.execute('UPDATE prospect_contacts SET opted_out_at=? WHERE id=?',(now,contact['id']))
        if category in ('opt-out','bounce') and contact['email']:
            self.suppress(contact['email'],category)
        elif category == 'interested':
            task=self.event(contact['account_id'],'needs Sai',{'classification':category,'text':text,'reason':'Project intake pending'},eid,status='pending')
            try:
                enquiry = self.convert(contact['account_id'],contact['id'],text)
                payload={'classification':category,'text':text,'enquiry_id':enquiry['id']}
            except Exception:
                payload={'classification':category,'text':text,'reason':'Customer intake failed; Sai must create/link this enquiry before continuing'}
            self.repo.execute('UPDATE outreach_events SET payload=? WHERE id=?',(json.dumps(payload),task))
        else:
            self.event(contact['account_id'],'needs Sai',{'classification':category,'text':text},eid,status='pending')
        self.stage(contact['account_id'],'replied')
        return {'classification':category}

    def poll_replies(self):
        processed = 0
        for address, box in self.mailboxes().items():
            enrolments = self.repo.rows('SELECT e.*,c.email FROM outreach_enrolments e JOIN prospect_contacts c ON c.id=e.contact_id WHERE e.mailbox=?', (address,))
            if not enrolments:
                continue
            messages = [{'recipient':r['email'] or '', 'channel':'email','reference':'[IKE-PROSPECT-'+str(r['id'])+']'} for r in enrolments]
            for raw in box.inbox.fetch_for_campaign(14,200,messages):
                msg = BytesParser(policy=policy.default).parsebytes(raw)
                mid = msg.get('Message-ID')
                if not mid:
                    continue
                sender = parseaddr(msg.get('From',''))[1].lower()
                body = msg.get_body(preferencelist=('plain',)) if msg.is_multipart() else msg
                text = body.get_content() if body else ''
                if not isinstance(text,str):
                    continue
                candidates = [r for r in enrolments if r['email']==sender]
                ref = re.search(r'\[IKE-PROSPECT-(\d+)\]', msg.get('Subject','')+' '+text)
                if ref:
                    candidates = [r for r in enrolments if r['id']==int(ref[1])]
                # In-Reply-To/provider thread evidence, then unique sender fallback.
                reply_to = msg.get('In-Reply-To','').strip('<>')
                if reply_to:
                    matched = self.repo.rows('SELECT enrolment_id FROM outreach_events WHERE provider_id IN (?,?)', (reply_to,'zoho:'+reply_to))
                    if matched:
                        candidates = [r for r in enrolments if r['id']==matched[0]['enrolment_id']]
                if len(candidates)!=1:
                    if candidates:
                        self.ambiguous_reply(address+':'+mid,sender,text,candidates)
                    continue
                if self.receive(candidates[0]['id'],text,address+':'+mid,msg.get('Subject','')).get('duplicate') is not True:
                    processed += 1
        return {'processed':processed}

    def ambiguous_reply(self,external_id,sender,text,candidates):
        with SEND_LOCK, connection_scope(self.repo.db_path) as c:
            if c.execute('SELECT 1 FROM outreach_events WHERE external_id=?',(external_id,)).fetchone():
                return
            now=stamp(self.now())
            for row in candidates:
                c.execute("UPDATE outreach_enrolments SET status='replied' WHERE contact_id=? AND status IN ('active','paused','finished')",(row['contact_id'],))
                c.execute("UPDATE outreach_events SET status='cancelled' WHERE status='pending' AND enrolment_id IN (SELECT id FROM outreach_enrolments WHERE contact_id=?)",(row['contact_id'],))
            c.execute("INSERT INTO outreach_events(kind,status,payload,external_id,created_at,updated_at) VALUES('needs Sai','pending',?,?,?,?)",(json.dumps({'reason':'Ambiguous reply match; candidates stopped without guessing a project','sender':sender,'text':text,'candidate_enrolments':[r['id'] for r in candidates]}),external_id,now,now))
        if classify(text) in ('opt-out','bounce'):
            self.suppress(sender,classify(text))

    def convert(self, aid, cid, text):
        from customers.customer_service import CustomerService
        from storage.customer_repository import CustomerRepository
        service = CustomerService(repo=CustomerRepository(self.repo.db_path))
        links = self.repo.rows('SELECT enquiry_id FROM prospect_enquiry_links WHERE account_id=? ORDER BY enquiry_id DESC', (aid,))
        if not links:
            recovered=self.repo.rows('SELECT id FROM customer_enquiries WHERE source_ref=?',('prospect-account:'+str(aid),))
            if recovered:
                self.repo.execute('INSERT OR IGNORE INTO prospect_enquiry_links(account_id,enquiry_id) VALUES(?,?)',(aid,recovered[0]['id']))
                links=[{'enquiry_id':recovered[0]['id']}]
        if links:
            service.log(links[0]['enquiry_id'],'email_in',text)
            return service.get_enquiry(links[0]['enquiry_id'])
        account = self.repo.get('prospect_accounts',aid)
        contact = self.repo.get('prospect_contacts',cid)
        if contact['account_id']!=aid or not text.strip():
            raise ValueError('A contact on this account and the exact enquiry message are required')
        existing = service.list_customers(q=contact['email']) if contact['email'] else []
        customer = next((c for c in existing if (c.get('email') or '').lower()==contact['email']),None)
        if not customer:
            customer = service.create_customer(name=contact['name'] or account['company'],company=account['company'],email=contact['email'],source='prospect outreach')
        enquiry = service.create_enquiry(customer['id'],title='Project enquiry: '+account['company'],source_ref='prospect-account:'+str(aid),requirements=text,next_action='Sai: review project requirements',waiting_on='us')
        self.repo.execute('INSERT INTO prospect_enquiry_links(account_id,enquiry_id) VALUES(?,?)', (aid,enquiry['id']))
        service.log(enquiry['id'],'email_in',text)
        return enquiry

    def link_enquiry(self,aid,enquiry_id):
        self.repo.get('prospect_accounts',aid)
        if not self.repo.rows('SELECT 1 FROM customer_enquiries WHERE id=?',(enquiry_id,)):
            raise ValueError('Customer enquiry not found')
        self.repo.execute('INSERT OR IGNORE INTO prospect_enquiry_links(account_id,enquiry_id) VALUES(?,?)',(aid,enquiry_id))
        self.event(aid,'project linked',{'enquiry_id':enquiry_id})
        return self.account(aid)

    def close(self, aid, enquiry_id, outcome, close_note, won_value=None, close_reason=None, margin_gbp=None):
        from customers.customer_service import CustomerService
        from storage.customer_repository import CustomerRepository
        import math
        if outcome=='lost' and enquiry_id is None:
            self.repo.get('prospect_accounts',aid)
            if not close_reason or not close_note.strip():
                raise ValueError('Sai must record a loss reason and note')
            with SEND_LOCK, connection_scope(self.repo.db_path) as c:
                c.execute("UPDATE prospect_accounts SET stage='lost',updated_at=? WHERE id=?",(stamp(self.now()),aid))
                c.execute("UPDATE outreach_enrolments SET status='stopped' WHERE contact_id IN (SELECT id FROM prospect_contacts WHERE account_id=?)",(aid,))
                c.execute("UPDATE outreach_events SET status='cancelled' WHERE account_id=? AND status='pending'",(aid,))
            self.event(aid,'stage change',{'stage':'lost','close_reason':close_reason,'close_note':close_note})
            return self.account(aid)
        if margin_gbp is not None and (isinstance(margin_gbp,bool) or not math.isfinite(margin_gbp)):
            raise ValueError('Margin must be finite GBP')
        if not self.repo.rows('SELECT 1 FROM prospect_enquiry_links WHERE account_id=? AND enquiry_id=?',(aid,enquiry_id)):
            raise ValueError('Linked enquiry not found')
        if outcome not in ('won','lost'):
            raise ValueError('Choose won or lost')
        service = CustomerService(repo=CustomerRepository(self.repo.db_path))
        enquiry = service.close_enquiry(enquiry_id,outcome,close_reason,close_note,won_value)
        self.repo.execute('UPDATE prospect_enquiry_links SET margin_gbp=? WHERE account_id=? AND enquiry_id=?',(margin_gbp,aid,enquiry_id))
        self.repo.execute('UPDATE prospect_accounts SET stage=?,updated_at=? WHERE id=?',(outcome,stamp(self.now()),aid))
        self.event(aid,'stage change',{'stage':outcome,'enquiry_id':enquiry_id})
        with SEND_LOCK, connection_scope(self.repo.db_path) as c:
            c.execute("UPDATE outreach_enrolments SET status='stopped' WHERE contact_id IN (SELECT id FROM prospect_contacts WHERE account_id=?)",(aid,))
            c.execute("UPDATE outreach_events SET status='cancelled' WHERE account_id=? AND status='pending'",(aid,))
        return enquiry

    def visit(self, aid, outcome, card=None):
        account = self.repo.get('prospect_accounts',aid)
        if not outcome.strip():
            raise ValueError('Visit outcome required')
        if card:
            self.import_rows([{'company':account['company'],'postcode':account['postcode'],**card}], 'business_card','Business card from visit '+stamp(self.now()))
        self.event(aid,'visited',{'outcome':outcome})
        with SEND_LOCK, connection_scope(self.repo.db_path) as c:
            c.execute("UPDATE outreach_enrolments SET status='paused' WHERE contact_id IN (SELECT id FROM prospect_contacts WHERE account_id=?) AND status='active'",(aid,))
            c.execute("UPDATE outreach_events SET status='cancelled' WHERE account_id=? AND status='pending'",(aid,))
        self.stage(aid,'meeting')
        # Draft-only sequence: explicitly enrol exact card contacts and approve before sending.
        sequence = self.save_sequence('Great to meet you — '+account['company'],account['segment'],[
            {'channel':'email','day_offset':0,'subject':'Great to meet you at {company}',
             'template':'Hello {first_name}.\nIt was good to meet you today. Could we price one item to drawing for your next project?'}])
        contacts = self.repo.rows('SELECT id FROM prospect_contacts WHERE account_id=? AND source=\'business_card\'',(aid,))
        if card and card.get('email'):
            contacts=self.repo.rows('SELECT id FROM prospect_contacts WHERE account_id=? AND lower(email)=?',(aid,card['email'].strip().lower()))
        if contacts:
            self.enrol(sequence['id'],[c['id'] for c in contacts],'sj@ikeng.online')
        return {'sequence':sequence,'contact_ids':[c['id'] for c in contacts], 'approval_required':True}

    def account(self, aid):
        account = self.repo.get('prospect_accounts',aid)
        account['contacts'] = self.repo.rows('SELECT * FROM prospect_contacts WHERE account_id=?',(aid,))
        account['events'] = self.repo.rows('SELECT * FROM outreach_events WHERE account_id=? ORDER BY id DESC',(aid,))
        account['enrolments'] = self.repo.rows('SELECT e.* FROM outreach_enrolments e JOIN prospect_contacts c ON c.id=e.contact_id WHERE c.account_id=?',(aid,))
        account['enquiries'] = self.repo.rows('SELECT q.*,l.margin_gbp FROM prospect_enquiry_links l JOIN customer_enquiries q ON q.id=l.enquiry_id WHERE l.account_id=?',(aid,))
        account['project_hooks'] = self.repo.rows("SELECT id,address,description,units,reference,status,source_url FROM planning_projects WHERE agent_account_id=? AND agent_role='architect' ORDER BY first_seen_at DESC", (aid,))
        account['project_hook'] = account['project_hooks'][0] if account['project_hooks'] else None
        return account

    def tasks(self):
        return self.repo.rows("SELECT e.*,coalesce(a.company,'Reply needs matching') company,a.area,a.address FROM outreach_events e LEFT JOIN prospect_accounts a ON a.id=e.account_id WHERE e.status='pending' AND (e.due_at IS NULL OR e.due_at<=?) ORDER BY coalesce(a.area,''),coalesce(a.address,''),e.id",(stamp(self.now()),))

    def metrics(self):
        counts = {r['stage']:r['n'] for r in self.repo.rows('SELECT stage,count(*) n FROM prospect_accounts GROUP BY stage')}
        # Distinct people across revised sequences.
        contacted = self.repo.rows("SELECT count(DISTINCT e.contact_id) n FROM outreach_enrolments e JOIN outreach_events v ON v.enrolment_id=e.id LEFT JOIN outreach_events original ON original.id=json_extract(v.payload,'$.task_id') WHERE (v.kind='email' AND v.status='sent') OR (v.kind='task done' AND original.kind IN ('linkedin_task','visit_task','call_task'))")[0]['n']
        replied = self.repo.rows("SELECT count(DISTINCT e.contact_id) n FROM outreach_enrolments e JOIN outreach_events v ON v.enrolment_id=e.id WHERE v.kind='reply' AND json_extract(v.payload,'$.classification') NOT IN ('bounce','out-of-office')")[0]['n']
        value = self.repo.rows("SELECT count(*) wins,coalesce(sum(q.won_value),0) won_gbp,sum(l.margin_gbp) margin_gbp FROM prospect_enquiry_links l JOIN customer_enquiries q ON q.id=l.enquiry_id WHERE q.status='won'")[0]
        return {'stages':counts,'contacted':contacted,'replied':replied,'reply_rate':replied/contacted if contacted else 0,
                **value,'wins_per_100':100*value['wins']/contacted if contacted else 0,'target_wins_per_100':2}

    def run_pending(self, limit=10, force=False):
        # force never overrides ledger, approval, suppression or delivery safeguards.
        self.poll_replies()
        return self.run_due(limit)
