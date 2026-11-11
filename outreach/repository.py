"""Small parameterised repository; no network access."""
from storage.database import connection_scope
from config.settings import DB_PATH


class OutreachRepository:
    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH

    def rows(self, sql, args=()):
        with connection_scope(self.db_path) as c:
            return [dict(r) for r in c.execute(sql, args)]

    def execute(self, sql, args=()):
        with connection_scope(self.db_path) as c:
            return c.execute(sql, args).lastrowid

    def get(self, table, ident):
        if table not in {'prospect_accounts', 'prospect_contacts', 'outreach_sequences',
                         'outreach_steps', 'outreach_enrolments', 'outreach_events'}:
            raise ValueError('Unsupported table')
        rows = self.rows(f'SELECT * FROM {table} WHERE id=?', (ident,))
        if not rows:
            raise ValueError('Record not found')
        return rows[0]
