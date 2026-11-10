"""Customer (buy-side) persistence: profiles, enquiries, activity log, follow-up messages."""
import json

from storage.database import connection_scope
from config import settings as cfg

TABLES = {'customers', 'customer_enquiries', 'customer_activities', 'customer_messages', 'customer_inbox_leads', 'customer_quotes'}
JSON_FIELDS = {'rfq_campaign_ids', 'qualification', 'form_fields'}


class CustomerRepository:

    def __init__(self, db_path=None):
        self.db_path = db_path

    @staticmethod
    def _table(table):
        if table not in TABLES:
            raise ValueError('Unknown customer table')
        return table

    def rows(self, table, where='1=1', args=(), order='id'):
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(f'SELECT * FROM {self._table(table)} WHERE {where} ORDER BY {order}', args).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in JSON_FIELDS & item.keys():
                item[key] = json.loads(item[key] or ('{}' if key in {'qualification', 'form_fields'} else '[]'))
            result.append(item)
        return result

    def get(self, table, row_id):
        rows = self.rows(table, 'id=?', (row_id,))
        if not rows:
            raise ValueError(f'{table} record {row_id} not found')
        return rows[0]

    def _columns(self, conn, table):
        return {r['name'] for r in conn.execute(f'PRAGMA table_info({self._table(table)})')}

    def insert(self, table, **values):
        values = {k: json.dumps(v) if k in JSON_FIELDS else v for k, v in values.items()}
        with connection_scope(self.db_path) as conn:
            if not values.keys() <= self._columns(conn, table):
                raise ValueError('Unknown fields: ' + ', '.join(sorted(values.keys() - self._columns(conn, table))))
            cursor = conn.execute(
                f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                tuple(values.values()))
            return cursor.lastrowid

    def update(self, table, row_id, **values):
        values = {k: json.dumps(v) if k in JSON_FIELDS else v for k, v in values.items()}
        with connection_scope(self.db_path) as conn:
            if not values or not values.keys() <= self._columns(conn, table):
                raise ValueError('Unknown fields')
            if 'updated_at' in self._columns(conn, table) and 'updated_at' not in values:
                values['updated_at'] = _now()
            conn.execute(f"UPDATE {table} SET {','.join(k + '=?' for k in values)} WHERE id=?",
                         (*values.values(), row_id))

    def transition(self, message_id, from_status, to_status, **values):
        """Conditional status change; True only for the caller that won the transition."""
        sets = ['status=?'] + [k + '=?' for k in values]
        with connection_scope(self.db_path) as conn:
            return conn.execute(f"UPDATE customer_messages SET {','.join(sets)} WHERE id=? AND status=?",
                                (to_status, *values.values(), message_id, from_status)).rowcount == 1

    def claim_send(self, message_id, cap, mailbox=None):
        """approved -> sending inside one write transaction, honouring the rolling 24h cap."""
        with connection_scope(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            count = conn.execute(
                "SELECT COUNT(*) FROM customer_messages WHERE channel='email' AND "
                "(sent_at >= datetime('now','-24 hours') OR status='sending') AND coalesce(mailbox,?)=?", (cfg.RFQ_FROM_ADDRESS, mailbox)).fetchone()[0]
            if cap is not None and count >= cap:
                return False
            return conn.execute(
                "UPDATE customer_messages SET status='sending',mailbox=? WHERE id=? AND status='approved' AND channel='email'",
                (mailbox, message_id)).rowcount == 1

    def check_mailbox_ledger(self, message, mailbox, sent):
        from datetime import datetime, timezone
        from customers.mailboxes import UK
        start = datetime.now(UK).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        rows = self.rows('customer_messages', "channel='email' AND coalesce(mailbox,?)=? AND ((coalesce(sent_at,created_at)>=? AND status IN ('sent','failed')) OR status='sending')", (cfg.RFQ_FROM_ADDRESS, mailbox, start))
        visible = {str(m.get('messageId', '')) for m in sent}
        unseen = [r for r in rows if (r.get('provider_message_id') or '').removeprefix('zoho:') not in visible]
        from customers.mailboxes import sent_time
        today_count = sum(sent_time(m).astimezone(UK).date() == datetime.now(UK).date() for m in sent)
        if today_count + len(unseen) >= 30:
            raise ValueError('Daily cap of 30 reached for this mailbox (UK calendar day)')
        if any(r['recipient'].lower() == message['recipient'].lower() and r['subject'] == message['subject'] for r in rows):
            raise ValueError('Duplicate refused: same recipient and subject already sent today')

    def daily_count(self):
        with connection_scope(self.db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM customer_messages WHERE channel='email' AND "
                "(sent_at >= datetime('now','-24 hours') OR status='sending')").fetchone()[0]

    def form_seen(self, external_id, fingerprint, other_transport):
        with connection_scope(self.db_path) as conn:
            return bool(conn.execute(
                "SELECT 1 FROM customer_form_receipts WHERE external_id=? OR "
                "(external_id LIKE ? AND fingerprint=?) LIMIT 1",
                (external_id, other_transport, fingerprint)).fetchone())

    def remember_form(self, external_id, fingerprint):
        with connection_scope(self.db_path) as conn:
            conn.execute("INSERT OR IGNORE INTO customer_form_receipts VALUES (?, ?)",
                         (external_id, fingerprint))

    def is_supplier_address(self, email):
        """True when the address belongs to a supplier we hold or have sent an RFQ to.
        Domain matching is skipped for free-mail domains (many customers use gmail)."""
        email = (email or '').strip().lower()
        if '@' not in email:
            return False
        domain = email.rsplit('@', 1)[1]
        with connection_scope(self.db_path) as conn:
            if conn.execute('SELECT 1 FROM suppliers WHERE lower(primary_email)=? LIMIT 1', (email,)).fetchone():
                return True
            if conn.execute('SELECT 1 FROM rfq_messages WHERE lower(recipient)=? LIMIT 1', (email,)).fetchone():
                return True
            if domain in FREE_MAIL:
                return False
            return bool(conn.execute(
                "SELECT 1 FROM suppliers WHERE lower(domain)=? OR lower(domain) LIKE ? "
                "OR lower(primary_email) LIKE ? LIMIT 1", (domain, '%.' + domain, '%@' + domain)).fetchone())


FREE_MAIL = {'gmail.com', 'googlemail.com', 'hotmail.com', 'hotmail.co.uk', 'outlook.com', 'live.com',
             'live.co.uk', 'yahoo.com', 'yahoo.co.uk', 'icloud.com', 'me.com', 'aol.com', 'msn.com',
             'btinternet.com', 'sky.com', 'protonmail.com', 'proton.me', 'qq.com', '163.com', '126.com',
             'zoho.com', 'zohomail.eu', 'mail.com', 'gmx.com', 'virginmedia.com', 'talktalk.net'}


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
