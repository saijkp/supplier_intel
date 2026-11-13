"""Zoho Mail REST transport. No IMAP requirement or browser automation.

Official API contracts: https://www.zoho.com/mail/help/api/getting-started-with-api.html
Credentials are server environment variables; never returned to the dashboard.
"""
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
import re
import os
import time

import logging

import httpx
from config import settings as cfg
from rfq.templates import header

logger = logging.getLogger(__name__)

REGIONS={'com':('mail.zoho.com','accounts.zoho.com'),
         'eu':('mail.zoho.eu','accounts.zoho.eu'),
         'in':('mail.zoho.in','accounts.zoho.in'),
         'com.au':('mail.zoho.com.au','accounts.zoho.com.au'),
         'jp':('mail.zoho.jp','accounts.zoho.jp')}


def missing(prefix='RFQ_ZOHO_'):
    return [prefix + key for key in ('REGION','CLIENT_ID','CLIENT_SECRET','REFRESH_TOKEN','ACCOUNT_ID') if not getattr(cfg, prefix + key, os.getenv(prefix+key,'')).strip()]


class ZohoError(RuntimeError):
    """Sanitised provider failure; only explicit rejections are safe to retry."""
    def __init__(self, category, retryable=False, safe_to_retry=False):
        self.category=category
        self.retryable=retryable
        self.safe_to_retry=safe_to_retry
        super().__init__({'auth':'Zoho authorization failed; reconnect Zoho',
            'rate_limit':'Zoho rate limit; retry later', 'recipient':'Zoho invalid recipient',
            'transient':'Zoho temporarily unavailable; retry later',
            'uncertain':'Zoho delivery uncertain; review Sent before retry'}.get(category, 'Zoho operation failed'))


class ZohoClient:
    def __init__(self,client=None, prefix='RFQ_ZOHO_', db_path=None):
        self.prefix = prefix
        self.client=client
        self.db_path=db_path or cfg.DB_PATH
        self.token=None;self.expires=0

    def _request(self,method,url,**kwargs):
        refreshing=url.endswith('/oauth/v2/token')
        try:
            if self.client is not None:
                response=self.client.request(method,url,**kwargs)
            else:
                with httpx.Client(timeout=30,follow_redirects=False) as client:
                    response=client.request(method,url,**kwargs)
            data=response.json()
            status=getattr(response,'status_code',200)
            code=data.get('status',{}).get('code',status)
            if status>=400 or code!=200 or data.get('error'):
                # Inspect provider text only here. Never persist or expose it.
                text=str(data).lower()
                if status==429 or 'too many' in text or 'rate limit' in text:
                    raise ZohoError('rate_limit',True,True)
                if status in (401,403) or refreshing:
                    raise ZohoError('auth',False,True)
                if status==400 and ('recipient' in text or 'invalid_address' in text):
                    raise ZohoError('recipient',False,True)
                raise ZohoError('transient',True,refreshing or method=='GET')
            response.raise_for_status()
            return data
        except ZohoError:
            raise
        except Exception:
            raise ZohoError('transient' if refreshing or method=='GET' else 'uncertain',True,refreshing or method=='GET') from None

    def _access_token(self, values, accounts):
        import hashlib
        import uuid
        from storage.database import connection_scope
        # Same OAuth identity shares across prefixes/processes; rotated credentials
        # get a new key. No credential or token ever leaves this private table.
        identity=hashlib.sha256(('|'.join(values[k] for k in
            ('REGION','ACCOUNT_ID','CLIENT_ID','CLIENT_SECRET','REFRESH_TOKEN'))).encode()).hexdigest()
        owner=uuid.uuid4().hex
        deadline=time.monotonic()+35
        while True:
            now=time.time()
            with connection_scope(self.db_path) as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute('INSERT OR IGNORE INTO zoho_token_cache(identity) VALUES(?)',(identity,))
                row=c.execute('SELECT * FROM zoho_token_cache WHERE identity=?',(identity,)).fetchone()
                if row['token'] and row['expires']>now:
                    return row['token']
                if row['retry_at']>now:
                    raise ZohoError(row['error_category'] or 'rate_limit',row['error_category']!='auth',True)
                claimed=c.execute('UPDATE zoho_token_cache SET lock_owner=?,lock_until=? WHERE identity=? AND lock_until<=?',
                    (owner,now+90,identity,now)).rowcount
            if claimed:
                break
            if time.monotonic()>=deadline:
                raise ZohoError('rate_limit',True,True)
            time.sleep(.1)
        try:
            result=self._request('POST','https://'+accounts+'/oauth/v2/token',data={
                'grant_type':'refresh_token','refresh_token':values['REFRESH_TOKEN'],
                'client_id':values['CLIENT_ID'],'client_secret':values['CLIENT_SECRET']})
            token=result.get('access_token')
            if not token:
                raise ZohoError('auth',False,True)
            with connection_scope(self.db_path) as c:
                c.execute('UPDATE zoho_token_cache SET token=?,expires=?,retry_at=0,error_category=NULL WHERE identity=? AND lock_owner=?',
                    (token,time.time()+max(0,int(result.get('expires_in',3600))-60),identity,owner))
            return token
        except ZohoError as exc:
            with connection_scope(self.db_path) as c:
                c.execute('UPDATE zoho_token_cache SET retry_at=?,error_category=? WHERE identity=? AND lock_owner=?',
                    (time.time()+(300 if exc.category=='rate_limit' else 60),exc.category,identity,owner))
            raise
        finally:
            with connection_scope(self.db_path) as c:
                c.execute('UPDATE zoho_token_cache SET lock_until=0,lock_owner=NULL WHERE identity=? AND lock_owner=?',(identity,owner))

    def request(self,method,path,**kwargs):
        values={k:getattr(cfg,self.prefix+k,os.getenv(self.prefix+k,'')) for k in ('REGION','CLIENT_ID','CLIENT_SECRET','REFRESH_TOKEN','ACCOUNT_ID')}
        if missing(self.prefix) or values['REGION'] not in REGIONS or not values['ACCOUNT_ID'].isdigit():
            raise ZohoError('auth',False,True)
        mail,accounts=REGIONS[values['REGION']]
        token=self._access_token(values,accounts)
        # A 401 explicitly rejects the operation. Invalidate only this token;
        # the next worker tick refreshes under the DB lease (no refresh storm).
        try:
            return self._request(method,'https://'+mail+'/api/accounts/'+values['ACCOUNT_ID']+path,
                headers={'Authorization':'Zoho-oauthtoken '+token},**kwargs).get('data')
        except ZohoError as exc:
            if exc.category=='auth':
                from storage.database import connection_scope
                with connection_scope(self.db_path) as c:
                    c.execute('UPDATE zoho_token_cache SET expires=0 WHERE token=?',(token,))
            raise



class ZohoSender:
    def __init__(self,client=None, address=None):
        self.client=client if client is not None else ZohoClient()
        self.address=address
    def check(self):
        self.client.request('GET','')
        return True
    def send(self,message):
        import os
        # When prospect sending is enabled, *all* Zoho sends share its mailbox
        # budget/spacing, including RFQ/customer routes and manual replies in Sent.
        # RLock permits OutreachService/CustomerService to hold the same lock.
        if os.getenv('OUTREACH_SEND_ENABLED','').lower() == 'true':
            from customers.mailboxes import SEND_LOCK
            with SEND_LOCK:
                self._shared_budget(message)
                return self._send(message)
        return self._send(message)

    def _shared_budget(self,message):
        import os
        from datetime import date
        from customers.mailboxes import Mailbox, UK, sent_time
        address=self.address or cfg.RFQ_FROM_ADDRESS
        box=Mailbox(address,self,ZohoInbox(self.client))
        sent=box.sent(2);current=datetime.now(timezone.utc)
        box.check_send(message,sent,current)
        if address.lower()=='sj@ikeng.online':
            from outreach.always_on import mailbox_config
            baseline=os.getenv('OUTREACH_WARMUP_START') or mailbox_config().get(address,{}).get('warmup_start','')
            try:
                age=(current.astimezone(UK).date()-date.fromisoformat(baseline)).days
            except ValueError:
                raise ValueError('Configure outreach warm-up start before sending from sj') from None
            cap=0 if age<0 else (5,10,20,30)[min(age//7,3)]
            if sum(sent_time(m).astimezone(UK).date()==current.astimezone(UK).date() for m in sent)>=cap:
                raise ValueError('Mailbox warm-up cap reached')
        if sent and (current-max(sent_time(m) for m in sent)).total_seconds()<90:
            raise ValueError('Mailbox sends must be spaced at least 90 seconds apart')

    def _send(self,message):
        data=self.client.request('POST','/messages',json={'fromAddress':header(self.address or cfg.RFQ_FROM_ADDRESS),
              'toAddress':header(message['recipient']),'subject':header(message['subject']),
              'content':message['body'],'mailFormat':'plaintext'})
        # Missing confirmation is ambiguous: existing send ledger marks failed,
        # so an accepted-but-unconfirmed send is never retried automatically.
        if not isinstance(data,dict) or not data.get('messageId'):
            raise RuntimeError('Zoho did not confirm message acceptance; review delivery before retrying')
        return 'zoho:'+str(data['messageId'])


class ZohoInbox:
    def __init__(self,client=None): self.client=client if client is not None else ZohoClient()
    def check(self):
        self.client.request('GET','/messages/view',params={'limit':1})
        return True
    def fetch_sent(self, since_days=14):
        """Paginate Sent; an incomplete or malformed count blocks sending.

        Contract: https://www.zoho.com/mail/help/api/get-emails-list.html
        """
        from customers.mailboxes import sent_time
        folders = self.client.request('GET', '/folders')
        sent = next((f for f in folders or [] if f.get('folderType') == 'Sent'), None)
        if not sent or not str(sent.get('folderId', '')).isdigit():
            raise ValueError('Cannot locate Sent folder; sending is blocked')
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        result = []
        deadline=time.monotonic()+90
        for start in range(1, 20001, 200):
            if time.monotonic()>deadline:
                raise ZohoError('transient',True,True)
            page = self.client.request('GET', '/messages/view', params={
                'folderId': sent['folderId'], 'start': start, 'limit': 200,
                'sortBy': 'date', 'sortorder': 'false', 'status': 'all',
                'includeto': 'true', 'includesent': 'true', 'threadedMails': 'false'})
            if not isinstance(page, list):
                raise ValueError('Cannot read Sent folder; sending is blocked')
            for item in page:
                if sent_time(item) >= cutoff:
                    result.append(item)
            if len(page) < 200 or (page and all(sent_time(m) < cutoff for m in page)):
                return result
        raise ValueError('Sent folder scan incomplete; sending is blocked')

    def fetch_recent(self,since_days=14,limit=50):
        # Standalone legacy poll remains available; filtering is tighter when
        # the linked workflow provides its exact campaign recipient list.
        return self.fetch_for_campaign(since_days,limit,None)
    def fetch_for_campaign(self,since_days,limit,messages):
        recipients={m['recipient'].strip().lower() for m in messages or [] if m['channel']=='email'}
        references=[m['reference'] for m in messages or []]
        cutoff=(datetime.now(timezone.utc)-timedelta(days=since_days)).timestamp()*1000
        meta=self.client.request('GET','/messages/view',params={'start':1,'limit':min(limit,200),'status':'all','sortBy':'date','sortorder':'false','includesent':'false'})
        result=[]
        deadline=time.monotonic()+90
        for item in meta or []:
            if time.monotonic()>deadline:
                raise ZohoError('transient',True,True)
            mid=str(item.get('messageId',''))
            if not mid.isdigit(): continue
            try:
                if float(item.get('receivedTime',item.get('sentDateInGMT',0)))<cutoff: continue
            except (TypeError,ValueError): continue
            sender=parseaddr(item.get('fromAddress',''))[1].lower()
            if messages is not None and sender not in recipients and not any(ref in item.get('subject','') for ref in references):
                continue
            original=self.client.request('GET','/messages/'+mid+'/originalmessage')
            content=(original or {}).get('content','')
            if not isinstance(content,str) or not content: continue
            if len(content)>12000000:
                if messages is not None:
                    # RFQ campaign: a matching supplier reply must never be silently dropped.
                    raise RuntimeError('Zoho message exceeds the supported import size; review it manually')
                # Customer inbox poll: one oversized email (large attachments) must not block the
                # whole mailbox. Skip it and leave a log line so it can be reviewed by hand in Zoho.
                logger.warning('Skipped oversized Zoho message %s (%d bytes, subject %r); review it manually in Zoho',
                               mid, len(content), str(item.get('subject',''))[:120])
                continue
            content = 'X-SupplierIntel-Thread: ' + str(item.get('threadId', '')).replace('\r','').replace('\n','') + '\r\n' + content
            result.append(content.encode('utf-8'))
        return result
