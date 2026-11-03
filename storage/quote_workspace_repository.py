"""Persistent RFQ workspace. Quote revisions are append-only source records."""
import json
import uuid

from storage.database import connection_scope


class QuoteWorkspaceRepository:
    def __init__(self, db_path=None):
        self.db_path = db_path

    def list_rfqs(self):
        with connection_scope(self.db_path) as conn:
            return [dict(json.loads(r['payload']), id=r['id'], created_at=r['created_at'], quote_count=r['quote_count']) for r in conn.execute(
                'SELECT r.*, (SELECT count(*) FROM rfq_workspace_quotes q WHERE q.rfq_id=r.id) quote_count FROM rfqs r ORDER BY r.created_at DESC, r.rowid DESC')]

    def create_rfq(self, payload):
        rid = str(uuid.uuid4())
        with connection_scope(self.db_path) as conn:
            conn.execute('INSERT INTO rfqs(id,payload) VALUES (?,?)', (rid, json.dumps(payload)))
        return self.get_rfq(rid)

    def get_rfq(self, rid):
        with connection_scope(self.db_path) as conn:
            row = conn.execute('SELECT * FROM rfqs WHERE id=?', (rid,)).fetchone()
            if row is None:
                return None
            result = dict(json.loads(row['payload']), id=row['id'], created_at=row['created_at'])
            result['quotes'] = [dict(json.loads(q['payload']), id=q['id'], created_at=q['created_at']) for q in conn.execute(
                'SELECT * FROM rfq_workspace_quotes WHERE rfq_id=? ORDER BY created_at, rowid', (rid,))]
            return result

    def add_quote(self, rid, payload):
        qid = str(uuid.uuid4())
        with connection_scope(self.db_path) as conn:
            conn.execute('INSERT INTO rfq_workspace_quotes(id,rfq_id,payload) VALUES (?,?,?)', (qid, rid, json.dumps(payload)))
        return qid
