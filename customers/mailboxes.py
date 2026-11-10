"""Injectable mailbox registry and fail-closed Sent evidence checks."""
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import getaddresses
from html import unescape
import re
from threading import RLock
from zoneinfo import ZoneInfo

from config import settings as cfg

SEND_LOCK = RLock()
UK = ZoneInfo('Europe/London')
DAILY_CAP = 30


def subject_key(subject):
    return re.sub(r'^(?:(?:re|fw|fwd)\s*:\s*)+', '', subject or '', flags=re.I).strip().casefold()


def recipients(item):
    return {addr.lower() for _, addr in getaddresses([unescape(item.get('toAddress') or '')])}


def sent_time(item):
    value = item.get('sentDateInGMT', item.get('receivedTime'))
    try:
        return datetime.fromtimestamp(float(value) / 1000, timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        raise ValueError('Cannot verify Sent timestamps; sending is blocked') from None


def replied(sent, address, subject, received_at, thread_id=None):
    received = datetime.fromisoformat(received_at).replace(tzinfo=timezone.utc)
    return any(sent_time(m) > received and (
        (bool(thread_id) and str(m.get('threadId', '')) == thread_id) or
        (address.lower() in recipients(m) and subject_key(m.get('subject')) == subject_key(subject))) for m in sent)


@dataclass
class Mailbox:
    address: str
    sender: object
    inbox: object

    def sent(self, since_days=14):
        if not hasattr(self.inbox, 'fetch_sent'):
            raise ValueError('Cannot verify Sent folder; sending is blocked')
        return self.inbox.fetch_sent(since_days)

    def check_send(self, message, sent=None, now=None):
        current = now or datetime.now(timezone.utc)
        today = current.astimezone(UK).date()
        items = self.sent(2) if sent is None else sent
        items = [m for m in items if sent_time(m).astimezone(UK).date() == today]
        if len(items) >= DAILY_CAP:
            raise ValueError(f'Daily cap of 30 reached for {self.address} (UK calendar day)')
        if any(not m.get('toAddress') for m in items):
            raise ValueError('Cannot verify Sent recipients; sending is blocked')
        if any(message['recipient'].lower() in recipients(m) and
               (message['subject'] or '') == (m.get('subject') or '') for m in items):
            raise ValueError('Duplicate refused: same recipient and subject already in Sent today')
        return DAILY_CAP - len(items)


def customer_mailbox():
    from rfq.zoho_mail import missing, ZohoClient, ZohoSender, ZohoInbox, REGIONS
    if missing('CUSTOMER_ZOHO_'):
        return None
    if cfg.CUSTOMER_ZOHO_REGION not in REGIONS or not cfg.CUSTOMER_ZOHO_ACCOUNT_ID.isdigit():
        return None
    client = ZohoClient(prefix='CUSTOMER_ZOHO_')
    address = cfg.CUSTOMER_ZOHO_ADDRESS.strip().lower()
    return Mailbox(address, ZohoSender(client, address=address), ZohoInbox(client))
