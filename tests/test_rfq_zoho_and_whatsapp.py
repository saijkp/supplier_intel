"""Zoho API contracts and WhatsApp imports; no live accounts or outbound mail."""
from datetime import datetime,timezone
import pytest

from config import settings as cfg
from rfq.zoho_mail import ZohoClient, ZohoInbox, ZohoSender
from rfq.whatsapp_import import supplier_messages
from rfq.rfq_service import RFQService
from storage.database import initialise_schema
from storage.rfq_repository import RFQRepository


@pytest.fixture
def zoho(monkeypatch,tmp_path):
    path=tmp_path/"tokens.db"
    initialise_schema(path)
    monkeypatch.setattr(cfg,"DB_PATH",path)
    for key,value in {'REGION':'eu','CLIENT_ID':'client','CLIENT_SECRET':'fake-client-secret',
                      'REFRESH_TOKEN':'fake-refresh-token','ACCOUNT_ID':'12345'}.items():
        monkeypatch.setattr(cfg,'RFQ_ZOHO_'+key,value)
    monkeypatch.setattr(cfg,'RFQ_FROM_ADDRESS','buyer@example.test')


class Response:
    def __init__(self,data):self.data=data
    def raise_for_status(self):pass
    def json(self):return self.data


class Client:
    def __init__(self):self.calls=[];self.fail=False
    def request(self,method,url,**kwargs):
        self.calls.append((method,url,kwargs))
        if self.fail:raise RuntimeError('fake-refresh-token fake-client-secret')
        if url.endswith('/oauth/v2/token'):return Response({'access_token':'fake-access-token','expires_in':3600})
        return Response({'status':{'code':200},'data':{'messageId':'654321'}})


def test_zoho_uses_correct_regional_oauth_and_no_smtp(zoho):
    client=Client(); transport=ZohoClient(client)
    sent=ZohoSender(transport).send({'recipient':'supplier@example.test','subject':'RFQ','body':'Confirm MOQ'})
    assert sent=='zoho:654321'
    assert client.calls[0][1]=='https://accounts.zoho.eu/oauth/v2/token'
    assert client.calls[0][2]['data']['refresh_token']=='fake-refresh-token'
    assert 'fake-refresh-token' not in client.calls[0][1]
    assert client.calls[1][1]=='https://mail.zoho.eu/api/accounts/12345/messages'
    assert client.calls[1][2]['headers']['Authorization']=='Zoho-oauthtoken fake-access-token'
    assert client.calls[1][2]['json']['mailFormat']=='plaintext'
    transport.request('GET','/messages/view')
    assert len([c for c in client.calls if c[1].endswith('/oauth/v2/token')])==1


def test_zoho_missing_confirmation_and_failures_are_safe(zoho):
    client=Client();client.fail=True
    with pytest.raises(RuntimeError) as error:ZohoClient(client).request('GET','/messages/view')
    assert 'fake-refresh-token' not in str(error.value)
    assert 'fake-client-secret' not in str(error.value)
    class NoConfirmation:
        def request(self,*args,**kwargs):return {}
    with pytest.raises(RuntimeError,match='review delivery'):ZohoSender(NoConfirmation()).send({'recipient':'supplier@test.test','subject':'RFQ','body':'Details'})


def test_zoho_filters_unrelated_mail_before_fetching_content(zoho):
    class Fake:
        def __init__(self):self.requests=[]
        def request(self,method,path,**kwargs):
            self.requests.append(path)
            if path=='/messages/view':return [
                {'messageId':'1','fromAddress':'supplier@test.test','subject':'Quote','receivedTime':datetime.now(timezone.utc).timestamp()*1000},
                {'messageId':'2','fromAddress':'personal@test.test','subject':'Private','receivedTime':datetime.now(timezone.utc).timestamp()*1000}]
            return {'content':'From: supplier@test.test\r\nSubject: Quote\r\n\r\nMOQ 50'}
    fake=Fake();messages=ZohoInbox(fake).fetch_for_campaign(14,50,[{'channel':'email','recipient':'supplier@test.test','reference':'RFQ-1-ABCDEF'}])
    assert len(messages)==1 and '/messages/2/originalmessage' not in fake.requests


def test_zoho_readiness_does_not_require_imap_and_never_exposes_tokens(zoho,tmp_path,monkeypatch):
    path=tmp_path/'zoho.db';initialise_schema(path)
    monkeypatch.setattr(cfg,'RFQ_MAIL_TRANSPORT','zoho')
    status=RFQService(repo=RFQRepository(path)).status()
    assert status['imap_configured']
    assert not any(k in status['missing'] for k in ['RFQ_SMTP_HOST','RFQ_EMAIL_USER','RFQ_EMAIL_PASSWORD'])
    assert 'fake-refresh-token' not in str(status)


def test_whatsapp_import_excludes_buyer_and_system_messages():
    text='''03/11/2026, 10:00 - Buyer: quantity: 500
03/11/2026, 10:01 - Factory Alice: unit_price: 18
currency: USD
03/11/2026, 10:02 - Messages are end-to-end encrypted.
moq: 999
[03/11/2026, 10:03:00] Buyer: material: aluminium
[03/11/2026, 10:04:00] Factory Alice: moq: 50'''
    messages=supplier_messages(text,'Factory Alice')
    assert len(messages)==2
    assert messages[0]['text']=='unit_price: 18\ncurrency: USD'
    assert messages[1]['text']=='moq: 50'
    assert '999' not in str(messages) and '500' not in str(messages)


def test_whatsapp_requires_exact_sender():
    with pytest.raises(ValueError,match='No messages'):
        supplier_messages('03/11/2026, 10:00 - Buyer: price: 1','Supplier')


def test_oversized_message_is_skipped_not_fatal():
    """One huge email (big attachments) must not stop the rest of the mailbox poll."""
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    class Fake:
        def request(self, method, path, **kwargs):
            if path == '/messages/view':
                return [{'messageId': '1', 'receivedTime': now_ms, 'subject': 'Huge quote PDFs', 'fromAddress': 'a@x.test'},
                        {'messageId': '2', 'receivedTime': now_ms, 'subject': 'Normal', 'fromAddress': 'b@x.test'}]
            if path == '/messages/1/originalmessage':
                return {'content': 'x' * 12000001}
            return {'content': 'Subject: Normal\r\n\r\nhello'}
    result = ZohoInbox(Fake()).fetch_recent(14, 50)
    assert len(result) == 1 and b'hello' in result[0]
