"""Cloud-only review worker. Its sole send capability is the fixed-owner digest.
Manually logged whatsapp_out activities use the same suggestion path as email_out.
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from customers.customer_service import CustomerService
from storage.customer_repository import CustomerRepository
from storage.database import connection_scope

UK = ZoneInfo('Europe/London')
logger = logging.getLogger(__name__)
INACTIVE = {'paused', 'nurture', 'not responding', 'won', 'lost', 'closed'}


def local_time(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return dt.replace(tzinfo=timezone.utc).astimezone(UK) if dt.tzinfo is None else dt.astimezone(UK)


def parse_promise(text, made_on):
    """Conservative suggestions only; date-only promises end at 17:00 UK."""
    base = local_time(made_on)
    text = text.lower()
    match = re.search(r'\b(?:by\s+)?(?:(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+)?(\d{1,2})\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?:\s+(20\d{2}))?\b', text)
    if match:
        try:
            month = datetime.strptime(match[3][:3], '%b').month
            due = base.replace(year=int(match[4] or base.year), month=month, day=int(match[2]))
            if match[1] and due.strftime('%A').lower() != match[1]:
                return None
        except ValueError:
            return None
    elif 'tomorrow' in text:
        due = base + timedelta(days=1)
    elif 'end of this week' in text:
        due = base + timedelta(days=4-base.weekday())
    else:
        match = re.search(r'\bby\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', text)
        if not match:
            return None
        day = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday'].index(match[1])
        due = base + timedelta(days=(day-base.weekday()) % 7)
    return due.replace(hour=17, minute=0, second=0, microsecond=0).isoformat()


def working_due(sent_at):
    due = local_time(sent_at)
    days = 0
    while days < 3:
        due += timedelta(days=1)
        days += due.weekday() < 5
    return due


class DealCloserService:
    def __init__(self, customers=None, clock=None, digest_sender=None):
        self.customers = customers if customers is not None else CustomerService()
        self.repo = self.customers.repo
        self.clock = clock or (lambda: datetime.now(UK))
        self.digest_sender = digest_sender

    def state(self, key, value=None):
        with connection_scope(self.repo.db_path) as c:
            if value is not None:
                c.execute('INSERT INTO deal_closer_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key,json.dumps(value)))
            row = c.execute('SELECT value FROM deal_closer_state WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None

    def add_promise(self, enquiry_id, text, due_at, made_on=None, channel='email', source_activity_id=None, confirmed=True):
        self.repo.get('customer_enquiries', enquiry_id)
        if not text.strip():
            raise ValueError('Promise text is required')
        due = local_time(due_at).isoformat()
        made = local_time(made_on).isoformat() if made_on else self.clock().isoformat()
        row = self.repo.insert('customer_promises', enquiry_id=enquiry_id, text=text, due_at=due,
            made_on=made, channel=channel, source_activity_id=source_activity_id,
            confirmed_at=self.clock().isoformat() if confirmed else None)
        return self.repo.get('customer_promises',row)

    def update_promise(self, promise_id, status=None, confirmed=None):
        self.repo.get('customer_promises',promise_id)
        values = {}
        if status is not None:
            if status not in {'open','kept','missed','renegotiated'}:
                raise ValueError('Invalid promise status')
            values.update(status=status, closed_at=None if status=='open' else self.clock().isoformat())
        if confirmed is True:
            values['confirmed_at'] = self.clock().isoformat()
        if not values:
            raise ValueError('Specify status or confirm the suggestion')
        self.repo.update('customer_promises',promise_id,**values)
        return self.repo.get('customer_promises',promise_id)

    def suggest(self):
        for a in self.repo.rows('customer_activities', "kind IN ('email_out','whatsapp_out')"):
            if self.repo.rows('customer_promises','source_activity_id=?',(a['id'],)):
                continue
            due = parse_promise(a['summary'],a['occurred_at'])
            if due:
                self.add_promise(a['enquiry_id'],a['summary'],due,a['occurred_at'],a['kind'].split('_')[0],a['id'],False)

    def summary(self):
        now = self.clock().astimezone(UK)
        groups = {k:[] for k in ('new_leads','promises','overdue_actions','supplier_chases','waiting_on_customer','missing_fields','promise_suggestions')}
        for lead in self.customers.list_leads():
            groups['new_leads'].append(dict(enquiry=lead.get('enquiry_id'), what=lead['subject'],due=lead['received_at'],suggested_action='Review triage: '+lead['from_address'],triage_id=lead['id']))
        for e in self.repo.rows('customer_enquiries'):
            if e['status'] in INACTIVE:
                continue
            def line(what,due,action):
                return dict(enquiry=e['id'],reference=e['reference'],what=what,due=due,suggested_action=action)
            if e['status']=='new' and local_time(e['created_at']) >= now-timedelta(days=1):
                groups['new_leads'].append(line(e['title'],e.get('next_action_due'),'Review new enquiry and qualify'))
            missing = [k for k in ('next_action','next_action_due','owner') if not e.get(k)]
            if missing:
                groups['missing_fields'].append(line('Missing '+', '.join(missing),None,'Assign owner and next action'))
            due = e.get('next_action_due')
            if due and (local_time(due).date() < now.date() if len(due)==10 else local_time(due)<now):
                groups['overdue_actions'].append(line(e['next_action'],due,'Complete or reschedule next action'))
            if e.get('waiting_on')=='customer':
                groups['waiting_on_customer'].append(line(e['title'],due,e.get('next_action') or 'Review next action'))
            for p in self.repo.rows('customer_promises',"enquiry_id=? AND status IN ('open','missed')",(e['id'],)):
                item = line(p['text'],p['due_at'],'Confirm promise suggestion' if not p['confirmed_at'] else 'Keep promise')
                item['promise_id']=p['id']
                if not p['confirmed_at']:
                    groups['promise_suggestions'].append(item)
                elif local_time(p['due_at']).date()<=now.date():
                    if local_time(p['due_at']).date()<now.date() or now.hour>=14:
                        item['suggested_action']='Update customer today'
                    groups['promises'].append(item)
            for campaign in e['rfq_campaign_ids']:
                with connection_scope(self.repo.db_path) as c:
                    messages=c.execute("SELECT * FROM rfq_messages m WHERE campaign_id=? AND status='sent' AND sent_at IS NOT NULL AND replied_at IS NULL AND NOT EXISTS(SELECT 1 FROM rfq_replies r WHERE r.message_id=m.id)",(campaign,)).fetchall()
                with connection_scope(self.repo.db_path) as c:
                    unsent=c.execute("SELECT id,recipient,status,created_at FROM rfq_messages WHERE campaign_id=? AND status IN ('draft','approved') AND sent_at IS NULL",(campaign,)).fetchall()
                for m in unsent:
                    groups['supplier_chases'].append(line('Unsent RFQ '+str(campaign)+' / '+str(m['recipient']),m['created_at'],'Review draft and explicitly approve/send'))
                for m in messages:
                    due = working_due(m['sent_at'])
                    if now>=due:
                        groups['supplier_chases'].append(line('RFQ '+str(campaign)+' / '+str(m['recipient']),due.isoformat(),'Chase supplier (manual approval)'))
        sections=[('new_leads','New leads / triage'),('promises','Promises due today / overdue'),('overdue_actions','Overdue next actions'),('supplier_chases','Supplier chases due'),('waiting_on_customer','Waiting on customer'),('missing_fields','Missing action / owner'),('promise_suggestions','Promise suggestions (confirm first)')]
        def short(value):
            return ' '.join(str(value or '').split())[:220]
        body='\n\n'.join(title+'\n'+('\n'.join(f"{x.get('reference') or x.get('enquiry') or 'New lead'}: {short(x['what'])} | due {x['due'] or 'unset'} | {x['suggested_action']}" for x in groups[key]) or 'None') for key,title in sections)
        return dict(generated_at=now.isoformat(),groups=groups,counts={k:len(v) for k,v in groups.items()},content=body)

    def digest(self, slot):
        now=self.clock().isoformat()
        with connection_scope(self.repo.db_path) as c:
            claimed=c.execute('INSERT OR IGNORE INTO deal_closer_digests(slot,attempted_at) VALUES (?,?)',(slot,now)).rowcount
        if not claimed:
            return {'status':'already attempted','slot':slot}
        try:
            payload={'recipient':os.getenv('DEAL_CLOSER_DIGEST_TO') or 'sai@ikeng.co.uk','subject':'Deal closer '+slot,'body':self.summary()['content']}
            if self.digest_sender is not None:
                self.digest_sender.send(payload)
            else:
                from customers.mailboxes import customer_mailbox
                box=customer_mailbox()
                if box is None or box.address!='sai@ikeng.co.uk':
                    raise ValueError('Customer digest mailbox unavailable')
                box.sender.send(payload)
            with connection_scope(self.repo.db_path) as c:
                c.execute('UPDATE deal_closer_digests SET digest_sent_at=? WHERE slot=?',(now,slot))
            self.state('last_digest_at',now)
            self.state('digest_error','')
            return {'status':'sent','slot':slot}
        except Exception as exc:
            # Ambiguous provider outcomes must never be automatically retried.
            with connection_scope(self.repo.db_path) as c:
                c.execute('UPDATE deal_closer_digests SET error=? WHERE slot=?',(type(exc).__name__,slot))
            self.state('digest_error',type(exc).__name__)
            return {'status':'review required','slot':slot}

    def tick(self):
        errors=[]
        try:
            result=self.customers.poll_inbox(since_days=14,limit=200)
            errors.extend(result.get('mailbox_errors',[]))
        except Exception as exc:
            errors.append(type(exc).__name__)
        try:
            # Recover form cards captured by older deployments too.
            for lead in self.customers.list_leads():
                if lead.get('form_fields'):
                    fields=lead['form_fields']
                    if re.match(r'\s*TEST\b',fields.get('name',''),re.I):
                        self.customers.dismiss_lead(lead['id'])
                    else:
                        self.customers.convert_lead(lead['id'],name=fields.get('name') or fields.get('email') or 'Website enquiry')
            self.suggest()
            summary=self.summary()
            self.state('counts',summary['counts'])
            current=self.clock().astimezone(UK)
            # Catch slots missed during a short restart, without sending yesterday's digest.
            for hour,minute in ((7,30),(16,30)):
                if (current.hour,current.minute)>=(hour,minute):
                    outcome=self.digest(f'{current.date()} {hour:02}:{minute:02}')
                    if outcome['status']=='review required': errors.append('Digest requires review')
        except Exception as exc:
            errors.append(type(exc).__name__)
        self.state('last_tick_at',self.clock().isoformat())
        self.state('errors',errors)
        return self.health()

    def health(self):
        return {k:self.state(k) for k in ('last_tick_at','last_digest_at','counts','errors','digest_error')}


async def run(get_db_path):
    if os.getenv('DEAL_CLOSER_ENABLED','true').lower()!='true':
        return
    while True:
        await asyncio.sleep(max(1,int(os.getenv('DEAL_CLOSER_INTERVAL_MINUTES') or '15'))*60)
        try:
            await asyncio.to_thread(DealCloserService(CustomerService(CustomerRepository(get_db_path()))).tick)
        except Exception as exc:
            logger.warning('deal closer tick failed: %s',type(exc).__name__)
