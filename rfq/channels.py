"""Email transports and human-operated channel links. No work at construction."""
import imaplib
import smtplib
import ssl
import re
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from urllib.parse import quote
from config import settings as cfg
from rfq.templates import header

def routes(supplier, body=''):
    from batch.tracker_exporter import _marketplace_search_link
    result = []
    search_only = []
    for channel, field in [('email', 'primary_email'), ('alibaba', 'alibaba_url'), ('made_in_china', 'made_in_china_url'), ('whatsapp', 'whatsapp'), ('wechat', 'wechat_id'), ('contact_form', 'contact_form_url')]:
        value = str(supplier.get(field) or "").strip()
        is_search_link = not value
        if not value and channel in {'alibaba', 'made_in_china'}:
            if supplier.get('canonical_name'):
                value = _marketplace_search_link(supplier['canonical_name'], 'alibaba.com' if channel == 'alibaba' else 'made-in-china.com')
        if not value:
            continue
        url = ''
        if channel in {'alibaba', 'made_in_china', 'contact_form'}:
            url = value if str(value).lower().startswith(('https://', 'http://')) else ''
            if not url:
                continue
        elif channel == 'whatsapp':
            digits = re.sub('\\D', '', str(value))
            if not digits:
                continue
            url = 'https://wa.me/' + digits + '?text=' + quote(body)
        (search_only if is_search_link else result).append({'channel': channel, 'recipient': str(value), 'action_url': url})
    # A generic marketplace search link is a last resort: a supplier's real
    # WeChat/WhatsApp/contact form must win over it.
    return result + search_only

class SmtpSender:

    def connect(self):
        client = smtplib.SMTP_SSL(cfg.RFQ_SMTP_HOST, cfg.RFQ_SMTP_PORT, timeout=30, context=ssl.create_default_context()) if cfg.RFQ_SMTP_PORT == 465 else smtplib.SMTP(cfg.RFQ_SMTP_HOST, cfg.RFQ_SMTP_PORT, timeout=30)
        try:
            if cfg.RFQ_SMTP_PORT != 465:
                client.ehlo()
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            client.login(cfg.RFQ_EMAIL_USER, cfg.RFQ_EMAIL_PASSWORD)
            return client
        except Exception:
            client.close()
            raise RuntimeError('SMTP login failed') from None

    def check(self):
        with self.connect():
            return True

    def send(self, message):
        mail = EmailMessage()
        domain = cfg.RFQ_FROM_ADDRESS.rsplit('@', 1)[-1].strip(' >') or None
        for key, value in {'From': cfg.RFQ_FROM_ADDRESS, 'To': message['recipient'], 'Subject': message['subject'], 'Date': formatdate(usegmt=True), 'Message-ID': make_msgid(domain=domain)}.items():
            mail[key] = header(value)
        mail.set_content(message['body'])
        with self.connect() as client:
            client.send_message(mail)
        return str(mail['Message-ID'])

class ImapInbox:

    def connect(self):
        client = imaplib.IMAP4_SSL(cfg.RFQ_IMAP_HOST, cfg.RFQ_IMAP_PORT, timeout=30)
        try:
            client.login(cfg.RFQ_EMAIL_USER, cfg.RFQ_EMAIL_PASSWORD)
            return client
        except Exception:
            client.logout()
            raise RuntimeError('IMAP login failed') from None

    def check(self):
        client = self.connect()
        client.logout()
        return True

    def fetch_recent(self, since_days=14, limit=200):
        from datetime import datetime, timedelta, timezone
        if since_days < 1 or limit < 1:
            raise ValueError('Inbox window and limit must be positive')
        client = self.connect()
        try:
            client.select(cfg.RFQ_IMAP_FOLDER, readonly=True)
            since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime('%d-%b-%Y')
            status, data = client.search(None, 'SINCE', since)
            if status != 'OK':
                return []
            result = []
            for number in data[0].split()[-limit:]:
                status, parts = client.fetch(number, '(BODY.PEEK[])')
                if status == 'OK':
                    result.extend((part[1] for part in parts if isinstance(part, tuple) and isinstance(part[1], bytes)))
            return result
        finally:
            client.logout()
