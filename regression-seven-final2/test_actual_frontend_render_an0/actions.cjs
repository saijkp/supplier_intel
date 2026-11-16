
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
function dailyDate(value){if(!value)return 0;return Date.parse(/Z$|[+-]\d\d:\d\d$/.test(value)?value:value.replace(' ','T')+'Z')||0;}
function dueChip(value) {
  if(!value)return '';
  const date=value.slice(0,10),today=new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/London',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  return `<span class="status-chip ${date<today?'due-overdue':date===today?'due-today':''}">${date<today?'Overdue: ':date===today?'Today: ':''}${esc(date)}</span>`;
}
async function loadToday(){
  if(workspaceBusy||!state.base||!state.token)return;
  workspaceBusy=true;
  const oldKeys=new Set(dailyItems.map(x=>x.key));const hadItems=dailyLoaded;
  try {
    api('/outreach/always-on/status').then(out=>{
      const h=out.health;const counts=out.daily;
      $('outreachHealth').innerHTML=`<article class="panel ${h.ok?'':'due-overdue'}"><h2>Daily project outreach ${h.dry_run?' (dry run)':''}</h2><p>${h.reasons.map(esc).join('; ')||'Running on Railway'}</p><p>${counts.projects_found} projects | ${counts.decision_makers_found} decision makers | ${counts.credits_used} credits | ${counts.emails_sent} sent | ${counts.followups_due} follow-ups due | ${counts.replies} replies</p><p>${counts.identity_waiting} projects waiting for identity | ${counts.states.map(x=>`${esc(x.state)}: ${x.count}`).join(' | ')}</p><button class="btn" id="outreachReview">Review outreach and mailbox setup</button></article>`;
      $('outreachReview').onclick=()=>{document.querySelector('[data-workspace="outreach"]')?.click();};
    }).catch(()=>{$('outreachHealth').textContent='Outreach health unavailable. Refresh to retry.';});
    const queue=await api('/customers/next-actions');
    dailyItems=queue.items;
    const button=(i,action,label)=>`<button class="btn ${['email','create'].includes(action)?'btn-primary':''}" data-daily="${i}" data-action="${action}">${label}</button>`;
    $('todayList').innerHTML=dailyItems.length?dailyItems.map((item,i)=>`<article class="work-item" data-key="${esc(item.key)}"><div>${dueChip(item.due)}<h2>${esc(item.title)}</h2>${item.happened?`<p class="hint">${esc(item.happened)}</p>`:''}<p>${esc(item.label)}</p><p class="hint">${dailyAge(item.time)}</p></div><div class="daily-buttons">${button(i,item.lead&&item.can_create?'create':'open',item.lead&&!item.can_create?'Review submission':item.action)+(item.whatsapp?button(i,'create-whatsapp','Create enquiry'):'')}${item.email&&queue.can_email!==false?button(i,'email','Reply by email'):''}${item.enquiry&&validDailyPhone(item.phone)?`<a class="btn" target="_blank" rel="noopener" href="https://wa.me/${esc(item.phone.replace(/\D/g,''))}?text=${encodeURIComponent('Hello, thank you for your enquiry. When would suit you for a call?')}">Open WhatsApp</a>`:''}${item.enquiry?button(i,'done','Mark done')+button(i,'snooze','Snooze')+button(i,'waiting','Waiting on them'):''}${item.task?button(i,'task-done','Mark done'):''}</div></article>`).join(''):`<p class="empty-state">Nothing needs you right now</p>${queue.next_scheduled?`<p>Next: ${esc(queue.next_scheduled.title)} ? ${esc(queue.next_scheduled.action)} ${dueChip(queue.next_scheduled.due)}</p>`:''}`;
    document.querySelectorAll('.work-item').forEach(el=>{if(hadItems&&!oldKeys.has(el.dataset.key))el.classList.add('new-arrival');});
    $('todayNotice').textContent='';$('todayCount').textContent=dailyItems.length||'';$('phoneTodayCount').textContent=dailyItems.length||'';$('dueCount').textContent=dailyItems.filter(i=>i.urgency===0).length||'';$('quoteCount').textContent=dailyItems.filter(i=>i.reply).length||'';
    dailyLoaded=true;$('phoneQuoteCount').textContent=$('quoteCount').textContent;window.dispatchEvent(new CustomEvent('daily-updated'));return true;
  }catch(error){$('todayNotice').textContent=error.status===401?'':'Could not update daily work. Refresh to retry; previous items may be out of date.';return false;}
  finally{workspaceBusy=false;}
}
$('todayRefresh').onclick=loadToday;
function dailyAge(value){const minutes=Math.max(0,Math.floor((Date.now()-dailyDate(value))/60000));return minutes<1?'Just now':minutes<60?`${minutes}m ago`:minutes<1440?`${Math.floor(minutes/60)}h ago`:`${Math.floor(minutes/1440)}d ago`;}
function validDailyPhone(value){return /^\+?[1-9][\d ()-]{7,19}$/.test(value||'')&&!value.startsWith('0');}
const dailyPost=(path,body)=>api(path,{method:'POST',body:JSON.stringify(body)});
async function dailyCompose(item){
  const dialog=document.createElement('dialog');dialog.className='daily-compose';
  dialog.innerHTML=`<form><h2>Reply by email</h2><p>From sai@ikeng.co.uk to ${esc(item.email)}</p>${item.lead&&!item.can_create?'<label>Customer name from their message<input class="input" name="customerName" required maxlength="300"></label>':''}<label>Message<textarea class="input" rows="8" required aria-label="Email message"></textarea></label><p class="hint" role="status"></p><div class="daily-buttons"><button class="btn btn-primary" type="submit">Send</button><button class="btn" type="button" data-close>Cancel</button><a class="btn" href="mailto:${esc(item.email)}">Use email app</a></div></form>`;
  document.body.append(dialog);dialog.showModal();dialog.querySelector('textarea').focus();
  dialog.querySelector('[data-close]').onclick=()=>dialog.close();dialog.onclose=()=>dialog.remove();
  let draft;
  dialog.querySelector('form').onsubmit=async event=>{event.preventDefault();const send=dialog.querySelector('[type=submit]');send.disabled=true;
    try{const body=dialog.querySelector('textarea').value.trim();if(!body)throw Error('Write a reply first');
      if(!draft)draft=await dailyPost('/customers/compose',{enquiry:item.enquiry||null,lead:item.lead||null,name:dialog.querySelector('[name=customerName]')?.value||null,body});
      else if(draft.status==='draft')draft=await api('/customers/messages/'+draft.id,{method:'PATCH',body:JSON.stringify({body})});
      draft.status='approved';dialog.querySelector('textarea').readOnly=true;
      const result=await dailyPost('/customers/messages/'+draft.id+'/send-now',{});
      if(result.status!=='sent')throw Error('Message was not sent');dialog.close();await loadToday();
    }catch(error){dialog.querySelector('[role=status]').textContent=error.message;send.disabled=false;}
  };
}
$('todayList').onclick=async event=>{const b=event.target.closest('[data-daily]');if(!b)return;const item=dailyItems[Number(b.dataset.daily)],action=b.dataset.action;b.disabled=true;
  try{
    if(action==='email'){await dailyCompose(item);return;}
    if(action==='create-whatsapp'){const name=prompt('Customer name shown in the message');if(!name)return;const phone=prompt('Phone number shown in the message (international format)')||'';const enquiry=await dailyPost('/customers/whatsapp/'+item.whatsapp+'/convert',{name,phone});await loadToday();navigateWorkspace('customers');await window.showCustomerEnquiry(enquiry.id);return;}
    if(action==='create'){const enquiry=await dailyPost('/customers/leads/'+item.lead+'/convert',{});await loadToday();navigateWorkspace('customers');await window.showCustomerEnquiry(enquiry.id);return;}
    if(['done','snooze','waiting'].includes(action)){const due=action==='snooze'?prompt('Snooze until (YYYY-MM-DD)'):null;if(action==='snooze'&&!due)return;await dailyPost('/customers/enquiries/'+item.enquiry+'/action',{action,due});await loadToday();return;}
    if(action==='task-done'){const text=prompt('What happened?');if(!text)return;await dailyPost('/outreach/tasks/'+item.task+'/done',{text});await loadToday();return;}
    navigateWorkspace(item.view);if(item.enquiry&&window.showCustomerEnquiry)await window.showCustomerEnquiry(item.enquiry);if(item.campaign)await rfqAction(()=>showRFQ(item.campaign));if(item.reply){const reply=$('rfqReply'+item.reply);if(reply){reply.open=true;reply.scrollIntoView({block:'start'});}}
  }catch(error){$('todayNotice').textContent=error.message;}finally{b.disabled=false;}
};

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
