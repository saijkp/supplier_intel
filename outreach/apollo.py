"""Free search; separate explicitly approved, no-retry credit-bounded reveals."""
import httpx
from config import settings as cfg
from outreach.service import stamp


class ProspectContactService:
    def __init__(self, outreach, client=None, api_key=None):
        self.outreach=outreach
        self.client=client
        self.api_key=api_key if api_key is not None else cfg.APOLLO_API_KEY

    def search(self, account_id, titles=None):
        account=self.outreach.repo.get('prospect_accounts',account_id)
        # Mark every attempt, including missing domain/config, so batches never loop.
        self.outreach.repo.execute('UPDATE prospect_accounts SET contact_search_attempted_at=? WHERE id=?',(stamp(self.outreach.now()),account_id))
        if not account['domain'] or not self.api_key:
            return {'skipped':True,'reason':'Domain and Apollo key required; no reveal performed'}
        payload={'q_organization_domains_list':[account['domain']],
                 'person_titles':titles or ['director','architect','interiors','FF&E','sourcing','founder','buyer'], 'per_page':10}
        def request(client):
            response=client.post('https://api.apollo.io/api/v1/mixed_people/api_search',headers={'x-api-key':self.api_key},json=payload)
            response.raise_for_status()
            return response.json().get('people',[])
        if self.client is not None:
            people=request(self.client)
        else:
            with httpx.Client(timeout=15) as client:
                people=request(client)
        # Search returns obfuscated surnames. Keep first name only; never guess it.
        for person in people:
            ident=person.get('id')
            if not ident:
                continue
            source='apollo-free:'+str(ident)
            if self.outreach.repo.rows('SELECT 1 FROM prospect_contacts WHERE source_url=? AND account_id=?',(source,account_id)):
                continue
            self.outreach.repo.execute('INSERT INTO prospect_contacts(account_id,name,title,source,source_url,search_attempted_at,created_at) VALUES(?,?,?,?,?,?,?)',
                (account_id,person.get('first_name'),person.get('title'),'apollo_free_search',source,stamp(self.outreach.now()),stamp(self.outreach.now())))
        return {'found':len(people),'reveal_performed':False}

    def search_pending(self, limit=10, force=False):
        from outreach.service import GATE
        import time
        if not 1<=limit<=25:
            raise ValueError('Limit must be 1–25')
        if not GATE.acquire(blocking=False):
            return {'busy':True}
        try:
            results=[]; deadline=time.monotonic()+45
            for row in self.outreach.repo.rows('SELECT id FROM prospect_accounts WHERE flagged=0 AND (contact_search_attempted_at IS NULL OR ?) LIMIT ?',(int(force),limit)):
                if time.monotonic()>deadline:
                    break
                try:
                    results.append(self.search(row['id']))
                except Exception:
                    results.append({'failed':True})
            return {'results':results}
        finally:
            GATE.release()

    def preview_reveal(self, contact_ids):
        import os, json, uuid
        ids=sorted(set(contact_ids))
        if not ids or len(ids)>10:
            raise ValueError('Reveal previews require 1–10 contact IDs')
        price=os.getenv('OUTREACH_APOLLO_CREDITS_PER_CONTACT','')
        if not price.isdigit() or int(price)<1:
            raise ValueError('Set OUTREACH_APOLLO_CREDITS_PER_CONTACT from your current Apollo plan before previewing paid work')
        for cid in ids:
            contact=self.outreach.repo.get('prospect_contacts',cid)
            if contact['source']!='apollo_free_search' or not (contact['source_url'] or '').startswith('apollo-free:'):
                raise ValueError('Only stored free-search contacts can be revealed')
            if contact['email'] or contact['opted_out_at'] or contact['bounced_at'] or contact['reveal_attempted_at']:
                raise ValueError('Contact already has email or is blocked')
        bid=uuid.uuid4().hex; cost=len(ids)*int(price)
        self.outreach.repo.execute('INSERT INTO outreach_reveal_batches(id,contact_ids,credit_cost,created_at) VALUES(?,?,?,?)',(bid,json.dumps(ids),cost,stamp(self.outreach.now())))
        return {'id':bid,'contact_ids':ids,'calls':len(ids),'maximum_credit_cost':cost,'approval_required':True}

    def reveal(self, batch_id, confirmed_credit_cost, approved_by):
        import os, json
        from storage.database import connection_scope
        if os.getenv('OUTREACH_APOLLO_REVEAL_ENABLED','').lower()!='true':
            raise ValueError('Paid Apollo reveal disabled on server')
        with connection_scope(self.outreach.repo.db_path) as c:
            c.execute('BEGIN IMMEDIATE')
            batch=c.execute('SELECT * FROM outreach_reveal_batches WHERE id=?',(batch_id,)).fetchone()
            if not batch or batch['status']!='preview' or batch['credit_cost']!=confirmed_credit_cost or approved_by!='Sai':
                raise ValueError('Sai must confirm the exact preview credit cost; attempted batches cannot retry')
            c.execute("UPDATE outreach_reveal_batches SET status='attempted',attempted_at=? WHERE id=?",(stamp(self.outreach.now()),batch_id))
        results=[]
        try:
            for cid in json.loads(batch['contact_ids']):
                contact=self.outreach.repo.get('prospect_contacts',cid)
                if contact['email'] or contact['opted_out_at'] or contact['bounced_at'] or contact['reveal_attempted_at']:
                    results.append({'id':cid,'skipped':True});continue
                with connection_scope(self.outreach.repo.db_path) as c:
                    from customers.mailboxes import UK
                    c.execute('BEGIN IMMEDIATE')
                    day=self.outreach.now().astimezone(UK).date().isoformat()
                    per=int(os.getenv('OUTREACH_APOLLO_CREDITS_PER_CONTACT','1'))
                    used=c.execute('SELECT coalesce(sum(credits),0) FROM outreach_credit_ledger WHERE day=?',(day,)).fetchone()[0]
                    if used+per>int(os.getenv('OUTREACH_APOLLO_DAILY_CREDITS','10')):
                        results.append({'id':cid,'queued':True,'reason':'Daily credit budget reached'});continue
                    if c.execute('SELECT 1 FROM outreach_credit_ledger WHERE contact_id=?',(cid,)).fetchone():
                        results.append({'id':cid,'skipped':True});continue
                    claimed=c.execute('UPDATE prospect_contacts SET reveal_attempted_at=? WHERE id=? AND reveal_attempted_at IS NULL',(stamp(self.outreach.now()),cid)).rowcount
                    if claimed:
                        c.execute('INSERT INTO outreach_credit_ledger VALUES(?,?,?,?)',(cid,day,per,stamp(self.outreach.now())))
                if not claimed:
                    results.append({'id':cid,'skipped':True});continue
                def request(client):
                    response=client.post('https://api.apollo.io/api/v1/people/match',headers={'x-api-key':self.api_key},json={'id':contact['source_url'].removeprefix('apollo-free:'),'reveal_personal_emails':False,'reveal_phone_number':False})
                    response.raise_for_status();return response.json().get('person') or {}
                if self.client is None:
                    with httpx.Client(timeout=15) as client:
                        person=request(client)
                else:
                    person=request(self.client)
                # Preserve every existing trusted field. Email remains unreviewed.
                import re
                email=(person.get('email') or '').strip().lower()
                if email and re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',email):
                    self.outreach.repo.execute("UPDATE prospect_contacts SET email=?,email_status='unreviewed' WHERE id=? AND (email IS NULL OR email='')",(email,cid))
                self.outreach.event(contact['account_id'],'paid reveal',{'batch_id':batch_id,'contact_id':cid,'candidate':person})
                results.append({'id':cid,'email_found':bool(email)})
            status='completed'
        except Exception:
            status='failed';results.append({'failed':True,'reason':'Stopped on first failure; never retry this batch blindly'})
        self.outreach.repo.execute('UPDATE outreach_reveal_batches SET status=?,result=? WHERE id=?',(status,json.dumps(results),batch_id))
        return {'status':status,'results':results}
    def reveal_automatic(self, contact_id):
        """One attempt, budget reserved by AlwaysOnService before this method.

        Apollo's verified status is required; no guessed email or domain repair.
        """
        from storage.database import connection_scope
        import re
        contact=self.outreach.repo.get('prospect_contacts',contact_id)
        if contact['email'] or contact['opted_out_at'] or contact['bounced_at'] or not (contact['source_url'] or '').startswith('apollo-free:'):
            return
        with connection_scope(self.outreach.repo.db_path) as c:
            if not c.execute('UPDATE prospect_contacts SET reveal_attempted_at=? WHERE id=? AND reveal_attempted_at IS NULL',(stamp(self.outreach.now()),contact_id)).rowcount:
                return
        def request(client):
            response=client.post('https://api.apollo.io/api/v1/people/match',headers={'x-api-key':self.api_key},json={
                'id':contact['source_url'].removeprefix('apollo-free:'),'reveal_personal_emails':False,'reveal_phone_number':False})
            response.raise_for_status()
            return response.json().get('person') or {}
        if self.client is None:
            with httpx.Client(timeout=15) as client:
                person=request(client)
        else:
            person=request(self.client)
        email=(person.get('email') or '').strip().lower()
        account=self.outreach.repo.get('prospect_accounts',contact['account_id'])
        valid=bool(re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',email))
        corporate=valid and email.rpartition('@')[2]==account['domain'] and person.get('email_status')=='verified'
        if valid:
            self.outreach.repo.execute("UPDATE prospect_contacts SET email=?,email_status=? WHERE id=? AND (email IS NULL OR email='')",
                (email,'corporate_verified' if corporate else 'unreviewed',contact_id))
        self.outreach.event(contact['account_id'],'paid reveal',{'contact_id':contact_id,'candidate':person,'automatic':True})

class FirmTrialApollo:
    """Review-only adapter: free people search, explicitly gated bounded match.

    Does not create accounts, contacts, enrolments or messages.
    """
    def __init__(self, client=None, api_key=None):
        self.client = client or httpx.Client(timeout=15)
        self.api_key = cfg.APOLLO_API_KEY if api_key is None else api_key

    def search(self, company, domain=None):
        if not self.api_key:
            return []
        payload = {'person_titles': ['director', 'partner', 'principal', 'associate', 'architect', 'interior designer'], 'per_page': 10}
        payload.update({'q_organization_domains_list': [domain]} if domain else {'q_organization_name': company})
        response = self.client.post('https://api.apollo.io/api/v1/mixed_people/api_search', headers={'x-api-key': self.api_key}, json=payload)
        response.raise_for_status()
        return response.json().get('people') or []

    def reveal(self, person_id):
        import os
        if os.getenv('OUTREACH_APOLLO_REVEAL_ENABLED', '').lower() != 'true':
            raise ValueError('Paid Apollo reveal disabled')
        if not self.api_key:
            return {}
        response = self.client.post('https://api.apollo.io/api/v1/people/match', headers={'x-api-key': self.api_key}, json={'id': person_id, 'reveal_personal_emails': False, 'reveal_phone_number': False})
        response.raise_for_status()
        return response.json().get('person') or {}
