"""Project-led daily loop, persistent leases, budgets and explicit fix queue.

No model-generated claims. Network adapters are injectable; dry-run is default.
"""
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from customers.mailboxes import UK
from outreach.service import OutreachService, stamp, domain
from planning.service import PlanningService, LONDON, MANCHESTER, in_window, name_key
from planning.relevance import fit
from storage.database import connection_scope

GATE = threading.Semaphore(1)
OFFSETS = (0, 4, 9, 16, 28)
SIGNATURE = '\n\nSai Jyothis | IK Eng Ltd, Salford M7 1AL\nTo opt out, reply "opt out".'


def setting(name, default):
    return os.getenv(name, default)


def enabled(name, default='false'):
    return setting(name, default).lower() == 'true'


def mailbox_config():
    return json.loads(setting('OUTREACH_MAILBOXES', '{"sj@ikeng.online":{"prefix":"OUTREACH_ZOHO_","cap":5,"warmup_start":"2026-11-13","ramp":[5,10,20,30]}}'))


def project_steps():
    first = ('Hello {first_name},\nI\'m Sai at IK Eng, with 15+ years in kitchens and joinery.\n'
             'I saw your scheme at {project_address}: {project_summary}\n'
             'We make kitchens, wardrobes and bespoke joinery to drawing through partner factories in China.\n'
             'Send me a drawing and I\'ll come back with a budget price, or happy to pop in for 15 minutes.')
    texts = [first,
             'Hello {first_name},\nA quick bump on {project_address}: {project_summary}\nWould a budget price for kitchens or joinery help? Send me a drawing, or happy to pop in for 15 minutes.',
             'Connect with {first_name} about {project_address}. Personal note only; Sai sends manually.',
             'Hello {first_name},\nFor {project_address}: {project_summary}\nWe could review kitchen and joinery lead times and budget against your drawings. Would that help your programme?',
             'Hello {first_name},\nClosing the loop on {project_address}: {project_summary}\nI\'ll leave this here. If a kitchen or joinery budget would help, send me a drawing.']
    return [{'channel':'linkedin_task' if i==2 else 'email','day_offset':day,
             'subject':'Kitchens and joinery for {project_ref}', 'template':texts[i]}
            for i,day in enumerate(OFFSETS)]


class AlwaysOnService:
    def __init__(self, outreach=None, planning=None, contacts=None, website_check=None, sources=None, alert_sender=None, rfq_retry=None):
        self.outreach=outreach or OutreachService()
        self.repo=self.outreach.repo
        self.now=self.outreach.now
        self.planning=planning or PlanningService(self.outreach)
        if contacts is None:
            from outreach.apollo import ProspectContactService
            contacts=ProspectContactService(self.outreach)
        self.contacts=contacts
        self.website_check=website_check or self._website_check
        self.sources=sources or []
        self.alert_sender=alert_sender
        self.rfq_retry=rfq_retry

    def state(self, key, default=None):
        rows=self.repo.rows('SELECT payload FROM outreach_runtime WHERE key=?',(key,))
        return json.loads(rows[0]['payload']) if rows else default

    def put(self,key,value):
        self.repo.execute('INSERT INTO outreach_runtime(key,payload) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',(key,json.dumps(value)))

    def paused(self):
        return enabled('OUTREACH_GLOBAL_PAUSE') or bool(self.state('pause',False))

    def dry_run(self):
        return enabled('OUTREACH_DRY_RUN','true')

    def approve_template(self, approved_by, note):
        if approved_by!='Sai' or len(note.strip())<10:
            raise ValueError('Sai and legitimate interest note required')
        self.put('template_approval',{'approved_at':stamp(self.now()),'approved_by':approved_by,'note':note,'steps':project_steps()})
        return self.state('template_approval')

    def _lease(self):
        owner=uuid.uuid4().hex
        with connection_scope(self.repo.db_path) as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT payload FROM outreach_runtime WHERE key='lease'").fetchone()
            if row and json.loads(row['payload'])['until']>self.now().timestamp():
                return None
            c.execute("INSERT INTO outreach_runtime VALUES('lease',?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                      (json.dumps({'owner':owner,'until':self.now().timestamp()+3000}),))
        return owner

    def _release(self,owner):
        with connection_scope(self.repo.db_path) as c:
            row=c.execute("SELECT payload FROM outreach_runtime WHERE key='lease'").fetchone()
            if row and json.loads(row['payload'])['owner']==owner:
                c.execute("UPDATE outreach_runtime SET payload=? WHERE key='lease'",(json.dumps({'owner':owner,'until':0}),))

    def identity_pending(self,limit=3):
        deadline=time.monotonic()+90
        for p in self.repo.rows("SELECT * FROM planning_projects WHERE fit_score>0 AND agent_account_id IS NULL ORDER BY first_seen_at,id LIMIT 100"):
            if limit<=0 or time.monotonic()>deadline:
                break
            key='identity:'+str(p['id'])
            previous=self.state(key,{'retry_at':0,'attempts':0})
            if previous['retry_at']>self.now().timestamp():
                continue
            limit-=1
            attempted={'attempted_at':stamp(self.now()),'attempts':previous['attempts']+1,
                       'retry_at':self.now().timestamp()+86400,'state':'waiting enrichment'}
            self.put(key,attempted)
            try:
                details=self.planning.council.fetch(p['source_url']) if p['source_url'] else {}
                raw=dict(p)
                for field in ('agent_name','agent_company','agent_address','agent_website','role_evidence','applicant'):
                    if details.get(field) and not raw.get(field):
                        raw[field]=details[field]
                raw['identity_evidence']=details.get('evidence',[])
                self.planning.store(raw)
                current=self.repo.rows('SELECT agent_account_id FROM planning_projects WHERE id=?',(p['id'],))[0]
                attempted['state']='resolved' if current['agent_account_id'] else 'waiting enrichment'
                attempted['reason']=details.get('error','No grounded architect company yet')
                if current['agent_account_id']:
                    self.put('project:'+str(p['id']),{'state':'resolved','reason':'Identity now linked'})
            except Exception as exc:
                attempted['reason']='Council identity retry failed: '+type(exc).__name__
            self.put(key,attempted)

    def queue_projects(self):
        for p in self.repo.rows('SELECT * FROM planning_projects ORDER BY first_seen_at,id'):
            for role,key in [('architect','agent_account_id'),('developer','applicant_account_id')]:
                if p[key]:
                    self.repo.execute('INSERT OR IGNORE INTO outreach_project_queue(project_id,account_id,role) VALUES(?,?,?)',(p['id'],p[key],role))
            if not p['agent_account_id'] and not p['applicant_account_id']:
                self.put('project:'+str(p['id']),{'state':'waiting enrichment','reason':'No grounded company identity from council record'})

    @staticmethod
    def _public_url(url):
        import socket
        import ipaddress
        from urllib.parse import urlsplit
        parsed=urlsplit(url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
            return False
        addresses=socket.getaddrinfo(parsed.hostname,443)
        return bool(addresses) and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses)

    def _website_check(self, account):
        import httpx
        from verification.website_contact_extractor import parking_page_reason
        from bs4 import BeautifulSoup
        url=account['website']
        if not url or domain(url)!=account['domain'] or not self._public_url(url):
            return False
        with httpx.Client(timeout=15,follow_redirects=False) as client:
            response=client.get(url)
            response.raise_for_status()
            return bool(response.text.strip()) and not parking_page_reason(BeautifulSoup(response.text,'html.parser').get_text(' ',strip=True))

    def quality(self,p,c,a,check_site=True):
        if a['domain'] in {'gmail.com','hotmail.com','outlook.com','yahoo.com','icloud.com','live.com','aol.com','proton.me'}:
            raise ValueError('Personal email providers are not corporate domains')
        if not c['name'] or not c['source_url'] or not p['address'] or not p['description'] or not p['source_url']:
            raise ValueError('Named person and address + proposal with source required')
        if not fit(p['description'],p['application_type'] or '')[0]:
            raise ValueError('Project outside furnishing scope')
        if not in_window(p,self.now().date()-timedelta(days=180),self.now().date()):
            raise ValueError('No recent validation or decision')
        if a['subscriber_type']!='corporate' or not self.planning.corporate_verified(a):
            raise ValueError('Companies House Ltd/LLP evidence required')
        if c['email_status']!='corporate_verified' or not a['domain'] or (c['email'] or '').rpartition('@')[2]!=a['domain']:
            raise ValueError('Verified corporate-domain work email required')
        if self.outreach.blocked({**a,**c,'flagged':a['flagged']}):
            raise ValueError('Suppressed or invalid contact')
        if check_site and not self.website_check(a):
            raise ValueError('Firm website unavailable or parking page')
        return True

    def render_email(self,p,c,position=0):
        from outreach.service import render
        # Exact proposal span, never an invented paraphrase. Overlong hooks queue
        # for review rather than silently dropping the project line.
        values={'first_name':c['name'].split()[0],'project_address':p['address'],
                'project_summary':p['description'],'project_ref':p['reference']}
        step=project_steps()[position]
        body=render(step['template'],values)+SIGNATURE
        if len(body.split())>=120 or '\n' in render(step['subject'],values):
            raise ValueError('Project email must be under 120 words with intact application evidence')
        return {'recipient':c['email'],'subject':render(step['subject'],values),'body':body}

    def _retry(self,row,reason,permanent=False):
        attempts=row['attempts']+1
        due=self.now()+timedelta(minutes=min(1440,5*2**min(attempts,9)))
        self.repo.execute('UPDATE outreach_project_queue SET state=?,reason=?,attempted_at=?,retry_at=?,attempts=? WHERE project_id=? AND account_id=?',
                         ('rejected' if permanent else 'waiting enrichment',reason,stamp(self.now()),stamp(due),attempts,row['project_id'],row['account_id']))

    def _corporate(self,a):
        if self.planning.corporate_verified(a):
            return a
        details,evidence=self.planning.corporate_details(a['company'],{})
        if evidence:
            self.planning.fill_account(a,details)
            if a['subscriber_type']=='unknown':
                self.repo.execute("UPDATE prospect_accounts SET subscriber_type='corporate' WHERE id=? AND subscriber_type='unknown'",(a['id'],))
            if a['flag_reason']=='Subscriber status requires review':
                self.repo.execute("UPDATE prospect_accounts SET flagged=0,flag_reason=NULL WHERE id=? AND flag_reason='Subscriber status requires review'",(a['id'],))
            self.outreach.event(a['id'],'planning corporate verification',evidence)
        return self.repo.get('prospect_accounts',a['id'])

    def _reveal(self,c):
        if c['reveal_attempted_at'] or not (c['source_url'] or '').startswith('apollo-free:'):
            return
        if not enabled('OUTREACH_APOLLO_REVEAL_ENABLED') or self.dry_run():
            return
        per=int(setting('OUTREACH_APOLLO_CREDITS_PER_CONTACT','1'))
        cap=int(setting('OUTREACH_APOLLO_DAILY_CREDITS','10'))
        if per<1 or cap<0:
            raise ValueError('Invalid Apollo budget')
        day=self.now().astimezone(UK).date().isoformat()
        with connection_scope(self.repo.db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            used=db.execute('SELECT coalesce(sum(credits),0) FROM outreach_credit_ledger WHERE day=?',(day,)).fetchone()[0]
            if used+per>cap or db.execute('SELECT 1 FROM outreach_credit_ledger WHERE contact_id=?',(c['id'],)).fetchone():
                return
            # Conservative reservation: charge even an uncertain HTTP result.
            db.execute('INSERT INTO outreach_credit_ledger VALUES(?,?,?,?)',(c['id'],day,per,stamp(self.now())))
        self.contacts.reveal_automatic(c['id'])

    def _enrol(self,p,c,a,role):
        approval=self.state('template_approval')
        if not approval or approval['steps']!=project_steps():
            raise ValueError('Sai must approve current project template once')
        with connection_scope(self.repo.db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            # Firm-level guard covers manual and other-project enrolments too.
            history=db.execute('''SELECT e.*,l.project_id FROM outreach_enrolments e
                JOIN prospect_contacts c ON c.id=e.contact_id LEFT JOIN planning_enrolment_projects l ON l.enrolment_id=e.id
                WHERE c.account_id IN (SELECT id FROM prospect_accounts WHERE id=? OR (domain IS NOT NULL AND domain=?)) AND c.bounced_at IS NULL AND NOT EXISTS(SELECT 1 FROM outreach_events v WHERE v.enrolment_id=e.id AND v.kind='reply' AND json_extract(v.payload,'$.classification')='departed') ORDER BY e.started_at DESC''',(a['id'],a['domain'])).fetchall()
            if any(e['status'] in ('active','paused','replied','stopped') for e in history):
                raise ValueError('Firm already contacted or has live/paused sequence')
            if history:
                latest=history[0]
                finished=db.execute('SELECT max(created_at) FROM outreach_events WHERE enrolment_id=?',(latest['id'],)).fetchone()[0] or latest['started_at']
                if datetime.fromisoformat(finished).replace(tzinfo=timezone.utc)+timedelta(days=90)>self.now():
                    raise ValueError('Firm in 90-day cooling period')
                if p['first_seen_at']<=finished or any(e['project_id']==p['id'] for e in history):
                    raise ValueError('Restart requires a new project after previous cadence')
            seq=db.execute('INSERT INTO outreach_sequences(title,segment,approved_at,approved_by,legitimate_interest_note,created_at) VALUES(?,?,?,?,?,?)',
                           ('Always-on '+p['reference'],role,approval['approved_at'],'Sai',approval['note'],stamp(self.now()))).lastrowid
            for i,step in enumerate(project_steps()):
                db.execute('INSERT INTO outreach_steps(sequence_id,position,channel,day_offset,subject,template) VALUES(?,?,?,?,?,?)',
                           (seq,i,step['channel'],step['day_offset'],step['subject'],step['template']))
            mailbox=next(iter(mailbox_config()))
            if domain(mailbox.rpartition('@')[2])=='ikeng.co.uk':
                raise ValueError('Never send prospect outreach from ikeng.co.uk')
            eid=db.execute('INSERT INTO outreach_enrolments(contact_id,sequence_id,mailbox,next_step_at,started_at) VALUES(?,?,?,?,?)',
                           (c['id'],seq,mailbox,stamp(self.now()),stamp(self.now()))).lastrowid
            db.execute('INSERT INTO planning_enrolment_projects VALUES(?,?,?)',(eid,p['id'],json.dumps(p)))
            db.execute("UPDATE outreach_project_queue SET enrolment_id=?,state='queued for tomorrow',reason='' WHERE project_id=? AND account_id=?",(eid,p['id'],a['id']))
        return eid

    def enrich_pending(self,limit=10):
        deadline=time.monotonic()+120
        rows=self.repo.rows('''SELECT q.* FROM outreach_project_queue q JOIN planning_projects p ON p.id=q.project_id
            WHERE q.enrolment_id IS NULL AND q.state!='rejected' AND (q.retry_at IS NULL OR q.retry_at<=?)
            ORDER BY p.fit_score DESC,p.first_seen_at,p.id LIMIT ?''',(stamp(self.now()),limit))
        for row in rows:
            if time.monotonic()>deadline:
                break
            try:
                p=self.repo.rows('SELECT * FROM planning_projects WHERE id=?',(row['project_id'],))[0]
                a=self.repo.get('prospect_accounts',row['account_id'])
                if not fit(p['description'],p['application_type'] or '')[0]:
                    self._retry(row,'Project outside furnishing scope',True);continue
                if row['role']=='architect' and p['agent_role']!='architect':
                    self._retry(row,'Agent is not an evidenced architect',True);continue
                if (a['flagged'] and a['flag_reason']!='Subscriber status requires review') or self.repo.rows('SELECT 1 FROM suppression_list WHERE identifier IN (?,?)',(a['domain'] or '',name_key(a['company']))):
                    self._retry(row,'Firm suppressed or flagged',True);continue
                a=self._corporate(a)
                if not a['domain'] or not self.planning.corporate_verified(a):
                    raise ValueError('Waiting for grounded firm domain and corporate evidence')
                contacts=self.repo.rows('SELECT * FROM prospect_contacts WHERE account_id=? AND opted_out_at IS NULL AND bounced_at IS NULL ORDER BY id',(a['id'],))
                if not contacts:
                    self.contacts.search(a['id'],titles=['director','associate','project architect','development director'])
                    contacts=self.repo.rows('SELECT * FROM prospect_contacts WHERE account_id=? AND opted_out_at IS NULL AND bounced_at IS NULL ORDER BY id',(a['id'],))
                candidate=None
                for c in contacts:
                    if not c['email']:
                        self._reveal(c)
                    c=self.repo.get('prospect_contacts',c['id'])
                    if c['email_status']=='corporate_verified':
                        candidate=c;break
                if not candidate:
                    raise ValueError('Waiting for verified named decision maker; daily reveal budget applies')
                self.quality(p,candidate,a)
                self.render_email(p,candidate)
                self._enrol(p,candidate,a,row['role'])
            except Exception as exc:
                self._retry(row,str(exc) if isinstance(exc,ValueError) else 'Enrichment failed: '+type(exc).__name__)

    def sync_daily(self):
        day=self.now().astimezone(UK).date().isoformat()
        retry=self.state('planning_retry',0)
        if retry>self.now().timestamp():
            return {'complete':False,'backoff':True}
        rows=self.repo.rows('SELECT * FROM outreach_daily_runs WHERE day=?',(day,))
        if rows and rows[0]['completed_at']:
            return
        self.repo.execute('INSERT OR IGNORE INTO outreach_daily_runs(day,started_at) VALUES(?,?)',(day,stamp(self.now())))
        areas=json.loads(setting('OUTREACH_PLANNING_AREAS',json.dumps(LONDON+MANCHESTER)))
        count=max(1,min(5,int(setting('OUTREACH_BOROUGHS_PER_DAY','3'))))
        cursor=self.state('area_cursor',0)
        selected=[areas[(cursor+i)%len(areas)] for i in range(min(count,len(areas)))]
        result=self.planning.sync_recent(areas=selected,days=90)
        self.put('planning_retry',0 if result.get('complete') else self.now().timestamp()+3600)
        if result.get('complete'):
            self.put('area_cursor',(cursor+len(selected))%len(areas))
        for source in self.sources:
            source.sync_pending(limit=10)
        # Mark only successful sync complete. A redeploy or partial error retries
        # the same areas with the overlapping 90-day window, not a lost day.
        self.repo.execute('UPDATE outreach_daily_runs SET result=?,completed_at=? WHERE day=?',
            (json.dumps(result),stamp(self.now()) if result.get('complete') else None,day))
        return result

    def seed_pending(self):
        if self.dry_run() or self.paused() or not enabled('OUTREACH_SEED_SEND_ENABLED'):
            return
        from customers.mailboxes import SEND_LOCK, sent_time
        from rfq.zoho_mail import ZohoError
        local=self.now().astimezone(UK)
        if local.weekday()>4 or not (8,30)<=(local.hour,local.minute)<(17,0):
            return
        for address,box in self.outreach.mailboxes().items():
            if address in getattr(self.outreach,'_blocked_mailboxes',set()) or address not in mailbox_config() or address.rpartition('@')[2]=='ikeng.co.uk':
                continue
            for seed in json.loads(setting('OUTREACH_SEED_ADDRESSES','[]')):
                if not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',seed):
                    continue
                if self.repo.rows('SELECT 1 FROM suppression_list WHERE identifier IN (?,?)',(seed,seed.rpartition('@')[2])):
                    continue
                with SEND_LOCK:
                    sent=box.sent(2)
                    if sum(sent_time(m).astimezone(UK).date()==local.date() for m in sent)>=5:
                        break
                    if sent and (self.now()-max(sent_time(m) for m in sent)).total_seconds()<90:
                        break
                    message={'recipient':seed,'subject':'IK Eng inbox placement check','body':'Hello, this is Sai at IK Eng. Please confirm whether this plain-text mailbox test arrived in your inbox. Thank you.','mailbox':address}
                    box.check_send(message,sent,self.now())
                    with connection_scope(self.repo.db_path) as c:
                        c.execute('BEGIN IMMEDIATE')
                        claimed=c.execute('INSERT OR IGNORE INTO outreach_seed_results(mailbox,seed,checked_at) VALUES(?,?,?)',(address,seed,stamp(self.now()))).rowcount
                        if not claimed:
                            claimed=c.execute("UPDATE outreach_seed_results SET placement='pending',checked_at=? WHERE mailbox=? AND seed=? AND placement='retry' AND checked_at<?",(stamp(self.now()),address,seed,stamp(self.now()-timedelta(minutes=15)))).rowcount
                    if not claimed:
                        continue
                    try:
                        provider=box.sender.send(message)
                        self.repo.execute('UPDATE outreach_seed_results SET provider_id=? WHERE mailbox=? AND seed=?',(provider,address,seed))
                    except ZohoError as exc:
                        # No blind retry of ambiguous seed delivery either.
                        self.repo.execute('UPDATE outreach_seed_results SET placement=? WHERE mailbox=? AND seed=?',('retry' if exc.safe_to_retry else 'uncertain',address,seed))
                    return

    def preview(self,limit=10):
        results=[]
        for row in self.repo.rows('''SELECT e.id,c.id AS cid,l.snapshot FROM outreach_enrolments e
            JOIN prospect_contacts c ON c.id=e.contact_id JOIN planning_enrolment_projects l ON l.enrolment_id=e.id
            JOIN outreach_sequences s ON s.id=e.sequence_id WHERE s.title LIKE 'Always-on %' AND e.status='active'
            AND e.next_step_at<=? ORDER BY e.next_step_at,e.id LIMIT ?''',(stamp(self.now()+timedelta(days=1)),limit)):
            c=self.repo.get('prospect_contacts',row['cid'])
            p=json.loads(row['snapshot'])
            steps=self.repo.rows('SELECT position FROM outreach_steps s WHERE sequence_id=(SELECT sequence_id FROM outreach_enrolments WHERE id=?) AND channel=\'email\' AND NOT EXISTS(SELECT 1 FROM outreach_events v WHERE v.enrolment_id=? AND v.step_id=s.id) ORDER BY position LIMIT 1',(row['id'],row['id']))
            try:
                self.quality(p,c,self.repo.get('prospect_accounts',c['account_id']))
                if steps:
                    results.append({'enrolment_id':row['id'],**self.render_email(p,c,steps[0]['position'])})
            except Exception as exc:
                results.append({'enrolment_id':row['id'],'blocked':type(exc).__name__})
        if len(results)<limit:
            # Review before template approval: render eligible queued candidates
            # without creating a sequence, spending credits or approving a list.
            for q in self.repo.rows('''SELECT q.* FROM outreach_project_queue q JOIN planning_projects p ON p.id=q.project_id
                    WHERE q.enrolment_id IS NULL ORDER BY p.fit_score DESC,p.first_seen_at LIMIT ?''',(limit-len(results),)):
                p=self.repo.rows('SELECT * FROM planning_projects WHERE id=?',(q['project_id'],))[0]
                account=self.repo.get('prospect_accounts',q['account_id'])
                contacts=self.repo.rows("SELECT * FROM prospect_contacts WHERE account_id=? AND email_status='corporate_verified' AND opted_out_at IS NULL AND bounced_at IS NULL ORDER BY id",(q['account_id'],))
                try:
                    if not contacts:
                        raise ValueError('Waiting for verified named decision maker')
                    self.quality(p,contacts[0],account)
                    results.append({'project_id':p['id'],'approved':False,**self.render_email(p,contacts[0])})
                except Exception as exc:
                    results.append({'project_id':p['id'],'blocked':str(exc) if isinstance(exc,ValueError) else type(exc).__name__})
        return results

    def seed_result(self,mailbox,seed,placement,confirmed_by):
        if mailbox not in mailbox_config() or seed not in json.loads(setting('OUTREACH_SEED_ADDRESSES','[]')):
            raise ValueError('Configured mailbox and seed required')
        if placement not in ('inbox','spam','missing') or confirmed_by!='Sai':
            raise ValueError('Sai must confirm seed placement')
        self.repo.execute('''INSERT INTO outreach_seed_results(mailbox,seed,placement,confirmed_by,checked_at) VALUES(?,?,?,?,?)
            ON CONFLICT(mailbox,seed) DO UPDATE SET placement=excluded.placement,confirmed_by=excluded.confirmed_by,checked_at=excluded.checked_at''',
            (mailbox,seed,placement,confirmed_by,stamp(self.now())))

    def mailbox_rates(self,mailbox):
        rows=self.repo.rows("SELECT kind,status,payload FROM outreach_events WHERE created_at>=? AND (json_extract(payload,'$.mailbox')=? OR enrolment_id IN (SELECT id FROM outreach_enrolments WHERE mailbox=?))",
            (stamp(self.now()-timedelta(days=30)),mailbox,mailbox))
        sent=sum(r['kind']=='email' and r['status']=='sent' for r in rows)
        replies=[json.loads(r['payload']).get('classification') for r in rows if r['kind']=='reply']
        return {'sent':sent,'bounces':replies.count('bounce'),'complaints':replies.count('complaint'),'replies':len(replies),
                'bounce_rate':replies.count('bounce')/max(1,sent)}

    def cap(self,mailbox):
        conf=mailbox_config().get(mailbox)
        if not conf or mailbox.rpartition('@')[2]=='ikeng.co.uk':
            return 0
        age=(self.now().astimezone(UK).date()-datetime.fromisoformat(conf['warmup_start']).date()).days
        ramp=conf.get('ramp',[5,10,20,30])
        target=min(int(conf.get('cap',5)),ramp[min(max(0,age)//7,len(ramp)-1)]) if age>=0 else 0
        seeds=json.loads(setting('OUTREACH_SEED_ADDRESSES','[]'))
        passed={r['seed'] for r in self.repo.rows("SELECT * FROM outreach_seed_results WHERE mailbox=? AND placement='inbox' AND checked_at>=?",(mailbox,stamp(self.now()-timedelta(days=30))))}
        # Every new mailbox starts with seeds only; sj's existing 5/day is allowed
        # but its ramp is gated by successful placement.
        if not seeds or not set(seeds)<=passed:
            target=min(target,5) if mailbox=='sj@ikeng.online' else 0
        rates=self.mailbox_rates(mailbox)
        if rates['bounce_rate']>.03 or rates['complaints']:
            target=max(1,target//2) if target else 0
        return target

    def health(self):
        last=self.state('heartbeat')
        reasons=[]
        if not last or self.now().timestamp()-last['at']>86400:
            reasons.append('No successful outreach heartbeat in 24 hours')
        days=self.repo.rows('SELECT * FROM outreach_daily_runs ORDER BY day DESC LIMIT 2')
        if len(days)==2 and all(r['completed_at'] and json.loads(r['result']).get('stored',0)==0 for r in days):
            reasons.append('No projects found for two runs')
        if last and last.get('failed'):
            reasons.append('Outreach error rate elevated; inspect fix queue / reconnect Zoho')
        local=self.now().astimezone(UK)
        if local.hour>=17:
            plan=self.state('plan:'+local.date().isoformat(),0)
            sent=self.repo.rows("SELECT count(*) n FROM outreach_events WHERE kind='email' AND status='sent' AND created_at>=?",(local.date().isoformat(),))[0]['n']
            if plan and sent<plan*.5 and not self.dry_run() and not self.paused():
                reasons.append('Fewer than 50% of planned sends completed')
        return {'ok':not reasons,'reasons':reasons,'heartbeat':last,'dry_run':self.dry_run(),'paused':self.paused(),
                'fix_url':'/outreach/always-on/status','mailboxes':{a:{'cap':self.cap(a),**self.mailbox_rates(a)} for a in mailbox_config()}}

    def summary(self,days=1):
        since=stamp(self.now()-timedelta(days=days))
        return {'projects_found':self.repo.rows('SELECT count(*) n FROM planning_projects WHERE fit_score>0 AND first_seen_at>=?',(since,))[0]['n'],
                'decision_makers_found':self.repo.rows('SELECT count(*) n FROM prospect_contacts WHERE created_at>=? AND email_status=\'corporate_verified\'',(since,))[0]['n'],
                'credits_used':self.repo.rows('SELECT coalesce(sum(credits),0) n FROM outreach_credit_ledger WHERE attempted_at>=?',(since,))[0]['n'],
                'emails_sent':self.repo.rows("SELECT count(*) n FROM outreach_events WHERE kind='email' AND status='sent' AND created_at>=?",(since,))[0]['n'],
                'replies':self.repo.rows("SELECT count(*) n FROM outreach_events WHERE kind='reply' AND created_at>=?",(since,))[0]['n'],
                'events':self.repo.rows('SELECT kind,status,count(*) count FROM outreach_events WHERE created_at>=? GROUP BY kind,status',(since,)),
                'states':self.repo.rows('SELECT state,count(*) count FROM outreach_project_queue GROUP BY state'),
                'identity_waiting':len(self.repo.rows("SELECT key FROM outreach_runtime WHERE key LIKE 'project:%' AND json_extract(payload,'$.state')='waiting enrichment'")),
                'followups_due':self.repo.rows("SELECT count(*) n FROM outreach_enrolments WHERE status='active' AND next_step_at<=?",(stamp(self.now()),))[0]['n']}

    def poll_pending(self):
        failures=[]
        blocked=set()
        for address in self.outreach.mailboxes():
            key='inbox_retry:'+address
            previous=self.state(key,{'attempts':0,'retry_at':0})
            if previous['retry_at']>self.now().timestamp():
                blocked.add(address);failures.append('inbox:'+address)
                continue
            try:
                self.outreach.poll_replies(mailbox=address)
                self.put(key,{'attempts':0,'retry_at':0,'attempted_at':stamp(self.now())})
            except Exception as exc:
                blocked.add(address);failures.append('inbox:'+address+':'+type(exc).__name__)
                attempts=previous['attempts']+1
                self.put(key,{'attempts':attempts,'retry_at':self.now().timestamp()+min(3600,60*2**min(attempts,6)),
                              'attempted_at':stamp(self.now()),'category':getattr(exc,'category',type(exc).__name__)})
        self.outreach._blocked_mailboxes=blocked
        return failures

    def tick(self,limit=10):
        if not GATE.acquire(False):
            return {'busy':True}
        owner=None
        try:
            owner=self._lease()
            if not owner:
                return {'busy':True}
            previous_heartbeat=self.state('heartbeat')
            if previous_heartbeat and self.now().timestamp()-previous_heartbeat['at']>86400:
                self.alert_health()
            # Always poll replies, even outside the send window or when paused.
            failures=[]
            failures.extend(self.poll_pending())
            local=self.now().astimezone(UK)
            if local.weekday()<5 and not self.paused():
                try:
                    sync=self.sync_daily()
                    if sync and not sync.get("complete"):
                        failures.append("planning:incomplete")
                except Exception as exc:
                    failures.append('planning:'+type(exc).__name__)
                self.identity_pending()
                self.queue_projects()
                self.enrich_pending(limit)
                day=local.date().isoformat()
                if self.state('plan:'+day) is None:
                    self.put('plan:'+day,min(self.summary()['followups_due'],sum(self.cap(a) for a in mailbox_config())))
                if not failures:
                    self.seed_pending()
                failures.extend(self.poll_pending())
                sent_result=self.outreach.run_due(limit)
                from config import settings as cfg
                if self.rfq_retry and not self.dry_run() and cfg.RFQ_FROM_ADDRESS not in self.outreach._blocked_mailboxes and (8,30)<=(local.hour,local.minute)<(17,0):
                    self.rfq_retry(limit=limit)
                if sent_result.get('failed'):
                    failures.append('sending')
            self.refresh_queue()
            previous=self.state('heartbeat',{'at':0})
            self.put('heartbeat',{'at':previous['at'] if failures else self.now().timestamp(),'failed':bool(failures),'errors':failures})
            self.put('daily_summary',self.summary())
            self.put('weekly_summary',self.summary(7))
            self.alert_health()
            return {**self.summary(),'failed':bool(failures)}
        except Exception as exc:
            self.put('last_error',{'at':stamp(self.now()),'category':type(exc).__name__})
            old=self.state('heartbeat',{'at':0})
            old['failed']=True
            self.put('heartbeat',old)
            self.alert_health()
            return {'failed':True,'category':type(exc).__name__}
        finally:
            if owner:
                self._release(owner)
            GATE.release()

    def refresh_queue(self):
        for q in self.repo.rows('SELECT * FROM outreach_project_queue WHERE enrolment_id IS NOT NULL'):
            e=self.repo.get('outreach_enrolments',q['enrolment_id'])
            state={'finished':'paused 90 days','replied':'reply for Sai','stopped':'rejected','paused':'waiting review'}.get(e['status'],'queued for tomorrow')
            sent=self.repo.rows("SELECT 1 FROM outreach_events WHERE enrolment_id=? AND kind='email' AND status='sent'",(e['id'],))
            if sent and e['status']=='active':
                state='sent; follow-up queued'
            if e['status']=='active' and q['state']=='waiting enrichment' and q['reason']:
                state='waiting enrichment'
            reason=q['reason'] or ('Sequence '+e['status'] if e['status'] in ('paused','stopped') else '')
            self.repo.execute('UPDATE outreach_project_queue SET state=?,reason=? WHERE project_id=? AND account_id=?',(state,reason,q['project_id'],q['account_id']))
            c=self.repo.get('prospect_contacts',e['contact_id'])
            departed=self.repo.rows("SELECT 1 FROM outreach_events WHERE enrolment_id=? AND kind='reply' AND json_extract(payload,'$.classification')='departed'",(e['id'],))
            if c['bounced_at'] or departed:
                # Keep the project; next enrichment searches another person at the
                # same firm. Never bypass a firm-level suppression or an opt-out.
                self.repo.execute("UPDATE outreach_project_queue SET enrolment_id=NULL,state='waiting enrichment',reason='Hard bounce: find next decision maker',retry_at=? WHERE project_id=? AND account_id=?",(stamp(self.now()+timedelta(days=1)),q['project_id'],q['account_id']))

    def alert_health(self):
        health=self.health()
        if health['ok']:
            return
        day=self.now().astimezone(UK).date().isoformat()
        if self.state('alert:'+day):
            return
        self.put('health_banner',health)
        if self.alert_sender:
            self.alert_sender({'recipient':'sai@ikeng.co.uk','subject':'Supplier Intel outreach needs attention','body':'\n'.join(health['reasons'])})
            self.put('alert:'+day,True)


def managed_payload(outreach,row,step,payload):
    """Called by every send path, including the legacy worker."""
    seq=outreach.repo.get('outreach_sequences',row['sequence_id'])
    if not seq['title'].startswith('Always-on '):
        return payload
    service=AlwaysOnService(outreach)
    approval=service.state('template_approval')
    if not approval or approval['steps']!=project_steps():
        return None
    if service.paused() or service.dry_run() or service.state('sequence_pause:'+str(row['sequence_id']),False):
        return None
    local=outreach.now().astimezone(UK)
    if local.weekday()>4 or not (8,30)<=(local.hour,local.minute)<(17,0):
        return None
    linked=outreach.repo.rows('SELECT snapshot FROM planning_enrolment_projects WHERE enrolment_id=?',(row['id'],))
    if not linked:
        return None
    p=json.loads(linked[0]['snapshot'])
    c=outreach.repo.get('prospect_contacts',row['contact_id'])
    a=outreach.repo.get('prospect_accounts',c['account_id'])
    service.quality(p,c,a)
    message=service.render_email(p,c,step['position'])
    message['mailbox']=row['mailbox']
    return message
