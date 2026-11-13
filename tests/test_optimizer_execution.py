"""Stage 4 제약 검증·폴백, Stage 5 스왑 기준, 리스크 게이트, 토스증권 클라이언트, 주문 관리."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from autopilot_korea.agents.optimizer import Candidate, fallback_portfolio, validate
from autopilot_korea.agents.rebalancer import apply_swap_rule, one_way_turnover
from autopilot_korea.db.models import Order
from autopilot_korea.execution.base import snap_price, tick_size
from autopilot_korea.execution.calendar import TradingCalendar
from autopilot_korea.execution.order_manager import OrderManager
from autopilot_korea.execution.paper import PaperBroker
from autopilot_korea.execution.toss import TossApiError, TossBroker, TossClient
from autopilot_korea.risk import PlannedOrder, max_drawdown, pre_execution_gate, split_quantity, turnover_scale

SECTORS = ["정보기술", "산업재", "금융", "헬스케어", "소재"]


def candidates(n=20, er=0.1, cash="459580"):
    cs = {f"{i:06d}": Candidate(ticker=f"{i:06d}", name=str(i), sector=SECTORS[i % 5], score=100 - i,
                                expected_return=er, confidence=0.7, market_cap=1e12 - i) for i in range(1, n + 1)}
    cs[cash] = Candidate(ticker=cash, name="cash", sector="현금성", score=None, expected_return=None, is_cash_etf=True)
    return cs


# ── Stage 4 ──────────────────────────────────────────────────────────

def test_validate_catches_each_constraint(settings):
    rules = settings.constraints
    cs = candidates()
    cs["000003"].sector = cs["000001"].sector = cs["000002"].sector = cs["000004"].sector = "정보기술"
    bad = {"000001": 0.12, "000002": 0.10, "000003": 0.10, "000004": 0.10, "999999": 0.1, "459580": 0.25}
    v = " | ".join(validate(bad, cs, rules))
    assert "후보에 없는 종목" in v and "000001 비중" in v and "현금성 ETF" in v and "섹터 정보기술" in v
    cs["000005"].expected_return = -0.01
    assert any("기대수익" in x for x in validate({"000005": 0.1}, cs, rules))


def test_fallback_satisfies_all_constraints(settings):
    rules = settings.constraints
    cs = candidates()
    w = fallback_portfolio(cs, rules)
    assert validate(w, cs, rules) == []
    assert len(w) == 15 and abs(sum(w.values()) - 1) < 1e-6
    dd = fallback_portfolio(cs, rules, drawdown_review=True)
    assert validate(dd, cs, rules, drawdown_review=True) == [] and dd["459580"] == pytest.approx(0.20)


def test_fallback_with_few_eligible_leaves_residual_cash(settings):
    rules = settings.constraints
    cs = candidates(n=5)
    w = fallback_portfolio(cs, rules)
    assert sum(w.values()) == pytest.approx(5 * 0.10 + 0.20)  # 적격 5종목 × 10% + 현금성 20%, 나머지는 미투자 현금
    assert validate(w, cs, rules) == []


# ── Stage 5 스왑 기준 (§3.4) ───────────────────────────────────────────

def test_swap_rule_keeps_holding_within_gap(settings):
    rules = settings.constraints
    cs = candidates()
    target = {t: 0.065 for t in [f"{i:06d}" for i in range(1, 15)]} | {"459580": 0.09}
    current = {"000020": 0.065}  # Stage 4가 뺀 보유 종목
    scores = {t: c.score for t, c in cs.items() if c.score}
    scores["000020"] = scores["000014"] - 5  # 신규 최저보다 5점 낮음 → 유지
    adjusted, decisions, _ = apply_swap_rule(target, current, scores, cs, rules, set())
    assert "000020" in adjusted and validate(adjusted, cs, rules) == []
    assert any(d["replace_decision"] == "Cancel" and d["ticker_in"] == "000020" for d in decisions)
    scores["000020"] = scores["000014"] - 15  # 10점 이상 낮음 → 교체
    adjusted, decisions, _ = apply_swap_rule(target, current, scores, cs, rules, set())
    assert "000020" not in adjusted and any(d["ticker_out"] == "000020" for d in decisions)


def test_forced_exit_ignores_gap(settings):
    cs = candidates()
    target = {t: 0.065 for t in [f"{i:06d}" for i in range(1, 15)]} | {"459580": 0.09}
    adjusted, _, _ = apply_swap_rule(target, {"000020": 0.065}, {"000020": 99}, cs, settings.constraints, {"000020"})
    assert "000020" not in adjusted


# ── 리스크 (§7) ─────────────────────────────────────────────────────

def test_risk_helpers():
    assert max_drawdown([100, 120, 96]) == pytest.approx(-0.2)
    assert split_quantity(1000, 10_000, adv_value=100_000_000, max_pct=0.05) == [500, 500]
    assert split_quantity(10, 10_000, adv_value=None, max_pct=0.05) == [10]
    assert turnover_scale(0.2, 0.2, 0.3) == pytest.approx(0.5)
    assert one_way_turnover([1_000_000, 500_000], 10_000_000) == 0  # 현금에서 신규 편입만 하면 회전율 0
    assert one_way_turnover([1_000_000, -400_000], 10_000_000) == pytest.approx(0.04)


def test_pre_execution_gate(settings):
    orders = [PlannedOrder("000001", "BUY", 10, "LIMIT", 10_000, "목표 비중"),
              PlannedOrder("000002", "SELL", 10, "LIMIT", 10_000, "목표 비중"),
              PlannedOrder("000003", "BUY", 10, "LIMIT", 10_000, "지금 매수하세요"),
              PlannedOrder("000004", "BUY", 10_000, "LIMIT", 10_000, "목표 비중")]
    pre_execution_gate(orders, settings, {"000001": ["관리종목"], "000002": ["관리종목"]})
    assert "매수 차단" in orders[0].blocked_reason
    assert orders[1].blocked_reason is None  # 관리종목 매도는 허용
    assert "컴플라이언스" in orders[2].blocked_reason
    assert "1건 상한" in orders[3].blocked_reason


# ── 호가단위·거래일 ─────────────────────────────────────────────────

@pytest.mark.parametrize("price,tick", [(1_999, 1), (4_999, 5), (19_990, 10), (49_950, 50), (199_900, 100), (499_500, 500), (812_000, 1_000)])
def test_tick_size(price, tick):
    assert tick_size(price) == tick


def test_snap_price():
    assert snap_price(10_003, "BUY") == 10_010 and snap_price(10_003, "SELL") == 10_000
    assert snap_price(100_003, "BUY", is_etf=True) == 100_005


def test_trading_calendar():
    cal = TradingCalendar({date(2026, 12, 25)})
    assert cal.trading_days_between(date(2026, 12, 23), date(2026, 12, 28)) == 2  # 24일, 28일 (25일 휴장, 주말)
    assert cal.add_trading_days(date(2026, 12, 24), 1) == date(2026, 12, 28)


# ── 토스증권 클라이언트 (모의 HTTP) ──────────────────────────────────

class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls, self.tokens = routes, [], 0

    def post(self, url, data=None, timeout=None):
        self.tokens += 1
        return FakeResponse(200, {"access_token": f"tok{self.tokens}", "token_type": "Bearer", "expires_in": 86400})

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        path = url.split("tossinvest.com")[1]
        self.calls.append((method, path, params, json, headers))
        handler = self.routes[(method, path)]
        return handler(params, json, headers) if callable(handler) else handler


def toss(routes):
    session = FakeSession(routes)
    return TossClient("https://openapi.tossinvest.com", "id", "secret", session=session), session


ACCOUNTS = FakeResponse(200, {"result": [{"accountNo": "123", "accountSeq": 7, "accountType": "BROKERAGE"}]})


def test_toss_order_flow_uses_official_fields():
    client, session = toss({
        ("GET", "/api/v1/accounts"): ACCOUNTS,
        ("POST", "/api/v1/orders"): FakeResponse(200, {"result": {"orderId": "o1", "clientOrderId": "c1"}}),
        ("GET", "/api/v1/orders/o1"): FakeResponse(200, {"result": {"orderId": "o1", "status": "PARTIAL_FILLED",
                                                                    "execution": {"filledQuantity": "3", "averageFilledPrice": "70100"}}}),
        ("POST", "/api/v1/orders/o1/cancel"): FakeResponse(200, {"result": {"orderId": "o2"}}),
        ("GET", "/api/v1/orderbook"): FakeResponse(200, {"result": {"currency": "KRW", "asks": [{"price": "70100", "volume": "5"}],
                                                                   "bids": [{"price": "70000", "volume": "9"}]}}),
    })
    broker = TossBroker(client)
    assert client.create_order("005930", "BUY", "LIMIT", 10, 70_100, "c1") == "o1"
    method, path, _, body, headers = session.calls[-1]
    assert body == {"symbol": "005930", "side": "BUY", "orderType": "LIMIT", "quantity": "10", "timeInForce": "DAY",
                    "clientOrderId": "c1", "price": "70100"}
    assert headers["X-Tossinvest-Account"] == "7" and headers["Authorization"] == "Bearer tok1"
    st = broker.order_status("o1")
    assert (st.status, st.filled_quantity, st.avg_fill_price) == ("partially_filled", 3, 70100.0)
    assert broker.cancel("o1") == "o2"
    assert broker.best_quote("005930") == {"bid": 70000.0, "ask": 70100.0, "bid_size": 9.0, "ask_size": 5.0}


def test_toss_high_value_order_confirmation():
    client, session = toss({("GET", "/api/v1/accounts"): ACCOUNTS,
                            ("POST", "/api/v1/orders"): FakeResponse(200, {"result": {"orderId": "o9"}})})
    client.create_order("005930", "BUY", "LIMIT", 2_000, 70_000, "c")
    assert session.calls[-1][3]["confirmHighValueOrder"] is True


def test_toss_refreshes_expired_token_once():
    state = {"n": 0}

    def holdings(params, body, headers):
        state["n"] += 1
        if state["n"] == 1:
            return FakeResponse(401, {"error": {"code": "expired-token", "message": "만료"}})
        return FakeResponse(200, {"result": {"items": []}})

    client, session = toss({("GET", "/api/v1/accounts"): ACCOUNTS, ("GET", "/api/v1/holdings"): holdings})
    assert client.holdings() == {"items": []}
    assert session.tokens == 2


def test_toss_error_envelope():
    client, _ = toss({("GET", "/api/v1/accounts"): ACCOUNTS, ("GET", "/api/v1/buying-power"): FakeResponse(
        403, {"error": {"code": "ip-not-allowed", "message": "허용되지 않은 IP", "requestId": "r1"}})})
    with pytest.raises(TossApiError) as exc:
        client.buying_power()
    assert exc.value.code == "ip-not-allowed" and exc.value.request_id == "r1"


def test_toss_regulatory_flags():
    client, _ = toss({
        ("GET", "/api/v1/stocks/000001/warnings"): FakeResponse(200, {"result": [{"warningType": "INVESTMENT_WARNING"}, {"warningType": "VI_STATIC"}]}),
        ("GET", "/api/v1/stocks"): FakeResponse(200, {"result": [{"symbol": "000001", "koreanMarketDetail": {"krxTradingSuspended": True, "liquidationTrading": False}}]}),
    })
    assert TossBroker(client).regulatory_flags("000001") == ["VI_발동", "거래정지", "투자경고"]


# ── 주문 수명주기 (모의 브로커) ─────────────────────────────────────

def test_order_lifecycle_and_stale_cancel(settings, db):
    cfg = dataclasses.replace(settings, mode="paper")
    prices = {"000001": 10_000.0}
    broker = PaperBroker(cfg, db, prices.get)
    om = OrderManager(cfg, db, broker, TradingCalendar())
    ids = om.propose("r1", [PlannedOrder("000001", "BUY", 100, "LIMIT", 9_000, "목표"),  # 지정가가 낮아 미체결
                            PlannedOrder("000001", "BUY", 50, "LIMIT", 10_050, "목표")])
    assert om.auto_approve("r1") == 2
    om.submit_approved(date(2026, 11, 12))
    with db.session() as s:
        statuses = [s.get(Order, i).status for i in ids]
        s.get(Order, ids[0]).submitted_at = datetime(2026, 11, 9, tzinfo=timezone.utc)
    assert statuses == ["submitted", "filled"]
    assert broker.account().holdings["000001"].quantity == 50
    assert om.cancel_stale(date(2026, 11, 12), trading_days=3) == 1
    with db.session() as s:
        assert s.get(Order, ids[0]).status == "canceled"


def test_dry_run_never_submits(settings, db):
    om = OrderManager(settings, db, PaperBroker(settings, db, lambda t: 10_000.0), TradingCalendar())
    om.propose("r1", [PlannedOrder("000001", "BUY", 1, "LIMIT", 10_000, "목표")])
    with db.session() as s:
        for o in s.scalars(select(Order)):
            o.status = "approved"
    assert om.submit_approved(date(2026, 11, 12)) == {"submitted": 0, "skipped_dry_run": 1}


# ── 관리종목 플래그 (FDR 소속부) ─────────────────────────────────────

def test_dept_flags_and_index_approximation_excludes_managed():
    import pandas as pd

    from autopilot_korea.collectors.sources.market import dept_flags, top_by_market_cap

    assert dept_flags("관리종목(소속부없음)") == ["관리종목"]
    assert dept_flags("투자주의환기종목(소속부없음)") == ["투자주의환기종목"]
    assert dept_flags("우량기업부") == [] and dept_flags(None) == []
    listing = pd.DataFrame({"ticker": ["100010", "100020", "100030"], "name": ["가", "나", "다"], "market": ["KOSDAQ"] * 3,
                            "market_cap": [3e12, 2e12, 1e12], "dept": ["관리종목(소속부없음)", "우량기업부", "벤처기업부"]})
    assert top_by_market_cap(listing, "KOSDAQ", 2) == ["100020", "100030"]
