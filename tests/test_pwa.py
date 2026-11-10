import json
from pathlib import Path
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]/'frontend'

def test_manifest_and_icons():
    manifest=json.loads((ROOT/'manifest.webmanifest').read_text())
    assert manifest['name']=='Supplier Intel'
    assert manifest['short_name']=='IK ENG'
    assert manifest['display']=='standalone'
    assert manifest['start_url']=='./index.html' and manifest['scope']=='./'
    assert any(i.get('purpose')=='maskable' for i in manifest['icons'])
    for icon in manifest['icons']:
        path=ROOT/icon['src']
        assert path.is_file()
        assert Image.open(path).size==tuple(map(int,icon['sizes'].split('x')))
    assert Image.open(ROOT/'icons/apple-touch-icon.png').size==(180,180)

def test_worker_only_handles_explicit_shell_and_never_authenticated_requests():
    worker=(ROOT/'sw.js').read_text()
    assert "request.headers.has('Authorization')" in worker
    assert '!shellURLs.has(request.url)' in worker
    assert "request.method !== 'GET'" in worker
    assert "cache: 'no-cache'" in worker
    assert 'console.html' not in worker and 'dashboard.html' not in worker

def test_worker_fetch_isolation_and_offline_shell(tmp_path):
    import shutil
    import subprocess
    import pytest
    node=shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    script=r'''
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const handlers={},writes=[],network=[];
let offline=false;
const cached={cached:true};
const cache={match:async()=>cached,put:async(req)=>writes.push(req.url)};
const context={URL,Set,Error,Promise,self:{registration:{scope:'https://example.test/'},addEventListener:(name,fn)=>handlers[name]=fn},caches:{open:async()=>cache},fetch:async(req)=>{network.push(req.url);if(offline)throw Error('offline');return {ok:true,clone(){return this;}};}};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
async function request(url,{method='GET',auth=false}={}){let response;handlers.fetch({request:{url,method,headers:{has:name=>auth&&name==='Authorization'}},respondWith:p=>response=p});return response;}
(async()=>{
 for(const url of ['https://api.example.test/customers','https://example.test/console.html','https://example.test/dashboard.html','https://example.test/index.html?token=secret'])assert.equal(await request(url),undefined);
 assert.equal(await request('https://example.test/index.html',{auth:true}),undefined);
 assert.equal(await request('https://example.test/index.html',{method:'POST'}),undefined);
 await request('https://example.test/index.html');assert.equal(network.length,1);assert.equal(writes.length,1);
 offline=true;assert.equal(await request('https://example.test/index.html'),cached);
 assert.equal(writes.length,1);console.log('Worker isolation, network-first and offline fallback passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    path=tmp_path/'worker-check.cjs';path.write_text(script)
    result=subprocess.run([node,str(path),str(ROOT/'sw.js')],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
