"""Run the connection state machine against actual frontend JS."""
from pathlib import Path
import shutil
import subprocess
import pytest


def test_authenticated_connection_states(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for JS checks')
    source = Path('frontend/index.html').read_text(encoding='utf-8')
    api = source[source.index('async function api('):source.index('function statusMarkdown(')]
    # The API helper ends immediately before the status module's state.
    api = api[:api.index('\n}', api.index('return data')) + 2]
    connection = source[source.index('function requireAccessToken('):source.index('$("connBtn").addEventListener')]
    script = r'''
const assert=require('node:assert/strict');
const els={}; const $=id=>els[id] ||= {className:'',textContent:'',classList:{add(){},remove(){}}};
const document={body:{classList:{add(){},remove(){}}}};
const state={base:'https://example.test',token:'test-only',connected:false};
let status=200,down=false,requested;
async function fetch(url,opts){requested={url,opts}; if(down)throw Error();return {status,ok:status===200,headers:{get:()=> 'application/json'},text:async()=> '{}'};}
'''+api+connection+r'''
(async()=>{
await checkHealth();assert.equal(state.connected,true);assert.equal($('connBtn').textContent,'Connected');
assert.equal(requested.url,'https://example.test/customers/status');assert.ok(requested.opts.headers.Authorization);
status=401;await checkHealth();assert.equal(state.connected,false);assert.equal($('connDot').className,'dot amber');
state.token='';await checkHealth();assert.equal($('connBtn').textContent,'Access token missing or wrong');
down=true;await checkHealth();assert.equal($('connBtn').textContent,'API unreachable');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    path=tmp_path/'connection.cjs'
    path.write_text(script,encoding='utf-8')
    result=subprocess.run([node,str(path)],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr


def test_daily_dates_and_international_phone_validation(tmp_path):
    node=shutil.which('node')
    if not node:
        pytest.skip('Node is required for JS checks')
    source=Path('frontend/index.html').read_text(encoding='utf-8')
    dates=source[source.index('function dailyDate('):source.index('function dueChip(')]
    phone=source[source.index('function validDailyPhone('):source.index('const dailyPost=')]
    script="const assert=require('node:assert/strict');\n"+dates+phone+r"""
assert.equal(dailyDate('2026-11-11 10:00:00'),Date.parse('2026-11-11T10:00:00Z'));
assert.ok(validDailyPhone('+44 7700 900111'));
assert.ok(!validDailyPhone('07700 900111'));
assert.ok(!validDailyPhone('unknown'));
"""
    path=tmp_path/'daily.cjs';path.write_text(script,encoding='utf-8')
    result=subprocess.run([node,str(path)],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr


def test_customer_detail_combines_mailboxes_and_private_screenshots(tmp_path):
    node=shutil.which('node')
    if not node:
        pytest.skip('Node is required for JS checks')
    source=Path('frontend/customers.js').read_text(encoding='utf-8')
    script=r'''
const assert=require('node:assert/strict');
const elements={};const element=()=>({innerHTML:'',textContent:'',value:'open',classList:{add(){},remove(){},toggle(){}},append(){},addEventListener(){},querySelector(){return element()},focus(){},scrollIntoView(){}});
const $=id=>elements[id] ||= element();
const document={createElement:element,head:{appendChild(){}},body:{append(){}},querySelectorAll(){return []}};
const window={};const esc=s=>String(s??'').replaceAll('<','&lt;');const matchMedia=()=>({matches:true});
const e={id:5,customer:{id:2,name:'Buyer<script>',email:'buyer@example.test'},status:'new',qualification:{},qualification_missing:[],quotes:[],messages:[],activities:[]};
async function api(path){
if(path==='/customers/enquiries/5')return e;
if(path==='/customers/status')return {statuses:['new']};
if(path==='/customers/whatsapp')return [{id:3,enquiry_id:5,text:'WhatsApp text',created_at:'2026-11-11',images:[{id:7}]}];
if(path==='/customers/whatsapp/email-cards')return [{enquiry_id:5,mailbox:'first@example.test',text:'First mailbox'},{enquiry_id:5,mailbox:'second@example.test',text:'Second mailbox'},{enquiry_id:99,text:'Unrelated private enquiry'}];
throw Error(path);
}
'''+source+r'''
(async()=>{
await window.showCustomerEnquiry(5);
const html=$('custDetail').innerHTML;
assert.match(html,/id="customerDetailTitle"/);assert.match(html,/data-cust="back"/);
assert.match(html,/First mailbox/);assert.match(html,/Second mailbox/);assert.match(html,/WhatsApp text/);assert.match(html,/data-customer-image="7"/);
assert.ok(!html.includes('Unrelated private enquiry'));assert.ok(!html.includes('Buyer<script>'));
assert.match(html,/id="enNext"/);assert.match(html,/id="enDue"/);assert.match(html,/id="customerQuotes"/);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    path=tmp_path/'customer.cjs';path.write_text(script,encoding='utf-8')
    result=subprocess.run([node,str(path)],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
