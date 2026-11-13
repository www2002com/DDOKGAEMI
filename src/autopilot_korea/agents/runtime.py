"""파이프라인 실행 컨텍스트와 병렬 실행 도우미."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Iterable, TypeVar

from autopilot_korea.agents.llm import LLMClient
from autopilot_korea.agents.personas import PersonaLibrary
from autopilot_korea.db import Database
from autopilot_korea.settings import Settings

log = logging.getLogger(__name__)
I = TypeVar("I")
R = TypeVar("R")


@dataclass
class PipelineContext:
    settings: Settings
    db: Database
    llm: LLMClient
    personas: PersonaLibrary
    run_id: str
    as_of: date
    factsheets: dict[str, Any] = field(default_factory=dict)  # ticker → collectors.factsheet.Factsheet
    news: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    macro: dict[str, Any] | None = None
    sentiment: dict[str, dict[str, Any]] = field(default_factory=dict)
    drawdown_review: bool = False

    def feature(self, name: str) -> bool:
        return self.settings.feature(name)

    @property
    def rules(self):
        return self.settings.constraints


def last_price(fs: Any) -> float | None:
    """팩트시트의 최신 종가. extras.last_close가 기준이며, 없으면 호가 중간값을 쓴다."""
    price = (getattr(fs, "extras", None) or {}).get("last_close")
    if price:
        return float(price)
    v = fs.variables
    if v.get("bid") and v.get("ask"):
        return (float(v["bid"]) + float(v["ask"])) / 2
    return None


def parallel_map(fn: Callable[[I], R], items: Iterable[I], max_workers: int) -> list[tuple[I, R | None, Exception | None]]:
    """항목별로 fn을 병렬 실행한다. 한 항목의 실패가 전체를 멈추지 않는다 (§15: 실패 시 스킵)."""
    items = list(items)

    def safe(item: I) -> tuple[I, R | None, Exception | None]:
        try:
            return item, fn(item), None
        except Exception as exc:  # noqa: BLE001 — 항목 단위 격리가 목적
            log.warning("항목 처리 실패 %s: %s", item, exc)
            return item, None, exc

    if max_workers <= 1 or len(items) <= 1:
        return [safe(i) for i in items]
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(safe, items))
