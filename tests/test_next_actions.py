"""Daily actions use only local records and fake mail transports."""
from datetime import datetime, timezone
from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from api.auth import require_api_token
from api.customers import router, get_customer_service
from customers.next_actions import next_actions
from rfq.zoho_mail import ZohoSender
from tests.test_customers import svc, mail
from tests.test_customer_mailboxes import service, SECOND, sent


@pytest.fixture
def client(service):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_api_token] = lambda: None
    app.dependency_overrides[get_customer_service] = lambda: service
    return TestClient(app)


def test_endpoint_ordering_done_snooze_and_empty(client, service):
    customer = service.create_customer(name='Buyer', email='buyer@example.test')
    old = service.create_enquiry(customer['id'], title='Old', next_action_due='2001-01-01')
    service.create_enquiry(customer['id'], title='Later', waiting_on='customer', next_action_due='2099-01-01')
    queue = client.get('/customers/next-actions').json()
    assert [i['enquiry'] for i in queue['items']] == [old['id']]
    assert queue['items'][0]['urgency'] == 0
    assert queue['next_scheduled']['due'] == '2099-01-01'
    assert client.post(f"/customers/enquiries/{old['id']}/action", json={'action':'snooze','due':'bad'}).status_code == 400
    assert client.post(f"/customers/enquiries/{old['id']}/action", json={'action':'done'}).status_code == 200
    assert client.get('/customers/next-actions').json()['items'] == []
    assert any(a['summary']=='Next action marked done' for a in service.get_enquiry(old['id'])['activities'])


def test_urgency_oldest_due_first_and_no_early_chase_of_not_responding(svc):
    svc.create_enquiry(svc.c['id'], title='Due soon', next_action_due='2026-11-11')
    svc.create_enquiry(svc.c['id'], title='Future', status='not responding', next_action_due='2099-01-01')
    q = next_actions(svc, datetime(2026,11,11,tzinfo=timezone.utc))
    assert [i['urgency'] for i in q['items']] == [0,1]
    assert q['items'][0]['enquiry'] == svc.e['id']


FORM = """Lead Source:
direct
Full name *:
Alex Example
Company (optional):
Example Homes
Email *:
alex@example.test
Phone / WhatsApp:
+44 7700 900111
Project name:
56-unit HMO
Location *:
Salford
Number of units:
56
Finish level:
Standard
Cat:
["Kitchen cabinets", "Beds"]
What do you need? Be as loose or precise as you like.:
Furnishing for 56 rooms
Required on-site date:
2027-04-01
Approximate budget:
50000 GBP
Anything else:
Plans available"""


def test_november_form_poll_and_one_click_conversion(svc):
    # Subject only, auto-generated notification, with folded whitespace and casing.
    svc._inbox.raw = [mail('no-reply@forms.example', 'Form submission from sourcing-brief FORM:', FORM, '<nov11>', Auto_Submitted='auto-generated')]
    result = svc.poll_inbox()
    assert result['possible_new_enquiries'] == 1 and result['ignored'] == 0
    lead = svc.list_leads()[0]
    assert lead['form_fields']['units'] == '56'
    enquiry = svc.convert_lead(lead['id'])
    assert enquiry['customer']['company'] == 'Example Homes'
    assert enquiry['customer']['email'] == 'alex@example.test'
    for value in ['56', 'Standard', 'Beds', '2027-04-01', '50000 GBP']:
        assert value in enquiry['requirements']
    assert enquiry['qualification']['location'] == 'Salford'
    assert svc.poll_inbox()['duplicates'] == 1
    assert not svc._sender.sent


class ZohoFake:
    def __init__(self): self.calls = []
    def request(self, method, path, **kw):
        self.calls.append((method,path,kw))
        return {'messageId':'fake-accepted'}


def compose(client, service):
    customer = service.create_customer(name='Buyer', email='buyer@example.test')
    enquiry = service.create_enquiry(customer['id'], title='Kitchen', blocker='PRIVATE COST 123')
    response = client.post('/customers/compose', json={'enquiry':enquiry['id'], 'body':'Thank you. Please send plans.'})
    assert response.status_code == 200
    message = response.json()
    assert message['mailbox'] == SECOND and 'PRIVATE' not in message['body']
    return message


def test_send_click_approves_once_through_fake_zoho(client, service):
    fake = ZohoFake()
    service.mailboxes()[SECOND].sender = ZohoSender(fake, address=SECOND)
    m = compose(client,service)
    assert fake.calls == []
    path = f"/customers/messages/{m['id']}/send-now"
    assert client.post(path).json()['status'] == 'sent'
    assert fake.calls[0][2]['json']['fromAddress'] == SECOND
    assert client.post(path).status_code == 400
    assert len(fake.calls) == 1


@pytest.mark.parametrize('block', ['cap','duplicate','sent_unavailable'])
def test_send_click_preserves_sent_safeguards(client, service, block):
    m = compose(client,service)
    box = service.mailboxes()[SECOND]
    if block == 'cap': box.inbox.sent_items = [sent(subject=str(i),mid=str(i)) for i in range(30)]
    if block == 'duplicate': box.inbox.sent_items = [sent(address=m['recipient'],subject=m['subject'])]
    if block == 'sent_unavailable': box.inbox.fail = True
    assert client.post(f"/customers/messages/{m['id']}/send-now").status_code == 400
    assert not box.sender.sent


def test_frontend_actions_are_real_and_no_mailto_primary():
    source = Path('frontend/index.html').read_text(encoding='utf-8')
    daily = source[source.index('async function loadToday'):source.index('let globalSearchSequence')]
    for value in ['/customers/next-actions','/customers/compose','/send-now','/convert','/action','https://wa.me/', 'Nothing needs you right now']:
        assert value in daily
    assert 'Use email app' in daily
    assert 'daily-buttons' in daily
    assert 'shell-v3' in Path('frontend/sw.js').read_text()


def test_queue_inbound_quotes_chases_outreach_and_test_exclusions(svc):
    from storage.rfq_repository import RFQRepository
    from storage.database import connection_scope
    repo = RFQRepository(svc.repo.db_path)
    test_id = repo.insert('campaigns', title='Test', product='test')
    campaign = repo.insert('campaigns', title='Kitchen', product='cabinets')
    old = repo.insert('messages', campaign_id=campaign, status='sent', sent_at='2026-11-09 12:00:00', recipient='factory@example.test')
    repo.insert('messages', campaign_id=test_id, status='sent', sent_at='2026-11-09 12:00:00')
    reply = repo.insert('replies', campaign_id=campaign, message_id=old, parse_status='pending', reply_type='quote', from_address='factory@example.test')
    svc.repo.insert('customer_inbox_leads', external_id='new-lead', from_name='New buyer', from_address='new@example.test', subject='Kitchen enquiry')
    with connection_scope(svc.repo.db_path) as conn:
        conn.execute("INSERT INTO whatsapp_cards(text) VALUES ('Please help furnish my home')")
        conn.execute("INSERT INTO outreach_events(kind,status,due_at,created_at,updated_at) VALUES ('call_task','pending','2026-11-11','2026-11-10','2026-11-10')")
    q = next_actions(svc, datetime(2026,11,11,15,tzinfo=timezone.utc))
    keys = [i['key'] for i in q['items']]
    assert 'reply-'+str(reply) in keys
    assert not any(k.startswith('chase-') for k in keys)  # real reply suppresses chase
    assert any(k.startswith('lead-') for k in keys)
    assert any(k.startswith('whatsapp-') for k in keys)
    assert any(k.startswith('outreach-') for k in keys)
    repo.update('replies', reply, reply_type='auto_reply')
    q = next_actions(svc, datetime(2026,11,11,15,tzinfo=timezone.utc))
    assert [i['key'] for i in q['items'] if i['key'].startswith('chase-')] == ['chase-'+str(old)]


def test_snooze_preserves_waiting_and_inbound_restores_queue(svc):
    svc.update_enquiry(svc.e['id'], waiting_on='us')
    result = svc.act(svc.e['id'], 'snooze', '2099-01-01')
    assert result['waiting_on'] == 'us'
    assert next_actions(svc)['items'] == []
    svc.log(svc.e['id'], 'email_in', 'Here are my plans')
    assert next_actions(svc)['items'][0]['enquiry'] == svc.e['id']


def test_actual_frontend_render_and_send_click(tmp_path):
    import shutil
    import subprocess
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for frontend execution')
    source = Path('frontend/index.html').read_text(encoding='utf-8')
    dates = source[source.index('function dailyDate('):source.index('async function loadToday(')]
    actions = source[source.index('async function loadToday('):source.index('window.refreshWorkspace=')]
    script = r"""
const assert=require('node:assert/strict');
const elements={};const $=id=>elements[id] ||= {innerHTML:'',textContent:''};
const esc=s=>String(s??'').replaceAll('<','&lt;').replaceAll('"','&quot;');
let workspaceBusy=false,dailyItems=[],dailyLoaded=false,dialog,calls=[];
const state={base:'http://fake.test',token:'fake'};
let queue={items:[{key:'enquiry-9',enquiry:9,title:'Buyer<script>',email:'buyer@example.test',phone:'+44 7700 900111',label:'Please reply',time:new Date().toISOString(),action:'Open enquiry',view:'customers'}]};
const document={querySelectorAll:()=>[],body:{append(){}},createElement:()=>{
const fields={};dialog={className:'',innerHTML:'',showModal(){},close(){this.onclose?.()},remove(){},querySelector:q=>fields[q] ||= {value:'',focus(){}}};return dialog;}};
const window={dispatchEvent(){}};const CustomEvent=class{};
async function api(path,options){calls.push({path,options});if(path==='/customers/next-actions')return queue;
if(path==='/customers/compose')return {id:4,status:'draft'};
if(path.endsWith('/send-now')){queue={items:[]};return {status:'sent'};}throw Error(path);}
"""+dates+actions+r"""
(async()=>{
await loadToday();
assert.match($('todayList').innerHTML,/Buyer&lt;script>/);
assert.match($('todayList').innerHTML,/Reply by email/);
assert.match($('todayList').innerHTML,/https:\/\/wa.me\/447700900111/);
assert.ok(!$('todayList').innerHTML.includes('mailto:'));
await dailyCompose(dailyItems[0]);
assert.equal(calls.filter(c=>c.path==='/customers/compose').length,0);
dialog.querySelector('textarea').value='Thanks. Please send plans.';
await dialog.querySelector('form').onsubmit({preventDefault(){}});
assert.deepEqual(calls.filter(c=>c.options).map(c=>c.path),['/customers/compose','/customers/messages/4/send-now']);
assert.match($('todayList').innerHTML,/Nothing needs you right now/);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    path = tmp_path/'actions.cjs'
    path.write_text(script,encoding='utf-8')
    result = subprocess.run([node,str(path)],capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stdout+result.stderr


def test_screenshot_conversion_is_manual_and_idempotent(client, service):
    from api.whatsapp import router as wa_router, get_whatsapp_inbox
    from customers.whatsapp_inbox import WhatsAppInbox
    from storage.database import connection_scope
    client.app.include_router(wa_router)
    client.app.dependency_overrides[get_whatsapp_inbox] = lambda: WhatsAppInbox(service.repo.db_path)
    with connection_scope(service.repo.db_path) as conn:
        card_id = conn.execute("INSERT INTO whatsapp_cards(text) VALUES ('Please help furnish my home')").lastrowid
    path = f'/customers/whatsapp/{card_id}/convert'
    result = client.post(path,json={'name':'Alex Example','phone':'+44 7700 900111'})
    assert result.status_code == 200
    assert client.post(path,json={'name':'Alex Example'}).json()['id'] == result.json()['id']
    assert len(service.repo.rows('customers')) == 1
    assert len(service.repo.rows('customer_activities', "kind='whatsapp_in'")) == 1
    assert not any(i['key'].startswith('whatsapp-') for i in next_actions(service)['items'])


def test_email_button_capability_is_configuration_only(service, monkeypatch):
    from config import settings as cfg
    monkeypatch.setattr(cfg,'RFQ_SEND_ENABLED',False)
    assert next_actions(service)['can_email'] is False
    monkeypatch.setattr(cfg,'RFQ_SEND_ENABLED',True)
    assert next_actions(service)['can_email'] is True


def test_snooze_migration_is_idempotent_and_preserves_enquiry(svc):
    from storage.database import connection_scope, initialise_schema, get_schema_version
    with connection_scope(svc.repo.db_path) as conn:
        conn.execute('ALTER TABLE customer_enquiries DROP COLUMN next_action_snoozed_until')
        conn.execute('DELETE FROM schema_migrations WHERE version=46')
    initialise_schema(svc.repo.db_path)
    initialise_schema(svc.repo.db_path)
    assert get_schema_version(svc.repo.db_path) == 52
    assert svc.get_enquiry(svc.e['id'])['title'] == svc.e['title']


def test_compose_never_guesses_a_missing_customer_name(client, service):
    lead = service.repo.insert('customer_inbox_leads', external_id='unknown-name', from_address='new@example.test', subject='Kitchen', excerpt='Please quote for a kitchen')
    payload = {'lead':lead, 'body':'Please send your plans.'}
    assert client.post('/customers/compose',json=payload).status_code == 400
    assert service.repo.rows('customers') == []
    payload['name'] = 'Alex Example'
    response = client.post('/customers/compose',json=payload)
    assert response.status_code == 200, response.text
    assert service.repo.rows('customers')[0]['name'] == 'Alex Example'
