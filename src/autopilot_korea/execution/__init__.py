"""실행 엔진. live = 토스증권 Open API, paper = 내부 모의 브로커, dry_run = 제출 안 함."""

from __future__ import annotations

from typing import Callable

from autopilot_korea.db import Database
from autopilot_korea.execution.base import AccountSnapshot, Broker, Holding, OrderRequest, OrderStatus
from autopilot_korea.execution.paper import PaperBroker
from autopilot_korea.settings import Settings


def make_broker(settings: Settings, db: Database, price_lookup: Callable[[str], float | None]) -> Broker:
    """live만 토스증권을 쓴다. dry_run과 paper는 모의 브로커로 계좌를 계산한다(dry_run은 제출하지 않는다)."""
    if settings.mode == "live":
        from autopilot_korea.execution.toss import TossBroker, TossClient

        return TossBroker(TossClient(settings.toss_base_url, settings.toss_app_key, settings.toss_app_secret,
                                     account_no=settings.toss_account_no))
    return PaperBroker(settings, db, price_lookup)


__all__ = ["AccountSnapshot", "Broker", "Holding", "OrderRequest", "OrderStatus", "PaperBroker", "make_broker"]
