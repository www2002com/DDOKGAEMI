"""OpenDART REST 클라이언트 (§4.1).

엔드포인트 이름·요청 인자·응답 필드는 opendart.fss.or.kr 개발가이드(guide/detail.do)에서 확인했다.
- 공시정보: list.json(공시검색), company.json(기업개황, acc_mt 결산월), document.xml(공시서류원본 zip),
  corpCode.xml(고유번호 zip)
- 정기보고서 주요정보: alotMatter(배당), hyslrSttus(최대주주), stockTotqySttus(주식의 총수),
  accnutAdtorNmNdAdtOpinion(감사인·감사의견), outcmpnyDrctrNdChangeSttus(사외이사),
  hmvAuditAllSttus(이사·감사 전체 보수지급금액)
- 정기보고서 재무정보: fnlttSinglAcntAll(단일회사 전체 재무제표)
- 지분공시: majorstock(대량보유 상황보고)

상태 코드: "000" 정상, "013" 데이터 없음(→ 빈 결과), "020" 요청 한도 초과(→ DartRateLimitError),
"010"/"011"/"012"/"901" 키·IP 문제(→ DartKeyError), 그 밖의 코드 → DartError.
"""

from __future__ import annotations

import io
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

import requests

from autopilot_korea.settings import PROJECT_ROOT

BASE_URL = "https://opendart.fss.or.kr/api"
REPORT_CODES = {1: "11013", 2: "11012", 3: "11014", 4: "11011"}  # 1분기·반기·3분기·사업보고서
QUARTER_OF_REPORT = {v: k for k, v in REPORT_CODES.items()}
KEY_ERRORS = {"010", "011", "012", "901"}
CORPCODE_MAX_AGE_SECONDS = 7 * 24 * 3600


class DartError(RuntimeError):
    def __init__(self, status: str, message: str):
        super().__init__(f"DART {status}: {message}")
        self.status = status
        self.message = message


class DartKeyError(DartError):
    """인증키·IP 문제. 재시도해도 소용없다."""


class DartRateLimitError(DartError):
    """요청 제한 초과(020)."""


def filed_date_from_rcept_no(rcept_no: str | None) -> date | None:
    """접수번호 14자리의 앞 8자리가 접수일(YYYYMMDD)이다."""
    try:
        return datetime.strptime(str(rcept_no)[:8], "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def parse_dart_date(text: str | None) -> date | None:
    """"20261029" 또는 "2026-10-29" 형식."""
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())[:8]
    try:
        return datetime.strptime(digits, "%Y%m%d").date()
    except ValueError:
        return None


def parse_amount(text: Any) -> float | None:
    """"1,234", "-5", "-", "" → float | None."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).replace(",", "").strip()
    if s in ("", "-", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _ymd(value: str | date) -> str:
    return value.strftime("%Y%m%d") if isinstance(value, date) else str(value).replace("-", "")


def _check_status(status: str, message: str) -> None:
    if status in ("000", "013"):
        return
    if status == "020":
        raise DartRateLimitError(status, message)
    if status in KEY_ERRORS:
        raise DartKeyError(status, message)
    raise DartError(status, message)


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


class DartClient:
    def __init__(self, api_key: str, cache_dir: Path | None = None, session: requests.Session | None = None,
                 timeout: float = 30.0, min_interval: float = 0.1):
        self.api_key = api_key
        self.cache_dir = Path(cache_dir or PROJECT_ROOT / "data" / "cache")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.min_interval = min_interval
        self._last_call = 0.0
        self._corp_codes: dict[str, str] | None = None

    # ── 저수준 호출 ────────────────────────────────────────────────────

    def _get(self, endpoint: str, params: dict[str, Any]) -> requests.Response:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()
        resp = self.session.get(f"{BASE_URL}/{endpoint}", params={"crtfc_key": self.api_key, **params}, timeout=self.timeout)
        resp.raise_for_status()
        return resp

    def get_json(self, endpoint: str, **params: Any) -> dict[str, Any]:
        """JSON 엔드포인트 호출. "013"(데이터 없음)이면 빈 list를 담은 dict를 돌려준다."""
        data = self._get(endpoint, {k: v for k, v in params.items() if v is not None}).json()
        status = str(data.get("status", ""))
        _check_status(status, str(data.get("message", "")))
        if status == "013":
            return {"status": status, "list": []}
        return data

    def get_list(self, endpoint: str, **params: Any) -> list[dict[str, Any]]:
        return list(self.get_json(endpoint, **params).get("list") or [])

    def _get_zip(self, endpoint: str, params: dict[str, Any]) -> bytes:
        raw = self._get(endpoint, params).content
        if not raw.startswith(b"PK"):
            # 오류는 zip 대신 XML(<result><status>..)로 온다.
            root = ET.fromstring(raw)
            _check_status(root.findtext("status") or "", root.findtext("message") or "")
            raise DartError(root.findtext("status") or "?", root.findtext("message") or "zip이 아닌 응답")
        return raw

    # ── 공시정보 ───────────────────────────────────────────────────────

    def corp_codes(self, refresh: bool = False) -> dict[str, str]:
        """종목코드(6자리) → DART 고유번호(8자리). corpCode.xml zip을 data/cache/에 7일간 캐시한다."""
        if self._corp_codes is not None and not refresh:
            return self._corp_codes
        path = self.cache_dir / "dart_corpcode.zip"
        fresh = path.exists() and time.time() - path.stat().st_mtime < CORPCODE_MAX_AGE_SECONDS
        if refresh or not fresh:
            raw = self._get_zip("corpCode.xml", {})
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        with zipfile.ZipFile(path) as zf:
            root = ET.fromstring(zf.read(zf.namelist()[0]))
        codes: dict[str, str] = {}
        for item in root.iter("list"):
            stock = (item.findtext("stock_code") or "").strip()
            if stock:
                codes[stock] = (item.findtext("corp_code") or "").strip()
        self._corp_codes = codes
        return codes

    def company(self, corp_code: str) -> dict[str, Any]:
        """기업개황 (acc_mt = 결산월 MM)."""
        return self.get_json("company.json", corp_code=corp_code)

    def list_disclosures(self, bgn_de: str | date, end_de: str | date, pblntf_ty: str | None = None,
                         corp_code: str | None = None) -> list[dict[str, Any]]:
        """공시검색. 모든 페이지를 모아 rcept_no, corp_code, stock_code, corp_name, report_nm, rcept_dt 등을 담은 dict 목록을 돌려준다.

        고유번호 없이 검색하면 DART가 기간을 3개월로 제한한다.
        """
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.get_json("list.json", bgn_de=_ymd(bgn_de), end_de=_ymd(end_de), pblntf_ty=pblntf_ty,
                                 corp_code=corp_code, page_no=page, page_count=100)
            for item in data.get("list") or []:
                item = dict(item)
                item["report_nm"] = " ".join(str(item.get("report_nm", "")).split())
                item["stock_code"] = str(item.get("stock_code") or "").strip()
                items.append(item)
            if page >= int(data.get("total_page") or 1):
                return items
            page += 1

    def document(self, rcept_no: str) -> str:
        """공시서류 원본(zip 안 XML/HTML 파일들)을 하나의 문자열로 이어 붙인다."""
        with zipfile.ZipFile(io.BytesIO(self._get_zip("document.xml", {"rcept_no": rcept_no}))) as zf:
            return "\n".join(_decode(zf.read(name)) for name in zf.namelist())

    # ── 정기보고서 (bsns_year, reprt_code) ────────────────────────────────

    def financial_statements(self, corp_code: str, bsns_year: int, reprt_code: str, fs_div: str = "CFS") -> list[dict]:
        """단일회사 전체 재무제표. 분·반기 손익은 thstrm_amount가 3개월, thstrm_add_amount가 누적이다."""
        return self.get_list("fnlttSinglAcntAll.json", corp_code=corp_code, bsns_year=str(bsns_year),
                             reprt_code=reprt_code, fs_div=fs_div)

    def _report(self, endpoint: str, corp_code: str, bsns_year: int, reprt_code: str) -> list[dict]:
        return self.get_list(endpoint, corp_code=corp_code, bsns_year=str(bsns_year), reprt_code=reprt_code)

    def dividends(self, corp_code: str, bsns_year: int, reprt_code: str = "11011") -> list[dict]:
        """배당에 관한 사항 (se: 주당 현금배당금(원)·현금배당수익률(%)·(연결)현금배당성향(%) …; thstrm/frmtrm/lwfr)."""
        return self._report("alotMatter.json", corp_code, bsns_year, reprt_code)

    def largest_holders(self, corp_code: str, bsns_year: int, reprt_code: str) -> list[dict]:
        """최대주주 현황 (nm, relate, stock_knd, trmend_posesn_stock_co, trmend_posesn_stock_qota_rt)."""
        return self._report("hyslrSttus.json", corp_code, bsns_year, reprt_code)

    def stock_totals(self, corp_code: str, bsns_year: int, reprt_code: str) -> list[dict]:
        """주식의 총수 현황 (se: 보통주·우선주·합계, istc_totqy 발행주식총수, tesstk_co 자기주식수)."""
        return self._report("stockTotqySttus.json", corp_code, bsns_year, reprt_code)

    def audit_opinions(self, corp_code: str, bsns_year: int, reprt_code: str = "11011") -> list[dict]:
        """회계감사인의 명칭 및 감사의견 (bsns_year: 당기·전기·전전기, adt_opinion, emphs_matter)."""
        return self._report("accnutAdtorNmNdAdtOpinion.json", corp_code, bsns_year, reprt_code)

    def outside_directors(self, corp_code: str, bsns_year: int, reprt_code: str = "11011") -> list[dict]:
        """독립(사외)이사 및 그 변동현황 (drctr_co 이사 수, otcmp_drctr_co 사외이사 수)."""
        return self._report("outcmpnyDrctrNdChangeSttus.json", corp_code, bsns_year, reprt_code)

    def director_pay(self, corp_code: str, bsns_year: int, reprt_code: str = "11011") -> list[dict]:
        """이사·감사 전체의 보수현황(보수지급금액 - 이사·감사 전체) (nmpr, mendng_totamt, fscl_year)."""
        return self._report("hmvAuditAllSttus.json", corp_code, bsns_year, reprt_code)

    def major_holdings(self, corp_code: str) -> list[dict]:
        """대량보유 상황보고 (repror 대표보고자, stkrt 보유비율, rcept_dt)."""
        return self.get_list("majorstock.json", corp_code=corp_code)


def get_dart_client(settings: Any) -> DartClient | None:
    if not getattr(settings, "dart_api_key", None):
        return None
    return DartClient(settings.dart_api_key, Path(settings.project_root) / "data" / "cache")
