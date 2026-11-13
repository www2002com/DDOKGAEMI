"""5단계 통합 테스트 (§17.1 test_pipeline.py). 가짜 LLM·가짜 팩트시트·모의 브로커로 네트워크 없이 돈다."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest
from sqlalchemy import select

from autopilot_korea.agents.orchestrator import Orchestrator
from autopilot_korea.db.models import AdversarialOpinion, Order, PortfolioPosition, PriceData, ScreeningResult, ThesisJournal
from fakes import FakeLLM, fake_factsheet

AS_OF = date(2026, 11, 12)
TICKERS = [f"{i:06d}" for i in range(1, 31)]


class FixtureOrchestrator(Orchestrator):
    """데이터 계층만 가짜로 바꾼 오케스트레이터."""

    def __init__(self, *args, factsheets, **kwargs):
        self.fixture_factsheets = factsheets
        super().__init__(*args, **kwargs)

    def universe(self, as_of):
        return TICKERS

    def load_inputs(self, ctx, tickers):
        ctx.factsheets = {t: self.fixture_factsheets[t] for t in tickers if t in self.fixture_factsheets}
        ctx.news = {t: [] for t in tickers}
        ctx.macro = None


def seed_prices(db, prices: dict[str, float], day: date = AS_OF):
    with db.session() as s:
        for t, p in prices.items():
            row = s.scalars(select(PriceData).where(PriceData.ticker == t, PriceData.date == day)).first()
            if row:
                row.close = p
            else:
                s.add(PriceData(ticker=t, date=day, close=p, volume=1e6))


@pytest.fixture
def world(settings, db):
    cfg = dataclasses.replace(settings, mode="paper")
    cash = [e.ticker for e in cfg.constraints.portfolio.cash_etfs]
    factsheets = {t: fake_factsheet(t, AS_OF) for t in TICKERS}
    factsheets.update({t: fake_factsheet(t, AS_OF, price=100_000.0) for t in cash})
    seed_prices(db, {t: fs.extras["last_close"] for t, fs in factsheets.items()})
    return cfg, db, factsheets


def test_full_pipeline_paper_mode(world):
    cfg, db, factsheets = world
    llm = FakeLLM(violate_optimizer_first=True)
    orch = FixtureOrchestrator(cfg, db, llm=llm, factsheets=factsheets)
    summary = orch.run_pipeline(as_of=AS_OF, trigger="test")

    # Stage 1: 30종목 전부 스코어링
    assert summary["stage1"]["scored"] == 30 and not summary["stage1"]["failed"]
    # Stage 2: 종목당 불 15 + 베어 15 에이전트, 베어 > 불인 종목은 관찰 플래그
    with db.session() as s:
        ops = s.scalars(select(AdversarialOpinion).where(AdversarialOpinion.ticker == "000001")).all()
        assert sum(o.stance == "bull" for o in ops) == 15 and sum(o.stance == "bear" for o in ops) == 15
        # 최근 7일 규칙: 2000년 날짜의 근거는 코드가 버린다
        assert all(o.payload["evidence"] == [] for o in ops)
    assert summary["stage2"]["watch"] == ["000015", "000030"]
    # Stage 3: 생존 종목 시나리오, 기대수익 음수 종목(7의 배수)은 양수 목록에 없다
    assert summary["stage3"]["modeled"] == 28
    assert "000007" not in summary["stage3"]["positive_er"]
    # Stage 4: 첫 출력이 종목 상한을 어겨 재생성 → 두 번째 시도에서 통과
    assert summary["stage4"]["source"] == "llm" and summary["stage4"]["attempts"] == 2
    weights = summary["stage4"]["weights"]
    assert len(weights) == 15 and abs(sum(weights.values()) - 1) < 1e-6
    assert all(w <= 0.10 + 1e-9 for t, w in weights.items() if t not in cfg.constraints.portfolio.cash_etf_tickers)
    # Stage 5 + 실행: paper 모드는 자동 승인 후 모의 체결
    assert summary["stage5"]["submitted"]["submitted"] == 15
    account = orch.broker.account()
    assert len(account.holdings) == 15
    assert account.cash >= 0
    with db.session() as s:
        assert len(s.scalars(select(ThesisJournal).where(ThesisJournal.status == "open")).all()) == 14
        assert len(s.scalars(select(PortfolioPosition)).all()) == 15
        assert s.scalars(select(ScreeningResult).where(ScreeningResult.stage == "S2")).first() is not None
    report = open(summary["report"], encoding="utf-8").read()
    assert "본 정보는 참고용이며 개별 투자 권유가 아닙니다." in report


def test_second_run_trades_less(world):
    """같은 데이터로 다시 돌리면 보유를 유지하고 매도 주문이 없어야 한다 (실측 'trades less often')."""
    cfg, db, factsheets = world
    orch = FixtureOrchestrator(cfg, db, llm=FakeLLM(), factsheets=factsheets)
    orch.run_pipeline(as_of=AS_OF)
    second = orch.run_pipeline(as_of=AS_OF)
    with db.session() as s:
        sells = s.scalars(select(Order).where(Order.run_id == second["run_id"], Order.side == "SELL")).all()
    assert sells == []
    assert second["stage5"]["turnover"] == 0


def test_thesis_review_forces_exit(world):
    """§7-2: 보유 종목이 -25% 이하로 떨어지면 운영 패널이 재평가하고, exit 과반이면 전량 매도한다."""
    cfg, db, factsheets = world
    orch = FixtureOrchestrator(cfg, db, llm=FakeLLM(thesis_verdict="exit"), factsheets=factsheets)
    first = orch.run_pipeline(as_of=AS_OF)
    held = sorted(t for t in first["stage4"]["weights"] if t not in cfg.constraints.portfolio.cash_etf_tickers)
    victim = held[0]
    day2 = date(2026, 11, 16)
    factsheets[victim] = fake_factsheet(victim, day2, price=7_000.0)
    seed_prices(db, {victim: 7_000.0}, day2)
    second = orch.run_pipeline(as_of=day2)
    assert second["thesis_reviews"][victim]["verdict"] == "exit"
    with db.session() as s:
        sells = s.scalars(select(Order).where(Order.run_id == second["run_id"], Order.side == "SELL")).all()
    assert [o.ticker for o in sells] == [victim]
    assert all(o.status == "filled" for o in sells)
    assert victim not in orch.broker.account().holdings


def test_live_mode_never_auto_approves(world):
    cfg, db, factsheets = world
    live = dataclasses.replace(cfg, mode="live", toss_app_key="k", toss_app_secret="s")
    live.constraints.execution.auto_approve_modes.append("live")  # 설정을 바꿔도 코드가 막아야 한다
    try:
        from autopilot_korea.execution.paper import PaperBroker

        orch = FixtureOrchestrator(live, db, llm=FakeLLM(), factsheets=factsheets,
                                   broker=PaperBroker(live, db, lambda t: 10_000.0))
        summary = orch.run_pipeline(as_of=AS_OF)
        assert summary["stage5"]["auto_approved"] == 0
        assert summary["stage5"]["awaiting_human_approval"] is True
        with db.session() as s:
            assert {o.status for o in s.scalars(select(Order)).all()} <= {"proposed", "blocked"}
    finally:
        live.constraints.execution.auto_approve_modes.remove("live")


def test_rebalance_review_cancel_requires_human(world):
    """Stage 5 오케스트레이터가 Cancel로 판정한 신규 편입은 자동 승인되지 않는다."""
    cfg, db, factsheets = world
    probe = FixtureOrchestrator(cfg, db, llm=FakeLLM(), factsheets=factsheets)
    first_pick = sorted(t for t in probe.run_pipeline(as_of=AS_OF)["stage4"]["weights"]
                        if t not in cfg.constraints.portfolio.cash_etf_tickers)[0]

    from autopilot_korea.db import Database

    db2 = Database(f"sqlite:///{cfg.project_root}/t2.db")
    db2.create_all()
    seed_prices(db2, {t: fs.extras["last_close"] for t, fs in factsheets.items()})
    orch = FixtureOrchestrator(cfg, db2, llm=FakeLLM(cancel_add=first_pick), factsheets=factsheets)
    summary = orch.run_pipeline(as_of=AS_OF)
    with db2.session() as s:
        o = s.scalars(select(Order).where(Order.run_id == summary["run_id"], Order.ticker == first_pick)).first()
    assert o.needs_review is True and o.status == "proposed"
