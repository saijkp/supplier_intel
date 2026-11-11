"""Run real Outreach JavaScript with a fake DOM/API; no mailbox or network."""
from pathlib import Path
import shutil
import subprocess
import pytest


def test_outreach_readonly_render_escapes_and_preserves_gates(tmp_path):
    node=shutil.which('node')
    if not node:
        pytest.skip('Node required for frontend checks')
    html=Path('frontend/index.html').read_text(encoding='utf-8')
    source=html[html.index('let outreachData='):html.rindex("navigateWorkspace('today');")]
    script=r'''
const assert=require('node:assert/strict');
const elements={};const $=id=>elements[id]||=( {innerHTML:'',textContent:'',value:'',append(){},scrollIntoView(){},addEventListener(){}} );
const document={createElement:()=>({}),};const state={base:'https://fake.test',token:'fake'};
const window={};const esc=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const account={id:1,company:'Studio <script>',stage:'prospect',segment:'architect',priority:'high',contacts:[],events:[],enrolments:[],enquiries:[]};
const task={id:2,account_id:1,company:account.company,kind:'linkedin_task',payload:JSON.stringify({connection_note:'Hello <script>',body:'Draft message',action_url:'javascript:alert(1)'})};
async function api(path,opts){assert.ok(!opts,'render must never mutate/send');if(path==='/outreach/accounts')return [account];if(path==='/outreach/tasks')return [task];if(path==='/outreach/sequences')return [{id:3,title:'Architects',approved_at:null,contacts:[{id:1,name:'Jo',email:'jo@test.test',company:'Studio',subscriber_type:'unknown',email_status:'unreviewed',mailbox:'sj@ikeng.online'}],steps:[{channel:'email',day_offset:0,subject:'One drawing',template:'Hello {first_name}.'}]}];if(path==='/outreach/metrics')return {stages:{},contacted:0,replied:0,reply_rate:0,wins:0,won_gbp:0,margin_gbp:null,wins_per_100:0};if(path==='/outreach/accounts/1')return account;throw Error(path);}
'''+source+r'''
(async()=>{
await loadOutreach();assert.match($('outBoard').innerHTML,/Studio &lt;script&gt;/);assert.ok(!$('outTasks').innerHTML.includes('javascript:'));
assert.match($('outTasks').innerHTML,/Copy connection note/);assert.match($('outTasks').innerHTML,/Open in Sales Navigator/);
assert.match($('outSequences').innerHTML,/Needs approval/);assert.match($('outSequences').innerHTML,/unknown/);assert.match($('outSequences').innerHTML,/approve these templates and this exact list/);
assert.equal(outSafeLink('https://evil.example'),'https://www.linkedin.com/sales/search/people');
assert.deepEqual(outCsv('company,name,email\r\n"A, B",Jo,jo@test.test\r\n').map(x=>x.company),['A, B']);
assert.deepEqual(outCsv('company,fit_notes\nA,"Two\nlines"')[0].fit_notes,'Two\nlines');
await outAccount(1);assert.ok(!$('outDetail').innerHTML.includes('Studio <script>'));assert.match($('outDetail').innerHTML,/No project enquiry yet/);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    path=tmp_path/'outreach.cjs';path.write_text(script,encoding='utf-8')
    result=subprocess.run([node,str(path)],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
