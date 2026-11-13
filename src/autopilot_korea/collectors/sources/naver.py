"""네이버 뉴스·종목토론방·외국인 보유율 (§4.1).

- 키가 있으면 네이버 검색 API(openapi.naver.com/v1/search/news.json, 헤더 X-Naver-Client-Id/Secret,
  query=회사명, sort=date)를 쓴다.
- 키가 없으면 종목 뉴스를 스크래핑한다. finance.naver.com/item/news_news.naver는 2026-11 실측 결과
  "이 페이지는 더 이상 제공되지 않습니다(Npay 증권)"로 막혀 있어, 같은 서비스의 모바일 API
  https://m.stock.naver.com/api/news/stock/{code}?pageSize=&page= 를 쓴다(로그인·키 불필요).
- 종목토론방도 finance.naver.com/item/board.naver가 302로 막혀 있어
  https://m.stock.naver.com/front-api/discussion/list?discussionType=domesticStock&itemCode=…&pageSize=…[&offset=]
  를 쓴다. 다음 페이지는 응답의 lastOffset을 offset으로 넘긴다.
- 외국인 보유율: https://m.stock.naver.com/api/stock/{code}/integration 의 foreignRate(외인소진율).

published_at은 모두 timezone-aware datetime이다.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import requests

KST = timezone(timedelta(hours=9))
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
SEARCH_URL = "https://openapi.naver.com/v1/search/news.json"
STOCK_NEWS_URL = "https://m.stock.naver.com/api/news/stock/{code}"
BOARD_URL = "https://m.stock.naver.com/front-api/discussion/list"
INTEGRATION_URL = "https://m.stock.naver.com/api/stock/{code}/integration"


def clean_text(text: str | None) -> str:
    """<b> 태그와 HTML 엔티티를 걷어낸다."""
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def search_news(query: str, client_id: str, client_secret: str, display: int = 50,
                session: requests.Session | None = None, timeout: float = 15.0) -> list[dict[str, Any]]:
    resp = (session or requests).get(
        SEARCH_URL, params={"query": query, "display": display, "start": 1, "sort": "date"},
        headers={"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}, timeout=timeout)
    resp.raise_for_status()
    out = []
    for item in resp.json().get("items", []):
        url = item.get("originallink") or item.get("link")
        out.append({
            "title": clean_text(item.get("title")),
            "source": urlparse(url).netloc if url else None,
            "url": url,
            "published_at": parsedate_to_datetime(item["pubDate"]),
            "kind": "news",
        })
    return out


def parse_stock_news(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """m.stock.naver.com 종목 뉴스 응답(클러스터 목록) → 뉴스 dict 목록."""
    out = []
    for cluster in payload or []:
        for it in cluster.get("items", []):
            url = it.get("mobileNewsUrl") or f"https://n.news.naver.com/mnews/article/{it.get('officeId')}/{it.get('articleId')}"
            out.append({
                "title": clean_text(it.get("titleFull") or it.get("title")),
                "source": it.get("officeName"),
                "url": url,
                "published_at": datetime.strptime(str(it["datetime"])[:12], "%Y%m%d%H%M").replace(tzinfo=KST),
                "kind": "news",
            })
    return out


def fetch_stock_news(ticker: str, pages: int = 1, page_size: int = 20, session: requests.Session | None = None,
                     timeout: float = 15.0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        resp = (session or requests).get(STOCK_NEWS_URL.format(code=ticker), params={"pageSize": page_size, "page": page},
                                         headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        items = parse_stock_news(resp.json())
        if not items:
            break
        out.extend(items)
    return out


def fetch_board_titles(ticker: str, pages: int = 1, page_size: int = 20, session: requests.Session | None = None,
                       timeout: float = 15.0) -> list[str]:
    """종목토론방 최근 글 제목 (감성 레이더 입력, 최신순)."""
    titles: list[str] = []
    offset = None
    for _ in range(pages):
        params: dict[str, Any] = {"discussionType": "domesticStock", "itemCode": ticker, "pageSize": page_size}
        if offset:
            params["offset"] = offset
        resp = (session or requests).get(BOARD_URL, params=params, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        result = resp.json().get("result") or {}
        posts = result.get("posts") or []
        titles.extend(clean_text(p.get("title")) for p in posts if p.get("title"))
        offset = result.get("lastOffset")
        if not posts or not offset:
            break
    return titles


def fetch_foreign_rate(ticker: str, session: requests.Session | None = None, timeout: float = 15.0) -> float | None:
    """외인소진율(소수). 외국인 한도가 없는 종목에서는 외국인 지분율과 같다."""
    resp = (session or requests).get(INTEGRATION_URL.format(code=ticker), headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    for info in resp.json().get("totalInfos", []):
        if info.get("code") == "foreignRate":
            m = re.search(r"-?[\d.]+", str(info.get("value", "")).replace(",", ""))
            return float(m.group()) / 100 if m else None
    return None
