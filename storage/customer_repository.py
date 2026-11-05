"""Customer (buy-side) persistence: profiles, enquiries, activity log, follow-up messages."""
import json

from storage.database import connection_scope

TABLES = {'customers', 'customer_enquiries', 'customer_activities', 'customer_messages'}
JSON_FIELDS = {'rfq_campaign_ids'}


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
                item[key] = json.loads(item[key] or '[]')
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

    def claim_send(self, message_id, cap):
        """approved -> sending inside one write transaction, honouring the rolling 24h cap."""
        with connection_scope(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            count = conn.execute(
                "SELECT COUNT(*) FROM customer_messages WHERE channel='email' AND "
                "(sent_at >= datetime('now','-24 hours') OR status='sending')").fetchone()[0]
            if count >= cap:
                return False
            return conn.execute(
                "UPDATE customer_messages SET status='sending' WHERE id=? AND status='approved' AND channel='email'",
                (message_id,)).rowcount == 1

    def daily_count(self):
        with connection_scope(self.db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM customer_messages WHERE channel='email' AND "
                "(sent_at >= datetime('now','-24 hours') OR status='sending')").fetchone()[0]


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
