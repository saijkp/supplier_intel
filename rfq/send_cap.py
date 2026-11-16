"""Shared UK-day send reservations. All callers must use the same SQLite DB.

Unknown delivery outcomes retain their slot; only explicit rejection releases it.
"""
import json
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from customers.mailboxes import UK, sent_time
from storage.database import connection_scope

class SendCapError(ValueError):
    pass


_ACTIVE = ContextVar('mailbox_reservation', default=None)


def daily_cap(mailbox):
    return 5 if mailbox.lower() == 'sj@ikeng.online' else 30


def reserve(db_path, mailbox, sent, now=None, cap=None):
    now = now or datetime.now(timezone.utc)
    mailbox = mailbox.lower()
    day = now.astimezone(UK).date().isoformat()
    key = 'send_cap:' + mailbox + ':' + day
    ids = {str(m['messageId']) for m in sent
           if sent_time(m).astimezone(UK).date().isoformat() == day}
    limit = min(daily_cap(mailbox), cap if cap is not None else daily_cap(mailbox))
    token = uuid.uuid4().hex
    with connection_scope(db_path) as db:
        db.execute('BEGIN IMMEDIATE')
        # Plain mailbox senders need the ledger, not the outreach subsystem.
        db.execute("CREATE TABLE IF NOT EXISTS outreach_runtime(key TEXT PRIMARY KEY, payload TEXT NOT NULL DEFAULT '{}')")
        row = db.execute('SELECT payload FROM outreach_runtime WHERE key=?', (key,)).fetchone()
        entries = json.loads(row['payload']) if row else {}
        used = len(ids) + sum(e.get('provider_id') not in ids for e in entries.values())
        if used >= limit:
            raise SendCapError('Shared UK daily mailbox cap reached')
        history=db.execute("SELECT payload FROM outreach_runtime WHERE key LIKE ?", ('send_cap:'+mailbox+':%',)).fetchall()
        if any(e['status']=='reserved' for row in history for e in json.loads(row['payload']).values()):
            raise SendCapError('Uncertain mailbox send requires human review')
        for mid in ids:
            if not any(e.get('provider_id') == mid for e in entries.values()):
                entries['observed:'+mid] = {'status':'sent','provider_id':mid,'at':now.timestamp()}
        entries[token] = {'status': 'reserved', 'at': now.timestamp()}
        db.execute('INSERT INTO outreach_runtime(key,payload) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload', (key, json.dumps(entries)))
    return key, token


def _finish(db_path, reservation, provider=None):
    key, token = reservation
    with connection_scope(db_path) as db:
        db.execute('BEGIN IMMEDIATE')
        entries = json.loads(db.execute('SELECT payload FROM outreach_runtime WHERE key=?', (key,)).fetchone()['payload'])
        if provider is None:
            del entries[token]
        else:
            entries[token].update(status='sent', provider_id=str(provider).removeprefix('zoho:'))
        db.execute('UPDATE outreach_runtime SET payload=? WHERE key=?', (json.dumps(entries), key))


def confirm(db_path, reservation, provider):
    if not provider:
        raise ValueError('Provider confirmation required')
    _finish(db_path, reservation, provider)


def release(db_path, reservation):
    """Only for a proven rejection before acceptance; never for timeouts."""
    _finish(db_path, reservation)


def send_with_cap(box, message, db_path=None, now=None, cap=None):
    """CLI-safe reserve -> send -> confirm; explicit rejection releases the slot."""
    if _ACTIVE.get() == box.address.lower():
        return box.sender.send(message)
    from config import settings as cfg
    db_path = db_path or cfg.DB_PATH
    now = now or datetime.now(timezone.utc)
    if box.address.lower() == 'sj@ikeng.online':
        from outreach.service import OutreachService
        from outreach.repository import OutreachRepository
        from outreach.always_on import AlwaysOnService
        # Cap evaluation only needs repository/clock; do not construct HTTP adapters.
        policy = AlwaysOnService(OutreachService(repo=OutreachRepository(db_path), now=lambda:now),
                                 planning=object(), contacts=object()).cap(box.address.lower())
        cap = min(cap, policy) if cap is not None else policy
    sent = box.sent(2)
    box.check_send(message, sent, now)
    if sent and (now-max(sent_time(m) for m in sent)).total_seconds()<90:
        raise SendCapError('Mailbox sends must be spaced at least 90 seconds apart')
    return send_reserved(box.sender, box.address, message, db_path, sent, now, cap)


def send_reserved(sender, address, message, db_path=None, sent=(), now=None, cap=None):
    """Apply the shared ledger without requiring an inbox-capable transport.

    Workflow callers supply their checked Sent evidence and repository path.
    The low-level sender still enforces the local cap when used on its own.
    """
    from config import settings as cfg
    from rfq.zoho_mail import ZohoError
    db_path = db_path or cfg.DB_PATH
    reservation = reserve(db_path, address, sent, now, cap)
    context = _ACTIVE.set(address.lower())
    try:
        provider = sender.send(message)
        confirm(db_path, reservation, provider)
        return provider
    except ZohoError as exc:
        if exc.safe_to_retry:
            release(db_path, reservation)
        raise
    finally:
        _ACTIVE.reset(context)
