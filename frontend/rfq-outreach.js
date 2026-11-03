'use strict';
const outreachPanel=node('section',undefined,'panel');outreachPanel.id='outreachPanel';
$('workspace').insertBefore(outreachPanel,$('quoteEntry'));
function action(title,fn){const b=node('button',title);b.type='button';b.onclick=()=>run(fn,b);return b;}
async function reloadWorkspace(){const rid=current.id;const fresh=await api('/rfqs/'+rid);if(current.id===rid)show(fresh);await refresh();}
async function waitWorkflowJob(job){message('Collecting and checking supplier replies…');for(let i=0;i<240;i++){const state=await api('/pipeline/jobs/'+job.job_id);if(state.status==='failed')throw new Error(state.error||'Workflow failed; check mailbox settings.');if(state.status==='completed'){await reloadWorkspace();message('Workflow finished. Comparison and message states updated.');return;}await new Promise(resolve=>setTimeout(resolve,1500));}message('The job is still running. Refresh this RFQ to check its progress.');}
window.renderOutreach=async function(rfq){
  outreachPanel.replaceChildren(node('h2','Supplier outreach & reply collection'),node('p','Loading outreach…'));
  try{
    const view=await api('/rfqs/'+rfq.id+'/outreach');if(current.id!==rfq.id)return;
    outreachPanel.replaceChildren(node('h2','Supplier outreach & reply collection'));
    const ready=view.readiness;
    outreachPanel.append(node('p',`Mailbox connection: ${ready.transport==='zoho'?'Zoho Mail API':'SMTP / IMAP'} · Inbox: ${ready.imap_configured?'configured':'not configured'} · Sending: ${ready.ready?'ready':'not ready'} · Daily capacity remaining: ${ready.remaining_cap}`));
    if(!ready.imap_configured)outreachPanel.append(node('p','Zoho Free may not include IMAP. A Zoho Mail API connection is supported after server-side OAuth authorization. You can import WhatsApp chat exports or paste supplier replies now.','help'));
    if(ready.missing?.length)outreachPanel.append(node('p','Mailbox / buyer identity settings needed on the server: '+ready.missing.join(', '),'help'));
    if(!view.linked){
      outreachPanel.append(node('p','Connect this master RFQ to an existing campaign, or draft requests for suppliers already in your database. Initial requests are sent only when you approve them.'));
      const campaigns=await api('/rfqs/outreach/campaigns');if(current.id!==rfq.id)return;
      const form=node('form'),select=node('select');select.id='outreachCampaign';select.setAttribute('aria-label','Existing outreach campaign');
      select.append(new Option('Select an existing campaign',''));
      for(const c of campaigns.campaigns)if(c.status==='open')select.append(new Option(`${c.title} · ${c.quantity||'?'} ${c.unit||''}`,c.id));
      const label=node('label');const confirm=node('input');confirm.type='checkbox';confirm.id='confirmCampaign';confirm.style.width='auto';label.append(confirm,document.createTextNode(' I confirm this campaign matches the master quantity, unit and specifications.'));
      const link=node('button','Link campaign');link.className='primary';form.append(select,label,link);form.onsubmit=e=>{e.preventDefault();run(async()=>{if(!select.value||!confirm.checked)throw new Error('Select a campaign and confirm its specification matches.');await api('/rfqs/'+rfq.id+'/outreach/link',{campaign_id:Number(select.value),confirm_specification_match:true});await reloadWorkspace();},link);};outreachPanel.append(form);
      const create=node('form'),query=node('input');query.placeholder='Supplier search term, e.g. lighting';query.setAttribute('aria-label','Supplier search term');const ids=node('input');ids.placeholder='Or supplier IDs, separated by commas';ids.setAttribute('aria-label','Supplier IDs');const draft=node('button','Draft requests for matching suppliers');create.append(node('p','New campaign · up to 25 suppliers'),query,ids,draft);create.onsubmit=e=>{e.preventDefault();run(async()=>{const supplierIds=ids.value.trim()?ids.value.split(',').map(s=>Number(s.trim())):[];if(supplierIds.some(id=>!Number.isInteger(id)||id<1))throw new Error('Enter valid supplier IDs.');await api('/rfqs/'+rfq.id+'/outreach/create',{query:query.value,supplier_ids:supplierIds});await reloadWorkspace();message('Requests drafted. Review recipients and approve each initial email to send.');},draft);};outreachPanel.append(create);return;
    }
    const policy=view.policy,campaign=view.campaign;
    outreachPanel.append(node('p',`${campaign.title} · ${campaign.progress.complete}/${campaign.progress.suppliers} supplier quotes complete`));
    outreachPanel.append(node('p',`Extraction: ${ready.extraction_mode}. Last check: ${policy.last_run_at||'never'}${policy.last_run_at?' UTC':''}.`,'help'));
    if(policy.last_error)outreachPanel.append(node('p',policy.last_error));
    const controls=node('div',undefined,'row');controls.append(action('Collect replies & validate now',async()=>waitWorkflowJob(await api('/rfqs/'+rfq.id+'/outreach/sync',{}))),action('Refresh',reloadWorkspace));outreachPanel.append(controls);
    const policyForm=node('form');policyForm.append(node('h3','Automatic collection and follow-ups'));
    function checkbox(id,title,checked){const label=node('label'),input=node('input');input.type='checkbox';input.id=id;input.checked=checked;input.style.width='auto';label.append(input,document.createTextNode(' '+title));policyForm.append(label);return input;}
    const poll=checkbox('pollEnabled','Collect replies automatically, even when this browser is closed',!!policy.poll_enabled);
    const auto=checkbox('autoSend','Automatically email only the missing details to suppliers in this campaign',!!policy.auto_send);
    const fields=node('div',undefined,'fields');const interval=addInput(fields,'pollInterval','Check inbox every (minutes)',policy.interval_minutes,'number');interval.min=5;interval.max=1440;
    const cadence=addInput(fields,'followupCadence','Wait between follow-ups (hours)',policy.followup_hours,'number');cadence.min=1;cadence.max=168;
    const rounds=addInput(fields,'maxRounds','Maximum clarification rounds per supplier',policy.max_rounds,'number');rounds.min=1;rounds.max=5;
    const days=addInput(fields,'policyDays','Permission valid for (days)',14,'number');days.min=1;days.max=30;policyForm.append(fields);
    const consent=checkbox('approveAutomation','I approve this supplier list, cadence and follow-up limit. Stop on complete quotes, opt-out, delivery failure or required human review.',false);
    policyForm.append(node('p','Automatic follow-ups use email only. Marketplace, WeChat and WhatsApp remain assisted. Conflicting values, unreadable attachments and failed extraction require review.','help'));
    const save=node('button','Save workflow settings');save.className='primary';policyForm.append(save);
    policyForm.append(action('Pause automatic workflow',async()=>{await api('/rfqs/'+rfq.id+'/outreach/pause',{});await reloadWorkspace();message('Automatic workflow paused.');}));
    policyForm.onsubmit=e=>{e.preventDefault();run(async()=>{await api('/rfqs/'+rfq.id+'/outreach/policy',{poll_enabled:poll.checked,auto_send:auto.checked,interval_minutes:Number(interval.value),followup_hours:Number(cadence.value),max_rounds:Number(rounds.value),days:Number(days.value),approve_scope:consent.checked});await reloadWorkspace();message('Workflow settings saved.');},save);};outreachPanel.append(policyForm);
    const messages=node('details');messages.open=true;messages.append(node('summary',`Outreach messages & replies (${campaign.messages.length})`));
    for(const item of campaign.messages){const card=node('div',undefined,'quote-card');card.append(node('h3',`${item.supplier_name} · ${item.channel} · ${item.status}`),node('p',item.recipient||'No recipient'));
      if(item.status_reason)card.append(node('p',item.status_reason));
      const content=node('details');content.append(node('summary',item.kind.startsWith('clarification:')?'Targeted clarification':'Initial request'),node('pre',item.body));card.append(content);
      if(item.channel==='email'&&['draft','approved'].includes(item.status))card.append(action('Approve & send this email',async()=>waitWorkflowJob(await api('/rfqs/'+rfq.id+'/outreach/messages/'+item.id+'/send',{}))));
      if(item.channel!=='email'&&item.action_url){const link=node('a','Open supplier channel');link.href=item.action_url;link.target='_blank';link.rel='noopener noreferrer';card.append(link);}
      card.append(action('Copy message',async()=>{await navigator.clipboard.writeText(item.body);message('Message copied.');}));
      const paste=node('form'),text=node('textarea');text.placeholder='Paste this supplier’s real reply for automatic checking';text.setAttribute('aria-label','Supplier reply for '+item.supplier_name);text.maxLength=50000;const record=node('button','Record reply & check');paste.append(text,record);paste.onsubmit=e=>{e.preventDefault();run(async()=>{if(!text.value.trim())throw new Error('Paste the supplier reply first.');await api('/rfqs/'+rfq.id+'/outreach/messages/'+item.id+'/reply',{text:text.value});await reloadWorkspace();message('Supplier reply recorded and checked.');},record);};card.append(paste);
      const chat=node('details');chat.append(node('summary','Import WhatsApp chat export (.txt)'));
      const chatForm=node('form'),senderName=node('input'),file=node('input');senderName.placeholder='Exact supplier sender name in the export';senderName.setAttribute('aria-label','WhatsApp supplier sender for '+item.supplier_name);senderName.required=true;file.type='file';file.accept='.txt,text/plain';file.required=true;file.setAttribute('aria-label','WhatsApp chat export for '+item.supplier_name);const importButton=node('button','Import supplier messages');chatForm.append(node('p','Export the relevant chat without media. Only the supplier name you select is imported; buyer messages are excluded. Media needs separate review.','help'),senderName,file,importButton);
      chatForm.onsubmit=e=>{e.preventDefault();run(async()=>{const upload=file.files[0];if(upload.size>2000000)throw new Error('Select a chat export smaller than 2 MB.');const result=await api('/rfqs/'+rfq.id+'/outreach/messages/'+item.id+'/whatsapp-import',{supplier_sender:senderName.value,text:await upload.text()});await reloadWorkspace();message(`${result.imported} supplier message(s) imported. Up to ten replies are checked per cycle; use Collect replies & validate now for any remaining entries.`);},importButton);};chat.append(chatForm);card.append(chat);
      const replies=campaign.replies.filter(r=>r.message_id===item.id);for(const reply of replies){const detail=node('details');detail.append(node('summary',`Reply · ${reply.parse_status} · ${reply.received_at} UTC`),node('pre',reply.body_text));card.append(detail);}messages.append(card);
    }
    outreachPanel.append(messages);
    for(const entry of view.ingestions)if(entry.state==='needs_review')outreachPanel.append(node('p','Reply '+entry.reply_id+' needs human review. '+entry.detail));
    for(const supplier of new Set(view.clarifications.map(m=>m.supplier_id))){const sent=view.clarifications.filter(m=>m.supplier_id===supplier&&!['skipped','draft','approved'].includes(m.status));if(sent.length>=policy.max_rounds)outreachPanel.append(node('p','Follow-up limit reached for supplier '+supplier+'. Review their latest reply and contact them manually if needed.'));}
  }catch(error){if(current?.id===rfq.id)outreachPanel.replaceChildren(node('h2','Supplier outreach'),node('p',error.message));}
};
if(current)window.renderOutreach(current);
