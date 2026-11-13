"""유사투자자문업 가드레일 (§11, §17.2 컴플라이언스 필터).

모든 LLM 출력에 대해:
  1. 금지 표현 검색 (손실보전, 이익보장, 확정 수익, 개별 권유)
  2. 고지 문구 자동 삽입 — 사람이 읽는 보고서·주문 근거에 붙인다 (with_disclaimer)
  3. compliance_check 필드 검증 — 모델의 자기 신고와 필터 결과가 어긋나면 위반
  4. 위반 시 출력 차단 + 재생성 (재생성 루프는 agents/llm.py가 담당)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from autopilot_korea import DISCLAIMER

# 범주별 금지 표현. 분석 문맥의 일반 용어("매수 논거", "강력매수 컨센서스")는 걸리지 않도록
# 약속·보장·2인칭 권유 형태만 잡는다.
FORBIDDEN_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "loss_guarantee": [
        re.compile(r"손실\s*(을\s*)?보전"),
        re.compile(r"원금\s*(이\s*)?보장"),
        re.compile(r"손실\s*(이|은)?\s*(전혀\s*)?없(다|습니다|는|을)"),
        re.compile(r"절대\s*(손해|손실)"),
    ],
    "profit_guarantee": [
        re.compile(r"(수익|이익|수익률)\s*(을\s*|이\s*)?보장"),
        re.compile(r"보장된\s*(수익|이익)"),
        re.compile(r"확정\s*(수익|이익|수익률)"),
        re.compile(r"(무조건|반드시|100\s*%)\s*(오른|오릅|상승|수익|이익)"),
        re.compile(r"틀림없이\s*(오른|오릅|상승)"),
    ],
    "individual_recommendation": [
        re.compile(r"(매수|매도|구매|구입|투자)\s*(하세요|하십시오|하시길|하시기\s*바랍니다|를?\s*추천(드립니다|합니다))"),
        re.compile(r"(사|파)세요"),
        re.compile(r"(지금|당장|꼭|빨리)\s*(사|팔|매수|매도)(야|하|세)"),
        re.compile(r"(매수|매도)\s*를?\s*권(합니다|해\s*드립니다|유합니다)"),
        re.compile(r"지금이\s*(매수|매도)\s*적기(입니다|다)"),
    ],
}


class ComplianceCheck(BaseModel):
    """§9 compliance_check — 모든 에이전트 출력에 포함되는 자기 신고 필드."""

    loss_guarantee_mention: bool
    individual_recommendation: bool
    disclaimer_included: bool


@dataclass
class ComplianceReport:
    passed: bool
    violations: list[dict[str, str]] = field(default_factory=list)

    def feedback(self) -> str:
        lines = [f"- [{v['category']}] \"{v['match']}\" (필드: {v['field']})" for v in self.violations]
        return "다음 표현이 유사투자자문업 가드레일을 위반합니다. 해당 표현 없이 다시 작성하십시오:\n" + "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "violations": self.violations}


# "원금 보장이 없다", "확정 수익은 아니다"처럼 보장을 부정하는 문장은 위반이 아니다.
_NEGATION = re.compile(r"^.{0,8}?(없|않|아니|아님|못|금지|불가)")
_NEGATABLE = {"loss_guarantee", "profit_guarantee"}


def scan_text(text: str, field_name: str = "text") -> list[dict[str, str]]:
    found = []
    for category, patterns in FORBIDDEN_PATTERNS.items():
        for pattern in patterns:
            for m in pattern.finditer(text):
                if category in _NEGATABLE and _NEGATION.match(text[m.end():]):
                    continue
                found.append({"category": category, "match": m.group(0), "field": field_name})
                break
    return found


def _walk_strings(value: Any, path: str):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _walk_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            yield from _walk_strings(v, f"{path}[{i}]")


def check_output(output: BaseModel | dict[str, Any]) -> ComplianceReport:
    """출력 전체의 문자열 필드를 검사하고 compliance_check 자기 신고와 대조한다."""
    data = output.model_dump() if isinstance(output, BaseModel) else dict(output)
    violations: list[dict[str, str]] = []
    for path, text in _walk_strings(data, ""):
        if path.startswith("compliance_check"):
            continue
        violations.extend(scan_text(text, path))

    declared = data.get("compliance_check")
    if declared is not None:
        if declared.get("loss_guarantee_mention") or declared.get("individual_recommendation"):
            violations.append({"category": "self_reported", "match": "compliance_check 자기 신고 위반", "field": "compliance_check"})
    return ComplianceReport(passed=not violations, violations=violations)


def with_disclaimer(text: str) -> str:
    """사람이 읽는 텍스트 끝에 고지 문구를 붙인다 (이미 있으면 그대로)."""
    text = (text or "").rstrip()
    if DISCLAIMER in text:
        return text
    return f"{text}\n\n※ {DISCLAIMER}" if text else f"※ {DISCLAIMER}"
