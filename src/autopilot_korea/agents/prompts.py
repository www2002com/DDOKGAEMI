"""프롬프트 조립.

시스템 프롬프트 = 기본 지시 + 페르소나 본문 + 정적 기능 토글 + 단계 과제 (모든 종목에 같으므로 캐시된다).
사용자 메시지 = 팩트시트·뉴스·매크로·메모리·직전 판단처럼 종목마다 바뀌는 내용.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from autopilot_korea.agents.personas import Persona
from autopilot_korea.settings import Settings

INITIAL_PROMPT_KR = "당신은 5,000만 원 포트폴리오를 관리합니다. KOSPI200을 이기세요. 시작할까요?"

BASE_SYSTEM = f"""# 역할
당신은 "오토파일럿 코리아"의 리서치 에이전트입니다. 운용 목표는 한 문장입니다: "{INITIAL_PROMPT_KR}"
투자 유니버스는 KOSPI200과 KOSDAQ150 종목입니다. 당신의 출력은 리서치 입력입니다. 최종 주문은 사람이 정한 규칙(포지션 한도, 섹터 한도, 회전율 한도)과 사람의 승인을 거칩니다.

# 근거 원칙
- 제공된 팩트시트(97개 변수), 최근 7일 뉴스·공시, 그리고 도구로 직접 확인한 사실만 근거로 씁니다. 수치를 지어내지 마십시오.
- 팩트시트에서 값이 없거나(missing) 오래된(stale) 변수는 그 사실을 밝히고 판단에서 무게를 줄이십시오.
- 최근 7일 이내 뉴스와 공시만 새로운 정보입니다. 그 이전 정보는 이미 가격에 반영되었다고 봅니다. 오래된 호재나 악재를 새 근거처럼 쓰면 판단이 왜곡되기 때문입니다.
- 비율은 소수(0.12 = 12%)로, 금액과 주가는 원 단위로 읽고 씁니다.

# 출력
- 지정된 JSON 스키마로만 답하고, 문자열은 한국어로 씁니다.
- compliance_check는 사실대로 채웁니다. 고지 문구는 시스템이 붙이므로 disclaimer_included는 true로 둡니다."""

RISK_FLAG_VOCAB = ["거래정지", "관리종목", "투자주의", "투자경고", "투자위험", "정리매매",
                   "공매도_규제_확인필요", "VI_발동", "가격제한폭_근접", "불성실공시"]


def toggle_block(settings: Settings) -> str:
    """정적 기능 토글 지시 (§10). 동적 토글(MEMORY 노트, LAST_REASONING, SOCIAL)은 사용자 메시지에 들어간다."""
    lines = ["# 출력 필드 지침"]
    if settings.feature("CONFIDENCE"):
        lines.append("- confidence: 판단의 확신도를 0~1로 보정해 씁니다. 0.5는 근거가 반반이라는 뜻입니다. 이 값은 포트폴리오 비중 산정에 쓰입니다.")
    else:
        lines.append("- confidence: 0.5로 둡니다.")
    if settings.feature("SECOND_ORDER"):
        lines.append("- edge: 시장이 이미 기대하는 것(주가·컨센서스에 반영된 것)을 먼저 쓰고, 당신의 견해가 어디서 다른지 씁니다. 차이가 없으면 \"차별점 없음\"이라고 씁니다.")
    else:
        lines.append("- edge: 빈 문자열로 둡니다.")
    if settings.feature("KOREA_REGULATION"):
        lines.append("- risk_flags: 한국 시장 규제 상태를 점검하고, 해당하면 다음 표준 표기를 씁니다: " + ", ".join(RISK_FLAG_VOCAB) + ". 해당 사항이 없으면 빈 배열입니다.")
    else:
        lines.append("- risk_flags: 해당하는 위험을 짧게 씁니다.")
    if settings.feature("MEMORY"):
        lines.append("- memory_note: 다음 라운드의 당신에게 남길 한 줄 교훈입니다. 없으면 빈 문자열입니다.")
    else:
        lines.append("- memory_note: 빈 문자열로 둡니다.")
    return "\n".join(lines)


def persona_block(persona: Persona) -> str:
    return f"# 당신의 페르소나: {persona.id} {persona.name}\n\n{persona.body}"


def system_blocks(settings: Settings, persona: Persona, task: str) -> list[str]:
    return [BASE_SYSTEM, persona_block(persona), toggle_block(settings), task]


# ── 단계 과제 ───────────────────────────────────────────────────────────

TASK_SCREENING = """# 과제: Stage 1 스크리닝
종목 하나의 팩트시트와 최근 7일 뉴스를 받아 1~100점으로 평가합니다.
점수는 앞으로 3~12개월 동안 이 종목이 KOSPI200 대비 초과 수익을 낼 가능성을 나타냅니다. 50점은 벤치마크와 비슷한 기대, 70점 이상은 뚜렷한 초과 수익 근거, 30점 이하는 뚜렷한 열위입니다. 점수는 유니버스 전체에서 비교되므로 근거가 약하면 극단적인 점수를 피하십시오.
- valuation: 1주당 적정 주가 추정치(원)
- price_prediction_t / t1 / t2: 이번·다음·다다음 리밸런싱 라운드(주 2회, 약 3~4거래일 간격)의 주가 방향과 예상 범위를 짧게 씁니다.
- score_reasoning: 한 줄 근거"""


def task_adversarial(stance: str) -> str:
    side = "불(매수 논거)" if stance == "bull" else "베어(매도·기피 논거)"
    return f"""# 과제: Stage 2 적대적 리서치 — {side} 케이스
당신은 이 종목의 {side} 케이스를 맡았습니다. 반대 입장은 다른 에이전트 15개가 독립적으로 맡습니다. 진짜 토론이 되어야 숨은 리스크가 드러나므로, 이 입장에서 가장 강한 논거를 근거와 함께 쓰십시오.
다만 근거가 약하면 strength를 낮게 주십시오. 결과는 15개 의견의 중앙값으로 집계되므로, 강도를 부풀리면 집계가 왜곡됩니다.
웹검색 도구로 최근 7일 이내 뉴스와 공시를 직접 확인할 수 있습니다. evidence에는 날짜를 확인한 7일 이내 자료만 넣습니다.
- stance: "{stance}"
- strength: 0~100, 이 입장 논거의 강도
- invalidation: 이 논거가 틀렸다고 판단할 구체 조건"""


TASK_SCENARIO = """# 과제: Stage 3 시나리오 모델링
후보 종목의 확률 가중 불/베이스/베어 시나리오를 만듭니다. 각 시나리오에 확률(세 확률의 합 = 1), 12개월 주가, 서사를 씁니다.
target_1m / target_3m / target_6m / target_12m은 확률 가중 목표주가입니다.
expected_return = Σ(확률 × (12개월 주가 ÷ 현재가 − 1)). 코드가 같은 식으로 다시 계산합니다.
시나리오를 만든 뒤 optimism_review에서 스스로 반박하십시오: 불 시나리오 확률이 근거보다 높지 않은지, Stage 2 베어 케이스를 충분히 반영했는지, 목표주가가 컨센서스 최고 목표가를 크게 넘는다면 그럴 근거가 있는지 점검합니다. 점검 결과에 따라 숫자를 고친 최종값을 쓰십시오."""


def task_optimizer(settings: Settings, *, drawdown_review: bool) -> str:
    p = settings.constraints.portfolio
    cash = ", ".join(f"{e.ticker} {e.name}" for e in p.cash_etfs)
    lines = [
        "# 과제: Stage 4 포트폴리오 구성 (Agent of agents)",
        "Stage 1~3 결과 표와 현재 보유 내역을 받아 목표 포트폴리오를 만듭니다. 비중은 확신도·분산·리스크를 반영해 정합니다.",
        "현재 보유 종목을 바꾸면 거래 비용과 회전율 한도(월 {:.0%})를 소모합니다. 뚜렷한 근거가 없으면 기존 보유를 유지하는 편이 낫습니다.".format(settings.constraints.turnover.monthly_cap),
        "",
        "제약 (코드가 다시 검증하며, 위반하면 위반 내용과 함께 재생성을 요청합니다):",
        f"1. 포지션 수 {p.target_positions}개 이하, 주식은 최소 {p.min_equity_positions}개 (적격 후보가 부족하면 가능한 만큼)",
        f"2. 종목당 비중 ≤ {p.max_position_weight:.0%}",
        f"3. 섹터당 비중 ≤ {p.max_sector_weight:.0%} (현금성 ETF는 섹터 합계에서 제외)",
        f"4. 현금성 ETF 합계 ≤ {p.max_cash_etf_weight:.0%}. 허용 목록: {cash}",
        "5. 모든 주식 포지션은 표의 expected_return > 0 이어야 합니다.",
        "6. 비중 합계 = 1.0",
        "7. 후보 표와 현재 보유 내역에 없는 종목은 넣을 수 없습니다.",
        "8. 동점이면 시가총액이 큰 종목을 우선합니다.",
        f"목표: 포트폴리오 전체로 {p.benchmark} 대비 초과 수익.",
        "각 포지션의 thesis는 반증 가능한 서면 논지이고, falsification은 그 논지가 틀렸다고 판단할 구체 조건입니다.",
    ]
    if drawdown_review:
        lines.append(f"\n[드로다운 재검토 모드] 포트폴리오 최대낙폭이 {settings.constraints.risk.portfolio_mdd_review:.0%}를 넘었습니다. 현금성 ETF 합계를 {settings.constraints.risk.drawdown_cash_target:.0%} 이상으로 올리고, 논지가 약해진 포지션부터 줄이십시오.")
    return "\n".join(lines)


TASK_REBALANCE_REVIEW = """# 과제: Stage 5 리밸런싱 검토 (마스터 오케스트레이터)
규칙 엔진이 만든 편입·편출·교체 후보 목록을 받습니다. 각 결정마다 완전한 근거를 씁니다.
- replace_decision: Add(신규 편입), Replace(보유 종목 교체), Cancel(규칙 엔진이 제안했지만 근거가 부족해 보류해야 함)
- Cancel로 판단한 결정은 자동 실행되지 않고 사람의 검토로 넘어갑니다. 근거가 충분하면 Cancel을 쓰지 마십시오.
- 보유 종목을 교체할 때는 기존 논지의 반증 조건이 충족되었는지, 신규 종목의 우위가 거래 비용을 넘는지 씁니다."""

TASK_THESIS_REVIEW = """# 과제: 보유 종목 thesis 재평가
이 보유 종목이 매입가 대비 크게 하락했습니다(리스크 규칙: -25%). 기록된 투자 논지와 반증 조건을 현재 데이터와 대조해, 논지가 유지되는지 판단하십시오.
verdict: keep(유지), trim(축소), exit(편출). 가격 하락 자체는 근거가 아닙니다. 논지의 전제가 깨졌는지가 기준입니다."""

TASK_DART_TRIAGE = """# 과제: DART 공시 1차 분류
공시 제목과 기본 정보를 보고 주가와 투자 논지에 대한 중요도를 분류합니다.
- high: 유상증자, 전환사채·신주인수권부사채 발행, 최대주주 변경, 합병·분할, 감사의견 비적정, 거래정지, 대규모 소송, 실적의 큰 서프라이즈 등 가치에 직접 영향
- medium: 자기주식 취득·처분, 배당 결정, 단일판매·공급계약 등
- low: 정정 공시, 일상적 기재 사항"""

TASK_DART_DEEP = """# 과제: DART 주요 공시 재분석
중요도 high로 분류된 공시를 팩트시트와 함께 분석해 주가·투자 논지 영향을 판단합니다.
magnitude는 1(미미)~5(매우 큼)입니다. 종목 점수를 즉시 다시 매겨야 하면 rescore를 true로 둡니다."""

TASK_MACRO = """# 과제: 월간 매크로 전망
한국은행 경제통계(기준금리, 물가, 환율, 고용 등)와 최근 지표를 바탕으로 앞으로 1~3개월 한국 주식시장의 매크로 환경을 판단합니다.
sector_tilts는 GICS 11개 섹터 이름(에너지, 소재, 산업재, 경기소비재, 필수소비재, 헬스케어, 금융, 정보기술, 커뮤니케이션서비스, 유틸리티, 부동산)으로 씁니다."""

TASK_SENTIMENT = """# 과제: 시장 감성 레이더 (분석 전용)
최근 커뮤니티 글 제목과 뉴스 제목으로 이 종목에 대한 투자자 감성을 측정합니다. 이 결과는 참고 정보이며 주문 결정에 직접 쓰이지 않습니다.
sentiment는 -1(극단적 비관)~+1(극단적 낙관), intensity는 0~1입니다. 테마 과열이나 리딩방·펌핑 정황이 보이면 overheating을 true로 둡니다."""

TASK_SELF_MODIFY = """# 과제: 프롬프트 개선 제안 (연구 모드 전용)
이번 라운드에서 당신의 페르소나 지시가 판단을 방해했거나 부족했던 점이 있으면 개선안을 제안합니다. 제안은 감사 로그에만 기록되며 자동 반영되지 않습니다."""


# ── 사용자 메시지 블록 ──────────────────────────────────────────────────

def factsheet_block(fs: Any) -> str:
    return (f"## 팩트시트 — {fs.name} ({fs.ticker}), 섹터 {fs.sector}, 기준일 {fs.as_of}\n"
            f"```json\n{fs.to_prompt_json()}\n```")


def news_block(items: Iterable[dict[str, Any]], window_days: int) -> str:
    items = list(items)
    if not items:
        return f"## 최근 {window_days}일 뉴스·공시\n(수집된 항목 없음)"
    lines = [f"## 최근 {window_days}일 뉴스·공시"]
    for it in items[:30]:
        kind = "공시" if it.get("kind") == "disclosure" else "뉴스"
        lines.append(f"- [{str(it.get('published_at', ''))[:10]}] ({kind}/{it.get('source') or '-'}) {it.get('title')}")
    return "\n".join(lines)


def macro_block(macro: dict[str, Any] | None) -> str:
    if not macro:
        return "## 매크로 전망\n(최근 전망 없음)"
    tilts = ", ".join(f"{t['sector']}:{t['tilt']}" for t in macro.get("sector_tilts", []))
    return f"## 매크로 전망 (월간)\n{macro.get('summary', '')}\n섹터 기울기: {tilts}"


def memory_block(notes: list[str]) -> str:
    if not notes:
        return ""
    return "## 당신의 최근 메모 (MEMORY)\n" + "\n".join(f"- {n}" for n in notes)


def last_reasoning_block(prev: dict[str, Any] | None) -> str:
    if not prev:
        return ""
    lines = ["## 직전 라운드 당신의 판단 (LAST_REASONING)", json.dumps(prev, ensure_ascii=False)]
    lines.append("견해가 바뀌었다면 무엇이 바뀌었는지 근거에 쓰십시오. 직전 예측이 빗나갔다면 그 이유도 쓰십시오.")
    return "\n".join(lines)


def sentiment_block(sentiment: dict[str, Any] | None) -> str:
    if not sentiment:
        return ""
    return ("## 감성 레이더 (SOCIAL, 참고용 — 감성만으로 판단하지 마십시오)\n"
            + json.dumps(sentiment, ensure_ascii=False))


def join_blocks(*blocks: str) -> str:
    return "\n\n".join(b for b in blocks if b)
