"""Agent quote reading (RFQ_QUOTE_PARSER=agent): no OpenAI call, same validation, fakes only."""
import pytest

from config import settings as cfg
from rfq.workspace_parser import WorkspaceParser
from tests.test_rfq_review_regressions import env, send  # noqa: F401  (fixture + helper)
from tests.test_rfq_api_cli import client  # noqa: F401

REPLY = "Unit price USD 2.50 per piece, MOQ 500 pcs, lead time 25 days, FOB Ningbo."

GOOD = [{
    "unit_price": 2.5, "currency": "USD", "moq": 500, "lead_time": "25 days", "incoterm": "FOB Ningbo",
    "evidence": {"unit_price": "Unit price USD 2.50 per piece", "currency": "USD 2.50",
                 "moq": "MOQ 500 pcs", "lead_time": "lead time 25 days", "incoterm": "FOB Ningbo"},
}]


@pytest.fixture
def agent(env, monkeypatch):
    monkeypatch.setattr(cfg, "RFQ_QUOTE_PARSER", "agent")
    send(env)
    env.s.record_manual_reply(env.mid, REPLY)
    return env


def test_agent_mode_stores_reply_pending_without_calling_openai(agent):
    assert agent.llm.calls == []
    reply = agent.repo.rows("replies")[0]
    assert reply["parse_status"] == "pending"
    assert agent.repo.rows("quotes") == []


def test_pending_lists_reply_with_exact_source_text_and_rfq_context(agent):
    pending = agent.s.pending_replies()["pending"]
    assert len(pending) == 1
    item = pending[0]
    assert item["source_text"] == REPLY
    assert item["rfq"]["product"] == "wheel"
    assert item["reference"].startswith("RFQ-")


def test_grounded_submission_is_stored_and_marks_reply_parsed(agent):
    result = agent.s.submit_agent_quotes(agent.repo.rows("replies")[0]["id"], GOOD)
    assert result["parse_status"] == "parsed" and result["rejections"] == []
    quote = agent.repo.rows("quotes")[0]
    assert quote["unit_price"] == 2.5 and quote["moq"] == 500 and quote["incoterm"] == "FOB Ningbo"
    assert agent.s.pending_replies()["pending"] == []


@pytest.mark.parametrize("field,value,proof,reason", [
    ("moq", 1000, "MOQ 500 pcs", "number does not appear"),
    ("incoterm", "CIF Felixstowe", "FOB Ningbo", "verbatim"),
    ("lead_time", "25 days", "delivery in 25 days", "exact quote"),
    ("currency", "GBP", "USD 2.50", "currency is not stated"),
])
def test_ungrounded_fields_are_rejected_with_a_reason(agent, field, value, proof, reason):
    candidate = {**GOOD[0], field: value, "evidence": {**GOOD[0]["evidence"], field: proof}}
    result = agent.s.submit_agent_quotes(agent.repo.rows("replies")[0]["id"], [candidate])
    assert any(r["field"] == field and reason in r["reason"] for r in result["rejections"])
    assert field not in result["accepted"][0]


def test_tier_without_grounded_price_is_not_stored(agent):
    bad = [{"unit_price": 9.99, "evidence": {"unit_price": "Unit price USD 2.50 per piece"}}]
    result = agent.s.submit_agent_quotes(agent.repo.rows("replies")[0]["id"], bad)
    assert result["accepted"] == [] and result["parse_status"] == "no_quote"
    assert agent.repo.rows("quotes") == []


def test_reply_can_only_be_submitted_once(agent):
    rid = agent.repo.rows("replies")[0]["id"]
    agent.s.submit_agent_quotes(rid, GOOD)
    with pytest.raises(ValueError, match="already read"):
        agent.s.submit_agent_quotes(rid, GOOD)


def test_opt_out_is_not_offered_or_accepted(env, monkeypatch):
    monkeypatch.setattr(cfg, "RFQ_QUOTE_PARSER", "agent")
    send(env)
    env.s.record_manual_reply(env.mid, "unsubscribe")
    assert env.s.pending_replies()["pending"] == []
    with pytest.raises(ValueError, match="no quote"):
        env.s.submit_agent_quotes(env.repo.rows("replies")[0]["id"], GOOD)


def test_failed_openai_parse_is_offered_to_the_agent(env):
    # Default mode with the fake LLM returning nothing usable -> 'failed'.
    env.llm.result = None
    send(env)
    env.s.record_manual_reply(env.mid, REPLY)
    assert env.repo.rows("replies")[0]["parse_status"] == "failed"
    assert len(env.s.pending_replies()["pending"]) == 1


def test_manual_reply_validates_against_stripped_text(agent, monkeypatch):
    # Evidence from our own quoted RFQ (below the history marker) must not count.
    agent.s.record_manual_reply(agent.mid, "Price USD 3.10\nFrom: buyer@test.com\nUnit price USD 2.50 per piece")
    rid = agent.repo.rows("replies")[-1]["id"]
    result = agent.s.submit_agent_quotes(rid, GOOD)
    assert result["accepted"] == []


def test_workspace_parser_never_uses_model_in_agent_mode(monkeypatch):
    monkeypatch.setattr(cfg, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(cfg, "RFQ_QUOTE_PARSER", "agent")
    assert WorkspaceParser(llm=object()).use_model is False
    monkeypatch.setattr(cfg, "RFQ_QUOTE_PARSER", "openai")
    assert WorkspaceParser(llm=object()).use_model is True


def test_status_reports_quote_parser_mode(agent):
    assert agent.s.status()["quote_parser"] == "agent"


def test_api_routes_require_token_and_round_trip(client, env, monkeypatch):
    monkeypatch.setattr(cfg, "RFQ_QUOTE_PARSER", "agent")
    env.s.approve(env.cid)
    env.s.send_approved(env.cid)
    env.s.record_manual_reply(env.mid, REPLY)
    bad = {"Authorization": "Bearer wrong"}
    assert client.get("/rfq/replies/pending", headers=bad).status_code == 401
    assert client.post("/rfq/replies/1/quotes", json={"quotes": []}, headers=bad).status_code == 401
    pending = client.get("/rfq/replies/pending").json()["pending"]
    rid = pending[0]["reply_id"]
    response = client.post(f"/rfq/replies/{rid}/quotes", json={"quotes": GOOD})
    assert response.status_code == 200 and response.json()["parse_status"] == "parsed"
    assert client.post(f"/rfq/replies/{rid}/quotes", json={"quotes": GOOD}).status_code == 400
