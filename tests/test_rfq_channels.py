"""Transport protocol tests with fake sockets; no email or network."""
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
import pytest
from config import settings as cfg
from rfq.channels import SmtpSender,ImapInbox,routes
from rfq.templates import draft

@pytest.mark.parametrize('port',[465,587])
def test_smtp_tls_header_sanitization_and_message_id(monkeypatch,port):
    import rfq.channels as mod
    events=[];sent=[]
    class SMTP:
        def __init__(self,*args,**kwargs): events.append(('connect',args))
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def ehlo(self): events.append('ehlo')
        def starttls(self,**kwargs): events.append('starttls')
        def login(self,user,password): events.append('login')
        def send_message(self,mail): sent.append(mail)
    monkeypatch.setattr(mod.smtplib,'SMTP',SMTP);monkeypatch.setattr(mod.smtplib,'SMTP_SSL',SMTP)
    monkeypatch.setattr(cfg,'RFQ_SMTP_PORT',port)
    monkeypatch.setattr(cfg,'RFQ_FROM_ADDRESS','buyer@test.com\r\nBad: header')
    result=SmtpSender().send({'recipient':'sales@test.com\r\nBad: x','subject':'subject\nInjected','body':'text'})
    assert result.startswith('<') and result.endswith('>')
    assert ('starttls' in events)==(port==587)
    assert events.index('login')>events.index('starttls') if port==587 else True
    assert '\n' not in str(sent[0]['To']) and '\n' not in str(sent[0]['Subject'])
    assert sent[0]['Bad'] is None


def test_imap_read_only_peek_limit_and_logout(monkeypatch):
    import rfq.channels as mod
    events=[]
    class IMAP:
        def __init__(self,*args,**kwargs): pass
        def login(self,*args): events.append('login')
        def select(self,folder,readonly=False): events.append(('select',readonly))
        def search(self,*args): return 'OK',[b'1 2 3']
        def fetch(self,number,query): events.append(('fetch',number,query));return 'OK',[(b'info',b'raw')]
        def logout(self): events.append('logout')
    monkeypatch.setattr(mod.imaplib,'IMAP4_SSL',IMAP)
    assert ImapInbox().fetch_recent(14,2)==[b'raw',b'raw']
    assert ('select',True) in events
    assert ('fetch',b'2','(BODY.PEEK[])') in events
    assert ('fetch',b'1','(BODY.PEEK[])') not in events
    assert events[-1]=='logout'


def test_constructible_without_config_and_assisted_routes(monkeypatch):
    for key in ('RFQ_EMAIL_USER','RFQ_EMAIL_PASSWORD','RFQ_SMTP_HOST','RFQ_IMAP_HOST'): monkeypatch.setattr(cfg,key,'')
    SmtpSender();ImapInbox()
    supplier={'canonical_name':'Factory','alibaba_url':'https://factory.alibaba.com','made_in_china_url':'https://factory.made-in-china.com','whatsapp':'+44 123','wechat_id':'wx','contact_form_url':'javascript:alert(1)'}
    available=routes(supplier,'中文 & quote')
    assert available[0]['action_url']=='https://factory.alibaba.com'
    assert available[1]['action_url']=='https://factory.made-in-china.com'
    assert available[2]['action_url'].startswith('https://wa.me/44123?text=')
    assert not any(r['channel']=='contact_form' for r in available)
    fallback=routes({'canonical_name':'Factory'})
    from batch.tracker_exporter import _marketplace_search_link
    assert fallback[0]['action_url']==_marketplace_search_link('Factory','alibaba.com')

@pytest.mark.parametrize('country',['China','Taiwan','Hong Kong'])
def test_chinese_first_bilingual_fact_only(country):
    content=draft({'product':'wheel','product_zh':'车轮','quantity':'200','specification':'stainless steel'}, {'country':country},'RFQ-1-ABCDEF')
    assert content['body'].startswith('您好')
    assert '车轮' in content['body'] and 'wheel' in content['body']
    assert '200' in content['body'] and 'stainless steel' in content['body']
    assert 'ISO' not in content['body']
