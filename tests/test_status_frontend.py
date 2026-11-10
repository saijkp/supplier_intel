"""Exercise the actual vanilla JS renderer/editor with a minimal DOM and fake API."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_status_renderer_copy_and_conflict_keep_unsaved_text(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is optional; needed to execute vanilla frontend checks')
    source = (Path(__file__).parents[1] / 'frontend/index.html').read_text(encoding='utf-8')
    start = source.index('function statusMarkdown(')
    end = source.index('function alertBox(', start)
    checks = r'''
const assert = require('node:assert/strict');
const elements = {};
const $ = id => elements[id] ||= {value:'',textContent:'',innerHTML:'',disabled:false,classList:{toggle(){}}};
const esc = s => String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let current = {content_md:'# Live\n- **Plans**\n<script>alert(1)</script>', updated_at:'2026-11-10T10:00:00Z', updated_by:'Sai'};
let copied = null, submitted = null;
Object.defineProperty(globalThis, 'navigator', {value:{clipboard:{writeText:async text=>{copied=text;}}}, configurable:true});
globalThis.confirm = () => true;
async function api(path, options) {
  if (options) { submitted=JSON.parse(options.body); const e=new Error('conflict'); e.status=409; throw e; }
  return current;
}
'''
    checks += source[start:end]
    checks += r'''
(async()=>{
  await loadStatus();
  assert.match($('statusRead').innerHTML, /<h1>Live<\/h1>/);
  assert.match($('statusRead').innerHTML, /<li><strong>Plans<\/strong><\/li>/);
  assert.ok(!$('statusRead').innerHTML.includes('<script>'));
  assert.match($('statusRead').innerHTML, /&lt;script&gt;/);
  assert.match($('statusMeta').textContent, /by Sai/);
  await $('statusCopy').onclick();
  assert.equal(copied,current.content_md);
  assert.equal($('statusNotice').textContent,'Copied');
  $('statusEdit').onclick();
  $('statusText').value='My unsaved phone edit';
  $('statusAuthor').value='Phone';
  await $('statusSave').onclick();
  assert.equal(submitted.expected_updated_at,current.updated_at);
  assert.equal($('statusText').value,'My unsaved phone edit');
  assert.equal(statusEditing,true);
  assert.match($('statusNotice').textContent,/Someone else updated it/);
  current={...current,content_md:'# New laptop edit',updated_at:'2026-11-10T11:00:00Z'};
  await loadStatus(true);
  assert.equal($('statusText').value,current.content_md);
  assert.equal(statusEditing,false);
  console.log('Status renderer, copy, edit conflict and reload passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    # Regex delimiters need a single escaped slash in the generated JS.
    checks = checks.replace(r'<\\/', r'<\/')
    script = tmp_path / 'status-checks.cjs'
    script.write_text(checks, encoding='utf-8')
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
