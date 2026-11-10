"""Transactional, optimistic edits with append-only snapshots (UTF-8 byte limit)."""
from datetime import datetime, timezone
from storage.database import connection_scope

MAX_BYTES = 200 * 1024

class StatusConflict(ValueError):
    pass

class StatusRepository:
    def __init__(self, db_path=None):
        self.db_path = db_path

    def get(self, doc_id):
        with connection_scope(self.db_path) as conn:
            row = conn.execute('SELECT * FROM status_documents WHERE id=?', (doc_id,)).fetchone()
        if row is None:
            raise KeyError(doc_id)
        return dict(row)

    def save(self, doc_id, content_md, updated_by, expected_updated_at):
        if len(content_md.encode('utf-8')) > MAX_BYTES:
            raise ValueError('Status document exceeds 200 KB')
        if not doc_id.strip() or len(doc_id) > 120 or not updated_by.strip() or len(updated_by) > 120:
            raise ValueError('Document ID and author must be 1-120 characters')
        with connection_scope(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = conn.execute('SELECT updated_at FROM status_documents WHERE id=?', (doc_id,)).fetchone()
            if expected_updated_at != (current['updated_at'] if current else None):
                raise StatusConflict('Someone else updated this document. Reload before saving.')
            stamp = datetime.now(timezone.utc).isoformat(timespec='microseconds')
            conn.execute('INSERT INTO status_documents VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET content_md=excluded.content_md, updated_at=excluded.updated_at, updated_by=excluded.updated_by',
                         (doc_id, content_md, stamp, updated_by.strip()))
            conn.execute('INSERT INTO status_document_versions(doc_id,content_md,updated_at,updated_by) VALUES (?,?,?,?)',
                         (doc_id, content_md, stamp, updated_by.strip()))
        return self.get(doc_id)

    def versions(self, doc_id):
        self.get(doc_id)
        with connection_scope(self.db_path) as conn:
            return [dict(r) for r in conn.execute('SELECT updated_at,updated_by FROM status_document_versions WHERE doc_id=? ORDER BY id DESC LIMIT 20', (doc_id,))]
