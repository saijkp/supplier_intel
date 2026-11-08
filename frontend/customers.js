/* Customers & enquiries tab (IK Eng's buy side). Uses $, esc and api() from index.html.
   All customer and reply text is untrusted: every value goes through esc(). */
(function () {
  const css = document.createElement('style');
  css.textContent = `
    .cust-bar{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin:8px 0 12px}
    .cust-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin:10px 0}
    .cust-grid label,.cust-detail label{display:flex;flex-direction:column;gap:4px;font-size:13px}
    .cust-wide{grid-column:1/-1}
    .cust-leads{border:2px solid var(--orange);border-radius:10px;padding:10px 14px;margin:10px 0}
    .cust-row{cursor:pointer}.cust-row:hover td{background:var(--paper)}
    .cust-due{color:var(--danger-dark);font-weight:700}
    .cust-detail{margin-top:18px;border-top:2px solid var(--rule);padding-top:14px}
    .cust-cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:18px}
    .cust-act{border-left:3px solid var(--rule);padding:4px 10px;margin:6px 0;white-space:pre-wrap;font-size:13px}
    .cust-act.in{border-color:var(--tier-verified)}.cust-act.out{border-color:var(--orange)}
    .cust-msg{border:1px solid var(--rule);border-radius:8px;padding:10px;margin:8px 0}
    @media (max-width:640px){.cust-cols{grid-template-columns:1fr}}`;
  document.head.appendChild(css);

  const closedStages = ['won', 'lost', 'closed'];
  const qualificationKeys = ['scope', 'location', 'budget', 'timeline', 'plans', 'decision_maker'];
  let current = null;
  const notice = (t) => { $('custNotice').textContent = t || ''; };
  const run = async (work) => { try { notice(''); await work(); } catch (e) { notice(e.message === 'NOT_CONNECTED' ? 'Connect to your API first.' : (e.detail || e.message)); } };
  const post = (path, body) => api(path, {method: 'POST', body: JSON.stringify(body || {})});
  const patch = (path, body) => api(path, {method: 'PATCH', body: JSON.stringify(body)});
  const day = (s) => (s || '').slice(0, 10);
  const today = () => new Date().toISOString().slice(0, 10);

  async function loadCustomers() {
    await run(async () => {
      const filter = $('custFilter').value;
      const qs = filter === 'due' ? '?due_only=true' : filter === 'all' ? '?include_closed=true' : filter === 'quiet' ? '?status=not%20responding' : '';
      const [status, rows, leads] = await Promise.all([api('/customers/status'), api('/customers/enquiries' + qs), api('/customers/leads')]);
      $('custLeads').innerHTML = leads.length ? `<div class="cust-leads"><h3>Possible new enquiries from email (${leads.length})</h3>
        <p class="hint">Review website submissions and first emails. Add each real enquiry, dismiss the rest.</p>` +
        leads.map(l => `<div class="cust-msg"><strong>${esc(l.from_name || l.from_address)}</strong> &lt;${esc(l.from_address)}&gt; · ${esc(l.received_at)}
          <div><strong>${esc(l.subject)}</strong></div>
          ${l.form_fields && Object.keys(l.form_fields).length ? `<div>${['phone', 'location', 'budget', 'requirement'].map(k => l.form_fields[k] ? `<div><strong>${esc(k)}:</strong> ${esc(l.form_fields[k])}</div>` : '').join('')}<div>${esc((l.form_fields.categories || []).join(', '))}</div>${!l.from_address ? '<strong>Missing valid customer email: review original submission</strong>' : ''}</div>` : ''}<div class="hint" style="white-space:pre-wrap">${esc((l.excerpt || '').slice(0, 500))}</div>
          <button class="btn btn-sm btn-primary" data-lead="convert" data-id="${Number(l.id)}">Add as customer enquiry</button>
          <button class="btn btn-sm btn-ghost" data-lead="dismiss" data-id="${Number(l.id)}">Not an enquiry</button></div>`).join('') + '</div>' : '';
      $('custReadiness').textContent = `${status.ready ? 'Email follow-ups ready' : 'Email sending unavailable'} · customer emails left in rolling 24h: ${status.remaining_cap}` +
        (status.missing.length ? ' · Missing: ' + status.missing.join(', ') : '');
      const pipeline = await api('/customers/pipeline');
      $('custPipeline').innerHTML = '<div class="cust-grid">' + Object.entries(pipeline.stages).filter(([, p]) => p.count).map(([stage, p]) => `<div class="cust-msg"><strong>${esc(stage)}</strong>: ${esc(p.count)}<br>GBP ${esc(p.estimated_value)}</div>`).join('') + '</div>';
      if (filter === 'today') {
        const groups = await api('/customers/today');
        $('custList').innerHTML = Object.entries(groups).map(([group, items]) => `<h3>${esc(group.replaceAll('_', ' '))} (${esc(items.length)})</h3><table class="tbl"><tbody>` + items.map(r => `<tr class="cust-row" data-enquiry="${esc(r.id)}"><td>${esc(r.reference)}<br>${esc(r.customer)}</td><td>${esc(r.stage)}<br>Waiting on: ${esc(r.waiting_on || '')}</td><td>${esc(r.next_action)}<br>${esc(r.due)}</td><td>${esc(r.why)}</td></tr>`).join('') + '</tbody></table>').join('');
        return;
      }
      const due = rows.filter(r => r.overdue).length;
      $('custList').innerHTML = `<p class="hint">${rows.length} enquiries · <span class="cust-due">${due} follow-ups due</span></p>
        <div style="overflow:auto"><table class="tbl"><thead><tr><th>Customer</th><th>Enquiry</th><th>Status</th><th>Next action</th><th>Due</th><th>Last heard</th></tr></thead><tbody>` +
        rows.map(r => `<tr class="cust-row" data-enquiry="${Number(r.id)}">
          <td><strong>${esc(r.customer_name)}</strong><br><span class="hint">${esc(r.reference)}</span></td>
          <td>${esc(r.title)}</td><td>${esc(r.status)}${r.unanswered_contacts ? `<br><span class="hint">${Number(r.unanswered_contacts)} contact${r.unanswered_contacts > 1 ? 's' : ''} without reply</span>` : ''}</td>
          <td>${esc((r.next_action || '').slice(0, 140))}${(r.next_action || '').length > 140 ? '…' : ''}</td>
          <td class="${r.overdue ? 'cust-due' : ''}">${esc(day(r.next_action_due)) || '-'}</td>
          <td>${esc(day(r.last_customer_contact_at)) || '-'}</td></tr>`).join('') + '</tbody></table></div>';
    });
  }
  window.loadCustomers = loadCustomers;

  async function showEnquiry(id) {
    await run(async () => {
      const e = await api('/customers/enquiries/' + Number(id)); current = e;
      const c = e.customer, status = await api('/customers/status');
      const isClosed = closedStages.includes(e.status);
      const opts = (status.statuses || []).filter(s => !closedStages.includes(s)).map(s => `<option ${s === e.status ? 'selected' : ''}>${esc(s)}</option>`).join('');
      $('custDetail').innerHTML = `<div class="cust-detail">
        <h3>${esc(c.name)} · ${esc(e.title)} <span class="hint">${esc(e.reference)}</span></h3>
        <div class="cust-cols">
          <div>
            <h4>Customer profile</h4>
            <div class="cust-grid">
              <label>Name<input class="input" id="cuName" value="${esc(c.name)}"></label>
              <label>Company<input class="input" id="cuCompany" value="${esc(c.company)}"></label>
              <label>Email<input class="input" id="cuEmail" value="${esc(c.email)}"></label>
              <label>Phone<input class="input" id="cuPhone" value="${esc(c.phone)}"></label>
              <label>WhatsApp<input class="input" id="cuWhatsapp" value="${esc(c.whatsapp)}"></label>
              <label>Location<input class="input" id="cuLocation" value="${esc(c.location)}"></label>
              <label class="cust-wide">Notes<textarea class="input" id="cuNotes" rows="3">${esc(c.notes)}</textarea></label>
            </div>
            <button class="btn btn-sm" data-cust="save-customer">Save profile</button>
            <h4>Enquiry</h4>
            <div class="cust-grid">
              <label>Stage ${isClosed ? esc(e.status) : ''}<select class="input" id="enStatus" ${isClosed ? 'disabled' : ''}>${opts}</select></label>
              <label>Waiting on<select class="input" id="enWaiting">${['', 'us', 'customer', 'supplier'].map(w => `<option value="${esc(w)}" ${w === (e.waiting_on || '') ? 'selected' : ''}>${esc(w || 'Unspecified')}</option>`).join('')}</select></label>
              <label>Next action due<input class="input" type="date" id="enDue" value="${esc(day(e.next_action_due))}"></label>
              <label>Estimated value<input class="input" id="enValue" value="${esc(e.estimated_value)}"></label>
              <label>Owner<input class="input" id="enOwner" value="${esc(e.owner)}"></label>
              <label class="cust-wide">Next action (internal)<textarea class="input" id="enNext" rows="2">${esc(e.next_action)}</textarea></label>
              <label class="cust-wide">Blocker (internal)<textarea class="input" id="enBlocker" rows="2">${esc(e.blocker)}</textarea></label>
              <label class="cust-wide">Requirements<textarea class="input" id="enReq" rows="4">${esc(e.requirements)}</textarea></label>
              <label class="cust-wide">Summary (internal)<textarea class="input" id="enSummary" rows="4">${esc(e.summary)}</textarea></label>
            </div>
            <button class="btn btn-sm" data-cust="save-enquiry">Save enquiry</button>
            <p class="hint">Linked supplier RFQ campaigns: ${(e.rfq_campaign_ids || []).map(Number).join(', ') || 'none'}
              <input class="input" id="enCampaign" type="number" min="1" placeholder="campaign id" style="width:120px;display:inline-block">
              <button class="btn btn-sm btn-ghost" data-cust="link">Link</button></p>
          </div>
          <div>
            <h4>Qualification</h4><p class="hint">Use only the customer's own messages. Blank fields are gaps.</p>
            <div class="cust-grid">${qualificationKeys.map(k => `<label class="${e.qualification_missing.includes(k) ? 'cust-due' : ''}">${esc(k.replaceAll('_', ' '))}<textarea class="input" id="qual_${esc(k)}">${esc(e.qualification[k] || '')}</textarea></label>`).join('')}</div>
            <button class="btn btn-sm" data-cust="qualification">Save qualification</button>
            <h4>Customer quotes</h4><p class="hint">Customer prices only. Never enter supplier costs.</p>
            ${e.quotes.map(q => `<div class="cust-msg">v${esc(q.version)}: ${esc(q.currency)} ${esc(q.amount)} · ${esc(q.status)}<br>${esc(q.summary)}<br>Sent ${esc(q.sent_at)} · Valid until ${esc(q.valid_until)}<br>${['sent', 'accepted', 'rejected'].map(st => `<button class="btn btn-sm" data-cust="quote-status" data-id="${esc(q.id)}" data-status="${esc(st)}">${esc(st === 'sent' ? 'Mark sent' : st)}</button>`).join('')}</div>`).join('')}
            <details><summary>Add quote</summary><div class="cust-grid"><label>Amount<input class="input" id="qAmount" type="number" step="0.01"></label><label>Currency<input class="input" id="qCurrency" value="GBP"></label><label>Summary<textarea class="input" id="qSummary"></textarea></label><label>Valid until<input class="input" id="qUntil" type="date"></label></div><button class="btn btn-sm" data-cust="add-quote">Add quote</button></details>
            ${isClosed ? `<label>Reopen stage<select class="input" id="reopenStage">${['new','qualifying','quoting','quote sent','negotiating'].map(st => `<option>${esc(st)}</option>`).join('')}</select></label><button class="btn" data-cust="reopen">Reopen</button>` : `<button class="btn" data-cust="close-form">Close enquiry</button><div id="closeForm" class="hidden"><label>Outcome<select class="input" id="closeOutcome"><option>won</option><option>lost</option><option>closed</option></select></label><label>Won value (won only)<input class="input" id="closeValue" type="number" step="0.01"></label><label>Reason (lost/closed)<select class="input" id="closeReason">${['price','timing','went elsewhere','no project','no response','not a customer','duplicate','other'].map(r => `<option>${esc(r)}</option>`).join('')}</select></label><label>Proof of order/deposit (won), or closing note<textarea class="input" id="closeNote"></textarea></label><button class="btn" data-cust="close">Confirm close</button></div>`}
            <h4>Follow-ups</h4>
            <p class="hint">Drafts never include internal notes. Nothing sends until you approve; a customer reply cancels pending follow-ups.</p>
            <button class="btn btn-sm" data-cust="draft-email" ${c.email ? '' : 'disabled title="Add an email to the profile first"'}>Draft email follow-up</button>
            <button class="btn btn-sm btn-ghost" data-cust="draft-whatsapp" ${c.whatsapp || c.phone ? '' : 'disabled title="Add a phone/WhatsApp number first"'}>Draft WhatsApp follow-up</button>
            ${e.messages.map(m => `<div class="cust-msg">
              <div><strong>${esc(m.channel)}</strong> to ${esc(m.recipient)} · <strong>${esc(m.status)}</strong> ${esc(m.status_reason || '')} ${m.sent_at ? '· sent ' + esc(m.sent_at) : ''}</div>
              ${m.channel === 'email' ? `<label>Subject<input class="input" id="cmSub${Number(m.id)}" value="${esc(m.subject)}" ${m.status === 'draft' ? '' : 'disabled'}></label>` : ''}
              <label>Message<textarea class="input" rows="7" id="cmBody${Number(m.id)}" ${m.status === 'draft' ? '' : 'disabled'}>${esc(m.body)}</textarea></label>
              ${m.status === 'draft' ? `<button class="btn btn-sm btn-ghost" data-cust-msg="save" data-id="${Number(m.id)}">Save</button>` : ''}
              ${m.status === 'draft' && m.channel === 'email' ? `<button class="btn btn-sm" data-cust-msg="approve" data-id="${Number(m.id)}">Approve</button>` : ''}
              ${m.status === 'approved' && m.channel === 'email' ? `<button class="btn btn-sm btn-primary" data-cust-msg="send" data-id="${Number(m.id)}">Send now</button>` : ''}
              ${m.channel === 'whatsapp' && ['draft', 'approved'].includes(m.status) && /^https:\/\/wa\.me\//.test(m.action_url || '') ? `<a class="btn btn-sm" href="${esc(m.action_url)}" target="_blank" rel="noopener noreferrer">Open WhatsApp</a> <button class="btn btn-sm" data-cust-msg="mark-sent" data-id="${Number(m.id)}">Mark sent</button>` : ''}
              ${['draft', 'approved'].includes(m.status) ? `<button class="btn btn-sm btn-ghost" data-cust-msg="cancel" data-id="${Number(m.id)}">Cancel</button>` : ''}
            </div>`).join('')}
            <h4>Log activity</h4>
            <div class="cust-grid">
              <label>Type<select class="input" id="acKind">
                <option value="note">Note</option><option value="call_out">Call (we called)</option><option value="call_in">Call (they called)</option>
                <option value="whatsapp_in">WhatsApp from customer</option><option value="whatsapp_out">WhatsApp to customer</option>
                <option value="email_in">Email from customer</option><option value="email_out">Email to customer</option><option value="meeting">Meeting / visit</option>
              </select></label>
              <label class="cust-wide">What happened<textarea class="input" id="acText" rows="3"></textarea></label>
            </div>
            <button class="btn btn-sm" data-cust="log">Add to timeline</button>
            <h4>Timeline</h4>
            ${e.activities.map(a => `<div class="cust-act ${a.kind.endsWith('_in') ? 'in' : a.kind.endsWith('_out') || a.kind === 'meeting' ? 'out' : ''}"><span class="hint">${esc(a.occurred_at)} · ${esc(a.kind)}</span>\n${esc(a.summary)}</div>`).join('')}
          </div>
        </div></div>`;
      $('custDetail').scrollIntoView({behavior: 'smooth', block: 'start'});
    });
  }

  const val = (id) => $(id).value.trim();
  $('view-customers').addEventListener('click', (ev) => {
    const row = ev.target.closest('tr[data-enquiry]');
    if (row) { showEnquiry(row.dataset.enquiry); return; }
    const b = ev.target.closest('button'); if (!b) return;
    if (b.id === 'custRefresh') { loadCustomers(); if (current) showEnquiry(current.id); return; }
    if (b.id === 'custNewToggle') { $('custCreate').classList.toggle('hidden'); return; }
    if (b.id === 'custPoll') {
      run(async () => { const r = await post('/customers/poll'); notice(`Inbox checked: ${r.logged} customer replies logged, ${r.possible_new_enquiries} possible new enquiries, ${r.ignored} supplier/automated emails ignored.`); await loadCustomers(); if (current) await showEnquiry(current.id); });
      return;
    }
    if (b.dataset.lead) {
      const id = Number(b.dataset.id);
      run(async () => {
        if (b.dataset.lead === 'dismiss') { await post(`/customers/leads/${id}/dismiss`); notice('Dismissed.'); await loadCustomers(); return; }
        const e = await post(`/customers/leads/${id}/convert`, {});
        await loadCustomers(); await showEnquiry(e.id); notice('Added ' + e.reference + '. Give it a clear title and next action.');
      });
      return;
    }
    const act = b.dataset.cust, msg = b.dataset.custMsg; if (!current || (!act && !msg)) return;
    const eid = current.id, cid = current.customer.id;
    run(async () => {
      if (act === 'save-customer') await patch('/customers/' + cid, {name: val('cuName'), company: val('cuCompany'), email: val('cuEmail'), phone: val('cuPhone'), whatsapp: val('cuWhatsapp'), location: val('cuLocation'), notes: $('cuNotes').value});
      if (act === 'save-enquiry') await patch('/customers/enquiries/' + eid, {...(!closedStages.includes(current.status) ? {status: val('enStatus')} : {}), waiting_on: val('enWaiting') || null, ...((val('enStatus') === current.status || val('enDue') !== day(current.next_action_due)) ? {next_action_due: val('enDue') || null} : {}), estimated_value: val('enValue'), owner: val('enOwner'), next_action: $('enNext').value, blocker: $('enBlocker').value, requirements: $('enReq').value, summary: $('enSummary').value});
      if (act === 'close-form') { $('closeForm').classList.toggle('hidden'); return; }
      if (act === 'qualification') await patch(`/customers/enquiries/${eid}`, {qualification: Object.fromEntries(qualificationKeys.map(k => [k, val('qual_' + k)]))});
      if (act === 'add-quote') await post(`/customers/enquiries/${eid}/quotes`, {...(val('qAmount') ? {amount: Number(val('qAmount'))} : {}), currency: val('qCurrency'), summary: val('qSummary'), valid_until: val('qUntil') || null});
      if (act === 'quote-status') await patch(`/customers/enquiries/${eid}/quotes/${Number(b.dataset.id)}`, {status: b.dataset.status});
      if (act === 'close') await post(`/customers/enquiries/${eid}/close`, {outcome: val('closeOutcome'), close_reason: val('closeReason'), close_note: val('closeNote'), won_value: val('closeValue') ? Number(val('closeValue')) : null});
      if (act === 'reopen') await post(`/customers/enquiries/${eid}/reopen`, {status: val('reopenStage')});
      if (act === 'link') await post(`/customers/enquiries/${eid}/campaigns`, {campaign_id: Number(val('enCampaign'))});
      if (act === 'draft-email') await post(`/customers/enquiries/${eid}/followups`, {channel: 'email'});
      if (act === 'draft-whatsapp') await post(`/customers/enquiries/${eid}/followups`, {channel: 'whatsapp'});
      if (act === 'log') { if (!val('acText')) throw new Error('Describe what happened first.'); await post(`/customers/enquiries/${eid}/activities`, {kind: val('acKind'), summary: val('acText')}); }
      const id = Number(b.dataset.id);
      if (msg === 'save') await patch('/customers/messages/' + id, {...($('cmSub' + id) ? {subject: $('cmSub' + id).value} : {}), body: $('cmBody' + id).value});
      if (msg === 'approve') await post(`/customers/messages/${id}/approve`);
      if (msg === 'send') { if (!confirm('Send this email to the customer now?')) return; await post(`/customers/messages/${id}/send`); notice('Sent.'); }
      if (msg === 'mark-sent') { if (!confirm('Confirm you sent this WhatsApp message yourself?')) return; await post(`/customers/messages/${id}/mark-sent`); }
      if (msg === 'cancel') await post(`/customers/messages/${id}/cancel`);
      await showEnquiry(eid); await loadCustomers();
      if (!$('custNotice').textContent) notice('Saved.');
    });
  });
  $('custFilter').addEventListener('change', loadCustomers);
  $('custCreate').addEventListener('submit', (ev) => {
    ev.preventDefault();
    run(async () => {
      const f = Object.fromEntries(new FormData(ev.target).entries());
      const profile = {name: f.name, company: f.company || undefined, email: f.email || undefined, phone: f.phone || undefined,
        whatsapp: f.phone || undefined, location: f.location || undefined, source: f.source || undefined};
      const c = await post('/customers', profile);
      const e = await post(`/customers/${c.id}/enquiries`, {title: f.title, requirements: f.requirements || undefined,
        next_action: f.next_action || undefined, next_action_due: f.next_action_due || today()});
      ev.target.reset(); $('custCreate').classList.add('hidden');
      await loadCustomers(); await showEnquiry(e.id); notice('Created ' + e.reference + '.');
    });
  });
})();
