"""Execute project UI with fake DOM/API; no network or sends."""
from pathlib import Path
import shutil
import subprocess
import pytest


def test_project_ui_escapes_and_enrols_only_drafts(tmp_path):
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    html=Path('frontend/index.html').read_text(encoding='utf-8')
    source=html[html.index('async function loadPlanning()'):html.index('let outreachData=')]
    script=r'''
const assert=require('node:assert/strict');
const elements={};const $=id=>elements[id]||=({innerHTML:'',textContent:'',addEventListener(type,fn){this.listener=fn;}});
const esc=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
const calls=[];const outJSON=(method,body)=>({method,body});
async function api(path,opts){calls.push([path,opts]);if(path==='/planning/projects')return [{id:1,reference:'26/1',address:'<script>',description:'24 flats',authority:'Council',status:'Received',fit_score:80,fit_reasons:[],agent:'Studio',applicant:'Developer',source_url:'javascript:alert(1)',contacts:[{id:2,name:'Alex <img>'}]}];return {id:3};}
'''+source+r'''
(async()=>{await loadPlanning();assert.equal(calls.length,1);assert.ok(!$('planningProjects').innerHTML.includes('<script>'));assert.ok(!$('planningProjects').innerHTML.includes('href="javascript:'));assert.match($('planningProjects').innerHTML,/Alex &lt;img&gt;/);const button={dataset:{planningProject:'1',planningContact:'2'}};await $('planningProjects').listener({target:{closest(){return button;}}});assert.equal(calls[1][0],'/planning/projects/1/enrol');assert.equal(calls[1][1].body.contact_id,2);assert.match($('planningNotice').textContent,/Review and approve/);assert.equal(button.disabled,false);})().catch(e=>{console.error(e);process.exitCode=1});
'''
    path=tmp_path/'projects.cjs';path.write_text(script,encoding='utf-8')
    result=subprocess.run([node,str(path)],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
