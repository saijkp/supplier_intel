from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from uuid import uuid4
import pytest
from customers.customer_service import CustomerService
from customers.mailboxes import Mailbox
from customers.form_parser import parse_form
from customers.deal_closer import DealCloserService, UK, parse_promise, working_due
from storage.customer_repository import CustomerRepository
from storage.database import initialise_schema

class Inbox:
    def __init__(self, mails=()): self.mails=mails
    def fetch_recent(self,*args): return self.mails
    def fetch_sent(self,*args): return []

class Sender:
    def __init__(self): self.sent=[]
    def send(self,message): self.sent.append(message); return 'fake:1'

@pytest.fixture
def service():
    # Flat test file avoids restricted Windows temporary-directory ACLs.
    path=Path(__file__).resolve().parents[1] / ('dc-test-'+uuid4().hex+'.sqlite')
    initialise_schema(path)
    customer=CustomerService(CustomerRepository(path),mailboxes={})
    sender=Sender()
    s=DealCloserService(customer,lambda:datetime(2026,11,18,16,30,tzinfo=UK),sender)
    yield s
    path.unlink(missing_ok=True)

@pytest.mark.parametrize('text,expected', [('by Monday','2026-11-23'),('by Friday 20 Nov','2026-11-20'),('end of this week','2026-11-20'),('tomorrow','2026-11-19'),('by 14 November 2026','2026-11-14')])
def test_dates(text,expected):
    assert parse_promise(text,'2026-11-18T10:00:00+00:00').startswith(expected)

def test_dst_weekend_and_ambiguous():
    assert '+01:00' in parse_promise('tomorrow','2026-03-28T10:00:00+00:00')
    assert working_due('2026-11-13T10:00:00+00:00').date().isoformat()=='2026-11-18'
    assert parse_promise('soon','2026-11-18') is None
    assert parse_promise('by Friday 19 Nov','2026-11-18') is None

def enquiry(s):
    c=s.customers.create_customer(name='Real customer',email='customer@example.org')
    return s.customers.create_enquiry(c['id'],title='Kitchen',next_action='Send drawings',next_action_due='2026-11-16')

def test_form_capture_and_test_skip(service):
    fields=parse_form('Full name: Daniel\nEmail: daniel@example.org\nLocation: Manchester')
    assert service.customers.store_form_submission(fields,'real form','cust:netlify:abc','New lead: Daniel')=='logged'
    assert len(service.customers.list_enquiries())==1
    assert service.customers.store_form_submission(fields,'real form','cust:netlify:abc','New lead: Daniel')=='duplicates'
    fields['name']='TEST please ignore'
    assert service.customers.store_form_submission(fields,'TEST please ignore','cust:netlify:test','Test')=='ignored'

def test_suggestions_and_overdue(service):
    e=enquiry(service)
    service.customers.log(e['id'],'whatsapp_out','I will send drawings by Friday 20 Nov',occurred_at='2026-11-18 10:00:00')
    service.suggest();service.suggest()
    assert len(service.repo.rows('customer_promises'))==1
    assert service.summary()['counts']['promise_suggestions']==1
    p=service.add_promise(e['id'],'Drawings','2026-11-18T17:00:00+00:00')
    assert service.summary()['groups']['promises'][0]['suggested_action']=='Update customer today'
    assert service.summary()['counts']['overdue_actions']==1
    service.update_promise(p['id'],status='kept')
    assert service.summary()['counts']['promises']==0

def mail(sender,subject,extra=None):
    m=EmailMessage();m['From']=sender;m['To']='sai@ikeng.co.uk';m['Subject']=subject;m['Message-ID']='<'+uuid4().hex+'@example.org>'
    if extra: m['List-Unsubscribe']='<https://example.org/unsubscribe>'
    m.set_content('Please quote.');return m.as_bytes()

def test_classifier_real_shaped(service):
    from storage.database import connection_scope
    with connection_scope(service.repo.db_path) as c:
        c.execute("INSERT INTO rfq_messages(recipient) VALUES ('sales@zofa.com')")
        c.execute("INSERT INTO rfq_messages(recipient) VALUES ('quotes@aegisfreight.com')")
    inbox=Inbox([mail('buyer@engineering.org','Quote request for compressor inspection and freight to Worcester'),mail('other@zofa.com','Re: quote'),mail('ops@aegisfreight.com','Shipping quote'),mail('no-reply@news.org','News',True)])
    service.customers._mailboxes={'sai@ikeng.co.uk':Mailbox('sai@ikeng.co.uk',Sender(),inbox)}
    service.customers.poll_inbox();service.customers.poll_inbox()
    leads=service.customers.list_leads()
    assert len(leads)==1 and leads[0]['from_address']=='buyer@engineering.org'

def test_digest_idempotency_and_no_customer_sends(service):
    enquiry(service)
    service.tick(); service.tick()
    assert len(service.digest_sender.sent)==2
    assert all(x['recipient']=='sai@ikeng.co.uk' for x in service.digest_sender.sent)
    assert service.repo.rows('customer_messages')==[]
    assert service.health()['last_digest_at']

def test_ambiguous_digest_never_retries(service):
    class Broken:
        def send(self,payload): raise RuntimeError('ambiguous')
    service.digest_sender=Broken()
    assert service.digest('slot')['status']=='review required'
    assert service.digest('slot')['status']=='already attempted'

def test_api_auth_and_promise_lifecycle(service, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.deal_closer import router, get_service
    from api import auth
    monkeypatch.setattr(auth,'API_ACCESS_TOKEN','fake-local-token')
    app=FastAPI();app.include_router(router)
    app.dependency_overrides[get_service]=lambda:service
    e=enquiry(service)
    with TestClient(app) as client:
        assert client.get('/deal-closer/summary').status_code==401
        headers={'Authorization':'Bearer fake-local-token'}
        result=client.post(f"/enquiries/{e['id']}/promises",headers=headers,json={'text':'Drawings','due_at':'2026-11-20T17:00:00+00:00'})
        assert result.status_code==200
        assert client.patch('/promises/'+str(result.json()['id']),headers=headers,json={'status':'kept'}).json()['status']=='kept'
        assert client.get('/health/deal-closer',headers=headers).status_code==200

def test_supplier_working_days_link_and_waiting(service):
    from storage.database import connection_scope
    e=enquiry(service)
    service.customers.update_enquiry(e['id'],rfq_campaign_ids=[1],waiting_on='supplier')
    with connection_scope(service.repo.db_path) as c:
        c.execute("INSERT INTO rfq_campaigns(id,title,product) VALUES(1,'test','cabinet')")
        c.execute("INSERT INTO rfq_messages(campaign_id,recipient,status,sent_at) VALUES(1,'factory@example.cn','sent','2026-11-13 09:00:00')")
    assert service.summary()['groups']['supplier_chases'][0]['enquiry']==e['id']
    service.clock=lambda:datetime(2026,11,17,16,30,tzinfo=UK)
    assert service.summary()['counts']['supplier_chases']==0

def test_migration_is_idempotent(service):
    from storage.database import get_schema_version
    initialise_schema(service.repo.db_path)
    assert get_schema_version(service.repo.db_path)==53

def test_zoho_customer_pagination_fake_only():
    from rfq.zoho_mail import ZohoInbox
    class Client:
        def __init__(self): self.starts=[]
        def request(self,method,path,**kwargs):
            if path=='/messages/view':
                start=kwargs['params']['start'];self.starts.append(start)
                return [{'messageId':str(x),'fromAddress':'buyer@example.org','receivedTime':'9999999999999'} for x in ([1,2] if start==1 else [3])]
            return {'content':'From: buyer@example.org\r\nSubject: Quote\r\n\r\nPlease quote'}
    client=Client()
    assert len(ZohoInbox(client).fetch_recent_all(14,2))==3
    assert client.starts==[1,3]

def test_alert_email_auto_capture(service):
    m=EmailMessage();m['From']='formresponses@netlify.com';m['Subject']='New enquiry: kitchen';m['Message-ID']='<form-alert@example.org>'
    m.set_content('Name: Ohigana\nEmail address: ohigana@example.org\nMessage: Kitchen drawings please')
    service.customers._mailboxes={'sai@ikeng.co.uk':Mailbox('sai@ikeng.co.uk',Sender(),Inbox([m.as_bytes()]))}
    assert service.customers.poll_inbox()['logged']==1
    e=service.customers.list_enquiries()[0]
    assert e['customer_name']=='Ohigana'
    assert service.customers.poll_inbox()['duplicates']==1
