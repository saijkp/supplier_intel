"""Offline tests for daily orchestration, caps, leases and delivery quality."""
import json
from datetime import timedelta
from types import SimpleNamespace
import pytest
from tests.test_prospect_outreach import engine, FakeMail
from outreach.always_on import AlwaysOnService, project_steps, mailbox_config
from outreach.service import stamp
from rfq.zoho_mail import ZohoError


@pytest.fixture
def loop(engine,monkeypatch):
    s,m,clock=engine
    monkeypatch.setenv('OUTREACH_DRY_RUN','true')
    monkeypatch.setenv('OUTREACH_MAILBOXES',json.dumps({'sj@ikeng.online':{'prefix':'OUTREACH_ZOHO_','cap':5,'warmup_start':'2026-11-11','ramp':[5,10,20,30]}}))
    monkeypatch.setattr(AlwaysOnService,'_website_check',lambda self,a:True)
    a=AlwaysOnService(s)
    a.planning.sync_recent=lambda **kw:{'complete':True,'stored':2,'areas':kw['areas']}
    a.approve_template('Sai','Project relevant corporate outreach with easy objection.')
    return a,m,clock


def target(loop,i=1,role='architect',**overrides):
    a,m,clock=loop
    s=a.outreach
    s.import_rows([{'company':f'Studio {i} Ltd','name':'Alex Smith','email':f'alex@studio{i}.example',
                    'website':f'https://studio{i}.example','number':str(100+i),'subscriber_type':'corporate','segment':role}], 'website','https://example.test/team')
    account=s.repo.rows('SELECT * FROM prospect_accounts WHERE company=?',(f'Studio {i} Ltd',))[0]
    c=s.repo.rows('SELECT * FROM prospect_contacts WHERE account_id=?',(account['id'],))[0]
    s.review_contact(c['id'],'corporate_verified')
    s.event(account['id'],'planning corporate verification',{'company_number':account['companies_house_number'],'source_url':'https://registry.example/company'})
    fields={'authority':'Camden','reference':f'26/{i}','address':f'{i} Test Road','description':'Erection of 24 flats with kitchens',
            'fit_score':80,'fit_reasons':'[]','validated_date':'2026-11-10','source_url':f'https://council.gov.uk/{i}',
            'agent_account_id':account['id'] if role=='architect' else None,'applicant_account_id':account['id'] if role=='developer' else None,
            'agent_role':'architect' if role=='architect' else None,'first_seen_at':stamp(clock[0])}
    fields.update(overrides)
    pid=s.repo.execute('INSERT INTO planning_projects('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+')',tuple(fields.values()))
    p=s.repo.rows('SELECT * FROM planning_projects WHERE id=?',(pid,))[0]
    c=s.repo.get('prospect_contacts',c['id'])
    return p,c,account


def enrol(loop,i=1,**kw):
    p,c,a=target(loop,i,**kw)
    service=loop[0]
    service.queue_projects()
    eid=service._enrol(p,c,a,kw.get('role','architect'))
    return eid,p,c,a


def test_defaults_and_five_touches(loop):
    a,_,_=loop
    assert a.dry_run()
    assert [x['day_offset'] for x in project_steps()]==[0,4,9,16,28]
    assert project_steps()[2]['channel']=='linkedin_task'
    p,c,firm=target(loop)
    message=a.render_email(p,c)
    assert p['address'] in message['body'] and p['description'] in message['body']
    assert '15+' in message['body'] and 'opt out' in message['body'] and len(message['body'].split())<120


def test_quality_does_not_infer(loop):
    a,_,_=loop
    p,c,firm=target(loop)
    for changed in ({'name':None},{'email':'alex@gmail.com'},{'email_status':'unreviewed'}):
        with pytest.raises(ValueError):a.quality(p,{**c,**changed},firm)
    with pytest.raises(ValueError):a.quality({**p,'description':None},c,firm)
    with pytest.raises(ValueError):a.quality({**p,'validated_date':'2020-01-01'},c,firm)
    a.website_check=lambda account:False
    with pytest.raises(ValueError,match='parking'):a.quality(p,c,firm)


def test_overlong_application_is_fix_queue(loop):
    a,_,_=loop
    p,c,firm=target(loop,description='Erection of 24 flats with kitchens '+('detail '*110))
    with pytest.raises(ValueError,match='120'):a.render_email(p,c)
    a.queue_projects();a.enrich_pending()
    assert a.repo.rows('SELECT state FROM outreach_project_queue')[0]['state']=='waiting enrichment'


def test_dry_run_no_send_or_paid_reveal(loop):
    a,m,_=loop
    enrol(loop)
    assert a.outreach.run_due()['sent']==0 and not m.calls
    assert len(a.preview())==1
    assert not a.repo.rows('SELECT * FROM outreach_credit_ledger')


def test_send_resume_and_stop_on_reply(loop,monkeypatch):
    a,m,clock=loop
    eid,p,c,firm=enrol(loop)
    monkeypatch.setenv('OUTREACH_DRY_RUN','false')
    assert a.outreach.run_due()['sent']==1
    assert a.outreach.run_due()['sent']==0
    a.outreach.receive(eid,'Please send details','reply:1')
    clock[0]+=timedelta(days=4)
    assert a.outreach.run_due()['sent']==0
    assert a.outreach.tasks()
    assert a.repo.rows('SELECT * FROM prospect_enquiry_links')


def test_global_sequence_pause_and_sending_window(loop,monkeypatch):
    a,m,clock=loop
    eid,*_=enrol(loop)
    monkeypatch.setenv('OUTREACH_DRY_RUN','false')
    a.put('pause',True);assert a.outreach.run_due()['sent']==0
    a.put('pause',False)
    seq=a.repo.get('outreach_enrolments',eid)['sequence_id']
    a.put('sequence_pause:'+str(seq),True);assert a.outreach.run_due()['sent']==0
    a.put('sequence_pause:'+str(seq),False)
    clock[0]=clock[0].replace(hour=18);assert a.outreach.run_due()['sent']==0
    assert not m.calls


def test_firm_dedupes_projects_and_multiple_people(loop):
    a,_,_=loop
    eid,p,c,firm=enrol(loop)
    with pytest.raises(ValueError,match='Firm'):a._enrol({**p,'id':999},c,firm,'architect')
    assert len(a.repo.rows('SELECT * FROM outreach_enrolments'))==1


def test_developer_second_target(loop):
    a,_,_=loop
    p,c,firm=target(loop,role='developer')
    a.queue_projects();a.enrich_pending()
    assert a.repo.rows('SELECT state,enrolment_id FROM outreach_project_queue')[0]['enrolment_id']


def test_restart_safe_daily_rotation_and_lease(loop):
    a,_,clock=loop
    result=a.sync_daily()
    assert len(result['areas'])==3
    assert a.sync_daily() is None
    b=AlwaysOnService(a.outreach)
    b.planning.sync_recent=lambda **kw:{'complete':True,'stored':1,'areas':kw['areas']}
    clock[0]+=timedelta(days=1)
    assert b.sync_daily()['areas']!=result['areas']
    owner=a._lease()
    assert owner and b._lease() is None
    a._release(owner);assert b._lease()


def test_failed_sync_retries_same_areas_with_backoff(loop):
    a,_,clock=loop
    calls=[]
    a.planning.sync_recent=lambda **kw:(calls.append(kw['areas']) or {'complete':False,'stored':0})
    a.sync_daily();a.sync_daily()
    assert len(calls)==1
    clock[0]+=timedelta(hours=2);a.sync_daily()
    assert calls[0]==calls[1]


def test_credit_cap_reserves_uncertain_attempts(loop,monkeypatch):
    a,_,_=loop
    monkeypatch.setenv('OUTREACH_DRY_RUN','false')
    monkeypatch.setenv('OUTREACH_APOLLO_REVEAL_ENABLED','true')
    monkeypatch.setenv('OUTREACH_APOLLO_DAILY_CREDITS','2')
    contacts=[]
    for i in range(1,5):
        p,c,firm=target(loop,i)
        a.repo.execute('UPDATE prospect_contacts SET email=NULL,source_url=? WHERE id=?',('apollo-free:'+str(i),c['id']))
        contacts.append(a.repo.get('prospect_contacts',c['id']))
    a.contacts=SimpleNamespace(reveal_automatic=lambda cid:None)
    for c in contacts:a._reveal(c)
    for c in contacts:a._reveal(c)
    assert a.repo.rows('SELECT sum(credits) n FROM outreach_credit_ledger')[0]['n']==2


def test_inbox_placement_gates_new_mailbox_and_ramp(loop,monkeypatch):
    a,_,clock=loop
    monkeypatch.setenv('OUTREACH_MAILBOXES',json.dumps({'sales@ikeng.org':{'prefix':'NEW_','cap':20,'ramp':[5,10,20],'warmup_start':'2026-10-01'}}))
    monkeypatch.setenv('OUTREACH_SEED_ADDRESSES','["seed@example.test"]')
    assert a.cap('sales@ikeng.org')==0
    a.seed_result('sales@ikeng.org','seed@example.test','spam','Sai')
    assert a.cap('sales@ikeng.org')==0
    a.seed_result('sales@ikeng.org','seed@example.test','inbox','Sai')
    assert a.cap('sales@ikeng.org')==20
    clock[0]+=timedelta(days=31)
    assert a.cap('sales@ikeng.org')==0


def test_daily_cap_overflow_is_retained(loop,monkeypatch):
    a,m,clock=loop
    monkeypatch.setenv('OUTREACH_DRY_RUN','false')
    for i in range(1,8):enrol(loop,i)
    for i in range(7):
        a.outreach.run_due();clock[0]+=timedelta(seconds=91)
    assert len(m.calls)==5
    assert len(a.repo.rows("SELECT * FROM outreach_enrolments WHERE status='active'"))==7


def test_retry_explicit_rate_limit_and_no_duplicate(loop,monkeypatch):
    a,m,clock=loop
    eid,*_=enrol(loop)
    monkeypatch.setenv('OUTREACH_DRY_RUN','false')
    original=m.send
    def fail(message):raise ZohoError('rate_limit',True,True)
    m.send=fail
    assert a.outreach.run_due()['failed']==1
    assert a.repo.get('outreach_enrolments',eid)['status']=='active'
    events=a.repo.rows("SELECT * FROM outreach_events WHERE kind='email'")
    assert events[0]['status']=='retry' and events[0]['step_id'] is None
    clock[0]+=timedelta(minutes=16);m.send=original
    assert a.outreach.run_due()['sent']==1


def test_ambiguous_send_is_not_blindly_retried(loop,monkeypatch):
    a,m,_=loop
    eid,*_=enrol(loop)
    monkeypatch.setenv('OUTREACH_DRY_RUN','false');m.fail=True
    assert a.outreach.run_due()['failed']==1
    assert a.repo.get('outreach_enrolments',eid)['status']=='paused'
    m.fail=False
    assert a.outreach.run_due()['sent']==0


def test_health_and_summaries_never_disappear(loop):
    a,_,clock=loop
    target(loop);a.queue_projects()
    assert not a.health()['ok']
    a.put('heartbeat',{'at':clock[0].timestamp(),'failed':False})
    assert a.health()['ok']
    assert a.summary()['states'][0]['count']==1
    clock[0]+=timedelta(days=2)
    assert not a.health()['ok']


def test_dns_checks_are_offline_with_fakes(monkeypatch):
    from diagnostics.outreach_health import sending_domain_checks
    monkeypatch.setenv('OUTREACH_DKIM_SELECTORS','{"ikeng.online":["zoho"]}')
    results=sending_domain_checks(lambda name:['v=spf1 -all','v=DMARC1; p=reject','v=DKIM1; p=fake'])
    assert all(r['ok'] for r in results)

def test_tick_isolates_poll_failure_and_keeps_backlog(loop):
    a,m,clock=loop
    target(loop)
    def fail(**kwargs):raise RuntimeError('provider unavailable')
    a.outreach.poll_replies=fail
    a.identity_pending=lambda:None
    result=a.tick()
    assert result['failed'] and not m.calls
    assert a.summary()['states']
    assert not a.health()['ok']
    assert a.state('lease')['until']==0


def test_health_alert_is_idempotent_with_fake_sender(loop):
    a,_,_=loop
    calls=[];a.alert_sender=calls.append
    a.alert_health();a.alert_health()
    assert len(calls)==1 and calls[0]['recipient']=='sai@ikeng.co.uk'


def test_bounced_contact_preserves_project_for_replacement(loop):
    a,_,clock=loop
    eid,p,c,firm=enrol(loop)
    a.outreach.receive(eid,'Delivery failed: address not found','bounce:1')
    a.refresh_queue()
    row=a.repo.rows('SELECT * FROM outreach_project_queue')[0]
    assert row['enrolment_id'] is None and row['state']=='waiting enrichment'
    assert a.repo.get('prospect_contacts',c['id'])['bounced_at']
    assert a.repo.rows('SELECT * FROM suppression_list')


def test_seed_pending_uses_manual_confirmation(loop,monkeypatch):
    a,m,_=loop
    monkeypatch.setenv('OUTREACH_DRY_RUN','false')
    monkeypatch.setenv('OUTREACH_SEED_SEND_ENABLED','true')
    monkeypatch.setenv('OUTREACH_SEED_ADDRESSES','["seed@example.test"]')
    a.seed_pending();a.seed_pending()
    assert len(m.calls)==1
    row=a.repo.rows('SELECT * FROM outreach_seed_results')[0]
    assert row['placement']=='pending' and row['provider_id']
    a.seed_result('sj@ikeng.online','seed@example.test','inbox','Sai')
    assert a.repo.rows('SELECT * FROM outreach_seed_results')[0]['placement']=='inbox'


def test_template_change_requires_new_approval(loop,monkeypatch):
    a,m,_=loop
    enrol(loop)
    approval=a.state('template_approval');approval['steps'][0]['template']='old wording'
    a.put('template_approval',approval)
    monkeypatch.setenv('OUTREACH_DRY_RUN','false')
    assert a.outreach.run_due()['sent']==0 and not m.calls


def test_cooling_period_and_new_project_required(loop):
    a,_,clock=loop
    eid,p,c,firm=enrol(loop)
    a.repo.execute("UPDATE outreach_enrolments SET status='finished' WHERE id=?",(eid,))
    with pytest.raises(ValueError,match='cooling'):a._enrol(p,c,firm,'architect')
    clock[0]+=timedelta(days=91)
    with pytest.raises(ValueError,match='new project'):a._enrol(p,c,firm,'architect')


def test_automatic_reveal_requires_provider_verified_status(loop):
    from outreach.apollo import ProspectContactService
    a,_,_=loop
    p,c,firm=target(loop)
    a.repo.execute("UPDATE prospect_contacts SET email=NULL,email_status='unreviewed',source_url='apollo-free:person' WHERE id=?",(c['id'],))
    class HTTP:
        def post(self,*args,**kwargs):
            return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'person':{'email':'alex@studio1.example','email_status':'verified'}})
    a.repo.execute('INSERT INTO outreach_credit_ledger VALUES(?,?,?,?)',(c['id'],'2026-11-11',1,stamp(a.now())))
    ProspectContactService(a.outreach,HTTP(),'fake').reveal_automatic(c['id'])
    assert a.repo.get('prospect_contacts',c['id'])['email_status']=='corporate_verified'

def test_delayed_first_touch_does_not_compress_cadence(loop,monkeypatch):
    a,m,clock=loop
    eid,*_=enrol(loop)
    clock[0]+=timedelta(days=7)
    monkeypatch.setenv('OUTREACH_DRY_RUN','false')
    assert a.outreach.run_due()['sent']==1
    e=a.repo.get('outreach_enrolments',eid)
    assert e['next_step_at']==stamp(clock[0]+timedelta(days=4))


def test_complaints_slow_mailbox_and_suppress_firm(loop,monkeypatch):
    a,m,clock=loop
    eid,p,c,firm=enrol(loop)
    a.outreach.receive(eid,'This is a spam complaint feedback report','complaint:1')
    assert a.repo.rows('SELECT 1 FROM suppression_list WHERE identifier=?',(firm['domain'],))
    assert a.mailbox_rates('sj@ikeng.online')['complaints']==1
    assert a.cap('sj@ikeng.online')==2

def test_failed_mailbox_does_not_block_healthy_mailbox(loop,monkeypatch):
    a,m,_=loop
    enrol(loop)
    monkeypatch.setenv('OUTREACH_DRY_RUN','false')
    def poll(mailbox=None):
        if mailbox=='sai@ikeng.online':raise RuntimeError('mailbox unavailable')
        return {'processed':0}
    a.outreach.poll_replies=poll
    a.identity_pending=lambda:None
    result=a.tick()
    assert result['failed'] and len(m.calls)==1
    assert a.state('inbox_retry:sai@ikeng.online')['attempts']==1

def test_non_apollo_contact_never_reserves_paid_credit(loop,monkeypatch):
    a,_,_=loop
    p,c,firm=target(loop)
    monkeypatch.setenv('OUTREACH_DRY_RUN','false')
    monkeypatch.setenv('OUTREACH_APOLLO_REVEAL_ENABLED','true')
    a._reveal(c)
    assert not a.repo.rows('SELECT * FROM outreach_credit_ledger')


def test_first_batch_can_be_previewed_before_approval(loop):
    a,_,_=loop
    target(loop);a.queue_projects();a.put('template_approval',None)
    preview=a.preview()
    assert len(preview)==1 and preview[0]['approved'] is False
    assert 'Test Road' in preview[0]['body']
    assert not a.repo.rows('SELECT * FROM outreach_enrolments')


def test_planning_failure_does_not_block_authorised_rfq_retry(loop,monkeypatch):
    a,m,_=loop
    monkeypatch.setenv('OUTREACH_DRY_RUN','false')
    a.planning.sync_recent=lambda **kwargs:{'complete':False,'stored':0}
    a.identity_pending=lambda:None
    calls=[];a.rfq_retry=lambda **kwargs:calls.append(kwargs)
    result=a.tick()
    assert result['failed'] and len(calls)==1
