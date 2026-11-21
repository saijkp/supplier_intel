import logging
"""Private persistent chat state; no customer text is logged or sent as telemetry."""
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from threading import RLock
from zoneinfo import ZoneInfo
from chatbot.prompt import HANDOFF, FALLBACK, system_prompt
from storage.database import connection_scope
from customers.customer_service import CustomerService
from storage.customer_repository import CustomerRepository
from config.settings import COLLECTION_ARTIFACTS_DIR

LOCK = RLock()  # Same single-instance deployment discipline as form intake.
STYLES = ['Modern handleless', 'Shaker', 'Warm wood', 'Scandinavian', 'Industrial', 'Classic', 'Minimal white', 'Dark/moody']
PRIVACY = "IK ENG stores your chat, details, style choices and files to handle your enquiry and contact you. AI replies use OpenAI; please do not include sensitive information. Contact Sai to request access or deletion. Unregistered chats are retained for 30 days; registered enquiries for 12 months after last contact, subject to order/legal records."

def mobile(value):
    value = re.sub(r'[\s()-]', '', value)
    if value.startswith('0044'): value = '+' + value[2:]
    if value.startswith('07'): value = '+44' + value[1:]
    if not re.fullmatch(r'\+447[1-5789]\d{8}', value):
        raise ValueError('Enter a valid UK mobile number')
    return value

class ChatbotService:
    def __init__(self, db_path=None, client=None, clock=None, artifacts=None):
        self.db_path, self.client = db_path, client
        self.clock = clock or time.time
        self.artifacts = Path(artifacts or COLLECTION_ARTIFACTS_DIR) / 'chat'
        self.customers = CustomerService(CustomerRepository(db_path))

    def usage(self, c, key, cap):
        c.execute('INSERT OR IGNORE INTO chat_usage VALUES (?,0)', (key,))
        return bool(c.execute('UPDATE chat_usage SET count=count+1 WHERE key=? AND count<?', (key, cap)).rowcount)

    def create(self, ip, honeypot=''):
        if honeypot: raise ValueError('Unable to start chat')
        ip = hashlib.sha256(ip.encode()).hexdigest()
        with connection_scope(self.db_path) as c:
            c.execute('BEGIN IMMEDIATE')
            if not self.usage(c, 'create:'+ip+':'+str(int(self.clock()//3600)), 20):
                raise ValueError('Please try again later')
            sid = uuid.uuid4().hex + uuid.uuid4().hex
            c.execute('INSERT INTO chat_sessions(id,ip_hash,created_at) VALUES (?,?,?)', (sid,ip,self.clock()))
        return {'id':sid, 'privacy_notice':PRIVACY}

    def get(self, sid):
        with connection_scope(self.db_path) as c:
            row = c.execute('SELECT * FROM chat_sessions WHERE id=?',(sid,)).fetchone()
        if not row: raise ValueError('Chat not found')
        result = dict(row)
        for k in ('transcript','files','styles'): result[k] = json.loads(result[k])
        return result

    def save(self, sid, **values):
        with connection_scope(self.db_path) as c:
            c.execute('UPDATE chat_sessions SET '+','.join(k+'=?' for k in values)+' WHERE id=?',
                      (*[json.dumps(v) if isinstance(v,list) else v for v in values.values()], sid))

    def check(self, sid, honeypot=''):
        s = self.get(sid)
        if honeypot or self.clock()-s['created_at'] < 2: raise ValueError('Please wait a moment and try again')
        with connection_scope(self.db_path) as c:
            c.execute('BEGIN IMMEDIATE')
            if not self.usage(c,'operation:'+sid+':'+str(int(self.clock()//60)),60):
                raise ValueError('Please try again shortly')
        return s

    def message(self, sid, text, honeypot=''):
        with LOCK:
            s = self.check(sid, honeypot)
            if not text.strip() or len(text)>2000: raise ValueError('Message must contain 1?2000 characters')
            if s['turns'] >= 12: return {'reply':FALLBACK, 'register':True}
            with connection_scope(self.db_path) as c:
                c.execute('BEGIN IMMEDIATE')
                allowed = self.usage(c,'ip:'+s['ip_hash']+':'+str(int(self.clock()//60)),20)
                allowed = self.usage(c,'session:'+sid+':'+str(int(self.clock()//60)),6) and allowed
                day = datetime.fromtimestamp(self.clock(), ZoneInfo('Europe/London')).date()
                allowed = allowed and self.usage(c,'day:'+str(day),max(0,int(os.getenv('CHATBOT_DAILY_TURN_CAP') or '300')))
            history = s['transcript'] + [{'role':'user','content':text}]
            reply = FALLBACK
            if re.search(r'ignore|system prompt|other customers|internal data|developer message|reveal.*prompt',text,re.I):
                reply = "I cannot share internal information or change my rules. I can help with your IK ENG enquiry."
            elif re.search(r'are you (?:a )?(?:human|bot|robot|real person)|are you ai', text, re.I):
                reply = "I am IK ENG’s assistant. Sai and the team follow up personally."
            elif re.search(r'price|cost|budget|discount|estimate|when|date|lead.?time|supplier|factor(?:y|ies)|margin|payment|tax|legal|how much|how long',text,re.I):
                reply = HANDOFF
            elif allowed:
                try:
                    client = self.client
                    if client is None and os.getenv('OPENAI_API_KEY'):
                        from openai import OpenAI
                        client = OpenAI(timeout=15, max_retries=0)
                    if client is not None:
                        prompt = system_prompt()
                        if len(prompt.encode('utf-8')) > 6000:
                            raise ValueError('Knowledge file exceeds prompt budget')
                        context = []
                        budget = 6000
                        for message in reversed(history):
                            size = len(message['content'].encode('utf-8'))
                            if size > budget: break
                            context.insert(0,message)
                            budget -= size
                        if not context: raise ValueError('Message exceeds context budget')
                        result = client.chat.completions.create(model=os.getenv('CHATBOT_MODEL') or 'gpt-4o-mini',
                            max_tokens=400, messages=[{'role':'system','content':prompt}]+context)
                        candidate = result.choices[0].message.content or ''
                        # Fail closed on commercial output and question/sentence overflow.
                        if (candidate and len(candidate)<=1800 and candidate.count('?')<=2 and not candidate.lstrip().startswith(('-', '*', '?'))
                            and len(re.findall(r'[.!?](?:\s|$)',candidate))<=3
                            and not re.search(r'\d|[£$€]|supplier|factory|payment|discount|budget|pounds?|GBP|USD|CNY|yuan|cost|price|estimate|guarantee|legal|tax|tomorrow|weeks?|months?|monday|tuesday|wednesday|thursday|friday|saturday|sunday',candidate,re.I)):
                            reply = candidate
                        else: reply = HANDOFF
                except Exception as exc:
                    logging.getLogger(__name__).warning('chatbot AI call failed: %s', type(exc).__name__)
                    reply = FALLBACK
            history.append({'role':'assistant','content':reply})
            self.save(sid, transcript=history, turns=s['turns']+1)
            self.sync(sid)
            prev = next((m['content'] for m in reversed(s['transcript']) if m['role']=='assistant'), '')
            offered = bool(re.search(r'quote|visit|call|your details|register', prev, re.I))
            agreed = bool(re.match(r'\s*(yes|yeah|yep|sure|ok|okay|please|go ahead|that would be)', text, re.I))
            wants = bool(re.search(r'quote|visit|callback|call me|call back|upload|register|contact me|measure|price', text, re.I))
            return {'reply':reply, 'register':reply in (HANDOFF,FALLBACK) or wants or (agreed and offered) or bool(re.search(r'your details|register', reply, re.I))}

    def register(self, sid, values):
        with LOCK:
            s = self.check(sid,values.pop('honeypot',''))
            if not values.get('consent'): raise ValueError('Contact consent is required')
            phone = mobile(values.get('mobile',''))
            email = values.get('email','').strip().lower()
            name = values.get('name','').strip()
            postcode = values.get('postcode','').strip().upper()
            if not name or len(name)>200: raise ValueError('Name is required')
            if not re.fullmatch(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+',email) or len(email)>254: raise ValueError('Enter a valid email')
            if not re.fullmatch(r'(?:GIR 0AA|[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2})',postcode): raise ValueError('Enter a UK postcode')
            if s['enquiry_id']: return {'registered':True}
            enquiry = self.customers.capture_chat(sid, name, phone, email, postcode, values.get('address',''), self.evidence(s))
            self.save(sid,enquiry_id=enquiry['id'],consent_at=datetime.fromtimestamp(self.clock(),ZoneInfo('Europe/London')).isoformat())
            return {'registered':True}

    def evidence(self, s):
        return 'Website chat\n'+'\n'.join(m['role']+': '+m['content'] for m in s['transcript'])+'\nStyles: '+', '.join(s['styles'])+'\nFiles: '+', '.join('/chat/files/'+f['id'] for f in s['files'])

    def sync(self,sid):
        s=self.get(sid)
        if s['enquiry_id']:
            self.customers.log(s['enquiry_id'],'note',self.evidence(s))

    def styles(self,sid,tags):
        with LOCK:
            self.check(sid)
            if len(tags)>8 or any(t not in STYLES for t in tags): raise ValueError('Unknown style')
            self.save(sid,styles=list(dict.fromkeys(tags))); self.sync(sid)
        return {'saved':True}

    def upload(self,sid,name,data):
        with LOCK:
            s=self.check(sid)
            if not s['enquiry_id']: raise ValueError('Please register before uploading')
            ext=Path(name).suffix.lower()
            valid = ((ext=='.pdf' and data.startswith(b'%PDF-')) or (ext=='.jpg' and data.startswith(b'\xff\xd8\xff')) or
                (ext=='.png' and data.startswith(b'\x89PNG\r\n\x1a\n')) or
                (ext=='.heic' and len(data)>12 and data[4:8]==b'ftyp' and data[8:12] in (b'heic',b'heix',b'hevc',b'hevx')))
            if not valid or not data or len(data)>15*1024*1024: raise ValueError('Use jpg/png/pdf/heic, up to 15 MB')
            if len(s['files'])>=10: raise ValueError('Maximum ten files')
            fid=uuid.uuid4().hex
            self.artifacts.mkdir(parents=True,exist_ok=True)
            (self.artifacts/fid).write_bytes(data)
            self.save(sid,files=s['files']+[{'id':fid,'extension':ext}]);self.sync(sid)
            return {'saved':True}

    def review(self,since=0):
        with connection_scope(self.db_path) as c:
            ids=c.execute('SELECT id FROM chat_sessions WHERE created_at>=? ORDER BY created_at DESC LIMIT 200',(since,)).fetchall()
        return [self.get(r['id']) for r in ids]
