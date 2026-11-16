"""Offline regressions for health semantics and the shared mailbox ledger."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import pytest
from tests.test_always_on_outreach import loop
from tests.test_prospect_outreach import engine
from rfq.send_cap import reserve, confirm, release, send_with_cap
from rfq.zoho_mail import ZohoError
from storage.database import connection_scope


@pytest.fixture
def ledger(tmp_path):
    path=tmp_path/'cap.db'
    with connection_scope(path) as db:
        db.execute('CREATE TABLE outreach_runtime(key TEXT PRIMARY KEY,payload TEXT)')
    return path


def test_mixed_paths_and_delayed_sent_share_five_slots(ledger):
    now=datetime(2026,11,16,9,tzinfo=timezone.utc)
    for i in range(5):
        r=reserve(ledger,'sj@ikeng.online',[],now)
        confirm(ledger,r,'zoho:'+str(i))
    with pytest.raises(ValueError,match='cap'):
        reserve(ledger,'sj@ikeng.online',[],now)
    reserve(ledger,'sai@ikeng.online',[],now)


def test_competing_fifth_slot(ledger):
    now=datetime(2026,11,16,9,tzinfo=timezone.utc)
    for i in range(4):confirm(ledger,reserve(ledger,'sj@ikeng.online',[],now),str(i))
    def attempt(_):
        try:return reserve(ledger,'sj@ikeng.online',[],now)
        except ValueError:return None
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(attempt,range(2)))
    assert sum(r is not None for r in results)==1


def test_confirmed_id_not_double_counted_and_release(ledger):
    now=datetime(2026,11,16,9,tzinfo=timezone.utc)
    r=reserve(ledger,'sj@ikeng.online',[],now);confirm(ledger,r,'zoho:1')
    sent=[{'messageId':'1','sentDateInGMT':now.timestamp()*1000}]
    r=reserve(ledger,'sj@ikeng.online',sent,now,cap=2);release(ledger,r)
    reserve(ledger,'sj@ikeng.online',sent,now,cap=2)


def test_uncertain_reservation_blocks(ledger):
    now=datetime(2026,11,16,9,tzinfo=timezone.utc)
    reserve(ledger,'sj@ikeng.online',[],now)
    with pytest.raises(ValueError,match='Uncertain'):reserve(ledger,'sj@ikeng.online',[],now)


@pytest.mark.parametrize('instant',['2026-07-01T23:00:00+00:00','2026-10-25T00:30:00+00:00','2026-10-25T01:30:00+00:00'])
def test_uk_calendar_boundary(ledger,instant):
    from customers.mailboxes import UK
    now=datetime.fromisoformat(instant)
    r=reserve(ledger,'sai@ikeng.online',[],now)
    assert r[0].endswith(now.astimezone(UK).date().isoformat())


def test_failure_tick_is_a_heartbeat_and_component_success_separate(loop):
    a,m,clock=loop
    a.put('pause',True)
    a.outreach.poll_replies=lambda **kw:(_ for _ in ()).throw(ZohoError('auth'))
    assert a.tick()['failed']
    h=a.health()
    assert h['last_tick_at']==clock[0].timestamp()
    assert not any('heartbeat' in reason.lower() for reason in h['reasons'])
    assert h['component_errors'][0]['count_24h']==1
    assert not h['components']['inbox:sj@ikeng.online'].get('last_success_at')
    clock[0]+=timedelta(hours=2)
    a.outreach.poll_replies=lambda **kw:None
    a.tick()
    assert a.health()['components']['inbox:sj@ikeng.online']['last_success_at']


def test_planning_backoff_preserves_source_result_and_is_not_recounted(loop):
    a,m,clock=loop
    a.planning.sync_recent=lambda **kw:{'complete':False,'errors':{'Croydon':'PlanIt 429 Retry-After: 295'},'stored':0}
    a.tick();a.tick()
    h=a.health()
    assert h['planning']['status']=='retrying'
    assert h['planning']['source']=='planning.sync_recent'
    assert h['planning']['result']['errors']['Croydon'].startswith('PlanIt 429')
    assert h['components']['planning']['status']=='retrying'
    assert next(e for e in h['component_errors'] if e['component']=='planning')['count_24h']==1
    clock[0]+=timedelta(hours=2)
    assert a.health()['planning']['status']=='overdue'


def test_manual_seed_and_engine_counters_deduplicate(loop):
    a,m,clock=loop
    for i in range(5):
        r=reserve(a.repo.db_path,'sj@ikeng.online',[],clock[0]);confirm(a.repo.db_path,r,str(i))
    for i in range(7):
        at=clock[0] if i<5 else clock[0]-timedelta(days=1)
        a.repo.execute("INSERT INTO outreach_events(kind,status,payload,provider_id,created_at,updated_at) VALUES('email','sent',?,?,?,?)",(json.dumps({'mailbox':'sj@ikeng.online'}),'zoho:'+str(i),at.isoformat(),at.isoformat()))
    rates=a.mailbox_rates('sj@ikeng.online')
    assert rates['sent_today_uk']==5 and rates['sent_30d']==7


def test_weekend_pause_and_busy_ticks(loop):
    a,m,clock=loop
    clock[0]=datetime(2026,11,15,9,tzinfo=timezone.utc)
    a.tick()
    assert a.health()['ok'] and not m.calls
    owner=a._lease()
    clock[0]+=timedelta(minutes=1)
    assert a.tick()['busy']
    assert a.health()['last_tick_at']==clock[0].timestamp()
    a._release(owner)


def test_manual_wrapper_retains_unknown_and_releases_rejection(loop):
    a,m,clock=loop
    box=a.outreach.mailboxes()['sj@ikeng.online']
    message={'recipient':'manual@example.test','subject':'Manual','body':'Test'}
    m.send=lambda message:(_ for _ in ()).throw(ZohoError('rate_limit',True,True))
    with pytest.raises(ZohoError):send_with_cap(box,message,a.repo.db_path,clock[0])
    assert a.state('send_cap:sj@ikeng.online:2026-11-11')=={}
    m.send=lambda message:(_ for _ in ()).throw(RuntimeError('unknown outcome'))
    with pytest.raises(RuntimeError):send_with_cap(box,message,a.repo.db_path,clock[0])
    with pytest.raises(ValueError,match='Uncertain'):send_with_cap(box,message,a.repo.db_path,clock[0])

def test_sai_thirty_and_hard_sj_ceiling(ledger):
    now=datetime(2026,11,16,9,tzinfo=timezone.utc)
    for i in range(30):confirm(ledger,reserve(ledger,'sai@ikeng.online',[],now),str(i))
    with pytest.raises(ValueError,match='cap'):reserve(ledger,'sai@ikeng.online',[],now)
    for i in range(5):confirm(ledger,reserve(ledger,'sj@ikeng.online',[],now,cap=30),str(i))
    with pytest.raises(ValueError,match='cap'):reserve(ledger,'sj@ikeng.online',[],now,cap=30)


def test_transport_flag_cannot_bypass_cap(loop,monkeypatch):
    from config import settings as cfg
    from rfq.zoho_mail import ZohoSender
    a,m,clock=loop
    monkeypatch.setenv('OUTREACH_SEND_ENABLED','false')
    monkeypatch.setattr(cfg,'DB_PATH',a.repo.db_path)
    now=datetime.now(timezone.utc)
    for i in range(5):confirm(a.repo.db_path,reserve(a.repo.db_path,'sj@ikeng.online',[],now),str(i))
    class Client:
        db_path=a.repo.db_path
        posts=0
        def request(self,method,path,**kwargs):
            if method=='POST':self.posts+=1;raise AssertionError('must not send')
            return [{'folderType':'Sent','folderId':'1'}] if path=='/folders' else []
    client=Client()
    with pytest.raises(ValueError,match='cap'):
        ZohoSender(client,'sj@ikeng.online').send({'recipient':'test@example.test','subject':'Test','body':'Test'})
    assert client.posts==0


def test_empty_success_and_identity_warning_are_success(loop):
    a,m,clock=loop
    a.planning.sync_recent=lambda **kw:{'complete':True,'stored':0,'identity_errors':{'ref':'No identity'}}
    assert not a.tick()['failed']
    assert a.health()['planning']['status']=='completed'
    assert a.health()['components']['planning']['last_success_at']
