"""한국은행 ECOS 100대 통계지표 (KeyStatisticList).

URL: https://ecos.bok.or.kr/api/KeyStatisticList/{인증키}/json/kr/{시작}/{끝}
응답: {"KeyStatisticList": {"list_total_count": 101, "row": [{CLASS_NAME, KEYSTAT_NAME, DATA_VALUE, CYCLE, UNIT_NAME}]}}
오류: {"RESULT": {"CODE": "ERROR-xxx" | "INFO-xxx", "MESSAGE": ...}}.
인증키 "sample"은 한 번에 10건까지만 조회된다(2026-11 실측).
"""

from __future__ import annotations

from typing import Any

import requests

URL = "https://ecos.bok.or.kr/api/KeyStatisticList/{key}/json/kr/{start}/{end}"


class EcosError(RuntimeError):
    pass


def parse_key_statistics(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """(통계 목록, 전체 건수)."""
    if "RESULT" in payload:
        code = str(payload["RESULT"].get("CODE", ""))
        if code == "INFO-200":  # 해당하는 데이터가 없음
            return [], 0
        raise EcosError(f"{code}: {payload['RESULT'].get('MESSAGE')}")
    body = payload.get("KeyStatisticList") or {}
    rows = []
    for r in body.get("row", []):
        try:
            value = float(str(r.get("DATA_VALUE", "")).replace(",", ""))
        except ValueError:
            value = None
        rows.append({"name": r.get("KEYSTAT_NAME"), "value": value, "unit": r.get("UNIT_NAME"),
                     "period": r.get("CYCLE"), "class_name": r.get("CLASS_NAME")})
    return rows, int(body.get("list_total_count") or len(rows))


def key_statistics(api_key: str, session: requests.Session | None = None, timeout: float = 20.0) -> list[dict[str, Any]]:
    """100대 통계지표 전체. sample 키는 첫 10건만 받는다."""
    page = 10 if api_key == "sample" else 100
    out: list[dict[str, Any]] = []
    start = 1
    while True:
        resp = (session or requests).get(URL.format(key=api_key, start=start, end=start + page - 1), timeout=timeout)
        resp.raise_for_status()
        rows, total = parse_key_statistics(resp.json())
        out.extend(rows)
        start += page
        if not rows or start > total or api_key == "sample":
            return out
