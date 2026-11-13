"""Only known rejected, buyer-authorised RFQ sends may retry automatically."""
from tests.test_rfq_service import env
from rfq.zoho_mail import ZohoError


def test_rate_limited_rfq_retries_after_backoff(env):
    original=env.sender.send
    env.s.approve(env.cid)
    def fail(message):raise ZohoError('rate_limit',True,True)
    env.sender.send=fail
    assert env.s.send_approved(env.cid)['failed']==1
    row=env.repo.get('messages',env.mid)
    assert row['status']=='approved' and row['zoho_retry_at'] and row['zoho_retry_attempts']==1
    assert env.s.retry_pending()==[]
    env.sender.send=original
    env.repo.update('messages',env.mid,zoho_retry_at='2000-01-01 00:00:00')
    assert env.s.retry_pending()[0]['sent']==1
    assert env.s.retry_pending()==[]


def test_approval_alone_does_not_authorise_automatic_retry(env):
    env.s.approve(env.cid)
    assert env.s.retry_pending()==[] and not env.sender.messages


def test_uncertain_rfq_is_held_for_review(env):
    env.s.approve(env.cid)
    def fail(message):raise ZohoError('uncertain',True,False)
    env.sender.send=fail
    assert env.s.send_approved(env.cid)['failed']==1
    assert env.repo.get('messages',env.mid)['status']=='failed'
    assert env.s.retry_pending()==[]
