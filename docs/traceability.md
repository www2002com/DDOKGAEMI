# 설계안 v1.3 추적표

설계안([design_v1.3.md](design_v1.3.md))의 각 절이 코드 어디에 구현되었는지, 그리고 설계와 다르게 구현한 점과 그 이유를 적습니다. 경로는 `src/autopilot_korea/` 기준입니다.

## 절별 구현 위치

| 절 | 설계 내용 | 구현 | 검증 테스트 |
|---|---|---|---|
| §0-1 | 페르소나 = 데이터 파일 23종, sha256 고정 | `agents/prompts/prompts_kr/*.md`, `manifest.json`, `agents/personas.py` | `tests/test_persona_sha256.py` |
| §0-2 | 한국 데이터 원천 | `collectors/sources/` (market·dart·fnguide·naver·ecos), `execution/toss.py`(호가·규제 상태) | `tests/test_collectors.py` |
| §0-3 | 규제 내장, compliance_check 강제 | 모든 출력 스키마에 `compliance_check` (`agents/schemas.py`), `compliance/filter.py` | `tests/test_compliance_llm.py` |
| §0-4 | 재현성: 온도 0·시드·구조화 출력·프롬프트 전문 로깅 | `agents/llm.py` (구조화 출력, `audit_log`, 리플레이 캐시) — 아래 차이점 2 | `tests/test_compliance_llm.py::test_replay_reproduces_without_api_call` |
| §0-5 | 통제형 우선 → 자율형 전환 | 고정 파이프라인(`agents/orchestrator.py`) + 사람 승인 게이트. 자율형(P4)은 CLAUDE.md로 Claude Code가 운영 | — |
| §1 | 아키텍처: 데이터 → 오케스트레이터 → 컴플라이언스·리스크 → 실행 → 성과·감사 | `Orchestrator.run_pipeline` → `risk/gate.py` → `execution/order_manager.py` → `nav_history`·`audit_log` | `tests/test_pipeline.py` |
| §1 | 운영 케이던스 주 2회 + DART 실시간 + 감성 레이더 | `config/schedule.yaml`, `scheduler/beat.py` | `tests/test_backtest_scheduler.py::test_beat_schedule_matches_cadence` |
| §2 | 운영 승인 패널 07·08·09·11·21 + 18/19, 감성 전용 16·17·20 | `config/personas.yaml` `panels`, 역할 규칙 검증 `PersonaLibrary.validate_config` | `test_panels_follow_role_rules`, `test_sentiment_persona_cannot_drive_decisions` |
| §3.1 | 초기 프롬프트, 유니버스, ~15 포지션, 거래 상한 주 2회 | `agents/prompts.py` `INITIAL_PROMPT_KR`, `collectors/universe.py`, `constraints.yaml` | — |
| §3.2 S1 | 전 종목 스코어링 → 상위 ~50 | `agents/screener.py` (`screening_top_n: 50`, 동점 시 시총) | `test_full_pipeline_paper_mode` |
| §3.2 S2 | 불 15 vs 베어 15, 독립 웹검색, 최근 7일만 | `agents/adversarial.py` (`web_search_20260209` 허용 도메인, 근거 7일 창 코드 재검증) | 같은 테스트 (불 15·베어 15·7일 필터 단언) |
| §3.2 S3 | 확률 가중 시나리오, 1·3·6·12개월 목표, 과낙관 방지 | `agents/scenario.py` (`optimism_review` + 확률 정규화 + 기대수익 재계산 + 컨센서스 1.5배 초과 플래그) | 같은 테스트 |
| §3.2 S4 | Agent of agents, 섹터 35%, 기대수익 > 0, KOSPI200 초과 | `agents/optimizer.py` (`task_optimizer` 프롬프트 + `validate` 코드) | `test_validate_catches_each_constraint` |
| §3.2 S5 | 상시 리밸런싱, 근거와 함께 스왑 | `agents/rebalancer.py`, 오케스트레이터 검토(`RebalanceReview`) | `test_swap_rule_*`, `test_second_run_trades_less` |
| §3.2 인간 역할 | 포지션 한도·회전율 한도·반증 가능한 thesis·인간 최종 서명 | `constraints.yaml`, `TargetPosition.falsification`, `thesis_journal`. 최종 서명은 사용자 결정으로 자동 승인 + 안전장치로 대체(아래 차이점 22). 수동 승인 방식은 설정으로 유지 | `test_live_mode_auto_trades_at_market_open`, `test_live_mode_requires_human_when_live_not_listed`, `test_live_gate_routes_unusual_batches_to_human`, `test_kill_switch_stops_auto_approval_and_submission` |
| §3.3 | 한국형 5단계 상세 (저장 테이블 포함) | `screening_results`(S1·S2), `adversarial_opinions`, `scenario_models`, `portfolio_positions` | `test_full_pipeline_paper_mode` |
| §3.3 S5 이벤트 | DART 주요사항보고 감지 시 즉시 재스코어링 | `agents/dart_monitor.py`, `Orchestrator.rescore` | — |
| §3.4 | 중앙값 집계, 확률 가중 기대수익, 재생성 최대 3회, 스왑 10점 | `adversarial.aggregate`, `scenario.expected_return`, `optimizer.optimize`, `rebalancer.apply_swap_rule` | `test_full_pipeline_paper_mode`(재생성), `test_swap_rule_keeps_holding_within_gap` |
| §4.1 | 데이터 소스 스택 | `collectors/sources/` | `tests/test_collectors.py` |
| §4.2 | 97개 변수 전수 매핑 (A~J) | `variables.py` 레지스트리 + 카테고리별 콜렉터 + `collectors/factsheet.py` | `tests/test_collectors.py` (97개 키·신선도) |
| §4.3 | 콜렉터 11종, 스케줄, 실패 처리, DB 스키마 | `collectors/*_collector.py`, `collectors/base.py`(`run_job`), `db/models.py` | `tests/test_collectors.py` |
| §5 | 포트폴리오 규칙 표 | `config/constraints.yaml` `portfolio`·`turnover` | `test_fallback_satisfies_all_constraints` |
| §6 | 모델 라우팅 | `config/models.yaml` `routes` — 아래 차이점 3 | `test_request_shape_and_audit`, `test_fallback_enabled_on_opus_route` |
| §7-1 | MDD −15% → 재검토 + 현금성 상향 | `Orchestrator.nav_drawdown` → `drawdown_review` → 옵티마이저 프롬프트·검증(현금성 ≥ 20%) | `test_fallback_satisfies_all_constraints`(드로다운 모드) |
| §7-2 | 종목 −25% → thesis 재평가 | `risk.positions_needing_review` → `aux_agents.review_thesis`(운영 승인 패널 다수결) | `test_thesis_review_forces_exit` |
| §7-3 | 실행 전 게이트 (risk_flags, KOREA_REGULATION) | `risk/gate.py` `pre_execution_gate` + 토스증권 경고·거래정지 조회 | `test_pre_execution_gate`, `test_toss_regulatory_flags` |
| §7-4 | 유동성 하위 제외, 거래대금 5% 초과 분할 | `collectors/universe.liquid_universe`, `risk.split_quantity` + `Order.not_before` | `test_risk_helpers` |
| §7-5·6 | 섹터 35% 하드 제약, 회전율 상한 | `optimizer.validate`, `risk.turnover_scale` | `test_risk_helpers` |
| §8 | thesis 저널, 메모리 10개, 프롬프트 로깅, 해시 고정, NAV | `thesis_journal`, `agents/memory.py`, `audit_log`, `manifest.json`, `nav_history` | `test_full_pipeline_paper_mode` |
| §9 | 구조화 출력 스키마 | `agents/schemas.py` (단계별 모델로 분리, 필드명은 §9 그대로) | `test_request_shape_and_audit` |
| §10 | 기능 토글 7종 | `config/features.yaml`, `prompts.toggle_block`, `Settings.validate`(SELF_MODIFY 운영 금지) | — |
| §11 | 유사투자자문업 가드레일 | 23개 파일 공통 블록 + `compliance/filter.py` + 보고서·주문 근거 고지 | `test_every_persona_ends_with_identical_guardrail` |
| §12 | 데이터 레이어 대응 | §4와 같음. Alpaca → 토스증권(아래 차이점 1) | — |
| §13 | 운영 케이던스 표 | `config/schedule.yaml` | `test_beat_schedule_matches_cadence` |
| §14 | 통제군, 룩어헤드 차단, CAGR·Sharpe·MDD·회전율, 강건성 스윕 | `backtest/` (`engine`, `metrics`, `strategies`) | `tests/test_backtest_scheduler.py` |
| §15 | 재시도 3회 → 폴백 → 스킵, 신선도 플래그, 회로 차단기, 미체결 3거래일 취소 | `collectors/base.run_job`, SDK `max_retries=3`, 옵티마이저 폴백, `OrderManager.cancel_stale` | `test_order_lifecycle_and_stale_cancel`, `tests/test_collectors.py` |
| §16 | 로드맵 P0~P4 | P0~P3 범위를 이번에 구현. P4(자율형 전환·실전)는 운영 검증 후 | — |
| §17.1 | 프로젝트 구조 | 아래 차이점 4 | — |
| §17.2 | 핵심 구현 명세 | 위 각 행 | — |
| §17.3 | CLAUDE.md / AGENTS.md | `CLAUDE.md`, `AGENTS.md` (Fable 5.2 지시 블록 포함, 두 파일 동일) | `tests/test_docs.py` |

## 설계와 다르게 구현한 점

1. **실행 엔진: 한국투자증권 → 토스증권 Open API.** 사용자 지시에 따른 변경입니다. 토스증권에는 모의투자 환경이 없어 모든 주문이 실주문입니다. 그래서 paper 모드용 내부 모의 브로커(`execution/paper.py`)를 따로 만들었고, live 모드는 설정과 무관하게 사람 승인 없이는 주문을 보내지 않습니다. API 명세는 공식 OpenAPI 문서(v1.2.28)의 필드 이름을 그대로 따랐습니다.
2. **온도 0·시드 42 → 리플레이 캐시.** 현행 Claude API(2026-10-21 버전)와 현행 모델은 `temperature`·`seed`를 받지 않습니다(보내면 400 오류). 같은 입력의 재현은 감사 로그에 저장된 응답을 `cache_key`로 재사용하는 방식으로 대신했습니다(`--replay`).
3. **모델: Opus 4.6 → 현행 세대.** 설계의 등급 구조(오케스트레이터·옵티마이저 = Opus, 대량 단계 = Sonnet, DART 1차 분류 = Haiku)는 유지하고 모델은 `claude-opus-5-5`·`claude-sonnet-5-5`·`claude-haiku-5-5`로 두었습니다. `config/models.yaml`에서 바꿀 수 있습니다. 거절(refusal) 시 서버 측 폴백을 켰습니다(Haiku 제외). effort는 Fable 5.2 지시파일 권고대로 high로 시작합니다.
4. **패키지 경로: `src/collectors/` → `src/autopilot_korea/collectors/`.** 설치 가능한 패키지로 만들기 위해 한 단계 감쌌습니다. 하위 모듈 이름(`price_collector.py` 등, `agents/screener.py` 등)은 설계안 그대로입니다. 리스크 게이트는 `risk/`로 분리했습니다.
5. **의존성: `dart-fss`·`openai` 제외.** DART는 설계안이 이름을 든 OpenDART REST 엔드포인트를 직접 호출합니다. 이 구현은 Claude만 쓰므로 OpenAI SDK가 필요 없습니다.
6. **pykrx 로그인.** pykrx 1.2.9부터 KRX 로그인(`KRX_ID`/`KRX_PW`)이 필요합니다. 로그인 정보가 없으면 시세는 FinanceDataReader로 대체하고, KRX 전용 데이터(공매도 등)는 missing 신선도 플래그로 남깁니다.
7. **주문 수량은 코드가 계산합니다.** §9 스키마의 `orders`는 옵티마이저 LLM이 아니라 Stage 5 코드가 목표 비중·현재가·호가단위로 계산해 `orders` 테이블에 기록합니다. LLM은 비중과 논지를 정합니다. 산술 오류를 막기 위해서입니다.
8. **회전율 정의.** min(매수액, 매도액) ÷ NAV(표준 정의)를 씁니다. 현금에서 처음 편입하는 매수가 월 30% 상한에 걸리지 않게 하기 위해서입니다.
9. **FCF(#84).** 설계안의 "영업현금흐름 − 투자현금흐름"은 부호에 따라 의미가 달라집니다. 표준인 영업현금흐름 − 유형자산 취득(CAPEX)으로 계산하고, CAPEX가 없으면 영업현금흐름 + 투자현금흐름으로 대체합니다.
10. **GICS 섹터.** 한국 공개 데이터에는 GICS가 없어 업종명 키워드 규칙과 대표 종목 확정표(`config/sectors.yaml`)로 근사합니다. 분류되지 않은 종목도 "미분류" 섹터로 35% 상한을 받습니다.
11. **Stage 3 에이전트 수.** "15 에이전트(종목당 1개)"는 동시 실행 15개로 구현했고, Stage 2 생존 종목 중 점수 상위 30개(+보유 종목)를 모델링합니다(`scenario_max_candidates`).
12. **페르소나 원문.** 설계안이 가리키는 기존 `prompts_kr/` 파일은 첨부되지 않아, 설계안이 언급한 번호·역할(07 가치, 08 모멘텀, 09 역발상, 11 공시, 14 공매도, 16·17·20 감성, 18/19 쌍둥이, 21 적응형, 01·04·05 통제군)에 맞춰 23종을 새로 작성했습니다. 기존 파일로 교체하려면 같은 파일 이름으로 덮어쓴 뒤 `autopilot personas rehash`를 실행합니다(front matter 형식은 README 참조).
13. **백테스트의 웹검색.** 웹검색은 현재 시점의 뉴스를 돌려주므로 백테스트에서는 꺼서 룩어헤드를 막습니다.
14. **호가(#21~#24).** 설계안의 pykrx/KIS 호가 대신 토스증권 호가 API를 씁니다. live 모드가 아니면 이 4개 변수는 missing으로 남습니다.
15. **지수 구성종목 근사.** KRX 로그인이 없으면 KOSPI200·KOSDAQ150 구성종목 목록을 받을 수 없습니다. 이때는 시가총액 상위 200(KOSPI)·150(KOSDAQ) 보통주로 근사하고, 실제 지수 규칙처럼 스팩·관리종목·투자주의환기종목을 뺍니다. 근사 여부는 `collection_status`(job=universe)에 기록됩니다.
16. **FnGuide 주소 변경.** 설계 당시의 `comp.fnguide.com` 페이지는 404입니다. `wcomp.fnguide.com/CompanyInfo/Consensus`를 씁니다. 예상 EPS(#56)는 FnGuide의 "PER(Fwd. 12M)"과 같은 방식으로 추정 연도 EPS를 12개월 선행 가중합니다(실측 검증: 삼성전자 예상 PER 2.98 vs FnGuide 표기 2.95, 기준가 날짜 차이). 컨센서스 평균(#70)은 FnGuide 점수(5=강력매수)를 1=강력매수 척도로 뒤집은 값(6 − 점수)입니다.
17. **네이버 금융 페이지 폐지.** 종목 뉴스·종목토론방의 기존 PC 페이지가 410/302를 돌려줍니다. `m.stock.naver.com` JSON API를 쓰고, 외국인 보유 비율도 여기서 받습니다. 네이버 검색 API 키가 있으면 검색 API를 우선 씁니다.
18. **관리종목 등 위험 표시.** KRX KIND는 이 환경에서 403입니다. FDR 상장 목록의 소속부(`Dept`) 표기로 관리종목·투자주의환기종목을 표시하고, 투자경고·투자위험·정리매매·VI·거래정지는 live 모드에서 토스증권 API로 주문 직전에 확인합니다.
19. **직전 배당(#64, #65).** 현금·현물배당결정 공시를 해석하지 못하면 비워 둡니다. 분기배당 기업에서 연간 배당금으로 대신 채우면 틀린 값이 되기 때문입니다.
20. **EBITDA(#61, #75, #89).** DART 현금흐름표에서 감가상각비를 따로 공시하지 않는 기업이 많아 자주 missing입니다. 지어내지 않고 missing으로 둡니다.
21. **키 없이 채워지는 변수.** DART 키와 KRX 로그인 없이 실데이터로 점검했을 때 97개 중 36개가 채워졌습니다(시세·모멘텀·컨센서스·밸류에이션 일부). DART 키를 넣으면 재무·배당·지배구조(B·C·G·H·J)가, KRX 로그인을 넣으면 공매도(E 일부)가 채워집니다. 비어 있는 변수는 팩트시트에 `missing`으로 표시되어 에이전트가 판단에서 무게를 줄입니다.
22. **live 완전 자동 매매 (2026-11-13 사용자 결정).** 설계안 §3.2의 "인간이 최종 서명"을 사용자 결정에 따라 자동 승인으로 바꿨습니다. 사람 감독 없이 실제 돈이 움직이므로 안전장치를 더했습니다: 실행 1회 주문 수(40건)·총액(평가액의 110%) 점검 한도를 넘으면 사람 검토, 정규장 시간(09:00~15:20)에만 제출, 제출 시 현재 호가로 지정가 재조정(불리한 방향 1% 이내), 새 계획이 이전 미제출 주문을 대체, 비상 정지(`autopilot halt`). 사람 승인 방식은 `auto_approve_modes`에서 live를 빼면 돌아옵니다.
