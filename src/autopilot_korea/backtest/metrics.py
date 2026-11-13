"""성과 지표 (§14): CAGR, Sharpe, MDD, 회전율, 벤치마크 대비 초과 수익."""

from __future__ import annotations

import math
from datetime import date

TRADING_DAYS = 252


def returns(navs: list[float]) -> list[float]:
    return [b / a - 1 for a, b in zip(navs, navs[1:]) if a > 0]


def cagr(navs: list[float], dates: list[date]) -> float:
    if len(navs) < 2 or navs[0] <= 0:
        return 0.0
    years = max((dates[-1] - dates[0]).days / 365.25, 1 / 365.25)
    return (navs[-1] / navs[0]) ** (1 / years) - 1


def sharpe(navs: list[float], risk_free_annual: float = 0.0) -> float:
    r = returns(navs)
    if len(r) < 2:
        return 0.0
    rf = (1 + risk_free_annual) ** (1 / TRADING_DAYS) - 1
    excess = [x - rf for x in r]
    mean = sum(excess) / len(excess)
    var = sum((x - mean) ** 2 for x in excess) / (len(excess) - 1)
    return mean / math.sqrt(var) * math.sqrt(TRADING_DAYS) if var > 0 else 0.0


def max_drawdown(navs: list[float]) -> float:
    peak, worst = float("-inf"), 0.0
    for v in navs:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst


def annual_turnover(one_way_turnovers: list[float], dates: list[date]) -> float:
    """리밸런싱별 회전율(min(매수, 매도) ÷ NAV)의 연환산 합."""
    if len(dates) < 2:
        return 0.0
    years = max((dates[-1] - dates[0]).days / 365.25, 1 / 365.25)
    return sum(one_way_turnovers) / years


def summarize(navs: list[float], dates: list[date], turnovers: list[float],
              benchmark: list[float] | None = None, risk_free_annual: float = 0.0) -> dict[str, float]:
    out = {
        "total_return": navs[-1] / navs[0] - 1 if navs and navs[0] else 0.0,
        "cagr": cagr(navs, dates),
        "sharpe": sharpe(navs, risk_free_annual),
        "mdd": max_drawdown(navs),
        "annual_turnover": annual_turnover(turnovers, dates),
    }
    if benchmark and len(benchmark) == len(navs):
        out["benchmark_cagr"] = cagr(benchmark, dates)
        out["excess_cagr"] = out["cagr"] - out["benchmark_cagr"]
        out["benchmark_mdd"] = max_drawdown(benchmark)
    return out
