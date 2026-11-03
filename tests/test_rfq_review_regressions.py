"""Regressions from code review of the RFQ workflow; fakes only, no network."""
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from config import settings as cfg
from rfq.channels import SmtpSender, routes
from rfq.quote_parser import QuoteParser, strip_history
from rfq.rfq_service import RFQService
from rfq.templates import draft, footer
from storage.database import initialise_schema
from storage.repository import SupplierRepository
from storage.rfq_repository import RFQRepository

QUOTED_FOOTER = "Reference: RFQ-1-ABC123\nA Buyer\nBuyer Ltd\n1 Buyer Street\nreply unsubscribe to stop"


class FakeLLM:
    def __init__(self, result=None):
        self.result = result or {"quotes": []}
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append(user)
        return self.result


class Sender:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        return f'<sent-{message["id"]}@test>'


class Inbox:
    def __init__(self):
        self.messages = []

    def fetch_recent(self, *args):
        return self.messages


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "rfq.db"
    initialise_schema(path)
    suppliers, repo, sender, inbox, llm = SupplierRepository(path), RFQRepository(path), Sender(), Inbox(), FakeLLM()
    service = RFQService(
        repo=repo, sender=sender, inbox=inbox, parser=QuoteParser(llm), sleep_fn=lambda _: None,
        mx_check=lambda e: SimpleNamespace(has_mx_records=True),
    )
    for key, value in {
        "SEND_ENABLED": True, "SMTP_HOST": "smtp.test", "EMAIL_USER": "u@test", "EMAIL_PASSWORD": "pw",
        "FROM_ADDRESS": "buyer@test.com", "BUYER_COMPANY": "Buyer Ltd", "BUYER_NAME": "A Buyer",
        "BUYER_ADDRESS": "1 Buyer Street", "DAILY_SEND_LIMIT": 25, "SEND_DELAY_SECONDS": 0,
        "SEND_JOB_MAX_SECONDS": 900,
    }.items():
        monkeypatch.setattr(cfg, "RFQ_" + key, value)
    sid = suppliers.create_golden_record(
        {"canonical_name": "Test Factory", "primary_email": "sales@factory.test", "country": "China"}
    )
    campaign = service.create_campaign(title="Wheel RFQ", product="wheel", quantity="100")
    service.draft_messages(campaign["id"], supplier_ids=[sid], channels=["email"])
    return SimpleNamespace(s=service, repo=repo, sender=sender, inbox=inbox, llm=llm, cid=campaign["id"], sid=sid,
                           mid=repo.rows("messages")[0]["id"], suppliers=suppliers)


def send(env):
    env.s.approve(env.cid)
    return env.s.send_approved(env.cid)


def inbound(env, text, subject="Re: RFQ", headers=None, sender="sales@factory.test", message_id="<r1@x>"):
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = message_id
    for key, value in (headers or {}).items():
        msg[key] = value
    msg.set_content(text)
    env.inbox.messages.append(msg.as_bytes())


def test_footer_appears_exactly_once_in_sent_email(env):
    send(env)
    assert env.sender.messages[0]["body"].count("reply unsubscribe to stop") == 1


def test_footer_restored_when_human_edits_it_out(env):
    env.s.update_draft(env.mid, body="Short custom note")
    send(env)
    assert env.sender.messages[0]["body"].endswith(footer())


def test_draft_with_blank_identity_has_no_blank_signature_lines(monkeypatch):
    for key in ("BUYER_NAME", "BUYER_COMPANY", "BUYER_ADDRESS"):
        monkeypatch.setattr(cfg, "RFQ_" + key, "")
    body = draft({"product": "wheel"}, {}, "RFQ-1-ABC123")["body"]
    assert "\n\n\n" not in body


@pytest.mark.parametrize("marker", [
    "------------------ Original ------------------",
    "------------------ 原始邮件 ------------------",
    "发件人: buyer@test.com",
    "发件人：buyer@test.com",
    "From: buyer@test.com",
    "Sent: Monday",
    "在 2026年11月2日，Buyer 写道：",
])
def test_quoted_history_from_chinese_and_outlook_clients_is_stripped(marker):
    assert strip_history(f"USD 2.50\n{marker}\nQuantity: 100") == "USD 2.50"


@pytest.mark.parametrize("marker", [
    "------------------ 原始邮件 ------------------", "From: buyer@test.com",
])
def test_quote_quoting_our_footer_is_not_an_opt_out(env, marker):
    send(env)
    inbound(env, f"Our price is USD 2.50 each.\n{marker}\n{QUOTED_FOOTER}", headers={
        "In-Reply-To": env.repo.get("messages", env.mid)["smtp_message_id"]})
    env.s.poll_inbox()
    assert env.repo.rows("replies")[0]["reply_type"] != "opt_out"
    assert not env.repo.suppressed("sales@factory.test")


def test_long_quote_mentioning_unsubscribe_is_not_an_opt_out(env):
    send(env)
    env.s.record_manual_reply(env.mid, "Price USD 2.50. " + "Details of our offer. " * 20 + "unsubscribe link below")
    assert not env.repo.suppressed("sales@factory.test")


def test_short_genuine_opt_out_still_suppresses(env):
    send(env)
    env.s.record_manual_reply(env.mid, "Please unsubscribe me")
    assert env.repo.suppressed("sales@factory.test")


def test_attachment_text_is_not_truncated_by_history_stripping(env):
    send(env)
    msg = EmailMessage()
    msg["From"] = "sales@factory.test"
    msg["Subject"] = "Quote"
    msg["Message-ID"] = "<a1@x>"
    msg.set_content("See attached")
    msg.add_attachment(b"From: Factory Co\nUnit price USD 2.50\n", maintype="text", subtype="plain", filename="q.txt")
    env.inbox.messages.append(msg.as_bytes())
    env.s.poll_inbox()
    assert "USD 2.50" in env.llm.calls[-1]


def test_delay_notice_does_not_suppress_address(env):
    send(env)
    ref = env.repo.rows("messages")[0]["reference"]
    inbound(env, f"Action: delayed\nStatus: 4.4.7\nSubject: [{ref}]", sender="mailer-daemon@mx.test")
    assert env.s.poll_inbox()["discarded"] == 1
    assert not env.repo.suppressed("sales@factory.test")
    assert env.repo.get("messages", env.mid)["status"] == "sent"


@pytest.mark.parametrize("subject,headers", [
    ("Out of office", {}), ("自动回复: RFQ", {}), ("Re: RFQ", {"Auto-Submitted": "auto-replied"}),
])
def test_auto_reply_does_not_count_as_an_answer(env, subject, headers):
    send(env)
    inbound(env, "I am away", subject=subject, headers=headers)
    assert env.s.poll_inbox()["discarded"] == 1
    assert env.repo.get("messages", env.mid)["replied_at"] is None


def test_message_without_message_id_is_not_stored_twice(env):
    send(env)
    inbound(env, "Price to follow", message_id=None)
    env.s.poll_inbox()
    assert env.s.poll_inbox()["duplicates"] == 1
    assert len(env.repo.rows("replies")) == 1


def test_no_followup_drafted_when_supplier_replied_on_another_channel(env):
    send(env)
    env.repo.update("messages", env.mid, sent_at="2020-01-01 00:00:00")
    other = env.repo.insert("messages", campaign_id=env.cid, supplier_id=env.sid, channel="wechat", reference="RFQ-1-WECHAT")
    env.s.record_manual_reply(other, "We will quote soon")
    assert env.s.draft_followups(env.cid)["drafted"] == 0


def test_approved_followup_is_skipped_if_reply_arrives_before_send(env):
    send(env)
    env.repo.update("messages", env.mid, sent_at="2020-01-01 00:00:00")
    assert env.s.draft_followups(env.cid)["drafted"] == 1
    env.s.approve(env.cid)
    env.s.record_manual_reply(env.mid, "Quote coming tomorrow")
    result = env.s.send_approved(env.cid)
    assert result["sent"] == 0 and result["skipped"] == 1


def test_real_channel_beats_generic_marketplace_search_link():
    supplier = {"canonical_name": "Test Factory", "wechat_id": "factory88"}
    assert routes(supplier)[0]["channel"] == "wechat"


def test_real_alibaba_page_keeps_priority_over_wechat():
    supplier = {"canonical_name": "T", "wechat_id": "f", "alibaba_url": "https://t.en.alibaba.com"}
    assert routes(supplier)[0]["channel"] == "alibaba"


def test_outgoing_mail_has_date_and_message_id_on_sender_domain(monkeypatch):
    import rfq.channels as mod
    sent = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def send_message(self, mail):
            sent.append(mail)

    monkeypatch.setattr(cfg, "RFQ_FROM_ADDRESS", "buyer@acme.test")
    monkeypatch.setattr(mod.SmtpSender, "connect", lambda self: Client())
    message_id = SmtpSender().send({"recipient": "a@b.test", "subject": "s", "body": "b"})
    assert sent[0]["Date"]
    assert message_id.rstrip(">").endswith("@acme.test")


def test_cors_allows_patch_for_draft_editing():
    from api.app import app
    cors = next(m for m in app.user_middleware if m.cls.__name__ == "CORSMiddleware")
    assert "PATCH" in cors.kwargs["allow_methods"]
