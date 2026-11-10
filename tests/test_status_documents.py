"""Versioned status edits against temporary SQLite, including authenticated API."""
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from storage.database import initialise_schema, connection_scope
from storage.status_repository import StatusRepository, StatusConflict, MAX_BYTES

@pytest.fixture
def repo(tmp_path):
    path = tmp_path / 'status.db'
    initialise_schema(path)
    return StatusRepository(path)


def test_snapshots_and_conflicts(repo):
    first = repo.save('live', '# First', 'Sai', None)
    second = repo.save('live', '**Second**', 'Claude', first['updated_at'])
    assert repo.get('live') == second
    assert repo.versions('live') == [dict(updated_at=second['updated_at'], updated_by='Claude'), dict(updated_at=first['updated_at'], updated_by='Sai')]
    with pytest.raises(StatusConflict):
        repo.save('live', 'stale', 'Phone', first['updated_at'])
    with pytest.raises(StatusConflict):
        repo.save('live', 'create twice', 'Phone', None)
    with connection_scope(repo.db_path) as c:
        assert [r[0] for r in c.execute('SELECT content_md FROM status_document_versions ORDER BY id')] == ['# First', '**Second**']
    assert repo.get('live') == second


def test_size_limit_is_utf8_bytes_and_atomic(repo):
    first = repo.save('live', 'x' * MAX_BYTES, 'Sai', None)
    with pytest.raises(ValueError, match='200 KB'):
        repo.save('live', '\u754c' * (MAX_BYTES // 3 + 1), 'Sai', first['updated_at'])
    assert len(repo.versions('live')) == 1


def test_history_last_twenty_and_document_isolation(repo):
    stamp = None
    for n in range(23):
        stamp = repo.save('live', str(n), 'Sai', stamp)['updated_at']
    repo.save('other', 'separate', 'Other', None)
    assert len(repo.versions('live')) == 20
    assert len(repo.versions('other')) == 1
    with pytest.raises(KeyError):
        repo.versions('missing')


def test_concurrent_edits_exactly_one_wins(repo):
    first = repo.save('live', 'initial', 'Sai', None)
    def edit(author):
        try:
            repo.save('live', author, author, first['updated_at'])
            return True
        except StatusConflict:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(edit, ['phone', 'laptop'])) == [False, True]
    assert len(repo.versions('live')) == 2


def test_api_auth_404_roundtrip_and_conflict(repo, monkeypatch):
    import api.app as mod
    import api.auth as auth
    from storage.repository import SupplierRepository
    monkeypatch.setattr(auth, 'API_ACCESS_TOKEN', 'status-test')
    mod.app.dependency_overrides[mod.get_repo] = lambda: SupplierRepository(repo.db_path)
    client = TestClient(mod.app)
    headers = {'Authorization': 'Bearer status-test'}
    body = dict(content_md='# Live', updated_by='Sai', expected_updated_at=None)
    try:
        for method, path in [('GET', '/status/live'), ('PUT', '/status/live'), ('GET', '/status/live/versions')]:
            assert client.request(method, path, json=body if method == 'PUT' else None).status_code == 401
        assert client.get('/status/live', headers=headers).status_code == 404
        assert client.get('/status/live/versions', headers=headers).status_code == 404
        response = client.put('/status/live', headers=headers, json=body)
        assert response.status_code == 200
        saved = response.json()
        assert client.get('/status/live', headers=headers).json() == saved
        assert client.put('/status/live', headers=headers, json=body).status_code == 409
        body.update(content_md='updated', expected_updated_at=saved['updated_at'])
        assert client.put('/status/live', headers=headers, json=body).status_code == 200
        assert len(client.get('/status/live/versions', headers=headers).json()) == 2
        body.update(content_md='\u754c' * 100000)
        assert client.put('/status/live', headers=headers, json=body).status_code == 413
        monkeypatch.setattr(auth, 'API_ACCESS_TOKEN', '')
        assert client.get('/status/live', headers=headers).status_code == 503
    finally:
        mod.app.dependency_overrides.clear()


def test_upgrade_from_v41_preserves_existing_customer_cards(tmp_path):
    from storage import database as db
    path = tmp_path / 'upgrade.db'
    # Build the previous customer shapes and mark its already-applied migrations.
    old = db.SCHEMA_SQL.replace(db.STATUS_SCHEMA_SQL, '')
    for fragment in ['mailbox TEXT, ', 'already_replied INTEGER NOT NULL DEFAULT 0, ', 'reply_checked_at TEXT, ', 'thread_id TEXT, ']:
        old = old.replace(fragment, '')
    with db.connection_scope(path) as c:
        c.executescript(old)
        for version in range(1, 42):
            c.execute('INSERT INTO schema_migrations(version,description) VALUES (?,?)', (version, 'previous'))
        c.execute("INSERT INTO customer_inbox_leads(external_id,from_address,subject) VALUES ('cust:old','old@example.test','Existing')")
    db.initialise_schema(path)
    db.initialise_schema(path)
    with db.connection_scope(path) as c:
        row = dict(c.execute('SELECT * FROM customer_inbox_leads').fetchone())
        assert row['subject'] == 'Existing' and row['mailbox'] is None and row['already_replied'] == 0
    assert db.get_schema_version(path) == db.SCHEMA_VERSION
    assert StatusRepository(path).save('live', 'new document', 'Sai', None)['content_md'] == 'new document'
