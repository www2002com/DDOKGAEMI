"""KRX 시세 원천 (§4.1).

- PykrxSource: KRX 정보데이터시스템(pykrx 1.2.9+). KRX_ID/KRX_PW 로그인이 있어야 데이터가 나온다.
  지수 구성종목·공매도·외국인 보유율을 정확히 준다.
- FdrSource: FinanceDataReader. 로그인 없이 동작하는 대체 원천.
  · 일별 OHLCV는 주지만 거래대금·시가총액·상장주식수는 없다 → 상장주식수는 현재 상장 목록 값으로 채우고
    시가총액은 종가 × 상장주식수로 근사한다(과거 시점의 주식수 변화는 반영하지 못한다).
  · KRX 지수 구성종목 조회(MDCSTAT00601)는 KRX 로그인이 필요해져 FDR로는 받을 수 없다.
    대신 KOSPI 시총 상위 200 / KOSDAQ 시총 상위 150(보통주, 스팩 제외)을 근사치로 쓰고 approximate=True로 표시한다.
  · 공매도 통계는 제공하지 않는다 → None (팩트시트 신선도 "missing").
  · 외국인 보유율은 네이버 증권 종목 통합 API(m.stock.naver.com)의 외인소진율로 대체한다.

모든 메서드는 날짜를 datetime.date로 받고, DataFrame 인덱스는 date 객체다.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any, Protocol

import pandas as pd

KOSPI200 = "KOSPI200"
KOSDAQ150 = "KOSDAQ150"
PYKRX_INDEX_CODES = {KOSPI200: "1028", KOSDAQ150: "2203"}
APPROX_TOP_N = {KOSPI200: ("STK", 200), KOSDAQ150: ("KSQ", 150)}
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume", "trading_value", "market_cap", "shares"]
LISTING_COLUMNS = ["ticker", "name", "market", "industry", "settle_month", "market_cap", "shares", "close"]
FDR_CACHE_URL = "https://raw.githubusercontent.com/FinanceData/fdr_krx_data_cache/refs/heads/master/data/listing/krx/{d}.csv"


class MarketDataSource(Protocol):
    name: str

    def ohlcv(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        """일별 시세. 컬럼 OHLCV_COLUMNS (없는 값은 NaN), 인덱스 date."""

    def index_closes(self, start: date, end: date) -> pd.Series:
        """KOSPI200 지수 일별 종가."""

    def index_constituents(self, index: str, as_of: date) -> tuple[list[str], bool]:
        """(구성종목 티커, 근사 여부). index는 "KOSPI200" | "KOSDAQ150"."""

    def short_selling(self, ticker: str, start: date, end: date) -> pd.DataFrame | None:
        """공매도 일별 통계 (short_volume, short_balance, short_ratio[%]). 원천이 지원하지 않으면 None."""

    def listing(self, as_of: date) -> pd.DataFrame:
        """상장 종목 목록. 컬럼 LISTING_COLUMNS."""

    def foreign_ownership(self, ticker: str, as_of: date) -> float | None:
        """외국인 보유 비율(소수)."""


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _settle_month(text: Any) -> int | None:
    """FDR KRX-DESC의 결산월("12월")을 정수로."""
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())
    return int(digits) if digits and 1 <= int(digits) <= 12 else None


def _market_name(market_id: str) -> str:
    return {"STK": "KOSPI", "KSQ": "KOSDAQ", "KNX": "KONEX"}.get(str(market_id), str(market_id))


def _to_date_index(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.index = [pd.Timestamp(i).date() for i in df.index]
    return df.sort_index()


def top_by_market_cap(listing: pd.DataFrame, market: str, n: int) -> list[str]:
    """시총 상위 n개 보통주(티커 끝자리 0, 스팩 제외). 지수 구성종목 근사용."""
    df = listing[(listing["market"] == market) & listing["ticker"].str.endswith("0")]
    df = df[~df["name"].astype(str).str.contains("스팩")]
    return df.sort_values("market_cap", ascending=False)["ticker"].head(n).tolist()


class FdrSource:
    """FinanceDataReader 기반 대체 원천 (로그인 불필요)."""

    name = "fdr"

    def __init__(self, fdr: Any = None):
        self._fdr = fdr
        self._listing: pd.DataFrame | None = None
        self._listing_failed = False

    @property
    def fdr(self) -> Any:
        if self._fdr is None:
            import FinanceDataReader

            self._fdr = FinanceDataReader
        return self._fdr

    def _shares(self, ticker: str, as_of: date) -> float | None:
        if self._listing_failed:
            return None
        try:
            row = self.listing(as_of).set_index("ticker")["shares"]
        except Exception:  # noqa: BLE001 — 상장주식수는 보조 정보다. 실패를 기억해 종목마다 재시도하지 않는다.
            self._listing_failed = True
            return None
        value = row.get(ticker)
        return float(value) if value is not None and pd.notna(value) else None

    def ohlcv(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        raw = self.fdr.DataReader(ticker, start.isoformat(), end.isoformat())
        if raw is None or raw.empty:
            return pd.DataFrame(columns=OHLCV_COLUMNS)
        df = pd.DataFrame({
            "open": raw["Open"], "high": raw["High"], "low": raw["Low"], "close": raw["Close"], "volume": raw["Volume"],
        }).astype(float)
        shares = self._shares(ticker, end)
        df["trading_value"] = float("nan")  # FDR 개별 종목 시세에는 거래대금이 없다
        df["shares"] = shares if shares else float("nan")
        df["market_cap"] = df["close"] * df["shares"]  # 근사: 현재 상장주식수 기준
        return _to_date_index(df[OHLCV_COLUMNS])

    def index_closes(self, start: date, end: date) -> pd.Series:
        raw = self.fdr.DataReader("KS200", start.isoformat(), end.isoformat())
        return _to_date_index(raw[["Close"]].astype(float))["Close"]

    def index_constituents(self, index: str, as_of: date) -> tuple[list[str], bool]:
        market_id, n = APPROX_TOP_N[index]
        return top_by_market_cap(self.listing(as_of), _market_name(market_id), n), True

    def short_selling(self, ticker: str, start: date, end: date) -> pd.DataFrame | None:
        return None

    def foreign_ownership(self, ticker: str, as_of: date) -> float | None:
        from autopilot_korea.collectors.sources.naver import fetch_foreign_rate

        return fetch_foreign_rate(ticker)

    def _marcap_listing(self, as_of: date) -> pd.DataFrame:
        """시총이 채워진 KRX 상장 목록. 장중에는 FDR 캐시의 당일 파일이 비어 있어 직전 영업일 파일로 내려간다."""
        df = self.fdr.StockListing("KRX")
        if df is not None and len(df) and df["Marcap"].notna().mean() > 0.5:
            return df
        for back in range(1, 8):
            d = as_of - timedelta(days=back)
            try:
                df = pd.read_csv(FDR_CACHE_URL.format(d=d.isoformat()), index_col=0,
                                 dtype={"Code": str, "Dept": str, "ChangeCode": str, "MarketId": str})
            except Exception:  # noqa: BLE001 — 휴장일 파일은 없다
                continue
            if len(df) and df["Marcap"].notna().mean() > 0.5:
                return df
        raise RuntimeError("시가총액이 있는 KRX 상장 목록을 찾지 못했습니다")

    def listing(self, as_of: date) -> pd.DataFrame:
        if self._listing is not None:
            return self._listing
        cap = self._marcap_listing(as_of)
        df = pd.DataFrame({
            "ticker": cap["Code"].astype(str), "name": cap["Name"].astype(str),
            "market": cap["MarketId"].map(_market_name),
            "market_cap": pd.to_numeric(cap["Marcap"], errors="coerce"),
            "shares": pd.to_numeric(cap["Stocks"], errors="coerce"),
            "close": pd.to_numeric(cap["Close"], errors="coerce"),
        })
        try:
            desc = self.fdr.StockListing("KRX-DESC")
            info = pd.DataFrame({"ticker": desc["Code"].astype(str), "industry": desc["Industry"],
                                 "settle_month": desc["SettleMonth"].map(_settle_month)})
            df = df.merge(info, on="ticker", how="left")
        except Exception:  # noqa: BLE001 — 업종·결산월은 없어도 목록은 쓸 수 있다
            df["industry"], df["settle_month"] = None, None
        self._listing = df[LISTING_COLUMNS].reset_index(drop=True)
        return self._listing


class PykrxSource(FdrSource):
    """pykrx(KRX 로그인) 원천. 상장 목록·업종은 로그인 없이 되는 FDR 것을 그대로 쓴다."""

    name = "pykrx"

    def __init__(self, krx_id: str, krx_pw: str, stock: Any = None, fdr: Any = None):
        super().__init__(fdr)
        self._stock = stock
        self._credentials = (krx_id, krx_pw)

    @property
    def stock(self) -> Any:
        if self._stock is None:
            # pykrx는 import 시점에 환경 변수로 로그인한다.
            os.environ.setdefault("KRX_ID", self._credentials[0])
            os.environ.setdefault("KRX_PW", self._credentials[1])
            from pykrx import stock

            self._stock = stock
        return self._stock

    def ohlcv(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        px = self.stock.get_market_ohlcv_by_date(_ymd(start), _ymd(end), ticker)
        if px is None or px.empty:
            return pd.DataFrame(columns=OHLCV_COLUMNS)
        df = pd.DataFrame({"open": px["시가"], "high": px["고가"], "low": px["저가"], "close": px["종가"],
                           "volume": px["거래량"], "trading_value": px["거래대금"]}).astype(float)
        cap = self.stock.get_market_cap_by_date(_ymd(start), _ymd(end), ticker)
        if cap is not None and not cap.empty:
            df["market_cap"] = cap["시가총액"].astype(float)
            df["shares"] = cap["상장주식수"].astype(float)
        else:
            df["market_cap"] = df["shares"] = float("nan")
        return _to_date_index(df[OHLCV_COLUMNS])

    def index_closes(self, start: date, end: date) -> pd.Series:
        df = self.stock.get_index_ohlcv_by_date(_ymd(start), _ymd(end), PYKRX_INDEX_CODES[KOSPI200])
        return _to_date_index(df[["종가"]].astype(float))["종가"].rename("Close")

    def index_constituents(self, index: str, as_of: date) -> tuple[list[str], bool]:
        tickers = self.stock.get_index_portfolio_deposit_file(PYKRX_INDEX_CODES[index], _ymd(as_of), alternative=True)
        if tickers:
            return [str(t) for t in tickers], False
        return super().index_constituents(index, as_of)

    def short_selling(self, ticker: str, start: date, end: date) -> pd.DataFrame | None:
        vol = self.stock.get_shorting_volume_by_date(_ymd(start), _ymd(end), ticker)
        bal = self.stock.get_shorting_balance_by_date(_ymd(start), _ymd(end), ticker)
        parts = []
        if vol is not None and not vol.empty:
            parts.append(pd.DataFrame({"short_volume": vol["공매도"].astype(float)}))
        if bal is not None and not bal.empty:
            parts.append(pd.DataFrame({"short_balance": bal["공매도잔고"].astype(float), "short_ratio": bal["비중"].astype(float)}))
        if not parts:
            return pd.DataFrame(columns=["short_volume", "short_balance", "short_ratio"])
        df = pd.concat(parts, axis=1)
        for col in ("short_volume", "short_balance", "short_ratio"):
            if col not in df:
                df[col] = float("nan")
        return _to_date_index(df[["short_volume", "short_balance", "short_ratio"]])

    def foreign_ownership(self, ticker: str, as_of: date) -> float | None:
        df = self.stock.get_exhaustion_rates_of_foreign_investment_by_date(_ymd(as_of - timedelta(days=10)), _ymd(as_of), ticker)
        if df is None or df.empty:
            return None
        return float(df["지분율"].iloc[-1]) / 100


def get_market_source(settings: Any) -> MarketDataSource:
    """KRX 로그인이 있으면 pykrx, 없으면 FinanceDataReader."""
    if getattr(settings, "krx_id", None) and getattr(settings, "krx_pw", None):
        return PykrxSource(settings.krx_id, settings.krx_pw)
    return FdrSource()
