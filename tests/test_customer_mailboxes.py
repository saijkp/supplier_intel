"""Two-account intake and approved sending. No network, no credentials."""
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import pytest
from config import settings as cfg
from customers.customer_service import CustomerService
from customers.mailboxes import Mailbox, replied, customer_mailbox
from storage.customer_repository import CustomerRepository
from storage.database import initialise_schema
from rfq.zoho_mail import ZohoInbox, ZohoClient

PRIMARY = 'sai@ikeng.online'
SECOND = 'sai@ikeng.co.uk'

class Inbox:
    def __init__(self):
        self.raw, self.sent_items = [], []
        self.fail = False
    def fetch_recent(self, *args):
        if self.fail:
            raise RuntimeError('private provider error')
        return self.raw
    def fetch_sent(self, days):
        if self.fail:
            raise RuntimeError('private provider error')
        return self.sent_items

class Sender:
    def __init__(self):
        self.sent = []
    def send(self, message):
        self.sent.append(message)
        return 'zoho:local' + str(len(self.sent))


def inbound(address, mid, subject='Kitchen', days=1):
    m = EmailMessage()
    m['From'], m['To'], m['Subject'], m['Message-ID'] = address, SECOND, subject, mid
    from email.utils import format_datetime
    m['Date'] = format_datetime(datetime.now(timezone.utc) - timedelta(days=days))
    m['X-SupplierIntel-Thread'] = 'thread1'
    m.set_content('Please quote for my kitchen in Salford.')
    return m.as_bytes()


def sent(address='new@example.test', subject='Re: Kitchen', when=None, thread='thread1', mid='1'):
    return dict(messageId=mid, toAddress=address, subject=subject, threadId=thread,
                sentDateInGMT=str(int((when or datetime.now(timezone.utc)).timestamp()*1000)))

@pytest.fixture
def service(tmp_path, monkeypatch):
    path = tmp_path / 'mailboxes.db'
    initialise_schema(path)
    monkeypatch.setattr(cfg, 'RFQ_FROM_ADDRESS', PRIMARY)
    monkeypatch.setattr(cfg, 'RFQ_SEND_ENABLED', True)
    boxes = {address: Mailbox(address, Sender(), Inbox()) for address in (PRIMARY, SECOND)}
    return CustomerService(CustomerRepository(path), readiness=lambda: [], mailboxes=boxes)


def test_two_mailboxes_attribution_dedup_and_replied_refresh(service):
    boxes = service.mailboxes()
    boxes[PRIMARY].inbox.raw = [inbound('first@example.test', '<first>')]
    boxes[SECOND].inbox.raw = [inbound('new@example.test', '<second>'), inbound('first@example.test', '<first>')]
    result = service.poll_inbox()
    assert result['possible_new_enquiries'] == 2 and result['duplicates'] == 1
    cards = {c['from_address']: c for c in service.list_leads()}
    assert cards['first@example.test']['mailbox'] == PRIMARY
    assert cards['new@example.test']['mailbox'] == SECOND
    assert not cards['new@example.test']['already_replied']
    boxes[SECOND].inbox.sent_items = [sent()]
    service.poll_inbox()
    assert next(c for c in service.list_leads() if c['mailbox'] == SECOND)['already_replied'] == 1
    # A reply in the second account must never mark the first account's card.
    assert next(c for c in service.list_leads() if c['mailbox'] == PRIMARY)['already_replied'] == 0


def test_same_sender_can_have_one_card_per_mailbox(service):
    for i, box in enumerate(service.mailboxes().values()):
        box.inbox.raw = [inbound('shared@example.test', f'<{i}>')]
    assert service.poll_inbox()['possible_new_enquiries'] == 2


def draft(service, address=SECOND):
    service.mailboxes()[address].inbox.raw = [inbound('new@example.test', '<draft>')]
    service.poll_inbox()
    return service.draft_lead_reply(service.list_leads()[0]['id'], 'Thank you. Please send your plans.')


def test_reply_draft_routes_to_original_account_and_requires_approval(service):
    message = draft(service)
    assert message['mailbox'] == SECOND and message['status'] == 'draft'
    with pytest.raises(ValueError, match='approved'):
        service.send(message['id'])
    service.approve(message['id'])
    assert service.send(message['id'])['status'] == 'sent'
    assert len(service.mailboxes()[SECOND].sender.sent) == 1
    assert not service.mailboxes()[PRIMARY].sender.sent
    with pytest.raises(ValueError):
        service.send(message['id'])


def test_sent_cap_counts_other_tools_and_does_not_claim_draft(service):
    message = service.approve(draft(service)['id'])
    service.mailboxes()[SECOND].inbox.sent_items = [sent(subject=str(n), mid=str(n)) for n in range(30)]
    with pytest.raises(ValueError, match='cap of 30'):
        service.send(message['id'])
    assert service.repo.get('customer_messages', message['id'])['status'] == 'approved'
    assert not service.mailboxes()[SECOND].sender.sent


def test_sent_duplicate_refusal(service):
    message = service.approve(draft(service)['id'])
    service.mailboxes()[SECOND].inbox.sent_items = [sent(subject=message['subject'])]
    with pytest.raises(ValueError, match='Duplicate refused'):
        service.send(message['id'])
    assert not service.mailboxes()[SECOND].sender.sent


def test_original_mailbox_missing_never_falls_back(service):
    message = service.approve(draft(service)['id'])
    del service.mailboxes()[SECOND]
    with pytest.raises(ValueError, match='Original mailbox'):
        service.send(message['id'])
    assert not service.mailboxes()[PRIMARY].sender.sent


def test_sent_unavailable_blocks_send_other_mailbox_poll_survives(service):
    message = service.approve(draft(service)['id'])
    service.mailboxes()[SECOND].inbox.fail = True
    with pytest.raises(ValueError, match='Cannot verify Sent'):
        service.send(message['id'])
    service.mailboxes()[PRIMARY].inbox.raw = [inbound('other@example.test', '<other>')]
    result = service.poll_inbox()
    assert result['possible_new_enquiries'] == 1 and result['mailbox_errors'] == [SECOND]


def test_cap_resets_at_uk_midnight_and_handles_dst():
    box = Mailbox(SECOND, Sender(), Inbox())
    # UK July midnight is 23:00 UTC the previous date.
    now = datetime(2026, 7, 1, 23, 5, tzinfo=timezone.utc)
    previous_day = [sent(when=now - timedelta(minutes=10)) for _ in range(30)]
    assert box.check_send(dict(recipient='x@example.test', subject='x'), previous_day, now) == 30
    current_day = [sent(when=now - timedelta(minutes=1)) for _ in range(30)]
    with pytest.raises(ValueError, match='cap of 30'):
        box.check_send(dict(recipient='x@example.test', subject='x'), current_day, now)


def test_replied_requires_later_message_and_thread_or_address_subject():
    received = '2026-11-10 10:00:00'
    later = datetime(2026,11,10,11,tzinfo=timezone.utc)
    assert replied([sent(when=later)], 'new@example.test', 'Kitchen', received)
    assert replied([sent(address='other@example.test', subject='Other', when=later)], 'new@example.test', 'Kitchen', received, 'thread1')
    assert not replied([sent(when=later-timedelta(hours=2))], 'new@example.test', 'Kitchen', received, 'thread1')
    assert not replied([sent(subject='Other', when=later)], 'new@example.test', 'Kitchen', received)


def test_missing_optional_config_is_skipped_and_does_not_construct_client(monkeypatch):
    monkeypatch.setattr(cfg, 'CUSTOMER_ZOHO_REFRESH_TOKEN', '')
    assert customer_mailbox() is None
    s = CustomerService(sender=Sender(), inbox=Inbox())
    assert len(s.mailboxes()) == 1


def test_sent_pagination_and_encoded_recipients():
    class Client:
        def __init__(self): self.starts = []
        def request(self, method, path, **kw):
            if path == '/folders': return [dict(folderType='Sent', folderId='123')]
            start = kw['params']['start']; self.starts.append(start)
            assert kw['params']['includeto'] == 'true'
            return [sent(mid=str(n)) for n in range(200)] if start == 1 else [sent(address='&quot;New&quot;&lt;new@example.test&gt;')]
    client = Client()
    items = ZohoInbox(client).fetch_sent(2)
    assert len(items) == 201 and client.starts == [1, 201]
    box = Mailbox(SECOND, Sender(), Inbox())
    with pytest.raises(ValueError, match='Duplicate'):
        box.check_send(dict(recipient='new@example.test', subject='Re: Kitchen'), items[-1:])


def test_customer_credentials_select_eu_account_without_primary_tokens(monkeypatch,tmp_path):
    from storage.database import initialise_schema
    path=tmp_path/"tokens.db"
    initialise_schema(path)
    monkeypatch.setattr(cfg,"DB_PATH",path)
    from rfq.zoho_mail import ZohoSender
    for key, value in dict(REGION='eu', ACCOUNT_ID='987', CLIENT_ID='second-id', CLIENT_SECRET='second-secret', REFRESH_TOKEN='second-refresh').items():
        monkeypatch.setattr(cfg, 'CUSTOMER_ZOHO_' + key, value)
    class HTTP:
        def request(self, method, url, **kw):
            class Response:
                def raise_for_status(self): pass
                def json(self):
                    return {'access_token':'access', 'expires_in':3600} if url.endswith('/token') else {'data': {'messageId':'42'}}
            if url.endswith('/token'):
                assert url == 'https://accounts.zoho.eu/oauth/v2/token'
                assert kw['data']['refresh_token'] == 'second-refresh'
            else:
                assert '/accounts/987/messages' in url
                assert kw['json']['fromAddress'] == SECOND
            return Response()
    transport = ZohoClient(HTTP(), prefix='CUSTOMER_ZOHO_')
    assert ZohoSender(transport, address=SECOND).send(dict(recipient='x@example.test', subject='x', body='x')) == 'zoho:42'


def test_connect_helper_scopes_account_verification_and_stdin(monkeypatch):
    import importlib.util
    from pathlib import Path
    import httpx
    spec = importlib.util.spec_from_file_location('connect_helper', Path(__file__).parents[1] / 'scripts' / 'zoho_connect.py')
    helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
    def handler(request):
        if request.url.path.endswith('/token'):
            return httpx.Response(200, json=dict(access_token='access-private', refresh_token='refresh-private'))
        return httpx.Response(200, json={'data':[dict(primaryEmailAddress=SECOND, accountId='987')]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = helper.authorize('eu', SECOND, 'id', 'secret-private', 'code', client)
        with pytest.raises(ValueError, match='Authorization failed'):
            helper.authorize('eu', PRIMARY, 'id', 'secret-private', 'code', client)
    assert result['ACCOUNT_ID'] == '987'
    assert 'ZohoMail.folders.READ' in helper.SCOPES
    calls = []
    def run(args, **kw):
        calls.append((args, kw))
        return type('Result', (), {'returncode':0})()
    helper.save_variables('railway', {'CUSTOMER_ZOHO_REFRESH_TOKEN': result['REFRESH_TOKEN']}, run=run)
    assert 'refresh-private' not in str(calls[0][0])
    assert calls[0][1]['input'] == 'refresh-private' and '--stdin' in calls[0][0]


def test_local_accepted_send_prevents_duplicate_before_provider_index_updates(service):
    message = service.approve(draft(service)['id'])
    service.send(message['id'])
    other = service.draft_followup(message['enquiry_id'], subject=message['subject'], body='Different body')
    # Exact duplicate with the same subject, regardless of body.
    service.repo.update('customer_messages', other['id'], subject=message['subject'])
    service.approve(other['id'])
    with pytest.raises(ValueError, match='Duplicate refused'):
        service.send(other['id'])
    assert len(service.mailboxes()[SECOND].sender.sent) == 1


def test_30th_send_allowed_independent_of_legacy_limit_and_other_mailbox(service, monkeypatch):
    monkeypatch.setattr(cfg, 'CUSTOMER_DAILY_SEND_LIMIT', 0)
    message = service.approve(draft(service)['id'])
    service.mailboxes()[PRIMARY].inbox.sent_items = [sent(subject=str(n), mid=str(n)) for n in range(40)]
    service.mailboxes()[SECOND].inbox.sent_items = [sent(subject=str(n), mid=str(n)) for n in range(29)]
    assert service.send(message['id'])['status'] == 'sent'
    other = service.draft_followup(message['enquiry_id'], subject='Another topic', body='Another reply')
    service.approve(other['id'])
    with pytest.raises(ValueError, match='cap of 30'):
        service.send(other['id'])


def test_card_reply_api_only_drafts_and_send_requires_approval(service, monkeypatch):
    import api.auth as auth
    from api.app import app
    from api.customers import get_customer_service
    from fastapi.testclient import TestClient
    monkeypatch.setattr(auth, 'API_ACCESS_TOKEN', 'mailbox-test')
    app.dependency_overrides[get_customer_service] = lambda: service
    client = TestClient(app)
    headers = {'Authorization':'Bearer mailbox-test'}
    service.mailboxes()[SECOND].inbox.raw = [inbound('new@example.test', '<api>')]
    service.poll_inbox()
    lead_id = service.list_leads()[0]['id']
    try:
        assert client.post(f'/customers/leads/{lead_id}/reply', json={'body':'Hello'}).status_code == 401
        response = client.post(f'/customers/leads/{lead_id}/reply', json={'body':'Hello'}, headers=headers)
        assert response.status_code == 200
        message = response.json()
        assert message['mailbox'] == SECOND and message['status'] == 'draft'
        assert client.post(f"/customers/messages/{message['id']}/send", headers=headers).status_code == 400
        assert not service.mailboxes()[SECOND].sender.sent
        assert client.post(f"/customers/messages/{message['id']}/approve", headers=headers).status_code == 200
        assert client.post(f"/customers/messages/{message['id']}/send", headers=headers).status_code == 200
        assert len(service.mailboxes()[SECOND].sender.sent) == 1
    finally:
        app.dependency_overrides.clear()


def test_idless_mime_dedup_ignores_account_thread_metadata(service):
    original = b'From: new@example.test\r\nSubject: Kitchen\r\n\r\nPlease quote'
    service.mailboxes()[PRIMARY].inbox.raw = [b'X-SupplierIntel-Thread: primary-thread\r\n' + original]
    service.mailboxes()[SECOND].inbox.raw = [b'X-SupplierIntel-Thread: second-thread\r\n' + original]
    result = service.poll_inbox()
    assert result['possible_new_enquiries'] == 1 and result['duplicates'] == 1
    assert len(service.list_leads()) == 1


def test_sent_check_failure_preserves_inbound_card_with_unknown_reply_status(service):
    class SentUnavailable(Inbox):
        def fetch_sent(self, days):
            raise RuntimeError('private provider error')
    inbox = SentUnavailable()
    inbox.raw = [inbound('new@example.test', '<sent-unavailable>')]
    service.mailboxes()[SECOND].inbox = inbox
    assert service.poll_inbox()['possible_new_enquiries'] == 1
    card = service.list_leads()[0]
    assert card['mailbox'] == SECOND and card['reply_checked_at'] is None
    assert not service.mailboxes()[SECOND].sender.sent


def test_second_account_sending_does_not_require_primary_credentials(service):
    service._readiness = lambda: ['RFQ_ZOHO_REFRESH_TOKEN']
    message = service.approve(draft(service)['id'])
    assert service.send(message['id'])['status'] == 'sent'


def test_missing_sent_recipient_blocks_exact_duplicate_check():
    box = Mailbox(SECOND, Sender(), Inbox())
    item = sent(); item.pop('toAddress')
    with pytest.raises(ValueError, match='Cannot verify Sent recipients'):
        box.check_send(dict(recipient='new@example.test', subject='Kitchen'), [item])
