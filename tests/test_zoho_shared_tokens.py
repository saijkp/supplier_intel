"""Shared OAuth cache tests: fake HTTP, isolated database, no credentials."""
from concurrent.futures import ThreadPoolExecutor
import threading
import httpx
import pytest
from config import settings as cfg
from rfq.zoho_mail import ZohoClient, ZohoError
from storage.database import initialise_schema, connection_scope


@pytest.fixture
def tokens(tmp_path,monkeypatch):
    path=tmp_path/'tokens.db';initialise_schema(path)
    for key,value in {'REGION':'eu','CLIENT_ID':'client','CLIENT_SECRET':'secret','REFRESH_TOKEN':'refresh','ACCOUNT_ID':'123'}.items():
        monkeypatch.setattr(cfg,'RFQ_ZOHO_'+key,value)
    return path


class HTTP:
    def __init__(self):
        self.refreshes=0;self.lock=threading.Lock();self.reject=None
    def request(self,method,url,**kw):
        if url.endswith('/token'):
            with self.lock:self.refreshes+=1
            if self.reject:
                return httpx.Response(400,json={'error':self.reject},request=httpx.Request(method,url))
            return httpx.Response(200,json={'access_token':'private-token','expires_in':3600},request=httpx.Request(method,url))
        return httpx.Response(200,json={'data':{'messageId':'42'}},request=httpx.Request(method,url))


def test_shared_across_instances_and_concurrent_single_refresh(tokens):
    http=HTTP()
    def call(_):return ZohoClient(http,db_path=tokens).request('GET','/folders')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(call,range(4)))
    assert len(results)==4 and http.refreshes==1


def test_expiry_refresh_and_credential_rotation(tokens,monkeypatch):
    http=HTTP();ZohoClient(http,db_path=tokens).request('GET','/folders')
    with connection_scope(tokens) as c:c.execute('UPDATE zoho_token_cache SET expires=0')
    ZohoClient(http,db_path=tokens).request('GET','/folders')
    monkeypatch.setattr(cfg,'RFQ_ZOHO_REFRESH_TOKEN','rotated')
    ZohoClient(http,db_path=tokens).request('GET','/folders')
    assert http.refreshes==3


def test_refresh_rate_limit_is_shared_and_backed_off(tokens):
    http=HTTP();http.reject='too many requests'
    for _ in range(3):
        with pytest.raises(ZohoError) as exc:ZohoClient(http,db_path=tokens).request('GET','/folders')
        assert exc.value.category=='rate_limit' and exc.value.safe_to_retry
        assert 'refresh' not in str(exc.value)
    assert http.refreshes==1


def test_refresh_auth_failure_is_distinct(tokens):
    http=HTTP();http.reject='invalid_code'
    with pytest.raises(ZohoError,match='reconnect') as exc:ZohoClient(http,db_path=tokens).request('GET','/folders')
    assert exc.value.category=='auth'


def test_explicit_401_invalidates_cache_without_refresh_storm(tokens):
    class Rejected(HTTP):
        def request(self,method,url,**kw):
            if not url.endswith('/token'):
                return httpx.Response(401,json={'error':'invalid token'},request=httpx.Request(method,url))
            return super().request(method,url,**kw)
    http=Rejected()
    with pytest.raises(ZohoError) as exc:ZohoClient(http,db_path=tokens).request('GET','/folders')
    assert exc.value.category=='auth' and http.refreshes==1
    with connection_scope(tokens) as c:assert c.execute('SELECT expires FROM zoho_token_cache').fetchone()[0]==0


def test_transport_timeout_after_post_is_uncertain(tokens):
    class Timeout(HTTP):
        def request(self,method,url,**kw):
            if not url.endswith('/token'):raise httpx.ReadTimeout('sensitive provider payload')
            return super().request(method,url,**kw)
    with pytest.raises(ZohoError) as exc:ZohoClient(Timeout(),db_path=tokens).request('POST','/messages')
    assert not exc.value.safe_to_retry and exc.value.category=='uncertain'
    assert 'sensitive' not in str(exc.value)
