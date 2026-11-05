"""Customer profiles, enquiries, activity log and approval-gated follow-ups. Fakes only."""
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from config import settings as cfg
from customers.customer_service import CustomerService
from storage.customer_repository import CustomerRepository
from storage.database import initialise_schema


class FakeSender:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, message):
        if self.fail:
            raise RuntimeError('boom')
        self.sent.append(message)
        return f'zoho:{len(self.sent)}'


class FakeInbox:
    def __init__(self):
        self.raw, self.watched = [], None

    def fetch_for_campaign(self, since_days, limit, messages):
        self.watched = messages
        return self.raw


def mail(sender, subject, body, message_id, **headers):
    m = EmailMessage()
    m['From'], m['To'], m['Subject'], m['Message-ID'] = sender, 'sai@ikeng.online', subject, message_id
    for k, v in headers.items():
        m[k.replace('_', '-')] = v
    m.set_content(body)
    return m.as_bytes()


@pytest.fixture
def svc(tmp_path, monkeypatch):
    db = tmp_path / 'c.db'
    initialise_schema(db)
    monkeypatch.setattr(cfg, 'RFQ_SEND_ENABLED', True)
    monkeypatch.setattr(cfg, 'CUSTOMER_DAILY_SEND_LIMIT', 2)
    monkeypatch.setattr(cfg, 'RFQ_BUYER_NAME', 'Sai Jyothis')
    monkeypatch.setattr(cfg, 'RFQ_BUYER_COMPANY', 'IK Eng Ltd')
    monkeypatch.setattr(cfg, 'RFQ_FROM_ADDRESS', 'sai@ikeng.online')
    s = CustomerService(repo=CustomerRepository(db), sender=FakeSender(), inbox=FakeInbox(), readiness=lambda: [])
    customer = s.create_customer(name='Colin Fennelly', email='colin@example.com', phone='+44 7700 900123')
    enquiry = s.create_enquiry(customer['id'], title='kitchen and wardrobes', status='active',
                               next_action='INTERNAL: no customer price release', next_action_due='2026-01-01',
                               blocker='INTERNAL blocker')
    s.c, s.e = customer, enquiry
    return s


def test_enquiry_gets_reference_and_creation_logged(svc):
    assert svc.e['reference'].startswith('IKE-') and len(svc.e['reference']) == 10
    assert [a['kind'] for a in svc.get_enquiry(svc.e['id'])['activities']] == ['note']


def test_status_change_is_logged(svc):
    svc.update_enquiry(svc.e['id'], status='quoting')
    kinds = [a['summary'] for a in svc.get_enquiry(svc.e['id'])['activities']]
    assert 'Status: active -> quoting' in kinds


def test_due_list_shows_overdue_and_hides_closed(svc):
    due = svc.list_enquiries(due_only=True)
    assert [e['id'] for e in due] == [svc.e['id']] and due[0]['customer_name'] == 'Colin Fennelly'
    svc.update_enquiry(svc.e['id'], status='won')
    assert svc.list_enquiries(due_only=True) == []
    assert svc.list_enquiries(include_closed=True)[0]['status'] == 'won'


def test_followup_draft_never_leaks_internal_notes(svc):
    m = svc.draft_followup(svc.e['id'])
    assert m['status'] == 'draft' and m['recipient'] == 'colin@example.com'
    assert 'INTERNAL' not in m['body'] and 'INTERNAL' not in m['subject']
    assert svc.e['reference'] in m['body'] and svc.e['reference'] in m['subject']
    assert m['body'].startswith('Hi Colin,') and 'Sai Jyothis' in m['body']


def test_nothing_sends_without_approval(svc):
    m = svc.draft_followup(svc.e['id'])
    with pytest.raises(ValueError, match='approved'):
        svc.send(m['id'])
    assert svc._sender.sent == []


def test_approved_email_sends_once_and_logs(svc):
    m = svc.approve(svc.draft_followup(svc.e['id'])['id'])
    sent = svc.send(m['id'])
    assert sent['status'] == 'sent' and sent['provider_message_id'] == 'zoho:1'
    with pytest.raises(ValueError):
        svc.send(m['id'])
    assert len(svc._sender.sent) == 1
    enquiry = svc.get_enquiry(svc.e['id'])
    assert enquiry['last_our_contact_at'] and enquiry['activities'][0]['kind'] == 'email_out'


def test_send_blocked_when_sending_disabled(svc, monkeypatch):
    monkeypatch.setattr(cfg, 'RFQ_SEND_ENABLED', False)
    m = svc.approve(svc.draft_followup(svc.e['id'])['id'])
    with pytest.raises(ValueError, match='RFQ_SEND_ENABLED'):
        svc.send(m['id'])
    assert svc._sender.sent == []


def test_daily_cap(svc):
    for _ in range(2):
        svc.send(svc.approve(svc.draft_followup(svc.e['id'])['id'])['id'])
    m = svc.approve(svc.draft_followup(svc.e['id'])['id'])
    with pytest.raises(ValueError, match='cap'):
        svc.send(m['id'])
    assert len(svc._sender.sent) == 2


def test_failed_send_is_not_retried(svc):
    svc._sender.fail = True
    m = svc.approve(svc.draft_followup(svc.e['id'])['id'])
    with pytest.raises(ValueError, match='Sent folder'):
        svc.send(m['id'])
    assert svc.repo.get('customer_messages', m['id'])['status'] == 'failed'


def test_customer_reply_cancels_pending_followups(svc):
    draft = svc.draft_followup(svc.e['id'])
    approved = svc.approve(svc.draft_followup(svc.e['id'])['id'])
    svc.log(svc.e['id'], 'whatsapp_in', 'Customer sent new plans on WhatsApp')
    for m in (draft, approved):
        row = svc.repo.get('customer_messages', m['id'])
        assert row['status'] == 'cancelled' and row['status_reason'] == 'Customer replied'


def test_inbox_reply_is_logged_and_stops_followups(svc):
    m = svc.approve(svc.draft_followup(svc.e['id'])['id'])
    svc._inbox.raw = [
        mail('Colin <colin@example.com>', 'Re: kitchens', 'Here are the drawings.\n\nOn Mon wrote:\n> old', '<a@x>'),
        mail('colin@example.com', 'Automatic reply: away', 'I am away', '<b@x>'),
        mail('stranger@example.com', 'Hello', 'Unrelated', '<c@x>'),
    ]
    assert svc.poll_inbox() == {'logged': 1, 'ignored': 2, 'duplicates': 0}
    assert {'channel': 'email', 'recipient': 'colin@example.com', 'reference': ''} in svc._inbox.watched
    acts = svc.get_enquiry(svc.e['id'])['activities']
    assert acts[0]['kind'] == 'email_in' and 'Here are the drawings.' in acts[0]['summary']
    assert '> old' not in acts[0]['summary']
    assert svc.repo.get('customer_messages', m['id'])['status'] == 'cancelled'
    assert svc.poll_inbox()['duplicates'] == 1


def test_reply_matched_by_reference_from_other_address(svc):
    svc._inbox.raw = [mail('assistant@example.org', f"Re: Your enquiry [{svc.e['reference']}]", 'Plans attached', '<d@x>')]
    assert svc.poll_inbox()['logged'] == 1


def test_whatsapp_followup_is_human_sent(svc):
    m = svc.draft_followup(svc.e['id'], channel='whatsapp')
    assert m['action_url'].startswith('https://wa.me/447700900123?text=')
    with pytest.raises(ValueError, match='by hand'):
        svc.send(m['id'])
    done = svc.mark_sent(m['id'])
    assert done['status'] == 'sent'
    assert svc.get_enquiry(svc.e['id'])['activities'][0]['kind'] == 'whatsapp_out'


def test_closed_enquiry_cannot_be_followed_up(svc):
    svc.update_enquiry(svc.e['id'], status='lost')
    with pytest.raises(ValueError, match='closed'):
        svc.draft_followup(svc.e['id'])


def test_validation(svc):
    with pytest.raises(ValueError):
        svc.create_customer(name='X', email='not-an-email')
    with pytest.raises(ValueError):
        svc.create_enquiry(svc.c['id'], title='t', reference='BAD')
    with pytest.raises(ValueError):
        svc.log(svc.e['id'], 'telepathy', 'x')
    with pytest.raises(ValueError):
        svc.update_enquiry(svc.e['id'], bogus=1)


def test_api_round_trip(svc, monkeypatch):
    import api.app as mod
    import api.auth
    from api.customers import get_customer_service
    monkeypatch.setattr(api.auth, 'API_ACCESS_TOKEN', 'cust-test')
    mod.app.dependency_overrides[get_customer_service] = lambda: svc
    try:
        client = TestClient(mod.app)
        assert client.get('/customers').status_code == 401
        client.headers['Authorization'] = 'Bearer cust-test'
        c = client.post('/customers', json={'name': 'Daniel', 'email': 'dan@example.com'}).json()
        e = client.post(f"/customers/{c['id']}/enquiries", json={'title': 'Manchester renovation'}).json()
        assert e['status'] == 'new'
        assert client.patch(f"/customers/enquiries/{e['id']}", json={'status': 'qualifying'}).json()['status'] == 'qualifying'
        assert client.post(f"/customers/enquiries/{e['id']}/activities", json={'kind': 'call_out', 'summary': 'Called'}).status_code == 200
        m = client.post(f"/customers/enquiries/{e['id']}/followups", json={}).json()
        assert client.patch(f"/customers/messages/{m['id']}", json={'body': 'Hi Daniel'}).json()['body'].endswith(e['reference'])
        assert client.post(f"/customers/messages/{m['id']}/approve").json()['status'] == 'approved'
        assert client.post(f"/customers/messages/{m['id']}/send").json()['status'] == 'sent'
        assert client.get(f"/customers/{c['id']}").json()['enquiries'][0]['id'] == e['id']
        assert client.get('/customers/enquiries/999').status_code == 404
        assert client.post('/customers', json={'name': 'Y', 'hacker': 1}).status_code == 422
    finally:
        mod.app.dependency_overrides.pop(get_customer_service, None)
