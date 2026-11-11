"""Zoho Mail REST transport. No IMAP requirement or browser automation.

Official API contracts: https://www.zoho.com/mail/help/api/getting-started-with-api.html
Credentials are server environment variables; never returned to the dashboard.
"""
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
import re
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
    return [prefix + key for key in ('REGION','CLIENT_ID','CLIENT_SECRET','REFRESH_TOKEN','ACCOUNT_ID') if not getattr(cfg, prefix + key, '').strip()]


class ZohoClient:
    def __init__(self,client=None, prefix='RFQ_ZOHO_'):
        self.prefix = prefix
        self.client=client
        self.token=None;self.expires=0

    def _request(self,method,url,**kwargs):
        try:
            if self.client is not None: response=self.client.request(method,url,**kwargs)
            else:
                with httpx.Client(timeout=30,follow_redirects=False) as client:
                    response=client.request(method,url,**kwargs)
            response.raise_for_status()
            data=response.json()
            code=data.get('status',{}).get('code',200)
            if code!=200: raise RuntimeError('Zoho operation failed')
            return data
        except Exception:
            # Provider responses and request URLs must not leak refresh tokens.
            raise RuntimeError('Zoho Mail connection failed; check OAuth authorization and account region') from None

    def request(self,method,path,**kwargs):
        values = {k: getattr(cfg, self.prefix + k, '') for k in ('REGION','CLIENT_ID','CLIENT_SECRET','REFRESH_TOKEN','ACCOUNT_ID')}
        if missing(self.prefix) or values['REGION'] not in REGIONS or not values['ACCOUNT_ID'].isdigit():
            raise RuntimeError('Zoho Mail server configuration is incomplete')
        mail,accounts=REGIONS[values['REGION']]
        if not self.token or self.expires<=time.monotonic():
            result=self._request('POST','https://'+accounts+'/oauth/v2/token',data={
                'grant_type':'refresh_token','refresh_token':values['REFRESH_TOKEN'],
                'client_id':values['CLIENT_ID'],'client_secret':values['CLIENT_SECRET']})
            if not result.get('access_token'): raise RuntimeError('Zoho Mail authorization failed')
            self.token=result['access_token'];self.expires=time.monotonic()+max(0,int(result.get('expires_in',3600))-60)
        return self._request(method,'https://'+mail+'/api/accounts/'+values['ACCOUNT_ID']+path,
                             headers={'Authorization':'Zoho-oauthtoken '+self.token},**kwargs).get('data')



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
            try:
                age=(current.astimezone(UK).date()-date.fromisoformat(os.getenv('OUTREACH_WARMUP_START',''))).days
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
        for start in range(1, 20001, 200):
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
        for item in meta or []:
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
