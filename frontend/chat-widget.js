/* No API credentials belong in the embed. */
(() => {
  'use strict';
  const api = document.currentScript.dataset.api.replace(/\/$/, '');
  const host = document.createElement('div'); document.body.append(host);
  const root = host.attachShadow({mode:'open'});
  root.innerHTML = `<style>
  :host{font:15px system-ui;color-scheme:light dark}*{box-sizing:border-box}button,input{font:inherit}button{cursor:pointer;min-height:44px}#details.primary{background:#b8862f;color:#fff;border:0;border-radius:6px;font-weight:600;width:100%;margin:6px 0}button:focus-visible,input:focus-visible{outline:3px solid #ce8c25}
  #launch{position:fixed;right:20px;bottom:20px;border:0;border-radius:28px;background:#173b36;color:white;padding:14px 22px;z-index:99999}
  #panel{position:fixed;right:16px;bottom:82px;width:min(390px,calc(100vw - 32px));max-height:calc(100dvh - 100px);overflow:auto;background:Canvas;color:CanvasText;border:1px solid #888;border-radius:16px;padding:16px;box-shadow:0 8px 40px #0004;z-index:99999}
  [hidden]{display:none!important}header{display:flex;justify-content:space-between;align-items:center}h2{font-size:20px}p{line-height:1.5}label{display:block;margin:10px 0}input:not([type=checkbox]){width:100%;padding:10px;border:1px solid #888;border-radius:6px}#log{max-height:220px;overflow:auto}.tiles{display:grid;grid-template-columns:1fr 1fr;gap:5px}.tiles button[aria-pressed=true]{outline:2px solid #298471}.trap{position:absolute;left:-9999px}small{line-height:1.4;display:block}
  </style><button id="launch" aria-expanded="false" aria-controls="panel">Ask IK ENG</button>
  <section id="panel" aria-label="Ask IK ENG" hidden><header><h2>Ask IK ENG</h2><button id="close" aria-label="Close chat">×</button></header>
  <small>IK ENG’s AI assistant. Sai confirms commercial details personally. Please avoid sensitive information.</small>
  <div id="log" role="log" aria-live="polite"></div><p id="error" role="status"></p>
  <form id="chat"><label>Your message<input id="text" required maxlength="2000" autocomplete="off"></label><label class="trap" aria-hidden="true">Leave blank<input id="trap" tabindex="-1" autocomplete="off"></label><button>Send</button></form>
  <button id="details" class="primary">Get a quote or callback: leave your details</button><button id="upload">Upload plans or photos</button>
  <form id="registration" hidden><h3>Your details</h3><label>Name<input name="name" required maxlength="200" autocomplete="name"></label><label>UK mobile<input name="mobile" type="tel" required autocomplete="tel"></label><label>Email<input name="email" type="email" required autocomplete="email"></label><label>Postcode<input name="postcode" required maxlength="12" autocomplete="postal-code"></label><label>Full address (optional)<input name="address" maxlength="500" autocomplete="street-address"></label><p id="privacy"></p><label><input name="consent" type="checkbox" required> I have read the privacy notice and consent to IK ENG contacting me about this enquiry.</label><button>Register details</button></form>
  <label id="filelabel" hidden>Plans, photos or PDFs (jpg/png/pdf/heic, 15 MB each; ten maximum)<input id="files" type="file" accept=".jpg,.png,.pdf,.heic" multiple></label>
  <h3>Styles you like (optional)</h3><div class="tiles"></div></section>`;
  const $ = id => root.getElementById(id);
  let session, registered = false, busy = false;
  const selected = new Set();
  const say = (who,text) => {const p=document.createElement('p');p.textContent=who+': '+text;$('log').append(p);p.scrollIntoView({block:'nearest'});};
  async function request(path,body) {
    const response=await fetch(api+'/chat/'+path,{method:'POST',headers:body instanceof FormData?{}:{'Content-Type':'application/json'},body:body instanceof FormData?body:JSON.stringify(body)});
    if(!response.ok){let message='Please try again shortly.';try{const data=await response.json();if(typeof data.detail==='string')message=data.detail;}catch{}throw new Error(message);}
    return response.json();
  }
  async function ensure(){if(!session){session=await request('sessions',{honeypot:$('trap').value});$('privacy').textContent=session.privacy_notice;await new Promise(resolve=>setTimeout(resolve,2100));}return session.id;}
  async function perform(fn){if(busy)return;busy=true;$('error').textContent='Please wait…';try{await fn();$('error').textContent='';}catch(e){$('error').textContent=e.message;}finally{busy=false;}}
  function registration(){$('registration').hidden=false;$('registration').querySelector('input').focus();}
  $('launch').onclick=()=>{$('panel').hidden=!$('panel').hidden;$('launch').setAttribute('aria-expanded',String(!$('panel').hidden));if(!$('panel').hidden){$('text').focus();perform(ensure);}};
  $('close').onclick=()=>{$('panel').hidden=true;$('launch').setAttribute('aria-expanded','false');$('launch').focus();};
  root.addEventListener('keydown',e=>{if(e.key==='Escape')$('close').click();});
  say('IK ENG','Hello! What are you planning for your home or shipment?');
  $('chat').onsubmit=e=>{e.preventDefault();perform(async()=>{const text=$('text').value;const id=await ensure();const result=await request('sessions/'+id+'/messages',{text,honeypot:$('trap').value});say('You',text);say('IK ENG',result.reply);$('text').value='';if(result.register&&!registered)registration();});};
  $('details').onclick=()=>perform(async()=>{await ensure();registration();});
  $('upload').onclick=()=>perform(async()=>{await ensure();if(registered){$('filelabel').hidden=false;$('files').focus();}else registration();});
  $('registration').onsubmit=e=>{e.preventDefault();perform(async()=>{const fields=Object.fromEntries(new FormData(e.target));fields.consent=e.target.elements.consent.checked;fields.honeypot=$('trap').value;const id=await ensure();await request('sessions/'+id+'/register',fields);registered=true;$('registration').hidden=true;$('filelabel').hidden=false;say('IK ENG','Thank you. Sai will review your enquiry and follow up personally.');});};
  $('files').onchange=()=>perform(async()=>{for(const file of $('files').files){if(file.size>15*1024*1024)throw new Error('Each file must be at most 15 MB.');const data=new FormData();data.append('file',file);await request('sessions/'+session.id+'/files',data);}say('IK ENG','Your files have been saved for Sai.');$('files').value='';});
  ['Modern handleless','Shaker','Warm wood','Scandinavian','Industrial','Classic','Minimal white','Dark/moody'].forEach(tag=>{const b=document.createElement('button');b.type='button';b.textContent=tag;b.setAttribute('aria-pressed','false');b.onclick=()=>perform(async()=>{const id=await ensure();const tags=new Set(selected);tags.has(tag)?tags.delete(tag):tags.add(tag);await request('sessions/'+id+'/styles',{tags:[...tags]});selected.clear();tags.forEach(x=>selected.add(x));b.setAttribute('aria-pressed',String(selected.has(tag)));});root.querySelector('.tiles').append(b);});
})();
