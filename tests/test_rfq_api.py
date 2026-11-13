"""HTTP + real SQLite integration tests; never use the production database."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.auth
from api.rfq import router, get_rfq_repo
from storage.database import initialise_schema, connection_scope, get_schema_version
from storage.quote_workspace_repository import QuoteWorkspaceRepository


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    path = tmp_path / 'workspace.db'
    initialise_schema(path)
    monkeypatch.setattr(api.auth, 'API_ACCESS_TOKEN', 'rfq-test')
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_rfq_repo] = lambda: QuoteWorkspaceRepository(path)
    return TestClient(app), path


AUTH = {'Authorization': 'Bearer rfq-test'}


def create(client):
    response = client.post('/rfqs', headers=AUTH, json={
        'title': 'LED lighting', 'customer': 'IK ENG', 'quantity': '100',
        'mandatory_specs': {'material': 'aluminium'},
    })
    assert response.status_code == 201, response.text
    return response.json()['id']


def quote():
    fields = {'unit_price': 18, 'currency': 'USD', 'quoted_quantity': 100, 'moq': 50,
              'incoterm': 'FOB', 'named_place': 'Shenzhen', 'production_lead_days': 25,
              'payment_terms': '30/70', 'valid_until': '2099-12-31'}
    text = '\n'.join(f'{k}: {v}' for k, v in fields.items()) + '\nmaterial: aluminium'
    return {'supplier_name': 'Factory A', 'source_text': text, 'fields': fields,
            'quoted_specs': {'material': 'aluminium'},
            'evidence': {**{k: f'{k}: {v}' for k, v in fields.items()}, 'spec:material': 'material: aluminium'}}


@pytest.mark.parametrize('method,path', [('get','/rfqs'),('post','/rfqs'),('get','/rfqs/nope'),('post','/rfqs/nope/quotes')])
def test_all_routes_fail_closed(workspace, monkeypatch, method, path):
    c, _ = workspace
    response = c.post(path, json={}) if method == 'post' else c.get(path)
    assert response.status_code == 401
    monkeypatch.setattr(api.auth, 'API_ACCESS_TOKEN', '')
    assert c.get('/rfqs', headers=AUTH).status_code == 503


def test_workflow_persists_and_keeps_revisions(workspace):
    c, path = workspace
    rid = create(c)
    q = quote()
    response = c.post(f'/rfqs/{rid}/quotes', headers=AUTH, json=q)
    assert response.status_code == 201, response.text
    saved = response.json()['quotes'][0]
    assert saved['review']['status'] == 'comparable'
    assert saved['clarification_zh'] == ''
    q['fields']['moq'] = None
    q['evidence'].pop('moq')
    q['supplier_name'] = 'Factory B'
    response = c.post(f'/rfqs/{rid}/quotes', headers=AUTH, json=q)
    second = response.json()['quotes'][1]
    assert second['review']['missing_fields'] == ['moq']
    assert '最小起订量' in second['clarification_zh']
    assert '付款条件' not in second['clarification_zh']
    stored = QuoteWorkspaceRepository(path).get_rfq(rid)
    assert len(stored['quotes']) == 2
    assert stored['quotes'][0]['source_text'] == quote()['source_text']
    assert c.get('/rfqs', headers=AUTH).json()[0]['quote_count'] == 2


def test_evidence_must_exist_and_missing_evidence_blocks(workspace):
    c, _ = workspace
    rid = create(c)
    q = quote()
    q['evidence']['moq'] = 'Invented evidence'
    assert c.post(f'/rfqs/{rid}/quotes', headers=AUTH, json=q).status_code == 422
    q['evidence']['moq'] = '   '
    assert c.post(f'/rfqs/{rid}/quotes', headers=AUTH, json=q).status_code == 422
    q['evidence'].pop('moq')
    r = c.post(f'/rfqs/{rid}/quotes', headers=AUTH, json=q).json()['quotes'][0]
    assert r['review']['missing_fields'] == ['moq_evidence']
    assert r['review']['status'] == 'clarification_required'


def test_deviations_and_expired_quotes(workspace):
    c, _ = workspace
    rid = create(c)
    q = quote()
    q['quoted_specs']['material'] = 'steel'
    q['fields']['valid_until'] = '2000-01-01'
    q['evidence'].pop('valid_until')
    q['evidence'].pop('spec:material')
    result = c.post(f'/rfqs/{rid}/quotes', headers=AUTH, json=q).json()['quotes'][0]
    assert result['review']['status'] == 'non_compliant'
    assert '报价已过有效期' in result['clarification_zh']
    assert 'aluminium' in result['clarification_zh'] and 'steel' in result['clarification_zh']


def test_validation_and_not_found(workspace):
    c, _ = workspace
    assert c.post('/rfqs', headers=AUTH, json={'title':' ', 'quantity':0}).status_code == 422
    assert c.get('/rfqs/nope', headers=AUTH).status_code == 404
    assert c.post('/rfqs/nope/quotes', headers=AUTH, json=quote()).status_code == 404
    rid = create(c)
    q = quote(); q['fields']['moq'] = True
    assert c.post(f'/rfqs/{rid}/quotes', headers=AUTH, json=q).status_code == 422


def test_migration_idempotent_and_retains_supplier(workspace):
    _, path = workspace
    with connection_scope(path) as conn:
        conn.execute("INSERT INTO suppliers(canonical_name) VALUES ('Existing supplier')")
        conn.execute("INSERT INTO rfq_campaigns(title,product) VALUES ('Existing outreach','Lighting')")
        conn.execute("INSERT INTO rfq_quotes(unit_price,currency) VALUES (42,'USD')")
        conn.execute('DROP TABLE rfq_workspace_quotes'); conn.execute('DROP TABLE rfqs')
        conn.execute('DELETE FROM schema_migrations WHERE version=35')
    initialise_schema(path); initialise_schema(path)
    assert get_schema_version(path) == 49
    with connection_scope(path) as conn:
        assert conn.execute("SELECT count(*) FROM suppliers WHERE canonical_name='Existing supplier'").fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM rfqs').fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM rfq_campaigns WHERE title='Existing outreach'").fetchone()[0] == 1
        assert conn.execute("SELECT unit_price FROM rfq_quotes").fetchone()[0] == 42
