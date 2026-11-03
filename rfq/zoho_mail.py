"""Zoho Mail REST transport. No IMAP requirement or browser automation.

Official API contracts: https://www.zoho.com/mail/help/api/getting-started-with-api.html
Credentials are server environment variables; never returned to the dashboard.
"""
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
import re
import time

import httpx
from config import settings as cfg
from rfq.templates import header

REGIONS={'com':('mail.zoho.com','accounts.zoho.com'),
         'eu':('mail.zoho.eu','accounts.zoho.eu'),
         'in':('mail.zoho.in','accounts.zoho.in'),
         'com.au':('mail.zoho.com.au','accounts.zoho.com.au'),
         'jp':('mail.zoho.jp','accounts.zoho.jp')}


def missing():
    return [name for name in ('RFQ_ZOHO_REGION','RFQ_ZOHO_CLIENT_ID','RFQ_ZOHO_CLIENT_SECRET',
                              'RFQ_ZOHO_REFRESH_TOKEN','RFQ_ZOHO_ACCOUNT_ID') if not getattr(cfg,name).strip()]


class ZohoClient:
    def __init__(self,client=None):
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
        if missing() or cfg.RFQ_ZOHO_REGION not in REGIONS or not cfg.RFQ_ZOHO_ACCOUNT_ID.isdigit():
            raise RuntimeError('Zoho Mail server configuration is incomplete')
        mail,accounts=REGIONS[cfg.RFQ_ZOHO_REGION]
        if not self.token or self.expires<=time.monotonic():
            result=self._request('POST','https://'+accounts+'/oauth/v2/token',data={
                'grant_type':'refresh_token','refresh_token':cfg.RFQ_ZOHO_REFRESH_TOKEN,
                'client_id':cfg.RFQ_ZOHO_CLIENT_ID,'client_secret':cfg.RFQ_ZOHO_CLIENT_SECRET})
            if not result.get('access_token'): raise RuntimeError('Zoho Mail authorization failed')
            self.token=result['access_token'];self.expires=time.monotonic()+max(0,int(result.get('expires_in',3600))-60)
        return self._request(method,'https://'+mail+'/api/accounts/'+cfg.RFQ_ZOHO_ACCOUNT_ID+path,
                             headers={'Authorization':'Zoho-oauthtoken '+self.token},**kwargs).get('data')


class ZohoSender:
    def __init__(self,client=None): self.client=client if client is not None else ZohoClient()
    def check(self):
        self.client.request('GET','')
        return True
    def send(self,message):
        data=self.client.request('POST','/messages',json={'fromAddress':header(cfg.RFQ_FROM_ADDRESS),
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
                raise RuntimeError('Zoho message exceeds the supported import size; review it manually')
            result.append(content.encode('utf-8'))
        return result
