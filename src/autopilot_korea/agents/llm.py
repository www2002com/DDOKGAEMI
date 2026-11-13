"""LLM 호출 계층.

설계 원칙 §0-4(재현성)의 구현:
  - 구조화 출력: client.beta.messages.parse(output_format=<pydantic 모델>)로 JSON을 강제한다.
  - 프롬프트 전문 로깅: 모든 시도를 audit_log에 남긴다 (§8-3).
  - 온도 0·시드 고정: 현행 Claude 모델과 API(2026-10-21)는 temperature·seed를 받지 않는다.
    대신 같은 입력의 응답을 cache_key로 저장하고, replay=True면 저장된 응답을 그대로 돌려준다.
    백테스트·재실행은 이 리플레이로 동일 결과를 재현한다. 코드 측 무작위성은 seed 42로 고정한다.

컴플라이언스(§17.2): 출력마다 금지 표현을 검사하고, 위반이면 피드백을 붙여 재생성한다.
재생성 후에도 위반이면 ComplianceBlocked로 출력을 차단한다.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel
from sqlalchemy import select

from autopilot_korea.compliance import check_output
from autopilot_korea.db import Database
from autopilot_korea.db.models import AuditLog
from autopilot_korea.settings import Settings

SEED = 42
FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEB_SEARCH_TOOL = "web_search_20260209"
MAX_COMPLIANCE_RETRIES = 2
MAX_PAUSE_TURNS = 3

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    pass


class ComplianceBlocked(LLMError):
    pass


@dataclass
class LLMRequest:
    route: str  # config/models.yaml routes 키
    agent: str  # 감사 로그 라벨, 예: "S2:bull:07_value"
    system_blocks: list[str]  # 고정 접두부 (기본 지시 + 페르소나 + 단계 지시) — 프롬프트 캐시 대상
    user: str  # 종목별 가변 입력
    output_model: type[BaseModel]
    stage: str | None = None
    ticker: str | None = None
    persona_sha256: str | None = None
    web_search: bool = False
    run_id: str | None = None

    def render(self, user: str | None = None) -> str:
        return "[SYSTEM]\n" + "\n\n".join(self.system_blocks) + "\n\n[USER]\n" + (user or self.user)


@dataclass
class CallResult:
    output: BaseModel
    raw: str
    meta: dict[str, Any] = field(default_factory=dict)


class LLMClient(Protocol):
    def generate(self, req: LLMRequest) -> Any: ...


class AuditedLLM:
    """컴플라이언스 재생성 루프 + 감사 로그. 실제 호출은 하위 클래스의 _call이 한다."""

    def __init__(self, settings: Settings, db: Database, *, replay: bool = False):
        self.settings = settings
        self.db = db
        self.replay = replay
        self._lock = threading.Lock()

    def cache_key(self, req: LLMRequest, user: str) -> str:
        route = self.settings.models["routes"][req.route]
        payload = {
            "model": route["model"],
            "effort": route.get("effort"),
            "system": req.system_blocks,
            "user": user,
            "schema": req.output_model.model_json_schema(),
            "web_search": req.web_search,
        }
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def generate(self, req: LLMRequest) -> Any:
        user = req.user
        for _ in range(1 + MAX_COMPLIANCE_RETRIES):
            key = self.cache_key(req, user)
            replayed = self._replay(req, key) if self.replay else None
            if replayed is not None:
                return replayed
            try:
                result = self._call(req, user)
            except LLMError as exc:
                self._audit(req, user, key, None, "refusal" if isinstance(exc, LLMRefusal) else "error", {}, {"error": str(exc)})
                raise
            report = check_output(result.output)
            self._audit(req, user, key, result.raw, "ok" if report.passed else "blocked", report.to_dict(), result.meta)
            if report.passed:
                return result.output
            user = f"{req.user}\n\n{report.feedback()}"
        raise ComplianceBlocked(f"{req.agent}: 재생성 {MAX_COMPLIANCE_RETRIES}회 후에도 컴플라이언스 위반")

    def _call(self, req: LLMRequest, user: str) -> CallResult:  # pragma: no cover - 하위 클래스 구현
        raise NotImplementedError

    def _replay(self, req: LLMRequest, key: str) -> BaseModel | None:
        with self.db.session() as s:
            row = s.scalars(
                select(AuditLog).where(AuditLog.cache_key == key, AuditLog.status == "ok").order_by(AuditLog.id.desc())
            ).first()
            if row is None or row.response is None:
                return None
            output = req.output_model.model_validate_json(row.response)
        self._audit(req, req.user, key, row.response, "replayed", {"passed": True}, {"replayed_from": row.id})
        return output

    def _audit(self, req: LLMRequest, user: str, key: str, raw: str | None, status: str,
               compliance: dict[str, Any], meta: dict[str, Any]) -> None:
        route = self.settings.models["routes"][req.route]
        with self._lock, self.db.session() as s:
            s.add(AuditLog(
                run_id=req.run_id, agent=req.agent, stage=req.stage, ticker=req.ticker,
                model=meta.get("model", route["model"]), persona_sha256=req.persona_sha256, cache_key=key,
                prompt_rendered=req.render(user), response=raw, status=status,
                compliance_check=compliance, usage=meta,
            ))


class AnthropicLLM(AuditedLLM):
    """Claude API 호출. 모델·effort·폴백은 config/models.yaml의 route가 정한다 (§6)."""

    def __init__(self, settings: Settings, db: Database, *, replay: bool = False, client: Any = None):
        super().__init__(settings, db, replay=replay)
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=3)  # §15 API 재시도 3회
        self.client = client

    def request_kwargs(self, req: LLMRequest, user: str) -> dict[str, Any]:
        route = self.settings.models["routes"][req.route]
        kwargs: dict[str, Any] = {
            "model": route["model"],
            "max_tokens": route.get("max_tokens", self.settings.models.get("defaults", {}).get("max_tokens", 16000)),
            # 고정 접두부를 한 블록으로 묶어 캐시한다 — 종목마다 바뀌는 내용은 user 메시지에만 둔다.
            "system": [{"type": "text", "text": "\n\n".join(req.system_blocks), "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_format": req.output_model,
            "output_config": {"effort": route.get("effort", "high")},
        }
        if route.get("fallbacks", "none") == "default":
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        web = self.settings.models.get("web_search", {})
        if req.web_search and web.get("enabled"):
            kwargs["tools"] = [{
                "type": WEB_SEARCH_TOOL, "name": "web_search",
                "max_uses": int(web.get("max_uses", 4)),
                "allowed_domains": list(web.get("allowed_domains", [])),
            }]
        return kwargs

    def _call(self, req: LLMRequest, user: str) -> CallResult:
        kwargs = self.request_kwargs(req, user)
        response = self.client.beta.messages.parse(**kwargs)
        # 서버 도구(web_search) 반복이 한도에 닿으면 pause_turn — 받은 내용을 그대로 이어 보낸다.
        for _ in range(MAX_PAUSE_TURNS):
            if response.stop_reason != "pause_turn":
                break
            content = [_block_param(b) for b in response.content]
            kwargs["messages"] = kwargs["messages"] + [{"role": "assistant", "content": content}]
            response = self.client.beta.messages.parse(**kwargs)

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMRefusal(f"{req.agent}: 모델 거절 ({getattr(details, 'category', None)})")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"{req.agent}: max_tokens 도달 — 출력이 잘렸습니다")
        output = response.parsed_output
        if output is None:
            raise LLMError(f"{req.agent}: 구조화 출력 파싱 실패 (stop_reason={response.stop_reason})")
        usage = response.usage
        meta = {
            "model": response.model,
            "request_id": getattr(response, "_request_id", None),
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
            "stop_reason": response.stop_reason,
        }
        return CallResult(output=output, raw=output.model_dump_json(), meta=meta)


def _block_param(block: Any) -> dict[str, Any]:
    data = block.to_dict() if hasattr(block, "to_dict") else dict(block)
    data.pop("parsed_output", None)
    return data
