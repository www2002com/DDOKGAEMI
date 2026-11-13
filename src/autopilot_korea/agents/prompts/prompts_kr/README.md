# 페르소나 프롬프트 (prompts_kr)

## 개요
- 이 폴더의 파일은 Autopilot Korea 다중 에이전트 시스템이 쓰는 한국어 페르소나 정의다. 코드가 아니라 Claude 에이전트 시스템 프롬프트의 조각으로 쓰이는 데이터 파일이다.
- 각 페르소나 파일은 front matter(`id`, `name`, `name_en`, `role`, `default_stance`, `key_variables`)와 본문(정체성, 투자 철학, 분석 절차, 한국 시장 관점, 중점 변수, 불/베어 논거 방식, 피해야 할 편향, 규제 가드레일)으로 구성된다. `key_variables`의 번호는 97개 변수 팩트시트의 번호를 따른다.
- 단계별 작업 지시와 JSON 출력 스키마는 코드가 따로 덧붙인다. 페르소나 파일에는 출력 형식을 정의하지 않는다.
- 파일 무결성은 sha256 매니페스트 `manifest.json`으로 고정하며, 매니페스트는 `autopilot personas rehash`로 생성한다.
- 파일을 수정한 뒤 rehash 하지 않으면 테스트가 실패한다.

## 페르소나 목록
| 번호 | id | 이름 | 역할 | 기본 입장 | 용도 |
|---|---|---|---|---|---|
| 01 | 01_default | 기본 분석가 | control | neutral | 기준선 통제군, Stage 1 스크리닝, Stage 3 시나리오 |
| 02 | 02_macro | 매크로 전략가 | analyst | neutral | Stage 2 불·베어 패널, 매크로 모듈 |
| 03 | 03_allocator | 자산배분가 | analyst | neutral | Stage 2 불·베어 패널, Stage 4 옵티마이저 |
| 04 | 04_minimal | 최소 프롬프트 | control | neutral | 통제군(최소 지시) |
| 05 | 05_hold_llm | 보유 유지형 | control | neutral | 통제군(매수 후 보유) |
| 06 | 06_quant | 퀀트 팩터 분석가 | analyst | neutral | Stage 2 불·베어 패널 |
| 07 | 07_value | 가치투자자 | analyst | neutral | Stage 2 불·베어 패널, 운영 승인 패널 |
| 08 | 08_momentum | 모멘텀 트레이더 | analyst | neutral | Stage 2 불·베어 패널, 운영 승인 패널 |
| 09 | 09_contrarian | 역발상 투자자 | analyst | neutral | Stage 2 불·베어 패널, 운영 승인 패널 |
| 10 | 10_growth | 성장주 투자자 | analyst | neutral | Stage 2 불 패널 |
| 11 | 11_disclosure | 공시 분석가 | analyst | neutral | Stage 2 불·베어 패널, 운영 승인 패널, DART 모니터 |
| 12 | 12_dividend | 배당·주주환원 투자자 | analyst | neutral | Stage 2 불·베어 패널 |
| 13 | 13_governance | 지배구조·밸류업 분석가 | analyst | neutral | Stage 2 불·베어 패널 |
| 14 | 14_short_seller | 공매도 리서처 | analyst | bear | Stage 2 베어 패널 |
| 15 | 15_risk_manager | 리스크 매니저 | analyst | neutral | Stage 2 불·베어 패널 |
| 16 | 16_retail_sentiment | 개인투자자 커뮤니티 감성 | sentiment | neutral | 감성 레이더 전용(분석 전용) |
| 17 | 17_news_sentiment | 뉴스 감성 분석가 | sentiment | neutral | 감성 레이더 전용(분석 전용) |
| 18 | 18_optimist | 낙관론자 | analyst | bull | Stage 2 불 패널, 운영 승인 패널 |
| 19 | 19_pessimist | 비관론자 | analyst | bear | Stage 2 베어 패널, 운영 승인 패널 |
| 20 | 20_theme_sentiment | 테마·소셜 트렌드 감성 | sentiment | neutral | 감성 레이더 전용(분석 전용) |
| 21 | 21_adaptive_learner | 적응형 학습자 | analyst | neutral | Stage 2 불·베어 패널, 운영 승인 패널 |
| 22 | 22_flow | 수급 분석가 | analyst | neutral | Stage 2 불·베어 패널 |
| 23 | 23_sector_cycle | 섹터·산업 사이클 분석가 | analyst | neutral | Stage 2 불·베어 패널 |

## 패널 구성
| 구성 | 페르소나 | 비고 |
|---|---|---|
| 운영 승인 패널 | 07, 08, 09, 11, 21 + 18/19 | 18·19는 낙관론자·비관론자 쌍으로 함께 쓴다 |
| 감성 레이더 전용 | 16, 17, 20 | 분석 전용이며 주문 판단에 직접 쓰지 않는다 |
| 통제군 | 01, 04, 05 | 다른 페르소나의 부가가치를 비교하는 기준 |
| Stage 2 불 패널 (15) | 02, 03, 06, 07, 08, 09, 10, 11, 12, 13, 15, 18, 21, 22, 23 | 각 페르소나에 불 입장 지시를 붙인다 |
| Stage 2 베어 패널 (15) | 02, 03, 06, 07, 08, 09, 11, 12, 13, 14, 15, 19, 21, 22, 23 | 각 페르소나에 베어 입장 지시를 붙인다 |
| Stage 1 스크리닝 / Stage 3 시나리오 | 01_default | |
| Stage 4 옵티마이저 | 03_allocator | |
| 매크로 모듈 | 02_macro | |
| DART 모니터 | 11_disclosure | |

## 기능 토글
| 토글 | 기본값 | 정의 |
|---|---|---|
| MEMORY | ON | 에이전트별 최근 메모 10개를 프롬프트에 주입한다. |
| SOCIAL | OFF | 감성 레이더(16·17·20) 결과를 참고 정보로 추가한다. 감성만으로 점수를 정하지 않는다. |
| LAST_REASONING | ON | 같은 종목에 대한 직전 라운드의 자기 근거 요약을 주입하고, 견해가 바뀌었으면 이유를 쓰게 한다. |
| SELF_MODIFY | OFF (운영 금지) | 에이전트가 자기 프롬프트 개선안을 제안한다. 제안은 감사 로그에만 남고 자동 반영되지 않는다. paper/live 모드에서 켜면 시스템이 시작을 거부한다. |
| CONFIDENCE | ON | 출력에 확신도(0~1)를 포함하게 하고 Stage 4 비중 산정에 쓴다. |
| SECOND_ORDER | ON | "시장이 이미 기대하는 것은 무엇이고 내 견해는 어디서 다른가"를 쓰게 한다 (edge 필드). |
| KOREA_REGULATION | ON | 공매도 규제, 투자경고·위험 종목, 관리종목, 거래정지, 가격제한폭 ±30%, 변동성완화장치(VI)를 점검하게 하고 실행 전 게이트에서도 확인한다. |

## 공통 가드레일
- 모든 페르소나 파일(04_minimal 포함)은 마지막 섹션으로 동일한 `## 규제 가드레일 (유사투자자문업 · 공통 블록 · 수정 금지)` 블록으로 끝난다.
- 테스트는 각 파일에 이 블록이 바이트 단위로 동일하게, 정확히 한 번, 마지막 섹션으로 들어 있는지 검사한다.
- 블록 내용을 바꿔야 한다면 모든 파일을 같은 내용으로 한꺼번에 수정한 뒤 rehash 한다.
