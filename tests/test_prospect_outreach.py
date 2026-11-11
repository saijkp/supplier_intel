import json
from datetime import datetime, timedelta, timezone
import pytest
from customers.mailboxes import Mailbox
from outreach.repository import OutreachRepository
from outreach.service import OutreachService, classify, render
from storage.database import initialise_schema, connection_scope, get_schema_version


class FakeMail:
    def __init__(self, clock):
        self.clock=clock; self.sent=[]; self.calls=[]; self.replies=[]; self.fail=False; self.hide=False

    def fetch_sent(self, days=14):
        return self.sent[:]

    def fetch_for_campaign(self,*args):
        return self.replies

    def send(self, message):
        self.calls.append(message)
        if self.fail:
            raise RuntimeError('ambiguous transport failure')
        mid=str(len(self.calls))
        if not self.hide:
            self.sent.append({'messageId':mid,'toAddress':message['recipient'],'subject':message['subject'],
                              'sentDateInGMT':self.clock[0].timestamp()*1000})
        return 'zoho:'+mid


@pytest.fixture
def engine(tmp_path):
    path=tmp_path/'outreach.db'; initialise_schema(path)
    clock=[datetime(2026,11,11,9,tzinfo=timezone.utc)]
    mail=FakeMail(clock)
    service=OutreachService(OutreachRepository(path), {a:Mailbox(a,mail,mail) for a in ('sai@ikeng.online','sj@ikeng.online')},lambda:clock[0],True,'2026-11-11')
    return service,mail,clock


def prepare(s, n=1, mailbox='sj@ikeng.online', subtype='corporate', steps=None):
    rows=[{'company':'Studio '+str(i),'name':'Alex Smith','email':f'alex{i}@studio{i}.co.uk',
           'number':f'{i+100:08d}','subscriber_type':subtype,'source_url':'https://example.com/team'} for i in range(n)]
    s.import_rows(rows,'website')
    contacts=s.repo.rows('SELECT id FROM prospect_contacts')
    for c in contacts:
        s.review_contact(c['id'],'corporate_verified')
    seq=s.save_sequence('Architect test','architect',steps or [{'channel':'email','day_offset':0,'subject':'Drawing for {company}', 'template':'Hello {first_name}. Could we price an item for {company}?'}])
    s.enrol(seq['id'],[c['id'] for c in contacts],mailbox)
    s.approve(seq['id'],'Sai','Relevant corporate contacts; sourced from studio website. Limited contact with easy objection.')
    return seq['id']


def test_schema_idempotent(engine):
    s,_,_=engine; initialise_schema(s.repo.db_path)
    assert get_schema_version(s.repo.db_path)==46


def test_approval_gate_and_disabled(engine):
    s,m,_=engine; sid=prepare(s)
    s.repo.execute('UPDATE outreach_sequences SET approved_at=NULL WHERE id=?',(sid,))
    assert s.run_due()['sent']==0 and not m.calls
    s.approve(sid,'Sai','Assessment grounded in source and relevance')
    s.enabled=False
    assert s.run_due()['sent']==0 and not m.calls


def test_sends_and_never_duplicates(engine):
    s,m,_=engine; prepare(s)
    assert s.run_due()['sent']==1
    assert s.run_due()['sent']==0
    assert len(m.calls)==1
    assert 'opt out' in m.calls[0]['body']
    assert s.repo.rows("SELECT * FROM outreach_events WHERE kind='email'")[0]['status']=='sent'
    assert len(s.tasks())==1
    assert 'Cadence finished' in s.tasks()[0]['payload']


def test_warmup_shared_sent_and_spacing(engine):
    s,m,c=engine; prepare(s,8)
    m.sent=[{'messageId':'other','toAddress':'elsewhere@test.co.uk','subject':'Unrelated customer send', 'sentDateInGMT':(c[0]-timedelta(seconds=20)).timestamp()*1000}]
    assert s.run_due()['sent']==0
    c[0]+=timedelta(seconds=90)
    for _ in range(8):
        s.run_due(); c[0]+=timedelta(seconds=91)
    assert len(m.calls)==4 # one unrelated plus four outreach = week-one cap 5
    c[0]+=timedelta(days=7)
    assert s._cap('sj@ikeng.online')==10
    assert s.run_due()['sent']==1


def test_global_cap(engine):
    s,m,c=engine; prepare(s,mailbox='sai@ikeng.online')
    m.sent=[{'messageId':str(i),'toAddress':'other@test.co.uk','subject':'Other traffic','sentDateInGMT':(c[0]-timedelta(minutes=5)).timestamp()*1000} for i in range(30)]
    assert s.run_due()['sent']==0 and not m.calls


@pytest.mark.parametrize('subtype',['sole_trader','partnership','unknown'])
def test_noncorporate_skip(engine,subtype):
    s,m,_=engine; prepare(s,subtype=subtype)
    assert s.run_due()['sent']==0 and not m.calls


def test_review_address_required(engine):
    s,m,_=engine; prepare(s)
    s.repo.execute("UPDATE prospect_contacts SET email_status='unreviewed'")
    assert s.run_due()['sent']==0 and not m.calls


@pytest.mark.parametrize('identifier',['alex0@studio0.co.uk','studio0.co.uk'])
def test_suppression_permanent_and_import_safe(engine,identifier):
    s,m,_=engine; prepare(s); s.suppress(identifier,'Objection')
    assert s.run_due()['sent']==0 and not m.calls
    with pytest.raises(Exception,match='permanent'):
        s.repo.execute('DELETE FROM suppression_list')
    s.import_rows([{'company':'Studio 0','email':'alex0@studio0.co.uk','number':'00000100'}],'manual')
    assert len(s.repo.rows('SELECT * FROM prospect_accounts'))==1
    assert s.run_due()['sent']==0


def test_rfq_suppression_respected(engine):
    s,m,_=engine; prepare(s)
    s.repo.execute("INSERT INTO rfq_suppressions(email,reason) VALUES('alex0@studio0.co.uk','stop')")
    assert s.run_due()['sent']==0 and not m.calls


@pytest.mark.parametrize('reply,category',[('Please opt out','opt-out'),('Not now, next month','not now'),('Out of office','out-of-office'),('Address not found','bounce'),('Could you explain?','needs Sai')])
def test_reply_cancels_all_tasks(engine,reply,category):
    s,m,_=engine
    prepare(s,steps=[{'channel':'linkedin_task','day_offset':0,'template':'Hello {first_name}.'},{'channel':'email','day_offset':0,'subject':'Hello','template':'Can we price a drawing?'}])
    s.run_due(limit=1)
    assert len(s.tasks())==1
    result=s.receive(1,reply,'reply-1')
    assert result['classification']==category
    assert s.run_due()['sent']==0 and not m.calls
    assert not s.repo.rows("SELECT * FROM outreach_events WHERE kind='linkedin_task' AND status='pending'")
    assert s.receive(1,reply,'reply-1')['duplicate']
    if category in ('opt-out','bounce'):
        assert s.repo.rows('SELECT * FROM suppression_list')


def test_interested_creates_customer_before_task(engine):
    s,_,_=engine; prepare(s)
    assert s.receive(1,'Please quote the attached drawing','interested-1')['classification']=='interested'
    links=s.repo.rows('SELECT * FROM prospect_enquiry_links')
    assert len(links)==1
    enquiry=s.repo.rows('SELECT * FROM customer_enquiries')[0]
    assert enquiry['requirements']=='Please quote the attached drawing'
    assert enquiry['next_action_due']


def test_customer_intake_failure_preserves_visible_action(engine,monkeypatch):
    s,_,_=engine;prepare(s)
    def broken(*args):
        raise RuntimeError('temporary intake error')
    monkeypatch.setattr(s,'convert',broken)
    result=s.receive(1,'Please quote this drawing','intake-failure')
    assert result['classification']=='interested'
    assert 'intake failed' in s.tasks()[0]['payload']
    assert s.repo.get('outreach_enrolments',1)['status']=='replied'


@pytest.mark.parametrize('hide',[False,True])
def test_failed_send_never_retried_and_stops_batch(engine,hide):
    s,m,_=engine; prepare(s,2)
    m.hide=hide; m.fail=not hide
    assert s.run_due()['failed']==1
    assert s.run_due()['sent']==0
    assert len(m.calls)==1
    assert s.repo.rows("SELECT * FROM outreach_events WHERE kind='email'")[0]['status']=='failed'


def test_sent_duplicate_refused(engine):
    s,m,c=engine; prepare(s)
    m.sent=[{'messageId':'past','toAddress':'alex0@studio0.co.uk','subject':'Drawing for Studio 0 [IKE-PROSPECT-1]', 'sentDateInGMT':(c[0]-timedelta(minutes=5)).timestamp()*1000}]
    assert s.run_due()['sent']==0 and not m.calls


def test_task_idempotency_and_note_length(engine):
    s,m,_=engine; prepare(s,steps=[{'channel':'linkedin_task','day_offset':0,'template':'Please connect, {first_name}.'}])
    assert s.run_due()['tasks']==1
    assert s.run_due()['tasks']==0
    task=s.tasks()[0]; payload=json.loads(task['payload'])
    assert len(payload['connection_note'])<300
    assert 'linkedin.com/sales/search/' in payload['action_url']
    s.task_done(task['id'],'Connected by hand')
    assert not s.tasks() and not m.calls


def test_enrolment_change_and_edit_clear_approval(engine):
    s,_,_=engine; sid=prepare(s)
    s.enrol(sid,[1],'sj@ikeng.online')
    assert s.sequence(sid)['approved_at'] is None
    s.approve(sid,'Sai','Professional relevance and limited contact assessment')
    s.save_sequence('Updated','architect',[{'channel':'email','day_offset':0,'subject':'Hi','template':'New text'}],sid)
    assert s.sequence(sid)['approved_at'] is None


def test_import_dedup_and_trusted_values(engine):
    s,_,_=engine
    s.import_rows([{'name':'Alpha','number':'00123456','website':'https://alpha.co.uk','postcode':'EC1 1AA','address':'Original'}],'companies_house')
    s.import_rows([{'name':'Changed','number':'00123456','website':'different.co.uk','address':'Candidate'}],'manual')
    s.import_rows([{'name':'Alias','website':'www.alpha.co.uk'}],'manual')
    s.import_rows([{'name':'ALPHA','postcode':'EC1 1AA'}],'manual')
    accounts=s.repo.rows('SELECT * FROM prospect_accounts')
    assert len(accounts)==1 and accounts[0]['address']=='Original'
    s.import_rows([{'name':'ALPHA','address':'Studio, EC1 1AA'}],'directory')
    assert len(s.repo.rows('SELECT * FROM prospect_accounts'))==1


def test_grounded_merge_and_template_validation(engine):
    assert render('Hello {first_name}. We work near {area}. Can we talk?',{'first_name':'Alex'})=='Hello Alex. Can we talk?'
    assert render('Regarding {project_hint}.',{})==''
    s,_,_=engine
    with pytest.raises(ValueError):
        s.save_sequence('Bad','architect',[{'channel':'email','day_offset':0,'template':'Margin {supplier_cost}'}])


def test_poll_plain_reply_and_no_self_footer_classification(engine):
    s,m,_=engine; prepare(s); s.run_due()
    m.replies=[b'From: Alex <alex0@studio0.co.uk>\r\nMessage-ID: <reply1>\r\nSubject: Re: Drawing\r\nContent-Type: text/plain\r\n\r\nCould you explain?\r\nOn Wed Sai wrote:\r\n> Reply opt out']
    assert s.poll_replies()['processed']==1
    assert s.poll_replies()['processed']==0
    assert not s.repo.rows('SELECT * FROM suppression_list')


def test_schedule_weekends_dst():
    from outreach.worker import in_window
    assert not in_window(datetime(2026,11,14,9,tzinfo=timezone.utc))
    assert in_window(datetime(2026,7,13,7,30,tzinfo=timezone.utc))
    assert not in_window(datetime(2026,11,11,8,29,tzinfo=timezone.utc))


def test_free_apollo_never_reveals_and_marks_attempt(engine):
    from outreach.apollo import ProspectContactService
    s,_,_=engine
    s.import_rows([{'name':'Practice','website':'practice.co.uk','number':'00123456'}],'companies_house')
    class FakeApollo:
        calls=[]
        def post(self,url,**kwargs):
            self.calls.append(url)
            assert url.endswith('/mixed_people/api_search')
            class Response:
                def raise_for_status(self):pass
                def json(self):return {'people':[{'id':'person-1','first_name':'Lee','title':'Director','last_name_obfuscated':'X***'}]}
            return Response()
    client=FakeApollo(); service=ProspectContactService(s,client,'fake-key')
    assert service.search_pending()['results'][0]['found']==1
    assert service.search_pending()['results']==[]
    assert len(client.calls)==1
    contact=s.repo.rows('SELECT * FROM prospect_contacts')[0]
    assert contact['name']=='Lee' and contact['email'] is None


def test_paid_reveal_separate_preview_exact_cost_and_no_retry(engine,monkeypatch):
    from outreach.apollo import ProspectContactService
    s,_,_=engine
    s.import_rows([{'name':'Practice','number':'00123456'}],'companies_house')
    s.repo.execute("INSERT INTO prospect_contacts(account_id,name,source,source_url,created_at) VALUES(1,'Lee','apollo_free_search','apollo-free:person1','2026-11-11')")
    class FakeApollo:
        calls=[]
        def post(self,url,**kwargs):
            self.calls.append(url)
            assert kwargs['json']['reveal_personal_emails'] is False
            class Response:
                def raise_for_status(self):pass
                def json(self):return {'person':{'email':'lee@practice.co.uk'}}
            return Response()
    client=FakeApollo();service=ProspectContactService(s,client,'fake')
    monkeypatch.setenv('OUTREACH_APOLLO_CREDITS_PER_CONTACT','2')
    preview=service.preview_reveal([1]); assert preview['maximum_credit_cost']==2 and not client.calls
    with pytest.raises(ValueError,match='disabled'):
        service.reveal(preview['id'],2,'Sai')
    monkeypatch.setenv('OUTREACH_APOLLO_REVEAL_ENABLED','true')
    with pytest.raises(ValueError,match='exact'):
        service.reveal(preview['id'],1,'Sai')
    assert not client.calls
    assert service.reveal(preview['id'],2,'Sai')['status']=='completed'
    assert s.repo.get('prospect_contacts',1)['email_status']=='unreviewed'
    with pytest.raises(ValueError,match='cannot retry'):
        service.reveal(preview['id'],2,'Sai')
    assert len(client.calls)==1


def test_edit_used_template_keeps_ledger_and_clears_approval(engine):
    s,m,_=engine; sid=prepare(s)
    s.run_due()
    old=s.repo.rows("SELECT payload FROM outreach_events WHERE kind='email'")[0]['payload']
    s.save_sequence('Revised','architect',[{'channel':'email','day_offset':0,'subject':'Revised','template':'Can we discuss a drawing?'}],sid)
    assert s.sequence(sid)['approved_at'] is None
    assert s.repo.rows("SELECT payload FROM outreach_events WHERE kind='email'")[0]['payload']==old
    assert len(m.calls)==1


def test_walk_in_replaces_cold_with_unapproved_same_day_draft(engine):
    s,m,_=engine; prepare(s)
    result=s.visit(1,'Met director',{'name':'Jo','email':'jo@studio0.co.uk'})
    seq=s.sequence(result['sequence']['id'])
    assert seq['approved_at'] is None and seq['contacts']
    assert s.repo.get('outreach_enrolments',1)['status']=='paused'
    assert s.run_due()['sent']==0 and not m.calls


def test_walk_in_uses_existing_card_email_without_overwriting_contact(engine):
    s,_,_=engine;prepare(s)
    result=s.visit(1,'Met Alex',{'name':'Alex Smith','email':'alex0@studio0.co.uk'})
    assert result['contact_ids']==[1]
    assert len(s.repo.rows('SELECT * FROM prospect_contacts'))==1
    assert s.sequence(result['sequence']['id'])['contacts'][0]['contact_id']==1


def test_linked_close_reports_value_and_margin(engine):
    s,_,_=engine; prepare(s)
    s.receive(1,'Please quote a drawing','project1')
    eid=s.account(1)['enquiries'][0]['id']
    s.close(1,eid,'won','Signed quote and deposit confirmed by Sai',25000,margin_gbp=5000)
    assert s.metrics()['won_gbp']==25000 and s.metrics()['margin_gbp']==5000


def test_api_auth_approval_and_job_fakes(engine,monkeypatch):
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from api.outreach import router,get_service
    import api.auth
    s,m,_=engine
    monkeypatch.setattr(api.auth,'API_ACCESS_TOKEN','test-outreach')
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_service]=lambda:s
    client=TestClient(app)
    assert client.get('/outreach/accounts').status_code==401
    client.headers['Authorization']='Bearer test-outreach'
    assert client.post('/outreach/imports',json={'source':'manual','rows':[{'company':'Practice','name':'Jo','email':'jo@practice.co.uk','number':'00123456'}]}).status_code==200
    assert client.patch('/outreach/contacts/1',json={'email_status':'corporate_verified'}).status_code==200
    seq=client.post('/outreach/sequences',json={'title':'Architects','segment':'architect','steps':[{'channel':'email','day_offset':0,'subject':'One drawing','template':'Could we price one item?'}]}).json()
    sid=seq['id'];assert client.post(f'/outreach/sequences/{sid}/enrol',json={'contact_ids':[1],'mailbox':'sj@ikeng.online'}).status_code==200
    job=client.post('/outreach/jobs',json={'limit':10}).json()
    assert client.get('/outreach/jobs/'+job['id']).json()['status']=='completed' and not m.calls
    assert client.post(f'/outreach/sequences/{sid}/approve',json={'approved_by':'Sai','legitimate_interest_note':'Grounded professional relevance and limited contact.'}).status_code==200
    client.post('/outreach/jobs',json={'limit':10})
    assert len(m.calls)==1


def test_migration_from_44_preserves_existing_data(engine):
    s,_,_=engine
    # Simulate an existing v44 schema with no outreach objects.
    with connection_scope(s.repo.db_path) as c:
        c.execute('DELETE FROM schema_migrations WHERE version=45')
        c.execute("INSERT INTO customers(name) VALUES('Existing buyer')")
    initialise_schema(s.repo.db_path)
    assert s.repo.rows('SELECT name FROM customers')[0]['name']=='Existing buyer'
    assert get_schema_version(s.repo.db_path)==46


def test_manual_next_action_and_preproject_loss(engine):
    s,_,clock=engine;prepare(s)
    s.receive(1,'Not now, next month','not-now')
    for task in s.tasks():
        s.task_done(task['id'],'Reviewed, next month agreed')
    s.manual_task(1,'needs Sai','Review agreed next-month timing',(clock[0]+timedelta(days=30)).isoformat())
    assert not s.tasks()
    clock[0]+=timedelta(days=30)
    assert len(s.tasks())==1
    s.close(1,None,'lost','Sai: no relevant project',close_reason='no project')
    assert s.account(1)['stage']=='lost' and not s.tasks()


def test_uncertain_send_review_consumes_step(engine):
    s,m,_=engine;prepare(s)
    m.hide=True;s.run_due()
    event=s.repo.rows("SELECT * FROM outreach_events WHERE kind='email'")[0]
    assert s.reconcile(event['id'],'Sai','Checked Sent and delivery manually')['retry_allowed'] is False
    assert s.run_due()['sent']==0 and len(m.calls)==1


def test_every_zoho_sender_shares_budget_when_outreach_enabled(monkeypatch):
    from rfq.zoho_mail import ZohoSender
    monkeypatch.setenv('OUTREACH_SEND_ENABLED','true')
    class Client:
        posts=0
        def request(self,method,path,**kwargs):
            if method=='POST':
                self.posts+=1;return {'messageId':'1'}
            if path=='/folders':return [{'folderType':'Sent','folderId':'1'}]
            if path=='/messages/view':return [{'messageId':str(i),'toAddress':'other@test.test','sentDateInGMT':datetime.now(timezone.utc).timestamp()*1000,'subject':'Other sends'} for i in range(30)]
    client=Client();sender=ZohoSender(client,'sai@ikeng.online')
    with pytest.raises(ValueError,match='cap'):
        sender.send({'recipient':'jo@test.test','subject':'Hi','body':'Hi'})
    assert client.posts==0


def test_ambiguous_reply_becomes_task_and_stops_candidates(engine):
    s,m,_=engine;sid=prepare(s)
    s.pause(1)
    q=s.save_sequence('Revised','architect',[{'channel':'email','day_offset':0,'subject':'A drawing','template':'Could we price a drawing?'}])
    s.enrol(q['id'],[1],'sj@ikeng.online')
    s.approve(q['id'],'Sai','Limited sourced professional relevance assessment')
    m.replies=[b'From: alex0@studio0.co.uk\r\nMessage-ID: <ambiguous>\r\nSubject: Hello\r\nContent-Type: text/plain\r\n\r\nCould you explain?']
    s.poll_replies()
    assert all(r['status']=='replied' for r in s.repo.rows('SELECT status FROM outreach_enrolments'))
    tasks=s.tasks();assert len(tasks)==1 and tasks[0]['company']=='Reply needs matching'
    assert 'candidate_enrolments' in tasks[0]['payload']
    assert s.run_due()['sent']==0 and not m.calls
    assert s.metrics()['contacted']==0 # reviewing an inbound message isn't a new contact
