"""RFQ persistence; all transactions use the shared connection scope."""
import json
from storage.database import connection_scope
TABLES = {'campaigns', 'messages', 'replies', 'quotes', 'suppressions'}
JSON_FIELDS = {'required_certifications', 'attachments', 'evidence'}

class RFQRepository:

    def __init__(self, db_path=None):
        self.db_path = db_path

    def _table(self, table):
        if table not in TABLES:
            raise ValueError('Unknown RFQ table')
        return 'rfq_' + table

    def rows(self, table, where='1=1', args=()):
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(f'SELECT * FROM {self._table(table)} WHERE {where} ORDER BY rowid', args).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in JSON_FIELDS & item.keys():
                item[key] = json.loads(item[key] or ('{}' if key == 'evidence' else '[]'))
            result.append(item)
        return result

    def get(self, table, row_id):
        rows = self.rows(table, 'id=?', (row_id,))
        if not rows:
            raise ValueError(f'RFQ {table} record not found')
        return rows[0]

    def insert(self, table, **values):
        values = {k: json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS else v for k, v in values.items()}
        with connection_scope(self.db_path) as conn:
            valid = {r['name'] for r in conn.execute(f'PRAGMA table_info({self._table(table)})')}
            if not values.keys() <= valid:
                raise ValueError('Unknown RFQ fields')
            cursor = conn.execute(f"INSERT INTO {self._table(table)} ({','.join(values)}) VALUES ({','.join(('?' for _ in values))})", tuple(values.values()))
            return cursor.lastrowid

    def update(self, table, row_id, **values):
        with connection_scope(self.db_path) as conn:
            valid = {r['name'] for r in conn.execute(f'PRAGMA table_info({self._table(table)})')}
            if not values or not values.keys() <= valid:
                raise ValueError('Unknown RFQ fields')
            conn.execute(f'UPDATE {self._table(table)} SET ' + ','.join((k + '=?' for k in values)) + ' WHERE id=?', (*values.values(), row_id))

    def suppress(self, email, reason):
        with connection_scope(self.db_path) as conn:
            conn.execute('INSERT OR IGNORE INTO rfq_suppressions(email,reason) VALUES (?,?)', (email.strip().lower(), reason))

    def suppressed(self, email):
        return bool(self.rows('suppressions', 'email=?', (email.strip().lower(),)))

    def daily_count(self):
        with connection_scope(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM rfq_messages WHERE channel='email' AND (sent_at >= datetime('now','-24 hours') OR status='sending')").fetchone()[0]

    def claim(self, message_id, cap):
        """Reserve a rolling-cap slot with the approved-to-sending transition in one write transaction. Interrupted reservations remain visible and consume capacity."""
        with connection_scope(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            count = conn.execute("SELECT COUNT(*) FROM rfq_messages WHERE channel='email' AND (sent_at >= datetime('now','-24 hours') OR status='sending')").fetchone()[0]
            if count >= cap:
                return False
            return conn.execute("UPDATE rfq_messages SET status='sending' WHERE id=? AND status='approved' AND channel='email'", (message_id,)).rowcount == 1

    def edit_draft(self, message_id, values):
        if not values or not set(values) <= {'subject', 'body', 'recipient'}:
            raise ValueError('Only subject, body and recipient can be edited')
        with connection_scope(self.db_path) as conn:
            changed = conn.execute('UPDATE rfq_messages SET ' + ','.join((k + '=?' for k in values)) + " WHERE id=? AND status='draft'", (*values.values(), message_id)).rowcount
        if not changed:
            raise ValueError('Only drafts can be edited')

    def approve(self, campaign_id, message_ids=None):
        with connection_scope(self.db_path) as conn:
            sql = "UPDATE rfq_messages SET status='approved',approved_at=CURRENT_TIMESTAMP WHERE campaign_id=? AND channel='email' AND status='draft'"
            args = [campaign_id]
            if message_ids is not None:
                sql += ' AND id IN (' + ','.join(('?' for _ in message_ids)) + ')'
                args.extend(message_ids)
            return conn.execute(sql, args).rowcount
