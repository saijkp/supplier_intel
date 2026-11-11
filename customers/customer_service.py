"""IK Eng customer enquiries: profiles, enquiry tracking, activity log and follow-ups.

Follow-ups follow the same safety model as supplier RFQs: drafted, edited and approved
by a human, then sent through the shared mailbox (Zoho or SMTP) under RFQ_SEND_ENABLED,
with an atomic approved->sending claim and a Sent-based cap of 30 per mailbox per UK day. A customer reply (email
picked up by poll_inbox, or logged by hand) cancels every pending follow-up for that
enquiry. Drafts never include internal fields (next_action, blocker, summary): those are
working notes, not customer-facing text.
"""
import hashlib
import json
import re
import secrets
import string
from threading import Lock
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from urllib.parse import quote

from config import settings as cfg
from storage.customer_repository import CustomerRepository, FREE_MAIL
from customers.mailboxes import Mailbox, customer_mailbox, replied, SEND_LOCK
from customers.form_parser import parse_form, form_qualification, parse_netlify_form, form_fingerprint

_FORM_INTAKE_LOCK = Lock()

CLOSED_STATUSES = {'won', 'lost', 'closed'}
NOT_RESPONDING = 'not responding'
OPEN_STATUSES = ['new', 'qualifying', 'quoting', 'quote sent', 'negotiating']
HOLDING_STATUSES = ['paused', 'nurture', NOT_RESPONDING]
SUGGESTED_STATUSES = OPEN_STATUSES + HOLDING_STATUSES + ['won', 'lost', 'closed']
QUALIFICATION_KEYS = ['scope', 'location', 'budget', 'timeline', 'plans', 'decision_maker']
REQUIRED_QUALIFICATION = ['scope', 'location', 'timeline']
CLOSE_REASONS = ['price', 'timing', 'went elsewhere', 'no project', 'no response', 'not a customer', 'duplicate', 'other']
QUOTE_STATUSES = {'draft', 'sent', 'accepted', 'rejected', 'superseded'}
# After this many of our contacts with no reply, an enquiry moves to 'not responding'
# and comes back up for a check after NO_REPLY_RECHECK_DAYS. Any reply restores it.
NO_REPLY_LIMIT = 3
NO_REPLY_RECHECK_DAYS = 30
INBOUND = {'email_in', 'whatsapp_in', 'call_in'}
OUTBOUND = {'email_out', 'whatsapp_out', 'call_out', 'meeting'}
CHASES = {'email_out', 'whatsapp_out', 'call_out'}  # contact attempts that count towards NO_REPLY_LIMIT
NEWSLETTER_SENDER_RE = re.compile(r'^(no-?reply|do-?not-?reply|notifications?|newsletter|marketing|mailer|'
                                  r'news|updates?|alerts?|billing|invoices?|support|hello@mail)\b', re.I)
ACTIVITY_KINDS = INBOUND | OUTBOUND | {'note', 'status_change'}
CUSTOMER_FIELDS = {'name', 'company', 'email', 'phone', 'whatsapp', 'location', 'source', 'notes'}
ENQUIRY_FIELDS = {'title', 'requirements', 'status', 'next_action', 'next_action_due', 'blocker', 'estimated_value',
                  'waiting_on', 'qualification', 'owner', 'summary', 'rfq_campaign_ids', 'last_customer_contact_at', 'last_our_contact_at'}
EMAIL_RE = re.compile(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+')
REFERENCE_RE = re.compile(r'\bIKE-[A-Z0-9]{6}\b')


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _mail_time(mail):
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(str(mail.get('Date'))).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return now()


def new_reference():
    return 'IKE-' + ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))


def _clean(values, allowed):
    unknown = set(values) - allowed
    if unknown:
        raise ValueError('Unknown fields: ' + ', '.join(sorted(unknown)))
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in values.items()}


class CustomerService:

    def __init__(self, repo=None, sender=None, inbox=None, readiness=None, mailboxes=None):
        self.repo = repo if repo is not None else CustomerRepository()
        self._sender, self._inbox, self._readiness = sender, inbox, readiness
        self._mailboxes = mailboxes

    # -- mail plumbing (lazy: nothing is constructed until a send or poll) ------
    def _mail(self):
        if self._sender is None or self._inbox is None:
            if cfg.RFQ_MAIL_TRANSPORT == 'zoho':
                from rfq.zoho_mail import ZohoClient, ZohoSender, ZohoInbox
                client = ZohoClient()
                self._sender = self._sender or ZohoSender(client)
                self._inbox = self._inbox or ZohoInbox(client)
            else:
                from rfq.channels import SmtpSender, ImapInbox
                self._sender = self._sender or SmtpSender()
                self._inbox = self._inbox or ImapInbox()
        return self._sender, self._inbox

    def mailboxes(self):
        if self._mailboxes is not None:
            return self._mailboxes
        sender, inbox = self._mail()
        primary = (cfg.RFQ_FROM_ADDRESS or '').strip().lower()
        boxes = {primary: Mailbox(primary, sender, inbox)}
        second = customer_mailbox()
        if second:
            boxes[second.address] = second
        return boxes

    def _message_mailbox(self, message):
        address = message.get('mailbox') or self.repo.get('customer_enquiries', message['enquiry_id']).get('mailbox') or cfg.RFQ_FROM_ADDRESS
        box = self.mailboxes().get(address.strip().lower())
        if box is None:
            raise ValueError('Original mailbox is not configured; sending is blocked')
        return box

    def status(self):
        if self._readiness is not None:
            missing = self._readiness()
        else:
            from rfq.rfq_service import RFQService
            from storage.rfq_repository import RFQRepository
            missing = RFQService(repo=RFQRepository(self.repo.db_path)).status()['missing']
        return {'send_enabled': cfg.RFQ_SEND_ENABLED, 'ready': cfg.RFQ_SEND_ENABLED and not missing,
                'missing': missing, 'remaining_cap': max(0, cfg.CUSTOMER_DAILY_SEND_LIMIT - self.repo.daily_count()),
                'mailbox_daily_cap': 30, 'statuses': SUGGESTED_STATUSES}

    # -- customers ------------------------------------------------------------
    def create_customer(self, **values):
        values = _clean(values, CUSTOMER_FIELDS)
        if not values.get('name'):
            raise ValueError('Customer name is required')
        if values.get('email') and not EMAIL_RE.fullmatch(values['email']):
            raise ValueError('Email address is not valid')
        return self.repo.get('customers', self.repo.insert('customers', **values))

    def update_customer(self, customer_id, **values):
        values = _clean(values, CUSTOMER_FIELDS)
        if 'name' in values and not values['name']:
            raise ValueError('Customer name cannot be blank')
        if values.get('email') and not EMAIL_RE.fullmatch(values['email']):
            raise ValueError('Email address is not valid')
        self.repo.get('customers', customer_id)
        if values:
            self.repo.update('customers', customer_id, **values)
        return self.get_customer(customer_id)

    def list_customers(self, q=None):
        if q:
            like = '%' + q.strip().lower() + '%'
            customers = self.repo.rows('customers', 'lower(name) LIKE ? OR lower(coalesce(company,\'\')) LIKE ? OR '
                                       'lower(coalesce(email,\'\')) LIKE ? OR coalesce(phone,\'\') LIKE ?',
                                       (like, like, like, like), order='name')
        else:
            customers = self.repo.rows('customers', order='name')
        enquiries = self.repo.rows('customer_enquiries')
        for c in customers:
            mine = [e for e in enquiries if e['customer_id'] == c['id']]
            c['enquiry_count'] = len(mine)
            c['open_enquiries'] = sum(1 for e in mine if (e['status'] or '').lower() not in CLOSED_STATUSES)
        return customers

    def get_customer(self, customer_id):
        customer = self.repo.get('customers', customer_id)
        customer['enquiries'] = self.repo.rows('customer_enquiries', 'customer_id=?', (customer_id,),
                                               order='updated_at DESC')
        customer['enquiries'] = [self._decorate(e) for e in customer['enquiries']]
        return customer

    # -- enquiries ------------------------------------------------------------
    def create_enquiry(self, customer_id, reference=None, source_ref=None, **values):
        values = _clean(values, ENQUIRY_FIELDS)
        self.repo.get('customers', customer_id)
        if not values.get('title'):
            raise ValueError('Enquiry title is required')
        if reference and not REFERENCE_RE.fullmatch(reference):
            raise ValueError('Reference must look like IKE-ABC123')
        values = self._stage_values({'status': 'new', **values})
        enquiry_id = self.repo.insert('customer_enquiries', customer_id=customer_id,
                                      reference=reference or new_reference(), source_ref=source_ref,
                                      **{'status': 'new', **values})
        self.log(enquiry_id, 'note', 'Enquiry created')
        return self.get_enquiry(enquiry_id)

    def update_enquiry(self, enquiry_id, **values):
        values = _clean(values, ENQUIRY_FIELDS)
        enquiry = self.repo.get('customer_enquiries', enquiry_id)
        values = self._stage_values(values, enquiry)
        if 'title' in values and not values['title']:
            raise ValueError('Enquiry title cannot be blank')
        if values:
            self.repo.update('customer_enquiries', enquiry_id, **values)
        if 'status' in values and (values['status'] or '') != (enquiry['status'] or ''):
            self.log(enquiry_id, 'status_change', f"Status: {enquiry['status']} -> {values['status']}")
        return self.get_enquiry(enquiry_id)

    def act(self, enquiry_id, action, due=None):
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo('Europe/London')).date()
        enquiry = self.repo.get('customer_enquiries', enquiry_id)
        if enquiry['status'] in CLOSED_STATUSES:
            raise ValueError('Enquiry is closed')
        if action == 'snooze':
            try:
                chosen = datetime.strptime(due or '', '%Y-%m-%d').date()
            except ValueError:
                raise ValueError('Choose a valid date') from None
            if chosen <= today:
                raise ValueError('Choose a future date')
            self.update_enquiry(enquiry_id, next_action_due=chosen.isoformat())
            self.repo.update('customer_enquiries', enquiry_id, next_action_snoozed_until=chosen.isoformat())
        elif action in {'done', 'waiting'}:
            # Three unanswered chases already schedule a 30-day recheck. Keep it.
            if enquiry['status'] != NOT_RESPONDING:
                self.update_enquiry(enquiry_id, waiting_on='customer', next_action_due=(today + timedelta(days=3)).isoformat())
            self.repo.update('customer_enquiries', enquiry_id, next_action_snoozed_until=None)
        else:
            raise ValueError('Unknown action')
        self.log(enquiry_id, 'note', {'done':'Next action marked done', 'snooze':'Next action snoozed', 'waiting':'Waiting on customer'}[action])
        return self.get_enquiry(enquiry_id)

    def get_enquiry(self, enquiry_id):
        enquiry = self.repo.get('customer_enquiries', enquiry_id)
        enquiry = self._decorate(enquiry)
        enquiry['quotes'] = self.repo.rows('customer_quotes', 'enquiry_id=?', (enquiry_id,), order='version DESC')
        enquiry['customer'] = self.repo.get('customers', enquiry['customer_id'])
        enquiry['activities'] = self.repo.rows('customer_activities', 'enquiry_id=?', (enquiry_id,),
                                               order='occurred_at DESC, id DESC')
        enquiry['messages'] = self.repo.rows('customer_messages', 'enquiry_id=?', (enquiry_id,), order='id DESC')
        return enquiry

    def list_enquiries(self, status=None, due_only=False, include_closed=False):
        enquiries = self.repo.rows('customer_enquiries', order="coalesce(next_action_due,'9999') , updated_at DESC")
        customers = {c['id']: c for c in self.repo.rows('customers')}
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        activities = {}
        for a in self.repo.rows('customer_activities', order='occurred_at, id'):
            activities.setdefault(a['enquiry_id'], []).append(a)
        result = []
        for e in enquiries:
            e['unanswered_contacts'] = self.unanswered_contacts(e['id'], activities.get(e['id'], []))
            closed = (e['status'] or '').lower() in CLOSED_STATUSES
            if status and (e['status'] or '').lower() != status.lower():
                continue
            if closed and not include_closed and not status:
                continue
            due = (e['next_action_due'] or '')[:10]
            e['overdue'] = bool(due and due <= today and not closed)
            if due_only and not e['overdue']:
                continue
            c = customers.get(e['customer_id'], {})
            e['customer_name'] = c.get('name')
            e['customer_email'] = c.get('email')
            e['customer_phone'] = c.get('phone') or c.get('whatsapp')
            result.append(self._decorate(e))
        return result

    @staticmethod
    def _decorate(e):
        e['qualification_missing'] = [k for k in QUALIFICATION_KEYS if not (e['qualification'].get(k) or '').strip()]
        return e

    def _stage_values(self, values, enquiry=None, reopening=False):
        enquiry = enquiry or {}
        stage = values.get('status', enquiry.get('status', 'new'))
        if stage not in SUGGESTED_STATUSES:
            raise ValueError('Invalid stage; choose: ' + ', '.join(SUGGESTED_STATUSES))
        if 'status' in values and stage in CLOSED_STATUSES:
            raise ValueError('Use /customers/enquiries/{id}/close to close an enquiry')
        if enquiry.get('status') in CLOSED_STATUSES and stage != enquiry['status'] and not reopening:
            raise ValueError('Use /customers/enquiries/{id}/reopen to reopen an enquiry')
        if 'waiting_on' in values and values['waiting_on'] not in (None, 'us', 'customer', 'supplier'):
            raise ValueError('waiting_on must be us, customer or supplier')
        if 'qualification' in values:
            q = values['qualification']
            if not isinstance(q, dict) or set(q) - set(QUALIFICATION_KEYS) or any(not isinstance(v, str) for v in q.values()):
                raise ValueError('qualification must contain only checklist keys with text values')
            values['qualification'] = {**enquiry.get('qualification', {}), **{k: v.strip() for k, v in q.items()}}
        entering = stage != enquiry.get('status') or reopening
        if entering and stage == NOT_RESPONDING:
            values['resume_status'] = enquiry.get('status') or 'qualifying'
        # Any route into quoting (from new, qualifying, a holding stage, a reopen or a create)
        # needs the required checklist; only stepping back from quote sent / negotiating is exempt.
        if entering and stage == 'quoting' and enquiry.get('status') not in ('quote sent', 'negotiating'):
            q = values.get('qualification', enquiry.get('qualification', {}))
            missing = [k for k in REQUIRED_QUALIFICATION if not q.get(k)]
            if missing:
                raise ValueError('Qualification missing: ' + ', '.join(missing))
        if entering and stage == 'quote sent' and not self.repo.rows('customer_quotes', "enquiry_id=? AND status='sent'", (enquiry.get('id', -1),)):
            raise ValueError('quote sent requires at least one sent quote')
        if stage in CLOSED_STATUSES:
            values['next_action_due'] = None
        else:
            if entering and 'next_action_due' not in values:
                values['next_action_due'] = (datetime.now(timezone.utc) + timedelta(days=cfg.CUSTOMER_STAGE_SLA_DAYS[stage])).strftime('%Y-%m-%d')
            due = values.get('next_action_due', enquiry.get('next_action_due'))
            if not due:
                raise ValueError('Open or holding enquiries require next_action_due; it cannot be blank')
            try:
                datetime.strptime(due, '%Y-%m-%d')
            except (ValueError, TypeError):
                raise ValueError('next_action_due must be YYYY-MM-DD') from None
            if 'next_action' in values and not values['next_action']:
                raise ValueError('next_action cannot be blank')
            if not values.get('next_action', enquiry.get('next_action')):
                values['next_action'] = 'Review enquiry and agree next step'
        return values

    def close_enquiry(self, enquiry_id, outcome, close_reason=None, close_note=None, won_value=None):
        e = self.repo.get('customer_enquiries', enquiry_id)
        if e['status'] in CLOSED_STATUSES:
            raise ValueError('Enquiry is already closed; reopen first')
        if outcome not in CLOSED_STATUSES:
            raise ValueError('outcome must be won, lost or closed')
        close_note = (close_note or '').strip()
        if outcome == 'won':
            import math
            if isinstance(won_value, bool) or not isinstance(won_value, (int, float)) or not math.isfinite(won_value):
                raise ValueError('won requires a numeric won_value')
            if not close_note:
                raise ValueError('won requires close_note proving the order or deposit')
            close_reason = None
        else:
            if close_reason not in CLOSE_REASONS:
                raise ValueError('close_reason must be one of: ' + ', '.join(CLOSE_REASONS))
            if close_reason == 'other' and not close_note:
                raise ValueError('other requires close_note')
            won_value = None
        self.repo.update('customer_enquiries', enquiry_id, status=outcome, close_reason=close_reason,
                         close_note=close_note, won_value=won_value, closed_at=now(), next_action_due=None)
        self.log(enquiry_id, 'status_change', f"Closed: {outcome} ({close_reason or close_note})")
        return self.get_enquiry(enquiry_id)

    def reopen_enquiry(self, enquiry_id, status='qualifying'):
        e = self.repo.get('customer_enquiries', enquiry_id)
        if e['status'] not in CLOSED_STATUSES or status not in OPEN_STATUSES:
            raise ValueError('Reopen requires a closed enquiry and an open stage')
        values = self._stage_values({'status': status}, e, reopening=True)
        self.repo.update('customer_enquiries', enquiry_id, **values, closed_at=None, close_reason=None,
                         close_note=None, won_value=None, waiting_on='us', resume_status=None)
        self.log(enquiry_id, 'status_change', f"Status: {e['status']} -> {status} (reopened)")
        return self.get_enquiry(enquiry_id)

    def add_quote(self, enquiry_id, **values):
        self.repo.get('customer_enquiries', enquiry_id)
        return self._save_quote(enquiry_id, None, values)

    def update_quote(self, enquiry_id, quote_id, **values):
        q = self.repo.get('customer_quotes', quote_id)
        if q['enquiry_id'] != enquiry_id:
            raise ValueError('Quote not found for this enquiry')
        return self._save_quote(enquiry_id, q, values)

    def _save_quote(self, enquiry_id, existing, values):
        import math
        from storage.database import connection_scope
        values = _clean(values, {'amount', 'currency', 'summary', 'sent_at', 'valid_until', 'status'})
        status = values.get('status', existing['status'] if existing else 'draft')
        if status not in QUOTE_STATUSES:
            raise ValueError('Invalid quote status')
        if 'amount' in values and (isinstance(values['amount'], bool) or not isinstance(values['amount'], (float, int)) or not math.isfinite(values['amount'])):
            raise ValueError('Quote amount must be a finite number')
        for field in ('valid_until',):
            if values.get(field):
                try:
                    datetime.strptime(values[field], '%Y-%m-%d')
                except (ValueError, TypeError):
                    raise ValueError(field + ' must be YYYY-MM-DD') from None
        became_sent = status == 'sent' and (not existing or existing['status'] != 'sent')
        if became_sent:
            values['sent_at'] = values.get('sent_at') or now()
        # Serialise version allocation and superseding so simultaneous edits cannot create two sent quotes.
        with connection_scope(self.repo.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if existing:
                qid = existing['id']
                if values:
                    values['updated_at'] = now()
                    conn.execute('UPDATE customer_quotes SET ' + ','.join(k+'=?' for k in values) + ' WHERE id=?', (*values.values(), qid))
            else:
                version = conn.execute('SELECT coalesce(max(version),0)+1 FROM customer_quotes WHERE enquiry_id=?', (enquiry_id,)).fetchone()[0]
                values = {'enquiry_id': enquiry_id, 'version': version, **values}
                qid = conn.execute('INSERT INTO customer_quotes ('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')', tuple(values.values())).lastrowid
            if became_sent:
                conn.execute("UPDATE customer_quotes SET status='superseded',updated_at=? WHERE enquiry_id=? AND id!=? AND status='sent'", (now(), enquiry_id, qid))
        q = self.repo.get('customer_quotes', qid)
        if became_sent:
            self.log(enquiry_id, 'note', f"Quote v{q['version']} sent: {q['currency']} {q['amount'] if q['amount'] is not None else ''}")
            e = self.repo.get('customer_enquiries', enquiry_id)
            if e['status'] in ('new', 'qualifying', 'quoting'):
                self.update_enquiry(enquiry_id, status='quote sent', waiting_on='customer')
        return q

    def today(self):
        groups = {k: [] for k in ('overdue', 'due_today', 'quotes_expiring', 'ready_to_close', 'qualification_gaps')}
        today = now()[:10]
        soon = (datetime.strptime(today, '%Y-%m-%d') + timedelta(days=7)).strftime('%Y-%m-%d')
        stale = (datetime.strptime(today, '%Y-%m-%d') - timedelta(days=90)).strftime('%Y-%m-%d')
        for e in self.list_enquiries():
            item = {k: e.get(k) for k in ('id', 'reference', 'waiting_on', 'next_action')}
            item.update(customer=e['customer_name'], stage=e['status'], due=e['next_action_due'])
            def add(group, why, **extra):
                groups[group].append({**item, 'why': why, **extra})
            due = (e['next_action_due'] or '')[:10]
            if due and due < today:
                add('overdue', 'Next action is overdue')
            elif due == today:
                add('due_today', 'Next action is due today')
            quotes = self.repo.rows('customer_quotes', 'enquiry_id=?', (e['id'],))
            for q in quotes:
                if q['status'] == 'sent' and q['valid_until'] and today <= q['valid_until'][:10] <= soon:
                    add('quotes_expiring', f"Quote v{q['version']} expires {q['valid_until']}", quote_id=q['id'])
            if any(q['status'] == 'accepted' for q in quotes):
                add('ready_to_close', 'Accepted quote: consider won; confirm order or deposit', suggested_outcome='won')
            elif e['status'] == NOT_RESPONDING and due and due < today:
                add('ready_to_close', 'No response past recheck date: consider lost / no response', suggested_outcome='lost', close_reason='no response')
            elif e['status'] in ('paused', 'nurture'):
                inbound = self.repo.rows('customer_activities', "enquiry_id=? AND kind IN ('email_in','whatsapp_in','call_in')", (e['id'],), order='occurred_at DESC')
                last = inbound[0]['occurred_at'] if inbound else e['created_at']
                if (last or '')[:10] <= stale:
                    add('ready_to_close', 'No inbound activity in 90 days: consider closed / timing', suggested_outcome='closed', close_reason='timing')
            if e['status'] == 'qualifying':
                missing = [k for k in REQUIRED_QUALIFICATION if k in e['qualification_missing']]
                if missing:
                    add('qualification_gaps', 'Qualification missing: ' + ', '.join(missing))
        return groups

    def pipeline(self):
        stages = {s: {'count': 0, 'estimated_value': 0.0} for s in SUGGESTED_STATUSES}
        outcomes = {s: {'count': 0, 'close_reasons': {}} for s in ('won', 'lost', 'closed')}
        for e in self.repo.rows('customer_enquiries'):
            bucket = stages[e['status']]
            bucket['count'] += 1
            try:
                import math
                value = float(e['estimated_value'] or 0)
                if math.isfinite(value):
                    bucket['estimated_value'] += value
            except (TypeError, ValueError):
                pass
            if e['status'] in outcomes:
                o = outcomes[e['status']]
                o['count'] += 1
                reason = e['close_reason'] or 'unspecified'
                o['close_reasons'][reason] = o['close_reasons'].get(reason, 0) + 1
        return {'stages': stages, 'outcomes': outcomes}

    def link_campaign(self, enquiry_id, campaign_id):
        enquiry = self.repo.get('customer_enquiries', enquiry_id)
        ids = sorted(set(enquiry['rfq_campaign_ids']) | {int(campaign_id)})
        self.repo.update('customer_enquiries', enquiry_id, rfq_campaign_ids=ids)
        self.log(enquiry_id, 'note', f'Linked supplier RFQ campaign {campaign_id}')
        return self.get_enquiry(enquiry_id)

    # -- activity log ---------------------------------------------------------
    def log(self, enquiry_id, kind, summary, occurred_at=None, external_id=None):
        if kind not in ACTIVITY_KINDS:
            raise ValueError('Activity kind must be one of: ' + ', '.join(sorted(ACTIVITY_KINDS)))
        summary = (summary or '').strip()
        if not summary:
            raise ValueError('Activity summary is required')
        enquiry = self.repo.get('customer_enquiries', enquiry_id)
        when = occurred_at or now()
        activity_id = self.repo.insert('customer_activities', enquiry_id=enquiry_id, kind=kind, summary=summary,
                                       occurred_at=when, external_id=external_id)
        if kind in INBOUND:
            self.repo.update('customer_enquiries', enquiry_id, last_customer_contact_at=when, next_action_snoozed_until=None,
                             **({'waiting_on':'us', 'next_action_due':now()[:10]} if enquiry['status'] not in CLOSED_STATUSES else {}))
            self._cancel_pending(enquiry_id, 'Customer replied')
            enquiry = self.repo.get('customer_enquiries', enquiry_id)
            if (enquiry['status'] or '').lower() == NOT_RESPONDING:
                self.repo.update('customer_enquiries', enquiry_id, status=enquiry.get('resume_status') or 'qualifying', resume_status=None, waiting_on='us', next_action_due=now()[:10])
                self.log(enquiry_id, 'status_change', f"Status: {NOT_RESPONDING} -> {enquiry.get('resume_status') or 'qualifying'} (customer replied)")
        elif kind in OUTBOUND:
            self.repo.update('customer_enquiries', enquiry_id, last_our_contact_at=when)
            self._check_not_responding(enquiry_id, when)
        return self.repo.get('customer_activities', activity_id)

    def unanswered_contacts(self, enquiry_id, activities=None):
        """Our chases (email/WhatsApp/call) since the customer last got in touch."""
        if activities is None:
            activities = self.repo.rows('customer_activities', 'enquiry_id=?', (enquiry_id,), order='occurred_at, id')
        count = 0
        for a in activities:
            if a['kind'] in INBOUND:
                count = 0
            elif a['kind'] in CHASES:
                count += 1
        return count

    def _check_not_responding(self, enquiry_id, when):
        enquiry = self.repo.get('customer_enquiries', enquiry_id)
        status = (enquiry['status'] or '').lower()
        if status in CLOSED_STATUSES or status == NOT_RESPONDING:
            return
        if self.unanswered_contacts(enquiry_id) < NO_REPLY_LIMIT:
            return
        recheck = (datetime.strptime(when[:10], '%Y-%m-%d') + timedelta(days=NO_REPLY_RECHECK_DAYS)).strftime('%Y-%m-%d')
        self.repo.update('customer_enquiries', enquiry_id, status=NOT_RESPONDING, resume_status=status, waiting_on='customer', next_action_due=recheck)
        self.log(enquiry_id, 'status_change',
                 f"Status: {enquiry['status']} -> {NOT_RESPONDING} (no reply after {NO_REPLY_LIMIT} contacts); "
                 f"check again on {recheck}")

    def _cancel_pending(self, enquiry_id, reason):
        for m in self.repo.rows('customer_messages', "enquiry_id=? AND status IN ('draft','approved')", (enquiry_id,)):
            self.repo.transition(m['id'], m['status'], 'cancelled', status_reason=reason)

    # -- follow-ups -------------------------------------------------------------
    def draft_followup(self, enquiry_id, channel='email', body=None, subject=None):
        enquiry = self.repo.get('customer_enquiries', enquiry_id)
        customer = self.repo.get('customers', enquiry['customer_id'])
        if (enquiry['status'] or '').lower() in CLOSED_STATUSES:
            raise ValueError('Enquiry is closed')
        if channel == 'email':
            recipient = (customer.get('email') or '').strip()
            if not EMAIL_RE.fullmatch(recipient):
                raise ValueError('Customer has no valid email address')
        elif channel == 'whatsapp':
            recipient = re.sub(r'\D', '', customer.get('whatsapp') or customer.get('phone') or '')
            if len(recipient) < 8:
                raise ValueError('Customer has no WhatsApp or phone number')
        else:
            raise ValueError('Channel must be email or whatsapp')
        first = (customer['name'] or '').split()[0] if customer.get('name') else ''
        ref = enquiry['reference']
        signature = '\n'.join(x for x in (cfg.RFQ_BUYER_NAME, cfg.RFQ_BUYER_COMPANY) if x)
        if body is None:
            body = (f"Hi {first},\n\n" if first else "Hello,\n\n") + (
                f"I'm following up on your {enquiry['title']} enquiry. Is there anything else you need from us "
                "to move this forward? I'm happy to arrange a call at a time that suits you.\n\n"
                f"Kind regards,\n{signature}")
        body = self._with_ref(body, ref, channel)
        subject = subject or f"Your {enquiry['title']} enquiry [{ref}]"
        if channel == 'email' and ref not in subject:
            subject += f' [{ref}]'
        action_url = 'https://wa.me/' + recipient + '?text=' + quote(body) if channel == 'whatsapp' else None
        message_id = self.repo.insert('customer_messages', enquiry_id=enquiry_id, mailbox=enquiry.get('mailbox') or cfg.RFQ_FROM_ADDRESS, channel=channel, recipient=recipient,
                                      subject=subject if channel == 'email' else None, body=body, action_url=action_url)
        return self.repo.get('customer_messages', message_id)

    @staticmethod
    def _with_ref(body, ref, channel):
        # Email bodies carry the enquiry reference; the subject tag is what inbox matching uses.
        # WhatsApp chats are already tied to the customer's number, so a ref line there only reads as robotic.
        if channel != 'email' or ref in body:
            return body
        return body.rstrip() + f'\nRef: {ref}'

    def edit_message(self, message_id, subject=None, body=None):
        message = self.repo.get('customer_messages', message_id)
        if message['status'] != 'draft':
            raise ValueError('Only drafts can be edited')
        ref = self.repo.get('customer_enquiries', message['enquiry_id'])['reference']
        values = {}
        if body is not None:
            values['body'] = self._with_ref(body, ref, message['channel'])
            if message['channel'] == 'whatsapp':
                values['action_url'] = 'https://wa.me/' + message['recipient'] + '?text=' + quote(values['body'])
        if subject is not None and message['channel'] == 'email':
            values['subject'] = subject if ref in subject else subject + f' [{ref}]'
        if values:
            self.repo.update('customer_messages', message_id, **values)
        return self.repo.get('customer_messages', message_id)

    def approve(self, message_id):
        if not self.repo.transition(message_id, 'draft', 'approved', approved_at=now()):
            raise ValueError('Only drafts can be approved')
        return self.repo.get('customer_messages', message_id)

    def cancel(self, message_id):
        message = self.repo.get('customer_messages', message_id)
        if message['status'] not in ('draft', 'approved') or \
                not self.repo.transition(message_id, message['status'], 'cancelled', status_reason='Cancelled by user'):
            raise ValueError('Only drafts or approved messages can be cancelled')
        return self.repo.get('customer_messages', message_id)

    def mark_sent(self, message_id):
        """WhatsApp is human-sent: open the link, send it, then mark it here."""
        message = self.repo.get('customer_messages', message_id)
        if message['channel'] != 'whatsapp':
            raise ValueError('Only WhatsApp messages are marked sent by hand')
        if message['status'] not in ('draft', 'approved') or \
                not self.repo.transition(message_id, message['status'], 'sent', sent_at=now()):
            raise ValueError('Message is not pending')
        self.log(message['enquiry_id'], 'whatsapp_out', 'WhatsApp follow-up sent: ' + message['body'][:500])
        return self.repo.get('customer_messages', message_id)

    def send(self, message_id):
        with SEND_LOCK:
            return self._send(message_id)

    def _send(self, message_id):
        """Send one approved email. The customer must not have replied since it was drafted."""
        message = self.repo.get('customer_messages', message_id)
        if message['channel'] != 'email':
            raise ValueError('WhatsApp messages are sent by hand, then marked sent')
        if message['status'] != 'approved':
            raise ValueError('Only approved messages can be sent')
        box = self._message_mailbox(message)
        # The second account is independent of the primary account's readiness.
        ready = self.status() if box.address == cfg.RFQ_FROM_ADDRESS.strip().lower() else {'ready': cfg.RFQ_SEND_ENABLED, 'missing': []}
        if not ready['ready']:
            raise ValueError('Sending is not ready: ' + ('RFQ_SEND_ENABLED is false' if not cfg.RFQ_SEND_ENABLED
                                                         else 'missing ' + ', '.join(ready['missing'])))
        enquiry = self.repo.get('customer_enquiries', message['enquiry_id'])
        if (enquiry['last_customer_contact_at'] or '') > (message['created_at'] or ''):
            self.repo.transition(message_id, 'approved', 'cancelled', status_reason='Customer replied after drafting')
            raise ValueError('Customer replied after this was drafted; it has been cancelled')
        if not EMAIL_RE.fullmatch(message['recipient'] or ''):
            self.repo.transition(message_id, 'approved', 'skipped', status_reason='Invalid recipient')
            raise ValueError('Recipient is not a single valid email address')
        # Production Zoho (and injected mailbox registries) always check real Sent.
        checked = self._mailboxes is not None or cfg.RFQ_MAIL_TRANSPORT == 'zoho' or box.address == cfg.CUSTOMER_ZOHO_ADDRESS.lower()
        if checked:
            try:
                sent = box.sent(2)
                box.check_send(message, sent=sent)
            except ValueError:
                raise
            except Exception:
                raise ValueError('Cannot verify Sent folder; sending is blocked') from None
            # Also cover accepted sends not yet visible in the provider's Sent index.
            self.repo.check_mailbox_ledger(message, box.address, sent)
        if not self.repo.claim_send(message_id, None if checked else cfg.CUSTOMER_DAILY_SEND_LIMIT, mailbox=box.address):
            raise ValueError('Daily customer send cap reached, or the message is already being sent')
        sender = box.sender
        try:
            provider_id = sender.send({'recipient': message['recipient'], 'subject': message['subject'],
                                       'body': message['body']})
        except Exception:
            # Ambiguous outcome: never retried automatically.
            self.repo.transition(message_id, 'sending', 'failed',
                                 status_reason='Send failed; check the mailbox before any retry')
            raise ValueError('Send failed; check the Sent folder before trying again') from None
        self.repo.transition(message_id, 'sending', 'sent', sent_at=now(), provider_message_id=provider_id)
        self.log(message['enquiry_id'], 'email_out', f"Email sent: {message['subject']}")
        return self.repo.get('customer_messages', message_id)

    # -- inbox ----------------------------------------------------------------
    def poll_inbox(self, since_days=14, limit=200):
        total = {'logged': 0, 'possible_new_enquiries': 0, 'ignored': 0, 'duplicates': 0}
        errors = []
        for address, box in self.mailboxes().items():
            try:
                counts = self._poll_mailbox(box, since_days, limit)
                for key in total:
                    total[key] += counts[key]
            except Exception:
                # Never expose provider errors (may contain credentials); continue other account.
                errors.append(address)
        if errors:
            total['mailbox_errors'] = errors
        return total

    def _poll_mailbox(self, box, since_days=14, limit=200):
        """Read recent inbound mail once and sort every message:
        - customer email (IKE- reference, or sender is a customer's address) -> logged on the
          enquiry; a reply stops pending follow-ups and lifts 'not responding';
        - supplier mail (RFQ- reference, known supplier address/domain), our own address,
          auto-replies, bounces and newsletters -> ignored;
        - anything else -> a 'possible new enquiry' lead for a human to add or dismiss, so a
          first email from a new customer is never silently dropped."""
        from rfq.quote_parser import strip_history
        result = {'logged': 0, 'possible_new_enquiries': 0, 'ignored': 0, 'duplicates': 0}
        customers = {c['id']: c for c in self.repo.rows('customers')}
        enquiries = self.repo.rows('customer_enquiries', order='updated_at DESC')
        by_ref = {e['reference']: e for e in enquiries}
        by_email = {}
        for e in enquiries:
            email = (customers.get(e['customer_id'], {}).get('email') or '').strip().lower()
            if email and (e['status'] or '').lower() not in CLOSED_STATUSES:
                by_email.setdefault(email, e)
        known_customer_emails = {(c.get('email') or '').strip().lower() for c in customers.values()} - {''}
        # A sender dismissed once (forwarder, partner, own other mailbox) is not listed again.
        dismissed = {l['from_address'] for l in self.repo.rows('customer_inbox_leads', "status='dismissed'")}
        pending = {l['from_address'] for l in self.repo.rows('customer_inbox_leads', "status='new' AND coalesce(mailbox,?)=?", (cfg.RFQ_FROM_ADDRESS, box.address))}
        own =(cfg.RFQ_FROM_ADDRESS or '').strip().lower()
        inbox = box.inbox
        try:
            sent = box.sent(since_days)
            checked_at = now()
        except Exception:
            sent, checked_at = [], None
        # Refresh existing cards, including previously answered supplier emails.
        for lead in self.repo.rows('customer_inbox_leads', "status='new' AND coalesce(mailbox,?)=?", (cfg.RFQ_FROM_ADDRESS, box.address)):
            self.repo.update('customer_inbox_leads', lead['id'], mailbox=box.address,
                already_replied=int(bool(lead['already_replied']) or replied(sent, lead['from_address'], lead['subject'], lead['received_at'] or now(), lead.get('thread_id'))) if checked_at else lead['already_replied'],
                reply_checked_at=checked_at)
        for raw in inbox.fetch_recent(since_days, limit):
            mail = BytesParser(policy=policy.default).parsebytes(raw)
            # Transport metadata must not change legacy hash dedup for ID-less MIME.
            original = re.sub(br'^X-SupplierIntel-Thread:[^\r\n]*\r?\n', b'', raw, count=1, flags=re.I)
            external = 'cust:' + (str(mail.get('Message-ID') or '').strip() or hashlib.sha256(original).hexdigest())
            if self.repo.rows('customer_activities', 'external_id=?', (external,)) or \
                    self.repo.rows('customer_inbox_leads', 'external_id=?', (external,)):
                result['duplicates'] += 1
                continue
            name, sender = parseaddr(str(mail.get('From', '')))
            sender = sender.lower()
            subject = str(mail.get('Subject', ''))
            auto = str(mail.get('Auto-Submitted', 'no')).strip().lower() != 'no' or mail.get('X-Autoreply') is not None \
                or re.match(r'\s*(out of office|automatic reply|auto.?reply)', subject, re.I) \
                or re.search('mailer-daemon|postmaster', sender, re.I) \
                or mail.get_content_type() == 'multipart/report'
            if not sender:
                result['ignored'] += 1
                continue
            text = ''
            for part in mail.walk():
                if part.get_content_type() == 'text/plain' and not part.get_filename():
                    try:
                        text += str(part.get_content())
                    except Exception:
                        pass
            if not text:
                for part in mail.walk():
                    if part.get_content_type() == 'text/html':
                        try:
                            from bs4 import BeautifulSoup
                            text = BeautifulSoup(part.get_content(), 'html.parser').get_text('\n')
                        except Exception:
                            pass
                        break
            try:
                is_form = (bool(re.search(cfg.CUSTOMER_FORM_SUBJECT_PATTERN, subject))
                           # Netlify puts the marker in the subject; the body starts at the fields.
                           and ' '.join(cfg.CUSTOMER_FORM_BODY_MARKER.split()).casefold() in ' '.join((subject + '\n' + text).split()).casefold()
                           and (not cfg.CUSTOMER_FORM_SENDERS or sender in cfg.CUSTOMER_FORM_SENDERS))
            except re.error:
                is_form = False  # invalid deployment config must not stop ordinary email
            if is_form:
                # Redelivered notifications may have a different MIME Message-ID.
                external = 'cust:form:' + hashlib.sha256(text.strip().encode('utf-8')).hexdigest()
                submission = re.search(r'\[netlify:([a-fA-F0-9]{24})\]', subject)
                if submission:
                    external = 'cust:netlify:' + submission.group(1)
                outcome = self.store_form_submission(parse_form(text), text, external,
                                                     subject, _mail_time(mail))
                leads = self.repo.rows('customer_inbox_leads', 'external_id=?', (external,))
                if leads:
                    self.repo.update('customer_inbox_leads', leads[0]['id'], mailbox=box.address)
                result[outcome] += 1
                continue
            if auto or sender in {own, box.address, cfg.CUSTOMER_ZOHO_ADDRESS.lower()}:
                result['ignored'] += 1
                continue
            excerpt = strip_history(text).strip()[:1500]
            ref = REFERENCE_RE.search(subject + '\n' + text)
            enquiry = by_ref.get(ref.group(0)) if ref else None
            if enquiry is None:
                enquiry = by_email.get(sender)
            if enquiry is not None:
                self.log(enquiry['id'], 'email_in', f"Email from {sender}: {subject}\n{excerpt}".strip(),
                         occurred_at=now(), external_id=external)
                self.repo.update('customer_enquiries', enquiry['id'], mailbox=box.address)
                activities = self.repo.rows('customer_activities', 'external_id=?', (external,))
                if activities:
                    self.repo.update('customer_activities', activities[0]['id'], mailbox=box.address)
                result['logged'] += 1
                continue
            bulk = mail.get('List-Unsubscribe') or mail.get('List-Id') or \
                str(mail.get('Precedence', '')).lower() in {'bulk', 'list', 'junk'} or \
                (sender.rsplit('@', 1)[-1] not in FREE_MAIL and NEWSLETTER_SENDER_RE.match(sender.split('@')[0]))
            if bulk or sender in known_customer_emails or sender in dismissed \
                    or re.search(r'\bRFQ-\d+-[A-Z0-9]{6}\b', subject + text) or self.repo.is_supplier_address(sender):
                result['ignored'] += 1
                continue
            if sender in pending:  # one card per sender; their first email is already waiting
                result['ignored'] += 1
                continue
            pending.add(sender)
            self.repo.insert('customer_inbox_leads', external_id=external, mailbox=box.address,
                             already_replied=int(replied(sent, sender, subject, _mail_time(mail), str(mail.get('X-SupplierIntel-Thread') or ''))) if checked_at else 0,
                             reply_checked_at=checked_at, thread_id=str(mail.get('X-SupplierIntel-Thread') or ''), from_address=sender,
                             from_name=(name or '').strip() or None, subject=subject[:300], excerpt=excerpt,
                             received_at=_mail_time(mail), full_text=text)
            result['possible_new_enquiries'] += 1
        return result

    def store_form_submission(self, fields, text, external_id, subject, received_at=None):
        """Shared email/webhook intake. No sends or automatic enquiry creation.

        Serialize the check/write on this single-instance service. Match IDs first;
        across transports only, compare canonical parsed fields, retaining original
        evidence on the first record. Distinct IDs on the same transport stay distinct.
        """
        with _FORM_INTAKE_LOCK:
            for table in ('customer_activities', 'customer_inbox_leads'):
                if self.repo.rows(table, 'external_id=?', (external_id,)):
                    return 'duplicates'
            other = 'cust:form:%' if external_id.startswith('cust:netlify:') else 'cust:netlify:%'
            fingerprint = form_fingerprint(fields)
            if self.repo.form_seen(external_id, fingerprint, other if fields.get('email') else ''):
                self.repo.remember_form(external_id, fingerprint)
                return 'duplicates'
            if fields.get('email'):
                for table in ('customer_inbox_leads', 'customer_activities'):
                    for row in self.repo.rows(table, 'external_id LIKE ?', (other,)):
                        previous = row.get('form_fields')
                        if not previous:
                            evidence = row.get('full_text') or row.get('summary') or ''
                            if row['external_id'].startswith('cust:netlify:'):
                                try:
                                    previous = parse_netlify_form(json.loads(evidence))
                                except (ValueError, TypeError):
                                    continue
                            else:
                                previous = parse_form(evidence)
                        if form_fingerprint(previous) == fingerprint:
                            self.repo.remember_form(external_id, fingerprint)
                            return 'duplicates'
            email = fields.get('email', '').lower()
            customers = self.repo.rows('customers', 'lower(email)=?', (email,)) if email else []
            for customer in customers:
                enquiries = self.repo.rows('customer_enquiries', 'customer_id=?',
                                           (customer['id'],), order='updated_at DESC')
                enquiry = next((e for e in enquiries if e['status'] not in CLOSED_STATUSES), None)
                if enquiry:
                    self._fill_form(enquiry, fields)
                    self.log(enquiry['id'], 'email_in', text, occurred_at=received_at, external_id=external_id)
                    self.repo.remember_form(external_id, fingerprint)
                    return 'logged'
            self.repo.insert('customer_inbox_leads', external_id=external_id,
                             from_address=email, from_name=fields.get('name') or None,
                             subject=subject[:300], excerpt=text[:1500], full_text=text,
                             form_fields=fields, received_at=received_at or now())
            self.repo.remember_form(external_id, fingerprint)
            return 'possible_new_enquiries'

    # -- possible new enquiries (from the inbox) -----------------------------------
    def list_leads(self, status='new'):
        return self.repo.rows('customer_inbox_leads', 'status=?', (status,), order='id DESC')

    def draft_lead_reply(self, lead_id, body):
        if not body.strip():
            raise ValueError('Reply body is required')
        lead = self.repo.get('customer_inbox_leads', lead_id)
        enquiry = self.convert_lead(lead_id) if lead['status'] == 'new' else self.get_enquiry(lead['enquiry_id'])
        subject = lead.get('subject') or 'Your enquiry'
        if not subject.lower().startswith('re:'):
            subject = 'Re: ' + subject
        message = self.draft_followup(enquiry['id'], body=body, subject=subject)
        # Preserve the reply subject for provider exact-duplicate checks.
        self.repo.update('customer_messages', message['id'], subject=subject,
                         mailbox=lead.get('mailbox') or cfg.RFQ_FROM_ADDRESS)
        return self.repo.get('customer_messages', message['id'])

    def dismiss_lead(self, lead_id):
        lead = self.repo.get('customer_inbox_leads', lead_id)
        if lead['status'] != 'new':
            raise ValueError('Lead already handled')
        self.repo.update('customer_inbox_leads', lead_id, status='dismissed')
        return self.repo.get('customer_inbox_leads', lead_id)

    def convert_lead(self, lead_id, name=None, title=None):
        """Create the customer (email = the sender, which is grounded: they wrote to us) and
        an enquiry, with the email itself as the first timeline entry."""
        lead = self.repo.get('customer_inbox_leads', lead_id)
        if lead['status'] != 'new':
            raise ValueError('Lead already handled')
        fields = lead.get('form_fields') or {}
        if fields and not EMAIL_RE.fullmatch(lead['from_address'] or ''):
            raise ValueError('Form has no valid customer email; review the original submission')
        name = (name or lead['from_name'] or ('' if fields else lead['from_address'].split('@')[0])).strip()
        if not name:
            raise ValueError('Customer name is required to add this submission')
        existing = self.repo.rows('customers', 'lower(email)=?', (lead['from_address'].lower(),))
        customer = existing[0] if existing else self.create_customer(
            name=name, email=lead['from_address'], source='Website form' if fields else 'Email enquiry',
            **{k: fields[k] for k in ('phone', 'company', 'location') if fields.get(k)})
        open_enquiries = self.repo.rows('customer_enquiries', 'customer_id=?', (customer['id'],), order='updated_at DESC')
        enquiry = next((e for e in open_enquiries if e['status'] not in CLOSED_STATUSES), None)
        if enquiry is None:
            enquiry = self.create_enquiry(customer['id'], title=(title or fields.get('project_name') or lead['subject'] or 'Email enquiry').strip()[:300],
                                          status='new', next_action='Reply to the first email and qualify the enquiry',
                                          next_action_due=now()[:10])
        if fields:
            self._fill_form(enquiry, fields)
        self.log(enquiry['id'], 'email_in', lead.get('full_text') or lead['excerpt'] or '',
                 occurred_at=lead['received_at'], external_id=lead['external_id'])
        activities = self.repo.rows('customer_activities', 'external_id=?', (lead['external_id'],))
        if activities:
            self.repo.update('customer_activities', activities[0]['id'], mailbox=lead.get('mailbox') or cfg.RFQ_FROM_ADDRESS)
        self.repo.update('customer_enquiries', enquiry['id'], mailbox=lead.get('mailbox') or cfg.RFQ_FROM_ADDRESS)
        self.repo.update('customer_inbox_leads', lead_id, status='added', enquiry_id=enquiry['id'])
        return self.get_enquiry(enquiry['id'])

    def _fill_form(self, enquiry, fields):
        """Only fill empty values; retain conflicting evidence in the email timeline."""
        enquiry = self.repo.get('customer_enquiries', enquiry['id'])
        customer = self.repo.get('customers', enquiry['customer_id'])
        updates = {k: fields[k] for k in ('name', 'phone', 'company', 'location')
                   if fields.get(k) and not customer.get(k)}
        if updates:
            self.update_customer(customer['id'], **updates)
        qualification = {k: v for k, v in form_qualification(fields).items()
                         if not (enquiry.get('qualification') or {}).get(k)}
        updates = {'qualification': qualification} if qualification else {}
        if not enquiry.get('requirements'):
            updates['requirements'] = '\n'.join(f'{k.replace("_", " ")}: {v}' for k, v in fields.items() if v and k not in {'name', 'email', 'phone', 'company', 'lead_source'})
        if updates:
            self.update_enquiry(enquiry['id'], **updates)
