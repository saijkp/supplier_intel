'use strict';
const $ = id => document.getElementById(id);
const labels = {unit_price:'Unit price',currency:'Currency (USD / CNY / GBP)',quoted_quantity:'Quoted quantity',moq:'MOQ',incoterm:'Incoterm',named_place:'Named place / port',production_lead_days:'Production lead time (days)',payment_terms:'Payment terms',valid_until:'Valid until (YYYY-MM-DD)'};
const numeric = new Set(['unit_price','quoted_quantity','moq','production_lead_days']);
const statusLabels = {comparable:'Complete · check terms',clarification_required:'Clarification needed',non_compliant:'Deviation / invalid offer'};
let current = null;
let editingSupplierId = null;
let base = sessionStorage.getItem('rfq.base') || (location.pathname.startsWith('/workspace/') ? location.origin : localStorage.getItem('si.base') || localStorage.getItem('si_api_url')) || '';
let token = sessionStorage.getItem('rfq.token') || localStorage.getItem('si.token') || localStorage.getItem('si_api_token') || '';
function normalizeApiUrl(value) {
  const raw = String(value || '').trim();
  if (!raw) throw new Error('Enter your Railway API address.');
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(raw) ? raw : 'https://' + raw;
  let url;
  try { url = new URL(withScheme); } catch { throw new Error('Enter a valid API address, such as https://your-service.up.railway.app.'); }
  if (!['https:', 'http:'].includes(url.protocol) || !url.hostname || url.username || url.password || url.search || url.hash || (url.pathname !== '/' && url.pathname !== '')) {
    throw new Error('Enter only the API address, without a path, query, or credentials.');
  }
  if (url.protocol === 'http:' && !['localhost', '127.0.0.1'].includes(url.hostname)) {
    throw new Error('Use HTTPS for a remote API.');
  }
  return url.origin;
}
try { if (base) base = normalizeApiUrl(base); } catch { base = ''; }
$('base').value = base; $('token').value = token;
function message(text, error=false) { $('message').textContent=text; $('message').className=error?'error':''; $('message').hidden=!text; }
function node(tag,text,cls) { const el=document.createElement(tag); if(text!==undefined)el.textContent=text; if(cls)el.className=cls; return el; }
async function api(path, data) {
  if(!base || !token) throw new Error('Connect your SupplierIntel API first.');
  const res=await fetch(base+path,{method:data?'POST':'GET',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:data?JSON.stringify(data):undefined});
  const body=await res.json().catch(()=>null);
  if(!res.ok) { const detail=body?.detail; throw new Error(res.status===404?'RFQ endpoints unavailable or record not found. The API must run this feature branch.':res.status===401?'Access token rejected.':res.status===503?'API access token is not configured on the server.':typeof detail==='string'?detail:detail?JSON.stringify(detail):'Request failed ('+res.status+').'); }
  return body;
}
async function run(fn, button) { if(button)button.disabled=true; message(''); try {await fn();} catch(e){message(e.message,true);} finally{if(button)button.disabled=false;} }
async function refresh() {
  const items=await api('/rfqs'); $('list').replaceChildren();
  if(!items.length)$('list').textContent='No RFQs yet. Create your first request above.';
  for(const item of items) { const b=node('button',item.title,'rfq-item'+(current?.id===item.id?' active':'')); b.append(node('small',`${item.quantity} ${item.unit} · ${item.quote_count} quote(s)`)); b.onclick=()=>run(async()=>{show(await api('/rfqs/'+item.id));await refresh();},b); $('list').append(b); }
}
function show(rfq) {
  current=rfq; $('workspace').hidden=false; $('empty').hidden=true; $('quoteEntry').hidden=true;
  if(window.renderOutreach)window.renderOutreach(rfq);
  const linkedLatest=new Map();
  for(const q of rfq.quotes)if(q.supplier_id)linkedLatest.set(q.supplier_id,q.id);
  const visibleQuotes=rfq.quotes.filter(q=>!q.supplier_id||linkedLatest.get(q.supplier_id)===q.id);
  $('rfqTitle').textContent=rfq.title; $('rfqSummary').textContent=`${rfq.customer || 'No customer specified'} · ${rfq.quantity} ${rfq.unit} · ${rfq.quotes.length} saved quote(s)`;
  $('rfqSpecs').textContent=Object.entries(rfq.mandatory_specs).map(([k,v])=>k+': '+v).join('\n');
  $('comparison').replaceChildren(); $('clarifications').replaceChildren();
  if(!visibleQuotes.length){$('comparison').textContent='No quotations recorded. Link outreach to collect replies, or add a supplier’s reply.';return;}
  const table=node('table'), head=node('tr'); head.append(node('th','Quote detail'));
  for(const q of visibleQuotes)head.append(node('th',q.supplier_name+'\n'+q.created_at+' UTC'));
  table.append(head);
  const rows=[['Status',q=>statusLabels[q.review.status]],...Object.entries(labels).map(([k,v])=>[v,q=>q.fields[k]??'Not supplied']),...Object.keys(rfq.mandatory_specs).map(k=>['Spec: '+k,q=>q.quoted_specs[k]??'Not confirmed']),['Missing / unconfirmed',q=>q.review.missing_fields.join('\n')||'None'],['Deviations',q=>q.review.deviations.join('\n')||'None'],['Validation issues',q=>q.review.errors.join('\n')||'None']];
  for(const [title,get] of rows){const tr=node('tr');tr.append(node('th',title));for(const q of visibleQuotes){const td=node('td');td.append(node('span',String(get(q)),title==='Status'?'badge '+q.review.status:undefined));tr.append(td);}table.append(tr);}
  $('comparison').append(table);
  for(const q of visibleQuotes){const card=node('div',undefined,'quote-card');card.append(node('h2',q.supplier_name));
    if(q.manual_review)card.append(node('p','Human review needed. Automatic follow-ups are stopped for this supplier. Check extraction, attachments or conflicting confirmations.'));
    for(const [key,conflict] of Object.entries(q.conflicts||{}))card.append(node('p',`${key}: previously ${conflict.previous}; new reply says ${conflict.candidate}. Review and record a revised offer to confirm the correct value.`));
    const history=rfq.quotes.filter(old=>old.supplier_id&&old.supplier_id===q.supplier_id&&old.id!==q.id);
    if(history.length){const versions=node('details');versions.append(node('summary',`${history.length} previous quote version(s)`));for(const old of history)versions.append(node('pre',old.created_at+' UTC\n'+JSON.stringify(old.fields,null,2)+'\n'+old.source_text));card.append(versions);}
    const details=node('details'), summary=node('summary','Original reply & field evidence');details.append(summary,node('pre',q.source_text||'No source text recorded.'));
    for(const [k,v] of Object.entries(q.evidence))details.append(node('p',k+': '+v));card.append(details);
    if(q.clarification_zh){card.append(node('p','Chinese clarification draft · review before sending'));const draft=node('textarea',undefined,'draft');draft.value=q.clarification_zh;draft.setAttribute('aria-label','Chinese clarification for '+q.supplier_name);const copy=node('button','Copy draft');copy.onclick=()=>run(async()=>{await navigator.clipboard.writeText(draft.value);message('Draft copied. Review it before sending to the supplier.');},copy);card.append(draft,copy);}else card.append(node('p','All required information has source evidence. No clarification draft needed.'));
    const revise=node('button','Record revised offer');revise.onclick=()=>openQuote(q);card.append(document.createTextNode(' '),revise);$('clarifications').append(card);
  }
}
function addInput(host, id, title, value='', type='text') {const div=node('div'), label=node('label',title), input=node('input');label.htmlFor=id;input.id=id;input.type=type;input.value=value;input.maxLength=5000;if(type==='number'){input.step='any';input.min='0';}div.append(label,input);host.append(div);return input;}
function openQuote(previous) {
  editingSupplierId=previous?.supplier_id||null;
  $('quoteForm').reset(); $('quoteFields').replaceChildren();$('specFields').replaceChildren();$('quoteEntry').hidden=false;
  if(previous){$('supplier').value=previous.supplier_name;$('source').value=previous.source_text;}
  for(const [k,label] of Object.entries(labels)){addInput($('quoteFields'),'f_'+k,label,previous?.fields[k]??'',numeric.has(k)?'number':k==='valid_until'?'date':'text');addInput($('quoteFields'),'e_'+k,'Source excerpt for '+label,previous?.evidence[k]??'');}
  let i=0; for(const k of Object.keys(current.mandatory_specs)){addInput($('specFields'),'s_'+i,k+' (requested: '+current.mandatory_specs[k]+')',previous?.quoted_specs[k]??'');addInput($('specFields'),'se_'+i,'Source excerpt for '+k,previous?.evidence['spec:'+k]??'');i++;}
  if(previous?.conflicts){for(const [key,value] of Object.entries(previous.conflicts)){if(key.startsWith('spec:')){const pos=Object.keys(current.mandatory_specs).indexOf(key.slice(5));if(pos>=0){$('s_'+pos).value=value.candidate;$('se_'+pos).value=value.evidence;}}else if($('f_'+key)){$('f_'+key).value=value.candidate;$('e_'+key).value=value.evidence;}}}
  $('quoteEntry').scrollIntoView({behavior:'smooth'});$('supplier').focus();
}
$('new').onclick=()=>{$('create').hidden=false;$('title').focus();}; $('cancel').onclick=()=>{$('create').hidden=true;};
$('add').onclick=()=>openQuote();$('cancelQuote').onclick=()=>{$('quoteEntry').hidden=true;};
$('connect').onsubmit=e=>{e.preventDefault();run(async()=>{
  const newBase = normalizeApiUrl($('base').value);
  const newToken = $('token').value.trim();
  if (!newToken) throw new Error('Enter your API access token.');
  base = newBase; token = newToken;
  $('base').value = newBase;
  current = null; $('workspace').hidden = true; $('empty').hidden = false;
  await refresh();
  sessionStorage.setItem('rfq.base', base);
  sessionStorage.setItem('rfq.token', token);
  $('connection').open = false;
  message('Connected. Saved RFQs loaded.');
},e.submitter);};
$('rfqForm').onsubmit=e=>{e.preventDefault();run(async()=>{const specs={};for(const line of $('specs').value.split('\n').filter(x=>x.trim())){const pos=line.indexOf('=');if(pos<1||!line.slice(pos+1).trim())throw new Error('Use name = value for each specification.');const key=line.slice(0,pos).trim();if(Object.hasOwn(specs,key))throw new Error('Duplicate specification: '+key);specs[key]=line.slice(pos+1).trim();}show(await api('/rfqs',{title:$('title').value,customer:$('customer').value,quantity:$('quantity').value,unit:$('unit').value,mandatory_specs:specs}));$('create').hidden=true;$('rfqForm').reset();$('unit').value='pcs';await refresh();message('RFQ saved. Add the first supplier quote.');},e.submitter);};
$('quoteForm').onsubmit=e=>{e.preventDefault();run(async()=>{const fields={},evidence={},specs={};for(const k of Object.keys(labels)){const value=$('f_'+k).value.trim();fields[k]=value?(numeric.has(k)?Number(value):value):null;const excerpt=$('e_'+k).value.trim();if(excerpt)evidence[k]=excerpt;}let i=0;for(const k of Object.keys(current.mandatory_specs)){if($('s_'+i).value.trim())specs[k]=$('s_'+i).value.trim();if($('se_'+i).value.trim())evidence['spec:'+k]=$('se_'+i).value.trim();i++;}show(await api('/rfqs/'+current.id+'/quotes',{supplier_id:editingSupplierId,supplier_name:$('supplier').value,source_text:$('source').value,fields,quoted_specs:specs,evidence,deviations:$('deviations').value.split('\n').map(x=>x.trim()).filter(Boolean)}));await refresh();message('Quote saved and validated. See comparison and clarification below.');},e.submitter);};
if(base&&token)run(refresh);else $('connection').open=true;
