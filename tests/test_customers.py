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

    def fetch_recent(self, since_days, limit):
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
    enquiry = s.create_enquiry(customer['id'], title='kitchen and wardrobes', status='qualifying',
                               next_action='INTERNAL: no customer price release', next_action_due='2026-01-01',
                               blocker='INTERNAL blocker')
    s.c, s.e = customer, enquiry
    return s


def test_enquiry_gets_reference_and_creation_logged(svc):
    assert svc.e['reference'].startswith('IKE-') and len(svc.e['reference']) == 10
    assert [a['kind'] for a in svc.get_enquiry(svc.e['id'])['activities']] == ['note']


def test_status_change_is_logged(svc):
    svc.update_enquiry(svc.e['id'], status='quoting', qualification={'scope': 'Kitchen', 'location': 'Salford', 'timeline': 'December'})
    kinds = [a['summary'] for a in svc.get_enquiry(svc.e['id'])['activities']]
    assert 'Status: qualifying -> quoting' in kinds


def test_due_list_shows_overdue_and_hides_closed(svc):
    due = svc.list_enquiries(due_only=True)
    assert [e['id'] for e in due] == [svc.e['id']] and due[0]['customer_name'] == 'Colin Fennelly'
    svc.close_enquiry(svc.e['id'], outcome='won', won_value=1000, close_note='Deposit received')
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
    assert svc.poll_inbox() == {'logged': 1, 'possible_new_enquiries': 1, 'ignored': 1, 'duplicates': 0}
    acts = svc.get_enquiry(svc.e['id'])['activities']
    assert acts[0]['kind'] == 'email_in' and 'Here are the drawings.' in acts[0]['summary']
    assert '> old' not in acts[0]['summary']
    assert svc.repo.get('customer_messages', m['id'])['status'] == 'cancelled'
    assert svc.poll_inbox()['duplicates'] == 2  # the auto-reply is re-read but never stored


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


def test_whatsapp_messages_carry_no_reference_line_but_emails_do(svc):
    ref = svc.e['reference']
    wa = svc.draft_followup(svc.e['id'], channel='whatsapp')
    assert ref not in wa['body'] and 'Ref:' not in wa['body'] and ref not in wa['action_url']
    assert wa['body'].startswith('Hi Colin,') and 'Sai Jyothis' in wa['body']
    custom = svc.draft_followup(svc.e['id'], channel='whatsapp', body='Hi Colin, quick check-in.\n\nThanks, Sai')
    assert custom['body'] == 'Hi Colin, quick check-in.\n\nThanks, Sai'
    edited = svc.edit_message(custom['id'], body='Hi Colin, updated.\n\nThanks, Sai')
    assert edited['body'] == 'Hi Colin, updated.\n\nThanks, Sai' and ref not in edited['action_url']
    email = svc.draft_followup(svc.e['id'], body='Hi Colin,\n\nQuick check-in.')
    assert email['body'].endswith(f'Ref: {ref}') and ref in email['subject']
    assert svc.edit_message(email['id'], body='Hi Colin, updated.')['body'].endswith(f'Ref: {ref}')


def test_closed_enquiry_cannot_be_followed_up(svc):
    svc.close_enquiry(svc.e['id'], outcome='lost', close_reason='price')
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


# -- not responding after 3 unanswered contacts --------------------------------------

def test_three_unanswered_contacts_move_to_not_responding(svc):
    eid = svc.e['id']
    svc.log(eid, 'email_out', 'chase 1', occurred_at='2026-11-01 09:00:00')
    svc.log(eid, 'call_out', 'chase 2', occurred_at='2026-11-02 09:00:00')
    assert svc.repo.get('customer_enquiries', eid)['status'] == 'qualifying'
    svc.log(eid, 'whatsapp_out', 'chase 3', occurred_at='2026-11-03 09:00:00')
    e = svc.repo.get('customer_enquiries', eid)
    assert e['status'] == 'not responding' and e['next_action_due'] == '2026-12-03'
    assert svc.list_enquiries(status='not responding')[0]['unanswered_contacts'] == 3
    # Kept, never closed: not overdue until the monthly re-check date.
    assert svc.list_enquiries(due_only=True) == []


def test_reply_resets_count_and_restores_enquiry(svc):
    eid = svc.e['id']
    for i in range(3):
        svc.log(eid, 'email_out', f'chase {i}', occurred_at=f'2026-11-0{i + 1} 09:00:00')
    svc.log(eid, 'whatsapp_in', 'Sorry, been away', occurred_at='2026-11-05 10:00:00')
    e = svc.repo.get('customer_enquiries', eid)
    assert e['status'] == 'qualifying' and e['next_action_due'] == __import__('customers.customer_service', fromlist=['now']).now()[:10]
    assert svc.unanswered_contacts(eid) == 0
    svc.log(eid, 'email_out', 'thanks', occurred_at='2026-11-05 11:00:00')
    assert svc.repo.get('customer_enquiries', eid)['status'] == 'qualifying'


def test_notes_and_meetings_do_not_count_as_chases(svc):
    eid = svc.e['id']
    for kind in ('note', 'meeting', 'note', 'meeting'):
        svc.log(eid, kind, 'x')
    assert svc.unanswered_contacts(eid) == 0


def test_closed_enquiry_is_not_moved(svc):
    eid = svc.e['id']
    svc.close_enquiry(eid, outcome='won', won_value=1000, close_note='Deposit received')
    for _ in range(4):
        svc.log(eid, 'email_out', 'x')
    assert svc.repo.get('customer_enquiries', eid)['status'] == 'won'


# -- possible new enquiries from the inbox --------------------------------------------

def test_inbox_sorts_new_people_from_suppliers_and_newsletters(svc):
    from storage.database import connection_scope
    with connection_scope(svc.repo.db_path) as conn:
        conn.execute("INSERT INTO suppliers(canonical_name, primary_email, domain) "
                     "VALUES ('Foshan Cab', 'sales@foshancab.com', 'foshancab.com')")
    svc._inbox.raw = [
        mail('Priya Shah <priya@gmail.com>', 'Kitchen for our new flat', 'Hi, can you quote a handleless kitchen?', '<n1@x>'),
        mail('Tom <tom@foshancab.com>', 'Re: catalogue', 'See attached', '<n2@x>'),
        mail('news@deals.com', 'Weekly deals', 'Buy now', '<n3@x>', List_Unsubscribe='<mailto:u@deals.com>'),
        mail('noreply@bank.com', 'Statement', 'Your statement', '<n4@x>'),
        mail('agent@supplier.cn', '[RFQ-4-ABC123] Request for quotation: hinges', 'price 1.2 USD', '<n5@x>'),
        mail('sai@ikeng.online', 'Sent by us', 'x', '<n6@x>'),
    ]
    assert svc.poll_inbox() == {'logged': 0, 'possible_new_enquiries': 1, 'ignored': 5, 'duplicates': 0}
    leads = svc.list_leads()
    assert [(l['from_address'], l['from_name']) for l in leads] == [('priya@gmail.com', 'Priya Shah')]
    assert 'handleless kitchen' in leads[0]['excerpt']
    assert svc.poll_inbox()['possible_new_enquiries'] == 0  # never listed twice


def test_convert_lead_creates_customer_and_enquiry(svc):
    svc._inbox.raw = [mail('Priya Shah <priya@gmail.com>', 'Kitchen for our new flat', 'Can you quote?', '<n1@x>')]
    svc.poll_inbox()
    lead = svc.list_leads()[0]
    e = svc.convert_lead(lead['id'], title='Handleless kitchen, Salford flat')
    assert e['customer']['email'] == 'priya@gmail.com' and e['customer']['name'] == 'Priya Shah'
    assert e['title'] == 'Handleless kitchen, Salford flat' and e['status'] == 'new'
    assert any(a['kind'] == 'email_in' and 'Can you quote?' in a['summary'] for a in e['activities'])
    assert svc.list_leads() == [] and svc.list_leads('added')[0]['enquiry_id'] == e['id']
    with pytest.raises(ValueError):
        svc.convert_lead(lead['id'])
    # Their next email is logged on the enquiry, not listed as a new lead.
    svc._inbox.raw = [mail('priya@gmail.com', 'Re: kitchen', 'Here are measurements', '<n7@x>')]
    assert svc.poll_inbox()['logged'] == 1


def test_dismiss_lead(svc):
    svc._inbox.raw = [mail('someone@company.co.uk', 'Partnership', 'Hello', '<n8@x>')]
    svc.poll_inbox()
    lead = svc.list_leads()[0]
    assert svc.dismiss_lead(lead['id'])['status'] == 'dismissed'
    assert svc.list_leads() == []


def test_dismissed_sender_is_not_listed_again_and_one_card_per_sender(svc):
    svc._inbox.raw = [mail('Lucy <lucy@forwarder.co.uk>', 'RE: DDP quote', 'Working on it', '<f1@x>'),
                      mail('Lucy <lucy@forwarder.co.uk>', 'RE: DDP quote', 'Here is the price', '<f2@x>')]
    assert svc.poll_inbox()['possible_new_enquiries'] == 1
    svc.dismiss_lead(svc.list_leads()[0]['id'])
    svc._inbox.raw = [mail('lucy@forwarder.co.uk', 'RE: DDP quote', 'Updated price', '<f3@x>')]
    assert svc.poll_inbox() == {'logged': 0, 'possible_new_enquiries': 0, 'ignored': 1, 'duplicates': 0}


def test_background_inbox_check_reads_mail_and_survives_failures(svc, monkeypatch):
    import asyncio
    from customers import inbox_worker

    svc._inbox.raw = [mail('Colin <colin@example.com>', 'Re: kitchen', 'Yes please go ahead.', '<w1@x>')]
    assert inbox_worker.tick(svc)['logged'] == 1

    # interval 0 = switched off: returns at once, never touches the mailbox
    asyncio.run(asyncio.wait_for(inbox_worker.run(lambda: 'unused', 0), 1))

    calls = []

    def flaky_tick(service):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('zoho down')
        raise asyncio.CancelledError

    async def no_sleep(_):
        return None

    monkeypatch.setattr(inbox_worker, 'tick', flaky_tick)
    monkeypatch.setattr(inbox_worker.asyncio, 'sleep', no_sleep)
    monkeypatch.setattr(inbox_worker, 'CustomerService', lambda repo: svc)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(inbox_worker.run(lambda: 'unused', 30))
    assert len(calls) == 2  # a failed check is logged and retried, not fatal

# -- fixed enquiry stages and explicit outcomes --------------------------------
from datetime import datetime, timedelta, timezone
from customers.customer_service import now, QUALIFICATION_KEYS, SUGGESTED_STATUSES
from storage.database import connection_scope


@pytest.mark.parametrize('stage', ['active', 'awaiting customer', 'unknown', '', None])
def test_fixed_stage_validation(svc, stage):
    with pytest.raises(ValueError, match='Invalid stage'):
        svc.create_enquiry(svc.c['id'], title='Invalid', status=stage)
    with pytest.raises(ValueError, match='Invalid stage'):
        svc.update_enquiry(svc.e['id'], status=stage)


@pytest.mark.parametrize('stage,days', list(cfg.CUSTOMER_STAGE_SLA_DAYS.items()))
def test_stage_entry_sla(svc, stage, days):
    e = svc.create_enquiry(svc.c['id'], title='SLA')
    if stage == 'quote sent':
        svc.add_quote(e['id'], status='sent', amount=100)
        e = svc.get_enquiry(e['id'])
    else:
        if stage == 'quoting':
            svc.update_enquiry(e['id'], qualification={'scope': 'Kitchen', 'location': 'Salford', 'timeline': 'Jan'})
        e = svc.update_enquiry(e['id'], status=stage) if stage != 'new' else e
    assert e['next_action_due'] == (datetime.now(timezone.utc) + timedelta(days=days)).strftime('%Y-%m-%d')
    assert e['next_action']


@pytest.mark.parametrize('field,value', [('next_action_due', None), ('next_action_due', ''), ('next_action_due', 'garbage'), ('next_action', ''), ('waiting_on', 'someone')])
def test_open_enquiry_cannot_lose_next_step(svc, field, value):
    with pytest.raises(ValueError):
        svc.update_enquiry(svc.e['id'], **{field: value})


def test_explicit_due_and_qualification_merge(svc):
    eid = svc.e['id']
    with pytest.raises(ValueError, match='scope, location, timeline'):
        svc.update_enquiry(eid, status='quoting')
    svc.update_enquiry(eid, qualification={'scope': 'Kitchen', 'budget': '5000'})
    with pytest.raises(ValueError, match='location, timeline'):
        svc.update_enquiry(eid, status='quoting')
    e = svc.update_enquiry(eid, status='quoting', next_action_due='2027-01-01', qualification={'location': 'Salford', 'timeline': 'December'})
    assert e['next_action_due'] == '2027-01-01'
    assert e['qualification']['budget'] == '5000'
    assert e['qualification_missing'] == ['plans', 'decision_maker']
    assert svc.list_enquiries()[0]['qualification_missing'] == e['qualification_missing']
    with pytest.raises(ValueError, match='qualification'):
        svc.update_enquiry(eid, qualification={'invented': 'x'})


def test_qualification_gate_has_no_side_routes(svc):
    e = svc.create_enquiry(svc.c['id'], title='Straight to quoting')
    with pytest.raises(ValueError, match='Qualification missing'):
        svc.update_enquiry(e['id'], status='quoting')  # new -> quoting
    svc.update_enquiry(e['id'], status='paused')
    with pytest.raises(ValueError, match='Qualification missing'):
        svc.update_enquiry(e['id'], status='quoting')  # paused -> quoting
    with pytest.raises(ValueError, match='Qualification missing'):
        svc.create_enquiry(svc.c['id'], title='Created as quoting', status='quoting')


def test_quote_sent_exit_and_superseding(svc):
    eid = svc.e['id']
    with pytest.raises(ValueError, match='sent quote'):
        svc.update_enquiry(eid, status='quote sent')
    q1 = svc.add_quote(eid, amount=1234, summary='Kitchen', status='draft')
    with pytest.raises(ValueError, match='sent quote'):
        svc.update_enquiry(eid, status='quote sent')
    q1 = svc.update_quote(eid, q1['id'], status='sent')
    e = svc.get_enquiry(eid)
    assert e['status'] == 'quote sent' and e['waiting_on'] == 'customer'
    assert e['next_action_due'] == (datetime.now(timezone.utc) + timedelta(days=3)).strftime('%Y-%m-%d')
    assert q1['sent_at'] and svc.unanswered_contacts(eid) == 0
    assert any(a['kind'] == 'note' and a['summary'] == 'Quote v1 sent: GBP 1234.0' for a in e['activities'])
    q2 = svc.add_quote(eid, amount=1300, status='sent')
    assert q2['version'] == 2
    assert svc.repo.get('customer_quotes', q1['id'])['status'] == 'superseded'
    svc.update_quote(eid, q2['id'], status='accepted')
    assert svc.get_enquiry(eid)['status'] == 'quote sent'
    assert svc.today()['ready_to_close'][0]['suggested_outcome'] == 'won'


@pytest.mark.parametrize('outcome', ['won', 'lost', 'closed'])
def test_explicit_close_and_reopen(svc, outcome):
    eid = svc.e['id']
    with pytest.raises(ValueError, match='/close'):
        svc.update_enquiry(eid, status=outcome)
    with pytest.raises(ValueError, match='/close'):
        svc.create_enquiry(svc.c['id'], title='No automatic closing', status=outcome)
    with pytest.raises(ValueError):
        svc.close_enquiry(eid, outcome)
    if outcome == 'won':
        with pytest.raises(ValueError, match='close_note'):
            svc.close_enquiry(eid, outcome, won_value=500)
        e = svc.close_enquiry(eid, outcome, won_value=500, close_note='Deposit received')
    else:
        with pytest.raises(ValueError, match='close_note'):
            svc.close_enquiry(eid, outcome, close_reason='other')
        e = svc.close_enquiry(eid, outcome, close_reason='other', close_note='Project cancelled')
    assert e['closed_at'] and e['next_action_due'] is None
    assert e['status'] == outcome
    with pytest.raises(ValueError, match='/reopen'):
        svc.update_enquiry(eid, status='new')
    e = svc.reopen_enquiry(eid, status='negotiating')
    assert e['status'] == 'negotiating' and e['closed_at'] is None
    assert e['next_action_due'] == (datetime.now(timezone.utc) + timedelta(days=3)).strftime('%Y-%m-%d')


def test_not_responding_restores_negotiating(svc):
    eid = svc.e['id']
    svc.update_enquiry(eid, status='negotiating')
    for _ in range(3):
        svc.log(eid, 'call_out', 'Called')
    e = svc.get_enquiry(eid)
    assert e['resume_status'] == 'negotiating'
    svc.log(eid, 'email_in', 'Please go ahead')
    e = svc.get_enquiry(eid)
    assert e['status'] == 'negotiating' and e['waiting_on'] == 'us' and e['next_action_due'] == now()[:10]


def test_today_groups_are_suggestions_only(svc):
    eid = svc.e['id']
    today = now()[:10]
    svc.update_enquiry(eid, next_action_due=today)
    exp = svc.add_quote(eid, amount=100, status='sent', valid_until=today)
    svc.update_enquiry(eid, next_action_due=today)
    gap = svc.create_enquiry(svc.c['id'], title='Needs qualification', status='qualifying', next_action_due='2020-01-01')
    quiet = svc.create_enquiry(svc.c['id'], title='Quiet', status='not responding', next_action_due='2020-01-01')
    paused = svc.create_enquiry(svc.c['id'], title='Paused', status='paused')
    svc.repo.update('customer_enquiries', paused['id'], created_at='2020-01-01 00:00:00')
    groups = svc.today()
    assert gap['id'] in [e['id'] for e in groups['overdue']]
    assert eid in [e['id'] for e in groups['due_today']]
    assert groups['quotes_expiring'][0]['quote_id'] == exp['id']
    assert groups['qualification_gaps'][0]['id'] == gap['id']
    assert {e['suggested_outcome'] for e in groups['ready_to_close']} == {'lost', 'closed'}
    assert svc.get_enquiry(quiet['id'])['status'] == 'not responding'
    assert svc.get_enquiry(paused['id'])['status'] == 'paused'


def test_pipeline_counts_and_close_reasons(svc):
    svc.update_enquiry(svc.e['id'], estimated_value='2500')
    e = svc.create_enquiry(svc.c['id'], title='Lost', estimated_value='900')
    svc.close_enquiry(e['id'], 'lost', close_reason='price')
    p = svc.pipeline()
    assert p['stages']['qualifying'] == {'count': 1, 'estimated_value': 2500.0}
    assert p['outcomes']['lost'] == {'count': 1, 'close_reasons': {'price': 1}}
    assert set(p['stages']) == set(SUGGESTED_STATUSES)


def test_new_api_endpoints(svc, monkeypatch):
    import api.app as mod
    import api.auth
    from api.customers import get_customer_service
    monkeypatch.setattr(api.auth, 'API_ACCESS_TOKEN', 'cust-test')
    mod.app.dependency_overrides[get_customer_service] = lambda: svc
    try:
        client = TestClient(mod.app, headers={'Authorization': 'Bearer cust-test'})
        path = f"/customers/enquiries/{svc.e['id']}"
        assert client.patch(path, json={'status': 'active'}).status_code == 400
        assert client.patch(path, json={'waiting_on': 'unknown'}).status_code == 400
        assert client.patch(path, json={'status': 'quoting'}).status_code == 400
        assert client.patch(path, json={'status': 'won'}).status_code == 400
        q = client.post(path + '/quotes', json={'amount': 500, 'status': 'sent'}).json()
        assert q['version'] == 1
        assert client.patch(path + f"/quotes/{q['id']}", json={'status': 'accepted'}).status_code == 200
        assert client.get('/customers/today').json()['ready_to_close'][0]['suggested_outcome'] == 'won'
        assert client.post(path + '/close', json={'outcome': 'won'}).status_code == 400
        assert client.post(path + '/close', json={'outcome': 'won', 'won_value': 500, 'close_note': 'Signed PO'}).json()['status'] == 'won'
        assert client.get('/customers/pipeline').json()['outcomes']['won']['count'] == 1
        assert client.post(path + '/reopen', json={'status': 'qualifying'}).json()['closed_at'] is None
    finally:
        mod.app.dependency_overrides.pop(get_customer_service, None)


def test_migration_39_maps_legacy_version_38(tmp_path, monkeypatch):
    import storage.database as db
    path = tmp_path / 'legacy.db'
    # Build the actual v38 customer shape, without v39 columns or quote table.
    fresh = db.SCHEMA_SQL
    cols = db.MIGRATIONS[39]['columns']
    legacy = fresh
    for _, name, kind in cols:
        legacy = legacy.replace(name + ' ' + kind + ', ', '').replace(name + ' ' + kind + ',', '')
    legacy = legacy.replace(db.CUSTOMER_QUOTES_SQL, '')
    with monkeypatch.context() as m:
        m.setattr(db, 'SCHEMA_SQL', legacy)
        m.setattr(db, 'MIGRATIONS', {k: v for k, v in db.MIGRATIONS.items() if k < 39})
        db.initialise_schema(path)
    with connection_scope(path) as conn:
        assert 'qualification' not in {r['name'] for r in conn.execute('PRAGMA table_info(customer_enquiries)')}
        conn.execute("INSERT INTO customers(id,name) VALUES(1,'Legacy')")
        statuses = ['active', 'awaiting customer', 'awaiting reply', 'awaiting qualification', 'owner_handled', 'deferred by owner', 'unqualified supplier', 'excluded', 'closed-lost', 'mystery', 'quoting', 'nurture', 'paused', 'not responding', 'new', 'lost', 'won', 'closed', 'quote sent', 'negotiating']
        for i, stage in enumerate(statuses, 1):
            conn.execute('INSERT INTO customer_enquiries(id,customer_id,reference,title,status) VALUES(?,1,?,?,?)', (i, f'IKE-{i:06}', 'Legacy', stage))
    db.initialise_schema(path)
    repo = CustomerRepository(path)
    expected = ['qualifying'] * 4 + ['paused'] * 3 + ['closed', 'lost', 'qualifying'] + statuses[10:]
    for e, old, stage in zip(repo.rows('customer_enquiries'), statuses, expected):
        assert e['status'] == stage
        assert bool(e['next_action_due']) == (stage not in {'won', 'lost', 'closed'})
        if stage not in {'won', 'lost', 'closed'}:
            assert e['next_action_due'] == now()[:10] and e['next_action']
        if old != stage:
            assert repo.rows('customer_activities', "enquiry_id=? AND kind='status_change'", (e['id'],))
        if old.startswith('awaiting'):
            assert e['waiting_on'] == 'customer'
        if old == 'active':
            assert e['waiting_on'] == 'us'
        if old == 'excluded':
            assert e['close_reason'] == 'not a customer'
        if old == 'closed-lost':
            assert e['close_reason'] == 'other' and e['close_note'] == 'migrated from closed-lost'
    n = len(repo.rows('customer_activities'))
    db.initialise_schema(path)
    assert len(repo.rows('customer_activities')) == n
    assert db.get_schema_version(path) == 39
