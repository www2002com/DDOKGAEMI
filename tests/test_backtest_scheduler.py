"""백테스트 프레임워크(§14)와 스케줄(§4.3, §13)."""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta

import pytest

from autopilot_korea.backtest import BacktestConfig, LookaheadError, PriceBook, run_backtest
from autopilot_korea.backtest.metrics import cagr, max_drawdown, sharpe
from autopilot_korea.backtest.strategies import (PARAPHRASES, HoldStrategy, ScreenTopN, control_groups,
                                                 estimate_llm_calls, sweep_strategies)
from autopilot_korea.db.models import PriceData
from autopilot_korea.scheduler.beat import build_beat_schedule
from autopilot_korea.settings import load_settings
from fakes import FakeLLM, fake_factsheet
from test_pipeline import TICKERS, FixtureOrchestrator


def seed_series(db, tickers, start, days, drift):
    """평일만 있는 가격 시계열. drift[t]는 일간 수익률."""
    out, d, i = {}, start, 0
    with db.session() as s:
        while i < days:
            if d.weekday() < 5:
                for t in tickers:
                    px = 10_000 * (1 + drift.get(t, 0)) ** i
                    s.add(PriceData(ticker=t, date=d, open=px, close=px, volume=1e6))
                out[d] = 1.0 + 0.0005 * i
                i += 1
            d += timedelta(days=1)
    return out


class Const:
    name = "const"

    def __init__(self, w):
        self.w, self.calls = w, []

    def target_weights(self, signal_date, current):
        self.calls.append(signal_date)
        return self.w


def test_metrics():
    assert max_drawdown([100, 120, 90, 130]) == pytest.approx(-0.25)
    assert cagr([100, 121], [date(2025, 1, 1), date(2027, 1, 1)]) == pytest.approx(0.1, abs=1e-3)
    assert sharpe([100, 101, 102, 103]) > 0


def test_engine_uses_previous_day_signal_and_costs(db):
    start = date(2026, 11, 2)  # 월요일
    bench = seed_series(db, ["A", "B"], start, 15, {"A": 0.01, "B": -0.01})
    prices = PriceBook(db, start, start + timedelta(days=30))
    strat = Const({"A": 1.0})
    res = run_backtest(prices, strat, BacktestConfig(start=start, end=start + timedelta(days=30), initial_capital=1e6), bench)
    # 결정은 항상 리밸런싱일의 직전 거래일 데이터로 한다 (룩어헤드 차단)
    assert all(sd < d for sd, d in zip(strat.calls, [d for d in res.dates[1:] if d.weekday() in (0, 3)]))
    assert res.metrics["total_return"] > 0.1  # A는 매일 +1%
    assert "excess_cagr" in res.metrics and res.trades[0]["side"] == "BUY"


def test_lookahead_guard_rejects_period_before_model_cutoff(db):
    cfg = BacktestConfig(start=date(2026, 1, 5), end=date(2026, 3, 1), model_knowledge_cutoff=date(2026, 6, 30))
    with pytest.raises(LookaheadError):
        run_backtest(PriceBook(db, cfg.start, cfg.end), Const({}), cfg)


def test_control_groups_and_sweep(settings, db):
    cfg = dataclasses.replace(settings, mode="paper")
    as_of = date(2026, 11, 12)
    fs = {t: fake_factsheet(t, as_of) for t in TICKERS}
    orch = FixtureOrchestrator(cfg, db, llm=FakeLLM(), factsheets=fs)
    groups = control_groups(orch, top_n=5)
    assert [g.name for g in groups] == ["control:01_default", "control:04_minimal", "control:05_hold_llm"]
    w = groups[0].target_weights(as_of, {})
    assert len(w) == 5 and sum(w.values()) == pytest.approx(1.0)
    hold = groups[2]
    first = hold.target_weights(as_of, {})
    assert hold.target_weights(as_of + timedelta(days=3), {"x": 1.0}) == {"x": 1.0} and first  # 보유 유지
    assert len(PARAPHRASES) == 4
    sweep = sweep_strategies(orch, framings=("excess", "risk_adjusted"), objectives=("score", "score_x_confidence"),
                             grid_top_n=(10, 15))
    assert len(sweep) == 4 * 2 * 2 * 2 and all(isinstance(s, ScreenTopN) for s in sweep)
    assert estimate_llm_calls(3, 10, 350) == 10_500
    assert isinstance(hold, HoldStrategy)


def test_beat_schedule_matches_cadence():
    class Cron:
        def __init__(self, **kw):
            self.kw = kw

    sched = build_beat_schedule(load_settings().schedule, Cron)
    run = sched["pipeline-full-run"]["schedule"].kw
    assert run == {"minute": "30", "hour": "7", "day_of_week": "1,4"}  # 주 2회 (월·목)
    assert sched["collect-daily_after_close"]["kwargs"]["jobs"] == ["price", "momentum", "short_interest", "info", "news"]
    assert sched["collect-daily_after_close"]["schedule"].kw["hour"] == "18"
    assert sched["collect-weekly"]["schedule"].kw["day_of_week"] == "0"  # 일요일
    assert sched["collect-monthly"]["schedule"].kw["day_of_month"] == "1"
    assert sched["dart-monitor"]["schedule"].kw["minute"] == "*/30"
    assert sched["collect-quarterly-on-filing"]["task"] == "autopilot.collect_on_filing"
    assert {"stale-orders", "nav-snapshot", "sentiment-radar", "macro-outlook"} <= set(sched)
