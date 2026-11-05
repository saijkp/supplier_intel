"""Durable inbound review queue. Never calls a provider or quote extractor."""
from storage.database import connection_scope

class WhatsAppRepository:
    def __init__(self, db_path=None):
        self.db_path = db_path

    def store(self, messages):
        with connection_scope(self.db_path) as conn:
            # Purge private content after 30 days, retaining only idempotency tombstones.
            self._purge(conn)
            count = 0
            for message in messages:
                count += conn.execute(
                    'INSERT INTO whatsapp_inbound(provider_id,sender,kind,body,sent_at) VALUES (?,?,?,?,?) ON CONFLICT(provider_id) DO NOTHING',
                    tuple(message[k] for k in ('provider_id','sender','kind','body','sent_at'))).rowcount
            return count

    @staticmethod
    def _purge(conn):
        conn.execute("UPDATE whatsapp_inbound SET sender=NULL,body=NULL,sent_at=NULL,supplier_id=NULL,rfq_id=NULL,reviewed_at=NULL WHERE received_at < datetime('now','-30 days') AND sender IS NOT NULL")

    def list(self, after=0):
        with connection_scope(self.db_path) as conn:
            self._purge(conn)
            return [dict(r) for r in conn.execute('SELECT * FROM whatsapp_inbound WHERE sender IS NOT NULL AND id>? ORDER BY id LIMIT 100', (after,))]

    def associate(self, mid, supplier_id, rfq_id):
        with connection_scope(self.db_path) as conn:
            self._purge(conn)
            if not conn.execute('SELECT 1 FROM suppliers WHERE id=? AND flagged=0',(supplier_id,)).fetchone():
                raise ValueError('Supplier not found or flagged')
            if not conn.execute('SELECT 1 FROM rfqs WHERE id=?',(rfq_id,)).fetchone():
                raise ValueError('RFQ not found')
            if not conn.execute('UPDATE whatsapp_inbound SET supplier_id=?,rfq_id=?,reviewed_at=CURRENT_TIMESTAMP WHERE id=? AND sender IS NOT NULL',(supplier_id,rfq_id,mid)).rowcount:
                raise ValueError('Message not found or expired')
