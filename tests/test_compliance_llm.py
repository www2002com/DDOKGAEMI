"""컴플라이언스 필터(§17.2)와 LLM 계층(구조화 출력·폴백·감사 로그·리플레이)."""

from __future__ import annotations

import json

import anthropic
import httpx2
import pytest
from anthropic import DefaultHttpxClient
from sqlalchemy import select

from autopilot_korea.agents.llm import AnthropicLLM, ComplianceBlocked, LLMRefusal, LLMRequest
from autopilot_korea.agents.schemas import DartTriageOutput
from autopilot_korea.compliance import check_output, scan_text, with_disclaimer
from autopilot_korea.db.models import AuditLog

CC = {"loss_guarantee_mention": False, "individual_recommendation": False, "disclaimer_included": True}


@pytest.mark.parametrize("text,bad", [
    ("원금 보장이 없다는 점에 유의", False), ("이 종목은 원금 보장됩니다", True),
    ("확정 수익은 아니다", False), ("확정 수익 15%", True), ("지금 매수하세요", True),
    ("불 케이스: 매수 논거는 수출 회복", False), ("컨센서스는 강력매수", False),
    ("무조건 오른다", True), ("손실이 전혀 없습니다", True), ("사세요", True),
])
def test_forbidden_expressions(text, bad):
    assert bool(scan_text(text)) is bad


def test_self_report_mismatch_is_violation():
    out = {"summary": "정상 문장", "compliance_check": dict(CC, individual_recommendation=True)}
    assert not check_output(out).passed


def test_disclaimer_inserted_once():
    once = with_disclaimer("보고서")
    assert once.endswith("본 정보는 참고용이며 개별 투자 권유가 아닙니다.")
    assert with_disclaimer(once) == once


class Transport:
    """Claude API 모의 응답 — 응답 본문을 차례대로 돌려주고 요청을 기록한다."""

    def __init__(self, bodies):
        self.bodies, self.requests = list(bodies), []

    def __call__(self, request):
        self.requests.append(json.loads(request.content))
        body = self.bodies.pop(0)
        return httpx2.Response(200, json=body)


def message(payload=None, stop="end_turn"):
    content = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}] if payload is not None else []
    return {"id": "msg", "type": "message", "role": "assistant", "model": "claude-haiku-5-5", "content": content,
            "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}


def make_llm(settings, db, transport, replay=False):
    client = anthropic.Anthropic(api_key="test", http_client=DefaultHttpxClient(transport=httpx2.MockTransport(transport)))
    return AnthropicLLM(settings, db, client=client, replay=replay)


def request(route="dart_triage"):
    return LLMRequest(route=route, agent="test", system_blocks=["기본", "페르소나"], user="공시 제목",
                      output_model=DartTriageOutput, run_id="r1")


GOOD = {"importance": "high", "category": "유상증자", "summary": "희석 우려", "compliance_check": CC}
BAD = dict(GOOD, summary="지금 매수하세요")


def test_request_shape_and_audit(settings, db):
    t = Transport([message(GOOD)])
    out = make_llm(settings, db, t).generate(request())
    assert out.importance == "high"
    body = t.requests[0]
    assert body["model"] == "claude-haiku-5-5" and body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in body and "fallbacks" not in body  # Haiku 경로는 폴백 없음
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    with db.session() as s:
        row = s.scalars(select(AuditLog)).one()
        assert row.status == "ok" and "[SYSTEM]" in row.prompt_rendered and row.cache_key


def test_fallback_enabled_on_opus_route(settings, db):
    t = Transport([message(GOOD)])
    make_llm(settings, db, t).generate(request("dart_deep"))
    assert t.requests[0]["fallbacks"] == "default"


def test_compliance_regenerates_then_passes(settings, db):
    t = Transport([message(BAD), message(GOOD)])
    out = make_llm(settings, db, t).generate(request())
    assert out.summary == "희석 우려"
    assert "가드레일을 위반" in t.requests[1]["messages"][0]["content"]
    with db.session() as s:
        assert [r.status for r in s.scalars(select(AuditLog).order_by(AuditLog.id))] == ["blocked", "ok"]


def test_compliance_blocks_after_retries(settings, db):
    t = Transport([message(BAD)] * 3)
    with pytest.raises(ComplianceBlocked):
        make_llm(settings, db, t).generate(request())


def test_refusal_raises(settings, db):
    t = Transport([message(None, stop="refusal")])
    with pytest.raises(LLMRefusal):
        make_llm(settings, db, t).generate(request())


def test_replay_reproduces_without_api_call(settings, db):
    make_llm(settings, db, Transport([message(GOOD)])).generate(request())
    t = Transport([])  # 호출되면 pop에서 실패한다
    out = make_llm(settings, db, t, replay=True).generate(request())
    assert out.summary == "희석 우려" and t.requests == []
    with db.session() as s:
        assert s.scalars(select(AuditLog).order_by(AuditLog.id.desc())).first().status == "replayed"
