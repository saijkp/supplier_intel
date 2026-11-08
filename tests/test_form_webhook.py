"""Signed outgoing Netlify submissions; invented customers, isolated local DB."""
import base64
import hashlib
import hmac
import json
import time
from email.message import EmailMessage

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import form_webhook as hook
from api.customers import get_customer_service, router as customers_router
from config import settings as cfg
from customers.customer_service import CustomerService
from customers.form_parser import parse_netlify_form
from storage.customer_repository import CustomerRepository
from storage.database import initialise_schema

SECRET = 'fabricated-local-signing-secret'


def signed(raw, **claims):
    def enc(v):
        return base64.urlsafe_b64encode(json.dumps(v).encode()).rstrip(b'=').decode()
    header = enc({'alg': claims.pop('alg', 'HS256'), 'typ': 'JWT'})
    body = enc(dict(iss='netlify', sha256=hashlib.sha256(raw).hexdigest()) | claims)
    signature = base64.urlsafe_b64encode(hmac.new(SECRET.encode(),
                        (header + '.' + body).encode(), hashlib.sha256).digest()).rstrip(b'=').decode()
    return header + '.' + body + '.' + signature


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / 'forms.db'
    initialise_schema(path)
    service = CustomerService(repo=CustomerRepository(path))
    app = FastAPI()
    app.include_router(hook.router)
    app.include_router(customers_router)
    app.dependency_overrides[get_customer_service] = lambda: service
    monkeypatch.setattr(cfg, 'NETLIFY_FORM_WEBHOOK_SECRET', SECRET)
    monkeypatch.setattr(cfg, 'NETLIFY_FORM_NAMES', ('sourcing-brief',))
    hook._buckets.clear()
    with TestClient(app) as client:
        yield client, service


@pytest.fixture
def payload():
    return {'id': 'aaaaaaaaaaaaaaaaaaaaaaaa', 'form_name': 'sourcing-brief',
            'form_id': '6a987868bb07e60008e0b59f', 'data': {
                'name': 'Alex Example', 'email': 'alex@example.com', 'phone': '+44 7700 900222',
                'company': 'Example Projects', 'location': 'Salford', 'cat': ['Kitchen cabinets'],
                'finish_level': 'Premium', 'description': 'Six oak cabinets',
                'design_support': 'Yes', 'delivery_date': '2027-04-01', 'budget': '5000 GBP',
                'references': 'https://example.com/plan', 'materials': 'Oak', 'dimensions': '600mm',
                'notes': 'Narrow access', 'lead_source': 'direct', 'project_name': 'Example House',
                'role': 'Homeowner', 'units': '1', 'ptype': 'Refurbishment', 'bot-field': ''}}


def post(client, payload, **claims):
    raw = json.dumps(payload, ensure_ascii=False).encode()
    return client.post('/customers/form-webhook', content=raw,
                       headers={'X-Webhook-Signature': signed(raw, **claims)})


def test_valid_public_webhook_card_and_conversion(setup, payload):
    client, service = setup
    assert post(client, payload).json() == {'status': 'possible_new_enquiries'}
    lead = service.list_leads()[0]
    assert lead['external_id'] == 'cust:netlify:' + payload['id']
    assert lead['from_address'] == 'alex@example.com'
    assert lead['form_fields'] == {
        'name': 'Alex Example', 'email': 'alex@example.com', 'phone': '+44 7700 900222',
        'company': 'Example Projects', 'location': 'Salford', 'categories': ['Kitchen cabinets'],
        'finish_level': 'Premium', 'requirement': 'Six oak cabinets', 'design_support': 'Yes',
        'required_date': '2027-04-01', 'budget': '5000 GBP', 'references': 'https://example.com/plan',
        'materials': 'Oak', 'dimensions': '600mm', 'anything_else': 'Narrow access',
        'lead_source': 'direct', 'project_name': 'Example House'}
    assert json.loads(lead['full_text']) == payload
    assert service.repo.rows('customers') == service.repo.rows('customer_enquiries') == []
    assert service.repo.rows('customer_messages') == []
    assert post(client, payload).json()['status'] == 'duplicates'
    assert len(service.list_leads()) == 1
    enquiry = service.convert_lead(lead['id'])
    assert enquiry['qualification']['timeline'] == '2027-04-01'
    assert post(client, payload).json()['status'] == 'duplicates'


def test_existing_open_enquiry_blanks_only_and_cancels_drafts(setup, payload):
    client, service = setup
    customer = service.create_customer(name='Trusted Name', email='alex@example.com', phone='trusted')
    enquiry = service.create_enquiry(customer['id'], title='Kitchen',
                qualification={'location': 'London', 'budget': 'trusted'}, requirements='Original')
    draft = service.draft_followup(enquiry['id'])
    assert post(client, payload).json()['status'] == 'logged'
    assert post(client, payload).json()['status'] == 'duplicates'
    updated = service.get_enquiry(enquiry['id'])
    assert updated['qualification'] == {'location': 'London', 'budget': 'trusted',
        'timeline': '2027-04-01', 'scope': 'Kitchen cabinets\nSix oak cabinets'}
    assert updated['requirements'] == 'Original'
    profile = service.get_customer(customer['id'])
    assert profile['name'] == 'Trusted Name' and profile['phone'] == 'trusted'
    assert profile['company'] == 'Example Projects'
    assert service.repo.get('customer_messages', draft['id'])['status'] == 'cancelled'
    assert len([a for a in updated['activities'] if a['kind'] == 'email_in']) == 1
    assert service.list_leads() == []


@pytest.mark.parametrize('claims', [dict(iss='other'), dict(sha256='0'*64),
    dict(exp=0), dict(nbf=time.time()+3600), dict(exp='tomorrow'), dict(exp=float('nan')),
    dict(alg='none'), dict(sha256=None), dict(exp=10**400)])
def test_invalid_claims(setup, payload, claims):
    client, service = setup
    assert post(client, payload, **claims).status_code == 401
    assert service.list_leads() == []


def test_bad_missing_signature_raw_bytes_and_missing_secret(setup, payload, monkeypatch):
    client, service = setup
    raw = json.dumps(payload).encode()
    for token in ['', 'invalid', signed(raw)[:-4] + 'xxxx']:
        assert client.post('/customers/form-webhook', content=raw,
                           headers={'X-Webhook-Signature': token}).status_code == 401
    assert client.post('/customers/form-webhook', content=raw + b' ',
                      headers={'X-Webhook-Signature': signed(raw)}).status_code == 401
    monkeypatch.setattr(cfg, 'NETLIFY_FORM_WEBHOOK_SECRET', '')
    assert post(client, payload).status_code == 503
    assert service.list_leads() == []


@pytest.mark.parametrize('change', ['honeypot', 'form', 'event'])
def test_ignored(setup, payload, change):
    client, service = setup
    if change == 'honeypot': payload['data']['bot-field'] = 'robot'
    if change == 'form': payload['form_name'] = 'guide-download'
    if change == 'event': payload['event'] = 'submission_deleted'
    assert post(client, payload).json() == {'status': 'ignored'}
    assert service.list_leads() == service.repo.rows('customer_activities') == []


def test_body_limit_and_rate_limit(setup, payload):
    client, service = setup
    assert client.post('/customers/form-webhook', content=b'x' * (hook.MAX_BODY + 1)).status_code == 413
    hook._buckets.clear()
    for _ in range(hook.RATE_LIMIT):
        assert client.post('/customers/form-webhook', content=b'{}').status_code == 401
    assert post(client, payload).status_code == 429
    assert service.list_leads() == []


def test_human_fields_fallback_and_raw_blank_precedence():
    payload = {'human_fields': {'Full name *': 'Ann Example', 'Email': 'ann@example.com',
                  'Cat': '["Beds", "Lighting"]', 'Required on-site date': '2027-01-01',
                  'What do you need?': 'Two beds', 'Anything else': 'Access'},
               'data': {'name': ''}}
    fields = parse_netlify_form(payload)
    assert fields['name'] == '' and fields['email'] == 'ann@example.com'
    assert fields['categories'] == ['Beds', 'Lighting']
    assert fields['required_date'] == '2027-01-01' and fields['requirement'] == 'Two beds'
    assert fields['anything_else'] == 'Access'
    assert parse_netlify_form({'ordered_human_fields': [{'name': 'name', 'value': 'Ann'}]})['name'] == 'Ann'


def notification(payload):
    # Same shared explicit values, in Netlify's labelled email representation.
    from customers.form_parser import LABELS
    fields = parse_netlify_form(payload)
    lines = []
    for label, key in LABELS.items():
        if key:
            value = fields[key]
            lines.append(label + ': ' + (json.dumps(value) if isinstance(value, list) else value))
    lines.insert(-1, 'What do you need?: ' + fields['requirement'])
    return '\n'.join(lines)


@pytest.mark.parametrize('email_first', [False, True])
@pytest.mark.parametrize('known', [False, True])
def test_cross_transport_dedupe_in_both_orders(setup, payload, email_first, known):
    client, service = setup
    if known:
        c = service.create_customer(name='Alex', email='alex@example.com')
        service.create_enquiry(c['id'], title='Existing')
    body = notification(payload)
    from customers.form_parser import parse_form
    def email():
        return service.store_form_submission(parse_form(body), body, 'cust:form:fakehash', 'Form submission')
    outcomes = [email(), post(client, payload).json()['status']] if email_first else [
        post(client, payload).json()['status'], email()]
    assert outcomes == ['logged' if known else 'possible_new_enquiries', 'duplicates']
    assert len(service.list_leads()) == (0 if known else 1)


def test_different_submission_ids_same_transport_stay_distinct(setup, payload):
    client, service = setup
    assert post(client, payload).json()['status'] == 'possible_new_enquiries'
    payload['id'] = 'bbbbbbbbbbbbbbbbbbbbbbbb'
    assert post(client, payload).json()['status'] == 'possible_new_enquiries'
    assert len(service.list_leads()) == 2


def test_duplicate_receipt_id_remembered_after_cross_transport_match(setup, payload):
    client, service = setup
    from customers.form_parser import parse_form
    body = notification(payload)
    service.store_form_submission(parse_form(body), body, 'cust:form:email-first', 'Form submission')
    assert post(client, payload).json()['status'] == 'duplicates'
    payload['data']['description'] = 'Redelivered with changed text'
    assert post(client, payload).json()['status'] == 'duplicates'
    assert len(service.list_leads()) == 1


def test_email_with_submission_id_after_conversion(setup, payload):
    client, service = setup
    post(client, payload)
    service.convert_lead(service.list_leads()[0]['id'])
    m = EmailMessage()
    m['From'] = 'formresponses@netlify.com'
    m['Subject'] = 'Form submission from sourcing-brief form: [netlify:' + payload['id'] + ']'
    m['Message-ID'] = '<invented-notification@example.com>'
    m.set_content('Full name: Alex Example Email: alex@example.com')
    class Inbox:
        def fetch_recent(self, *args): return [m.as_bytes()]
    service._inbox = Inbox()
    service._sender = object()
    assert service.poll_inbox()['duplicates'] == 1
    assert len(service.repo.rows('customer_inbox_leads')) == 1
    assert len([a for a in service.repo.rows('customer_activities') if a['kind'] == 'email_in']) == 1


def test_existing_customer_without_open_enquiry_gets_review_card(setup, payload):
    client, service = setup
    service.create_customer(name='Alex', email='alex@example.com')
    assert post(client, payload).json()['status'] == 'possible_new_enquiries'
    assert service.repo.rows('customer_enquiries') == []
    assert len(service.repo.rows('customers')) == 1


def test_production_router_auth_boundary(setup, payload, monkeypatch):
    _, service = setup
    from api.app import app
    from api import auth
    monkeypatch.setattr(auth, 'API_ACCESS_TOKEN', 'private-token')
    app.dependency_overrides[get_customer_service] = lambda: service
    try:
        client = TestClient(app)  # no lifespan, background inbox or production DB
        assert post(client, payload).status_code == 200
        assert client.get('/customers/leads').status_code == 401
    finally:
        app.dependency_overrides.pop(get_customer_service, None)


@pytest.mark.parametrize('payload', [{}, [], {'form_name': 'sourcing-brief'},
    {'form_name': 'sourcing-brief', 'id': 'abc', 'data': []}])
def test_malformed_signed_json(setup, payload):
    client, service = setup
    response = post(client, payload)
    assert response.status_code == (200 if payload == {} else 400)
    assert service.list_leads() == []


def test_migration_41_preserves_old_records(tmp_path, monkeypatch):
    import storage.database as db
    path = tmp_path / 'migration.db'
    statement = 'CREATE TABLE IF NOT EXISTS customer_form_receipts (external_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);'
    with monkeypatch.context() as patch:
        patch.setattr(db, 'SCHEMA_SQL', db.SCHEMA_SQL.replace(statement, ''))
        patch.setattr(db, 'MIGRATIONS', {k: v for k, v in db.MIGRATIONS.items() if k < 41})
        db.initialise_schema(path)
    repo = CustomerRepository(path)
    lead = repo.insert('customer_inbox_leads', external_id='cust:form:legacy',
        from_address='legacy@example.com', full_text='Original', received_at='2026-11-01')
    db.initialise_schema(path)
    db.initialise_schema(path)
    assert repo.get('customer_inbox_leads', lead)['full_text'] == 'Original'
    repo.remember_form('cust:netlify:abc', 'fingerprint')
    assert repo.form_seen('cust:netlify:abc', 'different', 'cust:form:%')


def test_human_fields_honeypot_is_rejected(setup, payload):
    client, service = setup
    payload.pop('data')
    payload['human_fields'] = {'Full name': 'Alex', 'Email': 'alex@example.com',
                               'Leave this empty': 'robot'}
    assert post(client, payload).json()['status'] == 'ignored'
    assert service.list_leads() == []
