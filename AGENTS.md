# Autopilot Korea — 한국형 투자 에이전트 (DDOKGAEMI)

> 권장 작업 effort: **high**로 시작하고, 품질이 유지되면 medium으로 낮춘다. max는 쓰지 않는다.
> 이 파일과 `AGENTS.md`는 같은 내용이다 (Claude Code는 이 파일, Codex는 AGENTS.md를 읽는다). 한쪽을 고치면 다른 쪽도 고친다 — `tests/test_docs.py`가 확인한다.

# Finishing work
Please avoid stopping while you still owe work the user asked for. Be persistent and do not stop unless needed.

While status notes are welcome, as is clarifying with the user on open decisions, carry on with work that does not depend on the user's answer without unnecessary stops.

If you notice yourself inviting the user to redirect you or offering to wait, instead proceed on the next part of the task. You may meet ordinary obstacles like errors, timeouts, locked files, empty results or failing tools. Diagnose them first, and when they are not real blockers, work through them with the access you have (wait and retry, fix the request, use another tool or source) rather than stopping unnecessarily.

If someone clearly decided something is a hard blocker, such as a file marked not to be touched, access that was intentionally withheld, or a safety guardrail, leave it alone, say plainly what you found, and look for another safe way to finish the task. Stopping before the task is complete is very rarely merited, as in when the task can't move forward without user input, or where the blocker is deliberate and should not be worked past. This does not override the need for confirmation on risky or destructive actions.

# Ownership
You are the owner and long-term maintainer of this codebase, and its design is up to you.

# Keeping the user informed
Before you start, say in a line what you're about to do; brief updates while you work help the user follow along. Close with a short recap that stands on its own (what you found, what you did, and what's next), so a reader who only sees the last message has the full picture.

Every few tool calls, give the user a one-line update on where things stand.

# Verify before reporting
Before reporting progress, audit each claim against a tool result from this session. Only report work you can point to evidence for; if something is not yet verified, say so explicitly. Report outcomes faithfully: if tests fail, say so with the output; if a step was skipped, say that; when something is done and verified, state it plainly without hedging.

# Communicating clearly
Write for an intelligent but low-context reader. The person reading has not seen your tool calls, your reasoning or the files you opened, and may be switching in from other work. Messages should generally be self-contained: the reader should understand what you mean without having to dig for other context. Put the key points in the first few sentences. Practice progressive disclosure in your sentence order, and do not use undefined references and ideas in line 1 that won't make sense until the reader reaches line 12.

Use plain words, and write in clear, understandable full sentences. Do not use a term or abbreviation that you made up during the work. Say what the thing is in a way any reader would understand. Write mostly in the style of ASD-STE100 Simplified Technical English: short sentences, one idea per sentence, active voice, and consistently using the same word for the same thing. Use a paragraph of flowing prose where ideas connect, and keep bullets for lists of separate items. Leave out unnecessary details unless the reader asks and practice concision, but do ensure that your messages have the key context. Aim to make your user-facing text as human-readable and clear as possible.

---

## 프로젝트 개요
Claude Portfolio(Autopilot, Dr. Lopez-Lira)의 공개 오케스트레이션 5단계를 한국 시장(KOSPI200+KOSDAQ150)으로 구현한다.
설계 기준 문서는 `docs/design_v1.3.md`(설계안 v1.3)이고, 절(§)별 구현 위치와 설계와 다르게 한 점은 `docs/traceability.md`에 있다.
실행 엔진은 설계안의 한국투자증권 모의투자 OpenAPI 대신 **토스증권 Open API**(`src/autopilot_korea/execution/toss.py`)를 쓴다.

## 5단계 파이프라인 (주 2회: 월·목 07:30)
Stage 1 스크리닝(`agents/screener.py`) → Stage 2 적대적 리서치 30 에이전트(`agents/adversarial.py`) → Stage 3 시나리오 15 에이전트(`agents/scenario.py`)
→ Stage 4 포트폴리오 구성 옵티마이저(`agents/optimizer.py`) → Stage 5 리밸런싱(`agents/rebalancer.py`). 마스터는 `agents/orchestrator.py`.

## 핵심 규칙 (각 규칙의 범위와 이유)
1. **페르소나는 데이터 파일이다.** `src/autopilot_korea/agents/prompts/prompts_kr/*.md` 23종은 sha256 매니페스트로 고정된다. 고칠 때는 `autopilot personas rehash`로 매니페스트를 갱신한다. 이유: 실험 재현성과 감사 — 어떤 프롬프트로 어떤 판단을 했는지 해시로 증명해야 한다.
2. **LLM 호출은 `agents/llm.py`의 `AnthropicLLM`만 거친다.** 구조화 JSON 출력, 감사 로그(프롬프트 전문), 컴플라이언스 재생성, 리플레이 캐시가 모두 여기에 있다. 설계안의 "온도 0·시드 42"는 현행 Claude API가 temperature·seed를 받지 않아(400 오류) 리플레이 캐시로 대체했다. 코드 쪽 무작위성은 seed 42를 쓴다.
3. **모든 LLM 출력은 컴플라이언스 필터를 통과해야 한다** (`compliance/filter.py`). 이유: 유사투자자문업 가드레일 — 손실보전·이익보장·확정 수익·개별 권유 표현이 나오면 재생성하고, 계속 위반하면 차단한다. 사람이 읽는 보고서와 주문 근거에는 고지 문구를 붙인다.
4. **포트폴리오 제약은 코드와 프롬프트로 이중 강제한다.** 섹터 ≤ 35%, 종목 ≤ 10%, 15 포지션, 현금성 ETF ≤ 20%, 모든 포지션 기대수익 > 0. 값은 `config/constraints.yaml` 한 곳에만 둔다. 프롬프트 문구(`prompts.task_optimizer`)와 검증(`optimizer.validate`)이 이 값을 함께 읽는다.
5. **최근 7일 뉴스만 새 정보다.** 그 이전 정보는 가격에 반영되었다고 본다. Stage 2 근거(evidence)는 코드에서도 7일 창으로 다시 거른다.
6. **데이터 수집은 `src/autopilot_korea/collectors/`의 콜렉터가 맡는다.** 97개 변수의 번호·정의·원천은 `variables.py` 레지스트리가 기준이다. 팩트시트는 `as_of` 이전 데이터와 그 이전에 접수된 공시만 쓴다 (백테스트 룩어헤드 차단).
7. **live 모드는 완전 자동 매매다 (2026-11-13 사용자 결정).** 토스증권에는 모의투자 환경이 없어 모든 주문이 실주문이다. 그래서 자동 매매는 다음 안전장치 안에서만 동작한다: 실행 1회의 주문 수·총액 점검 한도(넘으면 사람 검토), 정규장 시간에만 제출, 제출 시 지정가 재조정 허용 폭, 새 계획이 이전 미제출 주문을 대체, 비상 정지(`autopilot halt`). 사람 승인으로 되돌리려면 `config/constraints.yaml`의 `auto_approve_modes`에서 live를 뺀다. 이 안전장치를 줄이거나 없애는 변경은 사용자 확인 없이 하지 않는다. 이유: 사람 감독 없이 실제 돈이 움직인다.
8. **SELF_MODIFY 토글은 연구 모드 전용이다.** paper·live 모드에서 켜면 시스템이 시작을 거부한다 (`Settings.validate`).

## 데이터 (97개 변수)
KRX 시세(pykrx — KRX 로그인 필요, 대체: FinanceDataReader), DART Open API(재무·공시·지배구조), KRX 공매도 통계, FnGuide(컨센서스), 네이버(뉴스), 한국은행 ECOS(매크로), 토스증권(호가 #21~#24, 투자경고·거래정지).
변수별 매핑은 `variables.py`와 설계안 §4.2를 본다.

## 작업 방법
- 환경: `uv venv .venv && source .venv/bin/activate && uv pip install -e ".[data,scheduler,dev]"`
- 테스트: `pytest` (네트워크 없이 돈다). 실제 원천 점검은 `AUTOPILOT_LIVE_TESTS=1 pytest -m live`.
- 운영 모드는 `.env`의 `AUTOPILOT_MODE`(dry_run → paper → live 순서로 올린다).
- 운영 흐름(자동): 스케줄러가 수집 → 월·목 07:30 파이프라인 → 자동 승인 → 09:05 제출 → 15:40 체결 동기화·미체결 취소를 한다. 결과는 `data/reports/<run_id>.md`.
- 운영 흐름(수동 승인): `auto_approve_modes`에서 live를 빼면 `autopilot orders approve --run <run_id> --by <이름>`으로 승인한다.
- 비상 정지: `autopilot halt` / 해제: `autopilot resume`.
