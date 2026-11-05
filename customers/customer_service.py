"""IK Eng customer enquiries: profiles, enquiry tracking, activity log and follow-ups.

Follow-ups follow the same safety model as supplier RFQs: drafted, edited and approved
by a human, then sent through the shared mailbox (Zoho or SMTP) under RFQ_SEND_ENABLED,
with an atomic approved->sending claim and a rolling 24h cap. A customer reply (email
picked up by poll_inbox, or logged by hand) cancels every pending follow-up for that
enquiry. Drafts never include internal fields (next_action, blocker, summary): those are
working notes, not customer-facing text.
"""
import hashlib
import re
import secrets
import string
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from urllib.parse import quote

from config import settings as cfg
from storage.customer_repository import CustomerRepository

CLOSED_STATUSES = {'won', 'lost', 'closed', 'closed-lost', 'excluded'}
SUGGESTED_STATUSES = ['new', 'qualifying', 'active', 'quoting', 'awaiting customer', 'nurture', 'paused',
                      'won', 'lost', 'closed']
INBOUND = {'email_in', 'whatsapp_in', 'call_in'}
OUTBOUND = {'email_out', 'whatsapp_out', 'call_out', 'meeting'}
ACTIVITY_KINDS = INBOUND | OUTBOUND | {'note', 'status_change'}
CUSTOMER_FIELDS = {'name', 'company', 'email', 'phone', 'whatsapp', 'location', 'source', 'notes'}
ENQUIRY_FIELDS = {'title', 'requirements', 'status', 'next_action', 'next_action_due', 'blocker', 'estimated_value',
                  'owner', 'summary', 'rfq_campaign_ids', 'last_customer_contact_at', 'last_our_contact_at'}
EMAIL_RE = re.compile(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+')
REFERENCE_RE = re.compile(r'\bIKE-[A-Z0-9]{6}\b')


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def new_reference():
    return 'IKE-' + ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))


def _clean(values, allowed):
    unknown = set(values) - allowed
    if unknown:
        raise ValueError('Unknown fields: ' + ', '.join(sorted(unknown)))
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in values.items()}


class CustomerService:

    def __init__(self, repo=None, sender=None, inbox=None, readiness=None):
        self.repo = repo if repo is not None else CustomerRepository()
        self._sender, self._inbox, self._readiness = sender, inbox, readiness

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

    def status(self):
        if self._readiness is not None:
            missing = self._readiness()
        else:
            from rfq.rfq_service import RFQService
            from storage.rfq_repository import RFQRepository
            missing = RFQService(repo=RFQRepository(self.repo.db_path)).status()['missing']
        return {'send_enabled': cfg.RFQ_SEND_ENABLED, 'ready': cfg.RFQ_SEND_ENABLED and not missing,
                'missing': missing, 'remaining_cap': max(0, cfg.CUSTOMER_DAILY_SEND_LIMIT - self.repo.daily_count()),
                'statuses': SUGGESTED_STATUSES}

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
        return customer

    # -- enquiries ------------------------------------------------------------
    def create_enquiry(self, customer_id, reference=None, source_ref=None, **values):
        values = _clean(values, ENQUIRY_FIELDS)
        self.repo.get('customers', customer_id)
        if not values.get('title'):
            raise ValueError('Enquiry title is required')
        if reference and not REFERENCE_RE.fullmatch(reference):
            raise ValueError('Reference must look like IKE-ABC123')
        enquiry_id = self.repo.insert('customer_enquiries', customer_id=customer_id,
                                      reference=reference or new_reference(), source_ref=source_ref,
                                      **{'status': 'new', **values})
        self.log(enquiry_id, 'note', 'Enquiry created')
        return self.get_enquiry(enquiry_id)

    def update_enquiry(self, enquiry_id, **values):
        values = _clean(values, ENQUIRY_FIELDS)
        enquiry = self.repo.get('customer_enquiries', enquiry_id)
        if 'title' in values and not values['title']:
            raise ValueError('Enquiry title cannot be blank')
        if values:
            self.repo.update('customer_enquiries', enquiry_id, **values)
        if 'status' in values and (values['status'] or '') != (enquiry['status'] or ''):
            self.log(enquiry_id, 'status_change', f"Status: {enquiry['status']} -> {values['status']}")
        return self.get_enquiry(enquiry_id)

    def get_enquiry(self, enquiry_id):
        enquiry = self.repo.get('customer_enquiries', enquiry_id)
        enquiry['customer'] = self.repo.get('customers', enquiry['customer_id'])
        enquiry['activities'] = self.repo.rows('customer_activities', 'enquiry_id=?', (enquiry_id,),
                                               order='occurred_at DESC, id DESC')
        enquiry['messages'] = self.repo.rows('customer_messages', 'enquiry_id=?', (enquiry_id,), order='id DESC')
        return enquiry

    def list_enquiries(self, status=None, due_only=False, include_closed=False):
        enquiries = self.repo.rows('customer_enquiries', order="coalesce(next_action_due,'9999') , updated_at DESC")
        customers = {c['id']: c for c in self.repo.rows('customers')}
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        result = []
        for e in enquiries:
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
            result.append(e)
        return result

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
        self.repo.get('customer_enquiries', enquiry_id)
        when = occurred_at or now()
        activity_id = self.repo.insert('customer_activities', enquiry_id=enquiry_id, kind=kind, summary=summary,
                                       occurred_at=when, external_id=external_id)
        if kind in INBOUND:
            self.repo.update('customer_enquiries', enquiry_id, last_customer_contact_at=when)
            self._cancel_pending(enquiry_id, 'Customer replied')
        elif kind in OUTBOUND:
            self.repo.update('customer_enquiries', enquiry_id, last_our_contact_at=when)
        return self.repo.get('customer_activities', activity_id)

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
                f"Kind regards,\n{signature}\nRef: {ref}")
        elif ref not in body:
            body = body.rstrip() + f'\nRef: {ref}'
        subject = subject or f"Your {enquiry['title']} enquiry [{ref}]"
        if channel == 'email' and ref not in subject:
            subject += f' [{ref}]'
        action_url = 'https://wa.me/' + recipient + '?text=' + quote(body) if channel == 'whatsapp' else None
        message_id = self.repo.insert('customer_messages', enquiry_id=enquiry_id, channel=channel, recipient=recipient,
                                      subject=subject if channel == 'email' else None, body=body, action_url=action_url)
        return self.repo.get('customer_messages', message_id)

    def edit_message(self, message_id, subject=None, body=None):
        message = self.repo.get('customer_messages', message_id)
        if message['status'] != 'draft':
            raise ValueError('Only drafts can be edited')
        ref = self.repo.get('customer_enquiries', message['enquiry_id'])['reference']
        values = {}
        if body is not None:
            values['body'] = body if ref in body else body.rstrip() + f'\nRef: {ref}'
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
        """Send one approved email. The customer must not have replied since it was drafted."""
        message = self.repo.get('customer_messages', message_id)
        if message['channel'] != 'email':
            raise ValueError('WhatsApp messages are sent by hand, then marked sent')
        if message['status'] != 'approved':
            raise ValueError('Only approved messages can be sent')
        ready = self.status()
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
        if not self.repo.claim_send(message_id, cfg.CUSTOMER_DAILY_SEND_LIMIT):
            raise ValueError('Daily customer send cap reached, or the message is already being sent')
        sender, _ = self._mail()
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
        """Log customer emails against their enquiry; a reply stops pending follow-ups.
        Matching: an IKE- reference in the subject/body, else the sender's address against
        customer emails (most recently updated open enquiry). Everything else is ignored."""
        from rfq.quote_parser import strip_history
        result = {'logged': 0, 'ignored': 0, 'duplicates': 0}
        customers = {c['id']: c for c in self.repo.rows('customers')}
        enquiries = self.repo.rows('customer_enquiries', order='updated_at DESC')
        by_ref = {e['reference']: e for e in enquiries}
        by_email = {}
        for e in enquiries:
            email = (customers.get(e['customer_id'], {}).get('email') or '').strip().lower()
            if email and (e['status'] or '').lower() not in CLOSED_STATUSES:
                by_email.setdefault(email, e)
        if not by_email and not by_ref:
            return result
        _, inbox = self._mail()
        watch = [{'channel': 'email', 'recipient': email, 'reference': ''} for email in by_email] + \
                [{'channel': 'other', 'recipient': '', 'reference': ref} for ref in by_ref]
        raw_messages = inbox.fetch_for_campaign(since_days, limit, watch) if hasattr(inbox, 'fetch_for_campaign') \
            else inbox.fetch_recent(since_days, limit)
        for raw in raw_messages:
            mail = BytesParser(policy=policy.default).parsebytes(raw)
            external = 'cust:' + (str(mail.get('Message-ID') or '').strip() or hashlib.sha256(raw).hexdigest())
            if self.repo.rows('customer_activities', 'external_id=?', (external,)):
                result['duplicates'] += 1
                continue
            sender = parseaddr(str(mail.get('From', '')))[1].lower()
            subject = str(mail.get('Subject', ''))
            auto = str(mail.get('Auto-Submitted', 'no')).strip().lower() != 'no' or mail.get('X-Autoreply') is not None \
                or re.match(r'\s*(out of office|automatic reply|auto.?reply)', subject, re.I) \
                or re.search('mailer-daemon|postmaster', sender, re.I)
            if auto:
                result['ignored'] += 1
                continue
            text = ''
            for part in mail.walk():
                if part.get_content_type() == 'text/plain' and not part.get_filename():
                    try:
                        text += str(part.get_content())
                    except Exception:
                        pass
            ref = REFERENCE_RE.search(subject + '\n' + text)
            enquiry = by_ref.get(ref.group(0)) if ref else None
            if enquiry is None:
                enquiry = by_email.get(sender)
            if enquiry is None or sender == (cfg.RFQ_FROM_ADDRESS or '').strip().lower():
                result['ignored'] += 1
                continue
            excerpt = strip_history(text).strip()[:1500]
            self.log(enquiry['id'], 'email_in', f"Email from {sender}: {subject}\n{excerpt}".strip(),
                     occurred_at=now(), external_id=external)
            result['logged'] += 1
        return result
