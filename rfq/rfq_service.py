"""Approval-driven RFQ workflow. Assisted channels never send automatically."""
import hashlib
import random
import re
import secrets
import sqlite3
import string
import threading
import time
from datetime import datetime, timezone, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from config import settings as cfg
from storage.rfq_repository import RFQRepository
from storage.repository import SupplierRepository
from verification.email_deliverability import check_email_domain
from rfq.channels import SmtpSender, ImapInbox, routes
from rfq.templates import draft, footer, header
from rfq.quote_parser import QuoteParser, strip_history, validate_candidates
from rfq.attachments import extract_text, MAX_TEXT
_SEND_LOCK = threading.Semaphore(1)

def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

class RFQService:

    def __init__(self, repo=None, sender=None, inbox=None, parser=None, sleep_fn=None, supplier_repo=None, mx_check=None, defer_parsing=False):
        self.repo = repo if repo is not None else RFQRepository()
        self.suppliers = supplier_repo if supplier_repo is not None else SupplierRepository(self.repo.db_path)
        if cfg.RFQ_MAIL_TRANSPORT=='zoho':
            from rfq.zoho_mail import ZohoClient, ZohoSender, ZohoInbox
            zoho=ZohoClient(db_path=self.repo.db_path)
            self.sender=sender if sender is not None else ZohoSender(zoho)
            self.inbox=inbox if inbox is not None else ZohoInbox(zoho)
        else:
            self.sender = sender if sender is not None else SmtpSender()
            self.inbox = inbox if inbox is not None else ImapInbox()
        self.parser = parser if parser is not None else QuoteParser()
        self.sleep = sleep_fn if sleep_fn is not None else time.sleep
        self.mx_check = mx_check if mx_check is not None else check_email_domain
        self.defer_parsing = defer_parsing

    def status(self):
        missing = [name for name in ('RFQ_SMTP_HOST', 'RFQ_EMAIL_USER', 'RFQ_EMAIL_PASSWORD', 'RFQ_FROM_ADDRESS', 'RFQ_BUYER_COMPANY', 'RFQ_BUYER_NAME', 'RFQ_BUYER_ADDRESS') if not str(getattr(cfg, name)).strip()]
        inbox_configured=bool(cfg.RFQ_IMAP_HOST and cfg.RFQ_EMAIL_USER and cfg.RFQ_EMAIL_PASSWORD)
        if cfg.RFQ_MAIL_TRANSPORT=='zoho':
            from rfq.zoho_mail import missing as zoho_missing, REGIONS
            missing=[name for name in missing if name not in {'RFQ_SMTP_HOST','RFQ_EMAIL_USER','RFQ_EMAIL_PASSWORD'}]+zoho_missing()
            if cfg.RFQ_ZOHO_REGION and cfg.RFQ_ZOHO_REGION not in REGIONS: missing.append('RFQ_ZOHO_REGION')
            if cfg.RFQ_ZOHO_ACCOUNT_ID and not cfg.RFQ_ZOHO_ACCOUNT_ID.isdigit(): missing.append('RFQ_ZOHO_ACCOUNT_ID')
            inbox_configured=not zoho_missing() and cfg.RFQ_ZOHO_REGION in REGIONS and cfg.RFQ_ZOHO_ACCOUNT_ID.isdigit()
        if cfg.RFQ_MAIL_TRANSPORT not in {'smtp','zoho'}:
            missing.append('RFQ_MAIL_TRANSPORT');inbox_configured=False
        return {'transport':cfg.RFQ_MAIL_TRANSPORT,'quote_parser':cfg.RFQ_QUOTE_PARSER,'send_enabled': cfg.RFQ_SEND_ENABLED, 'ready': cfg.RFQ_SEND_ENABLED and (not missing), 'missing': missing, 'remaining_cap': max(0, cfg.RFQ_DAILY_SEND_LIMIT - self.repo.daily_count()), 'imap_configured':inbox_configured,'inbox_configured':inbox_configured, 'sending_needs_review': self.repo.rows('messages', "status='sending'")}

    def create_campaign(self, **values):
        if not values.get('title') or not values.get('product'):
            raise ValueError('Title and product required')
        allowed = {'title', 'product', 'product_zh', 'specification', 'quantity', 'unit', 'target_incoterm', 'destination', 'required_certifications', 'quote_deadline'}
        if not values.keys() <= allowed:
            raise ValueError('Unknown campaign fields')
        return self.repo.get('campaigns', self.repo.insert('campaigns', **values))

    def show(self, campaign_id):
        campaign = self.repo.get('campaigns', campaign_id)
        messages = self.repo.rows('messages', 'campaign_id=?', (campaign_id,))
        for message in messages:
            supplier = self.suppliers.get_supplier(message['supplier_id']) if message['supplier_id'] is not None else None
            message['supplier_name'] = (supplier or {}).get('canonical_name', 'Mailbox test')
        return {**campaign, 'messages': messages, 'replies': self.repo.rows('replies', 'campaign_id=?', (campaign_id,))}

    def draft_messages(self, campaign_id, supplier_ids=None, query=None, limit=25, channels=None, all_channels=False):
        campaign = self.repo.get('campaigns', campaign_id)
        if campaign['status'] != 'open':
            raise ValueError('Campaign is closed')
        if limit < 1:
            raise ValueError('Limit must be positive')
        suppliers = [self.suppliers.get_supplier(i) for i in supplier_ids[:limit]] if supplier_ids is not None else self.suppliers.search_suppliers(query or campaign['product'], limit=limit)
        counts = {'drafted': 0, 'no_contact_route': 0, 'flagged': 0, 'existing': 0}
        for supplier in suppliers:
            if not supplier:
                continue
            if supplier.get('flagged'):
                counts['flagged'] += 1
                continue
            available = [r for r in routes(supplier) if channels is None or r['channel'] in channels]
            if not available:
                counts['no_contact_route'] += 1
                continue
            for route in available if all_channels else available[:1]:
                ref = f'RFQ-{campaign_id}-' + ''.join((secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6)))
                content = draft(campaign, supplier, ref, chinese=True if route['channel'] == 'wechat' else None)
                route = next((r for r in routes(supplier, content['body']) if r['channel'] == route['channel']))
                try:
                    self.repo.insert('messages', campaign_id=campaign_id, supplier_id=supplier['id'], reference=ref, **content, **route)
                    counts['drafted'] += 1
                except sqlite3.IntegrityError:
                    counts['existing'] += 1
        return counts

    def update_draft(self, message_id, **values):
        message = self.repo.get('messages', message_id)
        if 'subject' in values:
            values['subject'] = header(values['subject'])
        if 'recipient' in values:
            values['recipient'] = header(values['recipient']).strip()
        if 'subject' in values and message['reference'] not in values['subject']:
            values['subject'] += ' [' + message['reference'] + ']'
        if 'body' in values and message['reference'] not in values['body']:
            values['body'] += '\nReference: ' + message['reference']
        self.repo.edit_draft(message_id, values)
        if message['channel'] == 'whatsapp' and 'body' in values:
            from urllib.parse import quote
            self.repo.update('messages', message_id, action_url='https://wa.me/' + re.sub('\\D', '', message['recipient']) + '?text=' + quote(values['body']))
        return self.repo.get('messages', message_id)

    def approve(self, campaign_id, message_ids=None):
        if self.repo.get('campaigns', campaign_id)['status'] != 'open':
            raise ValueError('Campaign is closed')
        return {'approved': self.repo.approve(campaign_id, message_ids)}

    def _has_replied(self, message):
        """True when this supplier answered this campaign on any channel."""
        return bool(self.repo.rows('replies', 'campaign_id=? AND supplier_id=? AND reply_type!=?', (message['campaign_id'], message['supplier_id'], 'bounce')))

    def _outcome(self, message, outcome):
        if message['supplier_id'] is None:
            return
        self.suppliers.record_procurement_outcome(supplier_id=message['supplier_id'], outcome=outcome, notes=f"RFQ campaign {message['campaign_id']}")

    def send_approved(self, campaign_id, limit=None, message_ids=None):
        """Send approved rows under a DB claim; explicit Zoho rejections back off, while uncertain delivery requires human review."""
        result = {'sent': 0, 'skipped': 0, 'failed': 0, 'partial': False}
        ready = self.status()
        if not ready['ready']:
            return {**result, 'reason': 'RFQ_SEND_ENABLED is false' if not cfg.RFQ_SEND_ENABLED else 'Missing configuration: ' + ', '.join(ready['missing'])}
        campaign = self.repo.get('campaigns', campaign_id)
        if campaign['status'] != 'open':
            return {**result, 'reason': 'Campaign is closed'}
        if not _SEND_LOCK.acquire(blocking=False):
            return {**result, 'partial': True, 'reason': 'Another send job is running'}
        start = time.monotonic()
        attempted = False
        try:
            rows = self.repo.rows('messages', "campaign_id=? AND channel='email' AND status='approved'", (campaign_id,))
            if message_ids is not None:
                rows = [m for m in rows if m['id'] in message_ids]
            if limit is not None:
                rows = rows[:limit]
            for message in rows:
                if message.get('zoho_retry_at') and message['zoho_retry_at']>now():
                    result['partial']=True
                    continue
                if attempted:
                    delay = cfg.RFQ_SEND_DELAY_SECONDS * random.uniform(1, 1.15)
                    if time.monotonic() - start + delay >= cfg.RFQ_SEND_JOB_MAX_SECONDS:
                        result['partial'] = True
                        break
                    self.sleep(delay)
                if time.monotonic() - start >= cfg.RFQ_SEND_JOB_MAX_SECONDS:
                    result['partial'] = True
                    break
                reason = None
                try:
                    if message['kind'].startswith('clarification:'):
                        from rfq.workspace_workflow import clarification_send_reason
                        reason = clarification_send_reason(self.repo.db_path, message)
                    supplier = self.suppliers.get_supplier(message['supplier_id'])
                    if not supplier or supplier.get('flagged'):
                        reason = 'Supplier is flagged or missing'
                    elif not re.fullmatch('[^\\s@,;<>]+@[^\\s@,;<>]+\\.[^\\s@,;<>]+', message['recipient']):
                        reason = 'A single valid email recipient is required'
                    elif self.repo.suppressed(message['recipient']):
                        reason = 'Address is suppressed'
                    elif message['kind'] == 'followup' and self._has_replied(message):
                        reason = 'Supplier already replied'
                    elif not self.mx_check(message['recipient']).has_mx_records:
                        reason = 'Email domain has no MX records'
                except Exception:
                    reason = 'Send-time validation failed'
                if reason:
                    self.repo.update('messages', message['id'], status='skipped', status_reason=reason)
                    result['skipped'] += 1
                    continue
                if time.monotonic() - start >= cfg.RFQ_SEND_JOB_MAX_SECONDS:
                    result['partial'] = True
                    break
                if not self.repo.claim(message['id'], cfg.RFQ_DAILY_SEND_LIMIT):
                    result['partial'] = True
                    result['reason'] = 'Rolling 24-hour cap reached or message already claimed'
                    continue
                # A human may have edited the footer out, or drafted before identity was set.
                body = message['body'] if footer() in message['body'] else message['body'] + '\n\n' + footer()
                attempted = True
                try:
                    # Recheck cancellation/freshness after the atomic claim as well.
                    if message['kind'].startswith('clarification:'):
                        reason = clarification_send_reason(self.repo.db_path, message)
                        if reason:
                            self.repo.update('messages', message['id'], status='skipped', status_reason=reason)
                            result['skipped'] += 1
                            continue
                    smtp_id = self.sender.send({**message, 'subject': header(message['subject']), 'recipient': header(message['recipient']), 'body': body})
                except Exception as exc:
                    from rfq.zoho_mail import ZohoError
                    if isinstance(exc,ZohoError) and exc.safe_to_retry and exc.category!='recipient':
                        attempts=message.get('zoho_retry_attempts',0)+1
                        retry_at=(datetime.now(timezone.utc)+timedelta(minutes=min(1440,5*2**min(attempts,9)))).strftime('%Y-%m-%d %H:%M:%S')
                        self.repo.update('messages',message['id'],status='approved',status_reason='Zoho '+exc.category+'; queued for retry',zoho_retry_at=retry_at,zoho_retry_attempts=attempts)
                        result['failed']+=1
                        continue
                    self.repo.update('messages', message['id'], status='failed', status_reason='SMTP send failed; check delivery before any new outreach')
                    result['failed'] += 1
                    continue
                self.repo.update('messages', message['id'], status='sent', sent_at=now(), smtp_message_id=smtp_id)
                self._outcome(message, 'rfq_submitted')
                result['sent'] += 1
            return result
        finally:
            _SEND_LOCK.release()

    def retry_pending(self,limit=10):
        """Retry only explicit rejected sends already authorised by the buyer.

        Approval alone is insufficient: retry_at is set only by an attempted send.
        Unknown/ambiguous submissions retain failed state and require review.
        """
        rows=self.repo.rows('messages',"status='approved' AND zoho_retry_at IS NOT NULL AND zoho_retry_at<=?",(now(),))[:limit]
        campaigns={m['campaign_id'] for m in rows}
        return [self.send_approved(cid,message_ids=[m['id'] for m in rows if m['campaign_id']==cid]) for cid in campaigns]

    def mark_sent(self, message_id):
        message = self.repo.get('messages', message_id)
        if message['channel'] == 'email' or message['status'] != 'draft':
            raise ValueError('Only assisted drafts can be marked sent')
        supplier = self.suppliers.get_supplier(message['supplier_id'])
        if not supplier or supplier.get('flagged'):
            raise ValueError('Supplier is flagged or missing')
        self.repo.update('messages', message_id, status='sent', sent_at=now())
        self._outcome(message, 'rfq_submitted')
        return self.repo.get('messages', message_id)

    def _reply(self, message, text, source='manual', external_message_id=None, from_address='', subject='', attachments=None, bounce=False, received_at=None):
        # Permanent suppression needs a short reply that is actually asking to stop:
        # a long quote that happens to contain the word must never lose the supplier.
        unquoted = strip_history(text).replace('reply unsubscribe to stop', '').strip()
        opt_out = len(unquoted) <= 200 and bool(re.search('\\bunsubscribe\\b|退订', unquoted, re.I))
        kind = 'bounce' if bounce else 'opt_out' if opt_out else 'other'
        try:
            reply_id = self.repo.insert('replies', message_id=message['id'], campaign_id=message['campaign_id'], supplier_id=message['supplier_id'], channel=message['channel'], source=source, external_message_id=external_message_id, from_address=from_address, subject=subject, body_text=text, attachments=attachments or [], reply_type=kind, received_at=received_at or now())
        except sqlite3.IntegrityError:
            return {'duplicate': True}
        self.repo.update('messages', message['id'], status='bounced' if bounce else 'replied', replied_at=now())
        if bounce or opt_out:
            if message['channel'] == 'email':
                self.repo.suppress(message['recipient'], kind)
            self.repo.update('replies', reply_id, parse_status='no_quote')
            return self.repo.get('replies', reply_id)
        # Agent mode: leave the reply 'pending' for a Claude Code / Codex session; no OpenAI call.
        if self.defer_parsing or cfg.RFQ_QUOTE_PARSER == 'agent':
            return self.repo.get('replies', reply_id)
        try:
            # IMAP text is already stripped; stripping again would truncate attachment text.
            parsed = self.parser.parse(text, strip=source != 'imap')
        except Exception:
            parsed = {'quotes': [], 'parse_status': 'failed'}
        for quote in parsed['quotes']:
            self.repo.insert('quotes', reply_id=reply_id, campaign_id=message['campaign_id'], supplier_id=message['supplier_id'], **quote)
        needs_review = any((a['read_status'] == 'needs_manual_review' for a in attachments or []))
        self.repo.update('replies', reply_id, reply_type='quote' if parsed['quotes'] else 'other', parse_status='needs_manual_review' if needs_review else parsed['parse_status'])
        if parsed['quotes']:
            self._outcome(message, 'quoted')
        return self.repo.get('replies', reply_id)

    def _validation_source(self, reply):
        # IMAP text was stripped of quoted history on arrival; manual pastes were not.
        text = reply.get('body_text') or ''
        return text if reply.get('source') == 'imap' else strip_history(text)

    def pending_replies(self, limit=20):
        """Replies waiting to be read, with the exact text any evidence must quote."""
        rows = self.repo.rows('replies', "parse_status IN ('pending','failed') AND COALESCE(reply_type,'') NOT IN ('bounce','opt_out')")
        result = []
        for reply in rows[:max(1, min(limit, 100))]:
            message = self.repo.get('messages', reply['message_id'])
            campaign = self.repo.get('campaigns', reply['campaign_id'])
            supplier = self.suppliers.get_supplier(reply['supplier_id']) if reply['supplier_id'] is not None else None
            result.append({
                'reply_id': reply['id'], 'campaign_id': reply['campaign_id'], 'message_id': reply['message_id'],
                'supplier_id': reply['supplier_id'], 'supplier_name': (supplier or {}).get('canonical_name'),
                'channel': reply['channel'], 'from_address': reply['from_address'], 'subject': reply['subject'],
                'received_at': reply['received_at'], 'parse_status': reply['parse_status'],
                'attachments': reply['attachments'], 'reference': message['reference'],
                'rfq': {k: campaign.get(k) for k in ('product', 'specification', 'quantity', 'unit', 'target_incoterm', 'destination')},
                'source_text': self._validation_source(reply),
            })
        return {'pending': result, 'total_pending': len(rows)}

    def submit_agent_quotes(self, reply_id, quotes):
        """Store candidate quotes proposed by a Claude Code / Codex session, after the same
        evidence validation the OpenAI parser uses. Anything ungrounded is rejected, not stored."""
        reply = self.repo.get('replies', reply_id)
        if reply.get('reply_type') in ('bounce', 'opt_out'):
            raise ValueError('Bounces and opt-outs carry no quote')
        if reply.get('parse_status') not in ('pending', 'failed'):
            raise ValueError(f"Reply already read (parse_status={reply.get('parse_status')})")
        accepted, rejections = validate_candidates(quotes, self._validation_source(reply))
        for quote in accepted:
            self.repo.insert('quotes', reply_id=reply_id, campaign_id=reply['campaign_id'], supplier_id=reply['supplier_id'], **quote)
        self.repo.update('replies', reply_id, reply_type='quote' if accepted else 'other', parse_status='parsed' if accepted else 'no_quote')
        if accepted:
            self._outcome(self.repo.get('messages', reply['message_id']), 'quoted')
        return {'reply_id': reply_id, 'accepted': accepted, 'rejections': rejections,
                'parse_status': 'parsed' if accepted else 'no_quote'}

    def record_manual_reply(self, message_id, text):
        if not text.strip():
            raise ValueError('Reply cannot be empty')
        return self._reply(self.repo.get('messages', message_id), text)

    def poll_inbox(self, since_days=14, limit=200, campaign_id=None, strict_sender=False):
        """Read without marking messages read; persist only matching RFQ correspondence. Defer attachment extraction until after a match."""
        result = {'matched': 0, 'discarded': 0, 'duplicates': 0}
        messages = self.repo.rows('messages', 'campaign_id=?', (campaign_id,)) if campaign_id is not None else self.repo.rows('messages')
        incoming=self.inbox.fetch_for_campaign(since_days,limit,messages) if hasattr(self.inbox,'fetch_for_campaign') else self.inbox.fetch_recent(since_days, limit)
        for raw in incoming:
            mail = BytesParser(policy=policy.default).parsebytes(raw)
            external = str(mail.get('Message-ID') or '').strip() or 'sha256:' + hashlib.sha256(raw).hexdigest()
            if external and self.repo.rows('replies', 'external_message_id=?', (external,)):
                result['duplicates'] += 1
                continue
            sender = parseaddr(str(mail.get('From', '')))[1].lower()
            subject = str(mail.get('Subject', ''))
            bodies = []
            html_bodies = []
            files = []
            for part in mail.walk():
                if part.get_filename():
                    files.append(part)
                    continue
                if part.get_content_type() == 'text/plain':
                    try:
                        bodies.append(part.get_content())
                    except Exception:
                        pass
                elif part.get_content_type() == 'text/html':
                    try:
                        from bs4 import BeautifulSoup
                        html = BeautifulSoup(part.get_content(), 'html.parser')
                        for node in html(['script', 'style']):
                            node.decompose()
                        html_bodies.append(html.get_text('\n'))
                    except Exception:
                        pass
            text = '\n'.join((str(b) for b in bodies or html_bodies))
            ids = str(mail.get('In-Reply-To', '')) + ' ' + str(mail.get('References', ''))
            bounce = bool(re.search('mailer-daemon|postmaster', sender, re.I) or mail.get_content_type() == 'multipart/report')
            original = raw.decode('utf-8', errors='replace') if bounce else ''
            # Delay notices and out-of-office replies are not answers: storing them
            # would suppress a good address or cancel a needed follow-up.
            auto_submitted = str(mail.get('Auto-Submitted', 'no')).strip().lower()
            auto_reply = not bounce and (auto_submitted != 'no' or mail.get('X-Autoreply') is not None or re.match(r'\s*(out of office|automatic reply|auto.?reply|自动回复|自動回覆)', subject, re.I))
            if auto_reply or (bounce and re.search(r'^(Action:\s*delayed|Status:\s*4\.)', original, re.M | re.I)):
                result['discarded'] += 1
                continue
            matched = next((m for m in reversed(messages) if m['smtp_message_id'] and m['smtp_message_id'] in ids), None)
            if not matched:
                matched = next((m for m in reversed(messages) if m['reference'] in subject + '\n' + text + '\n' + original), None)
            if not matched and bounce:
                matched = next((m for m in reversed(messages) if m['smtp_message_id'] and m['smtp_message_id'] in original), None)
            if not matched and (not bounce):
                matched = next((m for m in reversed(messages) if m['channel'] == 'email' and m['sent_at'] and m['recipient'].strip().lower() == sender), None)
            if not matched:
                result['discarded'] += 1
                continue
            if strict_sender and not bounce and sender != matched['recipient'].strip().lower():
                result['discarded'] += 1
                continue
            attachments = []
            source = strip_history(text)[:MAX_TEXT]
            for part in files:
                extracted, state = extract_text(part.get_filename(), part.get_payload(decode=True) or b'')
                attachments.append({'filename': part.get_filename(), 'read_status': state})
                room = MAX_TEXT - len(source)
                if room > 0:
                    source += '\n' + extracted[:max(0, room - 1)]
                elif extracted:
                    attachments[-1]['read_status'] = 'needs_manual_review'
            self._reply(matched, source, source='imap', external_message_id=external, from_address=sender, subject=subject, attachments=attachments, bounce=bounce)
            result['matched'] += 1
        return result

    def draft_followups(self, campaign_id, after_days=3):
        campaign = self.repo.get('campaigns', campaign_id)
        count = 0
        if campaign['status'] != 'open':
            raise ValueError('Campaign is closed')
        cutoff = (datetime.now(timezone.utc) - timedelta(days=after_days)).strftime('%Y-%m-%d %H:%M:%S')
        for message in self.repo.rows('messages', "campaign_id=? AND kind='initial' AND status='sent' AND replied_at IS NULL AND sent_at<=?", (campaign_id, cutoff)):
            supplier = self.suppliers.get_supplier(message['supplier_id'])
            if not supplier or supplier.get('flagged') or self._has_replied(message) or (message['channel'] == 'email' and self.repo.suppressed(message['recipient'])):
                continue
            ref = f'RFQ-{campaign_id}-' + ''.join((secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6)))
            content = draft(campaign, supplier, ref, followup=True, chinese=True if message['channel'] == 'wechat' else None)
            route = next((r for r in routes(supplier, content['body']) if r['channel'] == message['channel']), None)
            if not route:
                continue
            try:
                self.repo.insert('messages', campaign_id=campaign_id, supplier_id=message['supplier_id'], kind='followup', reference=ref, **content, **route)
                count += 1
            except sqlite3.IntegrityError:
                pass
        return {'drafted': count}

    def compare(self, campaign_id):
        self.repo.get('campaigns', campaign_id)
        groups = {}
        for quote in self.repo.rows('quotes', 'campaign_id=?', (campaign_id,)):
            quote['supplier_name'] = (self.suppliers.get_supplier(quote['supplier_id']) or {}).get('canonical_name', '')
            groups.setdefault(quote.get('currency') or 'Unstated', []).append(quote)
        for group in groups.values():
            group.sort(key=lambda q: q['unit_price'])
        replied_suppliers = {reply['supplier_id'] for reply in self.repo.rows('replies', 'campaign_id=? AND reply_type!=?', (campaign_id, 'bounce'))}
        unanswered = [message for message in self.repo.rows('messages', 'campaign_id=? AND sent_at IS NOT NULL AND replied_at IS NULL', (campaign_id,)) if message['supplier_id'] not in replied_suppliers]
        return {'currencies': groups, 'unanswered': unanswered}

    def check(self):
        """Perform mailbox logins only; never send or parse messages."""
        result = {}
        for name, transport in [('smtp', self.sender), ('imap', self.inbox)]:
            try:
                transport.check()
                result[name] = 'login successful'
            except OSError:
                # Reached no server at all: a blocked port or wrong host, not bad credentials.
                result[name] = 'could not connect (network blocked or wrong host)'
            except Exception:
                result[name] = 'login failed (check configuration)'
        return result

    def test_email(self, to):
        """Send to the explicitly named address through the same enablement, identity, suppression, MX and atomic cap gates. Never create a synthetic supplier."""
        if not self.status()['ready']:
            return {'sent': 0, 'reason': 'Sending disabled or configuration incomplete'}
        if not re.fullmatch('[^\\s@,;<>]+@[^\\s@,;<>]+\\.[^\\s@,;<>]+', to):
            raise ValueError('A single explicit recipient is required')
        if self.repo.suppressed(to):
            return {'sent': 0, 'reason': 'Address is suppressed'}
        if not self.mx_check(to).has_mx_records:
            return {'sent': 0, 'reason': 'Email domain has no MX records'}
        if not _SEND_LOCK.acquire(blocking=False):
            return {'sent': 0, 'reason': 'Another send job is running'}
        try:
            campaign = self.create_campaign(title='Mailbox test', product='Mailbox configuration test')
            ref = f"RFQ-{campaign['id']}-" + ''.join((secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6)))
            content = draft(campaign, {}, ref)
            message_id = self.repo.insert('messages', campaign_id=campaign['id'], supplier_id=None, channel='email', recipient=to, reference=ref, **content)
            self.repo.approve(campaign['id'], [message_id])
            if not self.repo.claim(message_id, cfg.RFQ_DAILY_SEND_LIMIT):
                return {'sent': 0, 'reason': 'Rolling 24-hour cap reached'}
            try:
                smtp_id = self.sender.send(self.repo.get('messages', message_id))
            except Exception:
                self.repo.update('messages', message_id, status='failed', status_reason='SMTP test failed; check delivery before retrying')
                return {'sent': 0, 'reason': 'SMTP test failed; check mailbox configuration'}
            self.repo.update('messages', message_id, status='sent', sent_at=now(), smtp_message_id=smtp_id)
            return {'sent': 1, 'smtp_message_id': smtp_id}
        finally:
            _SEND_LOCK.release()
