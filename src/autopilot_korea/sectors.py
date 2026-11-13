"""섹터 분류 (§3.3 섹터 비중 ≤ 35%).

한국 공개 데이터에는 GICS가 없으므로 config/sectors.yaml의 규칙으로 근사한다.
순서: 티커 overrides → keyword_rules를 위에서부터 대조해 처음 맞는 규칙 → 어디에도 없으면 "미분류".
"""

from __future__ import annotations

from typing import Any

UNCLASSIFIED = "미분류"


def classify_sector(ticker: str, industry_text: str | None, cfg: dict[str, Any]) -> str:
    """티커와 업종명(KRX 업종명 또는 KSIC 업종명)으로 GICS 근사 섹터를 정한다."""
    overrides = cfg.get("overrides") or {}
    if str(ticker) in overrides:
        return overrides[str(ticker)]
    text = industry_text or ""
    for rule in cfg.get("keyword_rules") or []:
        if any(keyword in text for keyword in rule.get("keywords", [])):
            return rule["sector"]
    return UNCLASSIFIED
