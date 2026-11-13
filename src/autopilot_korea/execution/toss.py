"""토스증권 Open API 클라이언트·브로커 (설계안의 한국투자증권 모의투자 OpenAPI를 대체).

근거: 공식 문서 https://openapi.tossinvest.com/openapi-docs/overview.md 와
OpenAPI 명세 https://openapi.tossinvest.com/openapi-docs/latest/openapi.json (v1.2.28).

운영상 주의:
  - 모의투자(샌드박스) 환경이 없다. 이 클래스의 모든 주문은 실주문이다.
    그래서 시스템은 live 모드에서만 이 브로커로 주문을 보내고, 주문마다 사람 승인을 요구한다.
  - 클라이언트당 유효 토큰은 1개다. 새로 발급하면 이전 토큰이 폐기된다 → 프로세스당 토큰 관리자 하나.
  - 허용 IP 등록이 필요하다(미등록 IP는 403).
  - 성공 응답은 {"result": ...}, 숫자는 문자열 decimal이다.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests

from autopilot_korea.execution.base import AccountSnapshot, Holding, OrderRequest, OrderStatus

# API 그룹별 초당 한도 (overview.md "Rate Limits Group")
RATE_LIMITS = {"AUTH": 5, "ACCOUNT": 1, "ASSET": 5, "STOCK": 5, "STOCK_TRADING_TREND": 10, "MARKET_INFO": 3,
               "MARKET_DATA": 15, "ORDER": 10, "ORDER_HISTORY": 5, "ORDER_INFO": 3}  # ORDER_INFO는 피크(09:00~09:10) 한도 3을 적용

STATUS_MAP = {
    "PENDING": "submitted", "PENDING_CANCEL": "submitted", "PENDING_REPLACE": "submitted",
    "PARTIAL_FILLED": "partially_filled", "FILLED": "filled", "CANCELED": "canceled",
    "REJECTED": "rejected", "CANCEL_REJECTED": "submitted", "REPLACE_REJECTED": "submitted", "REPLACED": "canceled",
}

# /stocks/{symbol}/warnings 의 warningType → 시스템 표준 risk_flags
WARNING_FLAGS = {
    "LIQUIDATION_TRADING": "정리매매", "OVERHEATED": "투자주의", "INVESTMENT_WARNING": "투자경고",
    "INVESTMENT_RISK": "투자위험", "VI_STATIC_AND_DYNAMIC": "VI_발동", "VI_STATIC": "VI_발동", "VI_DYNAMIC": "VI_발동",
}

HIGH_VALUE_ORDER_KRW = 100_000_000  # 1억 원 이상 주문은 confirmHighValueOrder=true 필요


class TossApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str = "", request_id: str | None = None):
        super().__init__(f"[{status} {code}] {message} (requestId={request_id})")
        self.status, self.code, self.request_id = status, code, request_id


class TossClient:
    def __init__(self, base_url: str, client_id: str, client_secret: str, *, account_no: str | None = None,
                 session: requests.Session | None = None, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.client_id, self.client_secret = client_id, client_secret
        self.account_no = account_no
        self.http = session or requests.Session()
        self.timeout = timeout
        self._token: str | None = None
        self._token_expires = 0.0
        self._account_seq: int | None = None
        self._lock = threading.Lock()
        self._last_call: dict[str, float] = {}

    # ── 인증·호출 공통 ──────────────────────────────────────────────────

    def _throttle(self, group: str) -> None:
        interval = 1.0 / RATE_LIMITS.get(group, 5)
        with self._lock:
            wait = self._last_call.get(group, 0.0) + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call[group] = time.monotonic()

    def _token_header(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or time.time() > self._token_expires - 60:
                resp = self.http.post(f"{self.base_url}/oauth2/token", timeout=self.timeout, data={
                    "grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret})
                if resp.status_code != 200:
                    body = _json(resp)
                    raise TossApiError(resp.status_code, body.get("error", "token-error"), body.get("error_description", ""))
                body = resp.json()
                self._token = body["access_token"]
                self._token_expires = time.time() + float(body.get("expires_in", 3600))
            return f"Bearer {self._token}"

    def request(self, method: str, path: str, group: str, *, params: dict | None = None,
                json_body: dict | None = None, account: bool = False) -> Any:
        refreshed = force_refresh = False
        for attempt in range(4):
            self._throttle(group)
            headers = {"Authorization": self._token_header(force=force_refresh)}
            force_refresh = False
            if account:
                headers["X-Tossinvest-Account"] = str(self.account_seq())
            resp = self.http.request(method, f"{self.base_url}{path}", params=params, json=json_body,
                                     headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                return resp.json().get("result")
            err = _json(resp).get("error", {})
            code = err.get("code", "unknown")
            if resp.status_code == 401 and code in ("expired-token", "token-revoked", "invalid-token") and not refreshed:
                refreshed = force_refresh = True  # 토큰 재발급은 한 번만 — 재발급은 이전 토큰을 폐기한다
                continue
            if resp.status_code == 429 and attempt < 3:
                time.sleep(float(resp.headers.get("Retry-After", "1")))
                continue
            if resp.status_code >= 500 and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise TossApiError(resp.status_code, code, err.get("message", ""), err.get("requestId"))
        raise TossApiError(429, "rate-limit-exceeded", "재시도 한도 초과")

    def account_seq(self) -> int:
        if self._account_seq is None:
            accounts = self.request("GET", "/api/v1/accounts", "ACCOUNT")
            brokerage = [a for a in accounts if a.get("accountType") == "BROKERAGE"]
            if self.account_no:
                brokerage = [a for a in brokerage if a.get("accountNo") == self.account_no] or brokerage
            if not brokerage:
                raise TossApiError(404, "account-not-found", "종합매매(BROKERAGE) 계좌가 없습니다")
            self._account_seq = int(brokerage[0]["accountSeq"])
        return self._account_seq

    # ── 시세 ────────────────────────────────────────────────────────────

    def orderbook(self, symbol: str) -> dict:
        return self.request("GET", "/api/v1/orderbook", "MARKET_DATA", params={"symbol": symbol})

    def prices(self, symbols: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for i in range(0, len(symbols), 200):
            for row in self.request("GET", "/api/v1/prices", "MARKET_DATA", params={"symbols": ",".join(symbols[i:i + 200])}):
                out[row["symbol"]] = float(row["lastPrice"])
        return out

    def price_limits(self, symbol: str) -> tuple[float | None, float | None]:
        r = self.request("GET", "/api/v1/price-limits", "MARKET_DATA", params={"symbol": symbol})
        return _num(r.get("lowerLimitPrice")), _num(r.get("upperLimitPrice"))

    def stocks(self, symbols: list[str]) -> list[dict]:
        return self.request("GET", "/api/v1/stocks", "STOCK", params={"symbols": ",".join(symbols)})

    def warnings(self, symbol: str) -> list[dict]:
        return self.request("GET", f"/api/v1/stocks/{symbol}/warnings", "STOCK") or []

    def short_selling(self, symbol: str, count: int = 30, until: str | None = None) -> list[dict]:
        params = {"count": count} | ({"until": until} if until else {})
        return (self.request("GET", f"/api/v1/stocks/{symbol}/short-selling", "STOCK_TRADING_TREND", params=params) or {}).get("records", [])

    def market_calendar(self, date: str | None = None) -> dict:
        return self.request("GET", "/api/v1/market-calendar/KR", "MARKET_INFO", params={"date": date} if date else None)

    # ── 계좌·주문 ────────────────────────────────────────────────────────

    def holdings(self) -> dict:
        return self.request("GET", "/api/v1/holdings", "ASSET", account=True)

    def buying_power(self) -> float:
        r = self.request("GET", "/api/v1/buying-power", "ORDER_INFO", params={"currency": "KRW"}, account=True)
        return float(r["cashBuyingPower"])

    def create_order(self, symbol: str, side: str, order_type: str, quantity: int, price: float | None,
                     client_order_id: str) -> str:
        body: dict[str, Any] = {"symbol": symbol, "side": side, "orderType": order_type, "quantity": str(int(quantity)),
                                "timeInForce": "DAY", "clientOrderId": client_order_id[:36]}
        if order_type == "LIMIT":
            body["price"] = str(int(price))  # 국내 주문가는 호가단위에 맞춘 정수 원
        notional = quantity * (price or 0)
        if notional >= HIGH_VALUE_ORDER_KRW:
            body["confirmHighValueOrder"] = True
        return self.request("POST", "/api/v1/orders", "ORDER", json_body=body, account=True)["orderId"]

    def get_order(self, order_id: str) -> dict:
        return self.request("GET", f"/api/v1/orders/{order_id}", "ORDER_HISTORY", account=True)

    def cancel_order(self, order_id: str) -> str:
        """취소도 새 orderId를 발급한다."""
        return self.request("POST", f"/api/v1/orders/{order_id}/cancel", "ORDER", json_body={}, account=True)["orderId"]


class TossBroker:
    """Broker 인터페이스 구현. live 모드 전용."""

    name = "toss"

    def __init__(self, client: TossClient):
        self.client = client

    def account(self) -> AccountSnapshot:
        h = self.client.holdings()
        holdings = {}
        for it in h.get("items", []):
            if it.get("marketCountry") != "KR":
                continue
            holdings[it["symbol"]] = Holding(ticker=it["symbol"], name=it.get("name", ""), quantity=int(float(it["quantity"])),
                                             avg_price=float(it["averagePurchasePrice"]), last_price=float(it["lastPrice"]))
        return AccountSnapshot(cash=self.client.buying_power(), holdings=holdings)

    def submit(self, req: OrderRequest) -> str:
        return self.client.create_order(req.ticker, req.side, req.order_type, req.quantity, req.price, req.client_order_id)

    def order_status(self, broker_order_id: str) -> OrderStatus:
        o = self.client.get_order(broker_order_id)
        ex = o.get("execution") or {}
        return OrderStatus(broker_order_id=broker_order_id, status=STATUS_MAP.get(o.get("status", ""), "submitted"),
                           filled_quantity=int(float(ex.get("filledQuantity") or 0)),
                           avg_fill_price=_num(ex.get("averageFilledPrice")))

    def cancel(self, broker_order_id: str) -> str:
        return self.client.cancel_order(broker_order_id)

    def best_quote(self, ticker: str) -> dict | None:
        """collectors.base.QuoteProvider — 97개 변수 #21~#24 (매수·매도 호가와 잔량)."""
        ob = self.client.orderbook(ticker)
        if not ob or not ob.get("asks") or not ob.get("bids"):
            return None
        ask, bid = ob["asks"][0], ob["bids"][0]  # asks: 낮은 가격순, bids: 높은 가격순
        return {"bid": float(bid["price"]), "ask": float(ask["price"]),
                "bid_size": float(bid["volume"]), "ask_size": float(ask["volume"])}

    def regulatory_flags(self, ticker: str) -> list[str]:
        """KOREA_REGULATION 게이트 입력: 투자경고·위험·정리매매·VI·거래정지."""
        flags = {WARNING_FLAGS[w["warningType"]] for w in self.client.warnings(ticker) if w.get("warningType") in WARNING_FLAGS}
        info = (self.client.stocks([ticker]) or [{}])[0]
        detail = info.get("koreanMarketDetail") or {}
        if detail.get("krxTradingSuspended"):
            flags.add("거래정지")
        if detail.get("liquidationTrading"):
            flags.add("정리매매")
        return sorted(flags)


def _num(value: Any) -> float | None:
    return None if value in (None, "") else float(value)


def _json(resp: requests.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}
