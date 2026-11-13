"""실제 외부 원천 스모크 테스트. AUTOPILOT_LIVE_TESTS=1일 때만 실행한다.

    AUTOPILOT_LIVE_TESTS=1 pytest -m live tests/test_live_sources.py

DART·KRX(pykrx)·네이버 검색 API 테스트는 해당 키(DART_API_KEY, KRX_ID/KRX_PW, NAVER_CLIENT_ID/SECRET)가 있을 때만 돈다.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from autopilot_korea.collectors.sources import ecos, fnguide, naver
from autopilot_korea.collectors.sources.market import FdrSource, PykrxSource

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("AUTOPILOT_LIVE_TESTS") != "1", reason="AUTOPILOT_LIVE_TESTS=1일 때만 실행"),
]
TODAY = datetime.now(timezone(timedelta(hours=9))).date()


def test_fdr_prices_index_and_listing():
    src = FdrSource()
    df = src.ohlcv("005930", TODAY - timedelta(days=30), TODAY)
    assert len(df) > 10 and df["close"].notna().all() and df["shares"].notna().any()
    assert len(src.index_closes(TODAY - timedelta(days=30), TODAY)) > 10
    listing = src.listing(TODAY)
    assert listing["market_cap"].notna().mean() > 0.9 and listing["industry"].notna().mean() > 0.5
    k200, approx = src.index_constituents("KOSPI200", TODAY)
    kq150, _ = src.index_constituents("KOSDAQ150", TODAY)
    assert approx and len(k200) == 200 and len(kq150) == 150 and "005930" in k200


def test_fnguide_consensus():
    out = fnguide.fetch_consensus("005930", TODAY)
    assert out["covered"] and out["n_analysts"] and out["target_mean"] and out["forward_eps"]
    assert 1 <= out["rec_mean"] <= 5


def test_naver_news_board_and_foreign_rate():
    assert naver.fetch_stock_news("005930")
    assert naver.fetch_board_titles("005930", pages=1)
    rate = naver.fetch_foreign_rate("005930")
    assert rate is not None and 0 < rate < 1


@pytest.mark.skipif(not (os.environ.get("NAVER_CLIENT_ID") and os.environ.get("NAVER_CLIENT_SECRET")), reason="네이버 검색 API 키 없음")
def test_naver_search_api():
    assert naver.search_news("삼성전자", os.environ["NAVER_CLIENT_ID"], os.environ["NAVER_CLIENT_SECRET"], display=5)


def test_ecos_sample_key():
    rows = ecos.key_statistics(os.environ.get("ECOS_API_KEY") or "sample")
    assert len(rows) >= 10 and all(r["name"] and r["period"] for r in rows)


@pytest.mark.skipif(not os.environ.get("DART_API_KEY"), reason="DART_API_KEY 없음")
def test_dart_endpoints(tmp_path):
    from autopilot_korea.collectors.financials_collector import normalize_statement
    from autopilot_korea.collectors.sources.dart import DartClient

    client = DartClient(os.environ["DART_API_KEY"], cache_dir=tmp_path)
    corp = client.corp_codes()["005930"]
    assert client.company(corp)["acc_mt"] == "12"
    assert client.list_disclosures(TODAY - timedelta(days=3), TODAY, pblntf_ty="A") is not None
    items = normalize_statement(client.financial_statements(corp, TODAY.year - 1, "11011", "CFS"))
    assert items["revenue"] and items["net_income_controlling"] and items["total_assets"]


@pytest.mark.skipif(not (os.environ.get("KRX_ID") and os.environ.get("KRX_PW")), reason="KRX 로그인 정보 없음")
def test_pykrx_source():
    src = PykrxSource(os.environ["KRX_ID"], os.environ["KRX_PW"])
    df = src.ohlcv("005930", TODAY - timedelta(days=30), TODAY)
    assert len(df) > 10 and df["trading_value"].notna().all()
    k200, approx = src.index_constituents("KOSPI200", TODAY)
    assert not approx and len(k200) == 200
    assert src.short_selling("005930", TODAY - timedelta(days=30), TODAY) is not None
