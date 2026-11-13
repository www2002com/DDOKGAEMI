"""FnGuide 컨센서스 (§4.1, 카테고리 I).

comp.fnguide.com의 구 주소(SVO2/ASP/SVD_Consensus.asp?gicode=…)는 이제 404로 리다이렉트된다.
2026-11 실측 기준 동작하는 주소는 https://wcomp.fnguide.com/CompanyInfo/Consensus?cmp_cd=005930 이며
(gicode 파라미터는 무시되고 cmp_cd로 종목을 고른다), 페이지에 다음이 서버 렌더링된다.
- "증권사별 적정주가 & 투자의견" 표: Consensus 행(평균 적정주가·평균 투자의견) + 증권사별 행(추정일자, 적정주가,
  직전 적정주가, 증감율, 투자의견 점수, 직전 투자의견). 커버리지가 없으면 "관련 데이터가 없습니다." 한 줄뿐이다.
- consensus.init({... perforTrend: {...}}) 스크립트: 연도별 실적·추정치(EP_CHK == "E"가 추정 연도)의 EPS 행.

투자의견 변환: FnGuide 점수는 5=Strong Buy, 4=Buy, 3=Hold, 2=Underperform(비중축소), 1=Sell 이다.
레지스트리 #70은 1=강력매수 ~ 5=매도 척도이므로 rec_mean = 6 − FnGuide 점수 로 뒤집는다.
#71 등급: rec_mean < 1.5 strong_buy, < 2.5 buy, < 3.5 hold, < 4.5 underperform, 그 외 sell.

예상 EPS(#56): 추정 연도 EPS를 12개월 선행으로 가중한다(FnGuide "PER(Fwd. 12M)"과 같은 방식).
기준일 이후 처음 끝나는 추정 회계연도 FY0의 남은 개월 수를 m이라 하면 EPS = m/12·EPS(FY0) + (1 − m/12)·EPS(FY1).
FY1 추정치가 없으면 EPS(FY0)를 그대로 쓴다.
"""

from __future__ import annotations

import calendar
import json
import re
import statistics
from datetime import date
from typing import Any

import requests

URL = "https://wcomp.fnguide.com/CompanyInfo/Consensus"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _num(text: Any) -> float | None:
    s = str(text or "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def rec_key(rec_mean: float | None) -> str | None:
    if rec_mean is None:
        return None
    for limit, key in ((1.5, "strong_buy"), (2.5, "buy"), (3.5, "hold"), (4.5, "underperform")):
        if rec_mean < limit:
            return key
    return "sell"


def _broker_rows(soup: Any) -> tuple[list[float | None], list[dict[str, Any]]]:
    """(Consensus 행 [평균 적정주가, 평균 투자의견], 증권사별 행 목록)."""
    for table in soup.find_all("table"):
        caption = table.find("caption")
        if not caption or "증권사별" not in caption.get_text():
            continue
        consensus: list[float | None] = [None, None]
        brokers: list[dict[str, Any]] = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if len(cells) < 6:
                continue
            if cells[0] == "Consensus":
                consensus = [_num(cells[2]), _num(cells[5])]
            elif re.match(r"\d{4}/\d{2}/\d{2}", cells[1]):
                brokers.append({"broker": cells[0], "date": cells[1].replace("/", "-"),
                                "target": _num(cells[2]), "score": _num(cells[5])})
        return consensus, brokers
    return [None, None], []


def _eps_estimates(html: str) -> list[tuple[date, float]]:
    """consensus.init의 perforTrend에서 추정 연도(EP_CHK == "E")의 (회계연도 말일, EPS)."""
    m = re.search(r"perforTrend:\s*(\{.*?\})\s*,\s*\n", html)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    eps_row = next((r for r in data.get("data", []) if str(r.get("NAME", "")).strip() == "EPS"), None)
    if not eps_row:
        return []
    out = []
    for h in data.get("header", []):
        if str(h.get("EP_CHK", "")).strip() != "E":
            continue
        value = _num(eps_row.get(h.get("CD")))
        ym = re.match(r"(\d{4})/(\d{2})", str(h.get("YYMM", "")))
        if value is not None and ym:
            y, mth = int(ym.group(1)), int(ym.group(2))
            out.append((date(y, mth, calendar.monthrange(y, mth)[1]), value))
    return sorted(out)


def forward_12m_eps(estimates: list[tuple[date, float]], as_of: date) -> float | None:
    """12개월 선행 EPS (모듈 docstring 참조)."""
    future = [(d, v) for d, v in sorted(estimates) if d >= as_of]
    if not future:
        return None
    fy0_end, eps0 = future[0]
    if len(future) == 1:
        return eps0
    months_left = (fy0_end.year - as_of.year) * 12 + fy0_end.month - as_of.month + (fy0_end.day - as_of.day) / 30.0
    w = min(max(months_left / 12.0, 0.0), 1.0)
    return w * eps0 + (1 - w) * future[1][1]


def parse_consensus_html(html: str, as_of: date) -> dict[str, Any]:
    """FnGuide 컨센서스 페이지 → ConsensusData 필드."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    (mean_target, mean_score), brokers = _broker_rows(soup)
    targets = [b["target"] for b in brokers if b["target"]]
    scores = [b["score"] for b in brokers if b["score"]]
    score = mean_score if mean_score is not None else (sum(scores) / len(scores) if scores else None)
    rec_mean = round(6 - score, 2) if score is not None else None
    estimates = _eps_estimates(html)
    fwd = forward_12m_eps(estimates, as_of)
    covered = bool(brokers) or fwd is not None
    return {
        "covered": covered,
        "target_mean": mean_target if mean_target is not None else (sum(targets) / len(targets) if targets else None),
        "target_high": max(targets) if targets else None,
        "target_low": min(targets) if targets else None,
        "target_median": statistics.median(targets) if targets else None,
        "rec_mean": rec_mean,
        "rec_key": rec_key(rec_mean),
        "n_analysts": len(brokers) or None,
        "forward_eps": fwd,
        "eps_estimates": [(d.isoformat(), v) for d, v in estimates],
        "brokers": brokers,
    }


def fetch_consensus(ticker: str, as_of: date, session: requests.Session | None = None, timeout: float = 30.0) -> dict[str, Any]:
    resp = (session or requests).get(URL, params={"cmp_cd": ticker}, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    soup_cmp = re.search(r'id="cmp_cd"\s+value="(\w+)"', resp.text)
    if soup_cmp and soup_cmp.group(1) != ticker:
        raise RuntimeError(f"FnGuide가 다른 종목({soup_cmp.group(1)})을 돌려줬습니다: {ticker}")
    return parse_consensus_html(resp.text, as_of)
