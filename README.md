# DDOKGAEMI — 스스로 판단하고 움직이는 개미

한국형 투자 에이전트 **Autopilot Korea**입니다. Claude Portfolio(Autopilot)가 공개한 5단계 멀티 에이전트 오케스트레이션을 KOSPI200 + KOSDAQ150 시장에 맞게 구현했습니다. 설계 기준은 [설계안 v1.3](docs/design_v1.3.md)이고, 절(§)마다 어디에 구현했는지와 설계와 다르게 한 점은 [추적표](docs/traceability.md)에 정리했습니다. 주문 실행은 **토스증권 Open API**를 씁니다.

> ※ 본 정보는 참고용이며 개별 투자 권유가 아닙니다. 이 소프트웨어는 자기 계정 운용을 위한 도구입니다. 다른 사람에게 투자 정보를 제공하는 서비스로 쓰려면 유사투자자문업 신고가 필요합니다(설계안 §11).

## 무엇을 하는가

주 2회(월·목 07:30) 다음 다섯 단계를 실행합니다.

| 단계 | 하는 일 | 구현 |
|---|---|---|
| 1. 스크리닝 | 유니버스 전 종목을 97개 변수 팩트시트와 최근 7일 뉴스로 1~100점 평가 → 상위 50종목 | `agents/screener.py` |
| 2. 적대적 리서치 | 종목마다 불(매수 논거) 에이전트 15개와 베어(매도 논거) 에이전트 15개가 웹검색으로 독립 조사 → 중앙값 비교 | `agents/adversarial.py` |
| 3. 시나리오 | 확률 가중 불/베이스/베어 시나리오와 1·3·6·12개월 목표주가, 자기 반박으로 과낙관 점검 | `agents/scenario.py` |
| 4. 포트폴리오 구성 | 옵티마이저가 약 15포지션 구성. 섹터 ≤35%, 종목 ≤10%, 현금성 ETF ≤20%, 기대수익 > 0을 코드와 프롬프트로 이중 강제 | `agents/optimizer.py` |
| 5. 리밸런싱 | 보유 종목 점수가 신규 후보 최저보다 10점 이상 낮을 때만 교체, 회전율·유동성 한도 적용, 근거 작성 | `agents/rebalancer.py` |

그 밖에 DART 주요사항보고를 30분마다 감시해 중요한 공시가 나오면 해당 종목을 즉시 재평가하고, 월 1회 매크로 전망을 만들며, 감성 레이더(분석 전용)를 돌립니다.

```
데이터 레이어 (collectors/)        KRX 시세 · DART 재무/공시 · 공매도 · FnGuide 컨센서스 · 네이버 뉴스 · 한국은행 ECOS
        ↓  97개 변수 팩트시트 (variables.py)
오케스트레이터 (agents/)            Stage 1 → 2 → 3 → 4 → 5, 페르소나 23종 (agents/prompts/prompts_kr/)
        ↓
컴플라이언스 필터 + 리스크 게이트    compliance/, risk/
        ↓
실행 엔진 (execution/)              dry_run: 제안만 · paper: 내부 모의 브로커 · live: 토스증권 (사람 승인 필수)
        ↓
감사 로그 · thesis 저널 · NAV       db/ (audit_log, thesis_journal, nav_history …)
```

## 빠른 시작

```bash
uv venv .venv && source .venv/bin/activate
```

```bash
uv pip install -e ".[data,scheduler,dev]"
```

```bash
cp .env.example .env
```

`.env`에 최소한 `ANTHROPIC_API_KEY`와 `DART_API_KEY`를 넣습니다. 시세는 `KRX_ID`/`KRX_PW`(KRX 정보데이터시스템 회원)가 있으면 pykrx로, 없으면 FinanceDataReader로 받습니다. 공매도 통계와 실제 지수 구성종목은 KRX 로그인이 있어야 받을 수 있습니다(없으면 시총 상위로 근사).

키가 없을 때 97개 변수 중 무엇이 채워지는지는 원천에 따라 다릅니다. 키 없이 실데이터로 점검했을 때 36개(시세·모멘텀·컨센서스·밸류에이션 일부)가 채워졌습니다. DART 키를 넣으면 재무·배당·지배구조가, KRX 로그인을 넣으면 공매도 변수가 채워집니다.

```bash
autopilot init-db
```

```bash
autopilot personas verify
```

```bash
autopilot universe
```

```bash
autopilot collect
```

```bash
autopilot run
```

`autopilot run`이 끝나면 `data/reports/<run_id>.md`에 단계별 결과와 주문 내역이 담긴 보고서가 생깁니다. paper·live 모드는 기본 설정에서 주문을 자동 승인합니다. 사람 검토로 넘어간 주문(점검 한도 초과, Stage 5 보류 판정)은 아래 명령으로 확인하고 승인합니다.

```bash
autopilot orders list --run <run_id>
```

```bash
autopilot orders approve --run <run_id> --by <이름>
```

시험할 때는 종목을 좁혀서 비용을 줄일 수 있습니다: `autopilot run --tickers 005930,000660,035420`.

## 운용 모드

`.env`의 `AUTOPILOT_MODE`로 정하며 `dry_run → paper → live` 순서로 올리는 것을 권장합니다.

- **dry_run** (기본): 주문을 제안 상태로 저장만 합니다. 어디에도 보내지 않습니다.
- **paper**: 내부 모의 브로커가 체결을 시뮬레이션합니다. 토스증권에는 모의투자 환경이 없어서 직접 구현했습니다. 계좌 상태는 주문 테이블의 체결 내역으로 계산합니다.
- **live**: 토스증권으로 실제 주문을 보냅니다. **완전 자동 매매**입니다. 07:30 파이프라인이 주문을 자동 승인하고, 09:05에 장이 열린 뒤 제출하며, 15:40에 체결을 확인하고 3거래일 미체결 주문을 취소합니다.

live 자동 매매의 안전장치:
- 한 번의 실행에서 주문이 40건을 넘거나 주문 총액이 계좌 평가액의 110%를 넘으면 자동 승인하지 않고 사람 검토로 넘깁니다(`autopilot orders approve`).
- 정규장 시간(거래일 09:00~15:20)에만 제출합니다.
- 제출 직전 현재 호가로 지정가를 다시 맞추되, 계획가보다 1% 넘게 불리하면 추격하지 않습니다. 매수는 계획 금액 안에서 수량을 맞춥니다.
- 새 실행의 주문 계획이 이전 실행의 미제출 주문을 대체합니다(이중 주문 방지).
- 주문 1건 상한, 섹터·종목 한도, 거래정지·관리종목 매수 차단, Stage 5 검토가 보류(Cancel)로 판정한 주문의 사람 검토는 그대로입니다.
- **비상 정지:** `autopilot halt`로 자동 승인과 제출을 즉시 멈추고, `autopilot resume`으로 해제합니다. 이미 제출된 주문은 취소하지 않으므로 필요하면 토스증권 앱에서 취소합니다.

사람 승인 방식으로 되돌리려면 `config/constraints.yaml`의 `auto_approve_modes`에서 `live`를 뺍니다.

## 토스증권 Open API 설정 (live 모드)

1. 토스증권 웹(WTS)에서 설정 → Open API로 들어가 `client_id`와 `client_secret`을 발급합니다.
2. 같은 화면에서 이 프로그램을 실행할 서버의 IP를 허용 목록에 등록합니다. 등록되지 않은 IP의 요청은 403으로 거부됩니다.
3. `.env`에 `TOSS_APP_KEY`(client_id), `TOSS_APP_SECRET`(client_secret)을 넣습니다. 종합매매 계좌가 여러 개면 `TOSS_ACCOUNT_NO`로 지정합니다.

알아 둘 점:
- 클라이언트당 유효한 토큰은 하나입니다. 다른 프로그램이 같은 키로 토큰을 받으면 이 프로그램의 토큰이 폐기됩니다.
- 1억 원 이상 주문에는 확인 플래그가 자동으로 붙습니다. 별도로 주문 1건 상한(`max_order_value_krw`, 기본 2,000만 원)이 있습니다.
- 호가(97개 변수 #21~#24), 투자경고·투자위험·VI·거래정지 상태도 토스증권 API에서 가져옵니다.

## 스케줄러

```bash
celery -A autopilot_korea.scheduler.celery_app worker -B -l info
```

`config/schedule.yaml`의 케이던스(§13)로 돌아갑니다: 장 마감 후 시세·모멘텀·공매도 수집(18:00), 일요일 밸류에이션·배당, 정기보고서 감지 시 재무·지배구조, 매월 1일 컨센서스·매크로, 월·목 07:30 전체 파이프라인, 평일 30분마다 DART 모니터링, 15:40 주문 동기화와 미체결 3거래일 취소, 16:10 NAV 스냅샷. Redis가 필요합니다(`CELERY_BROKER_URL`).

## 설정 파일

| 파일 | 내용 |
|---|---|
| `config/constraints.yaml` | 포트폴리오 규칙(§5), 리스크(§7), 실행, 단계 파라미터(§3.4) |
| `config/schedule.yaml` | 운영 케이던스, 재시도·회로 차단기(§15), 휴장일 |
| `config/personas.yaml` | 패널 구성(불 15·베어 15·운영 승인·감성·통제군), 단계별 페르소나 |
| `config/features.yaml` | 기능 토글 MEMORY·SOCIAL·LAST_REASONING·SELF_MODIFY·CONFIDENCE·SECOND_ORDER·KOREA_REGULATION |
| `config/models.yaml` | 단계별 모델·effort·폴백, 웹검색 허용 도메인 |
| `config/sectors.yaml` | 업종명 → GICS 11개 섹터 근사 규칙 |

## 재현성과 감사

- 모든 LLM 호출의 렌더링된 프롬프트 전문과 응답이 `audit_log`에 남습니다.
- 현행 Claude 모델은 temperature·seed를 받지 않습니다. 같은 입력을 같은 결과로 재현하려면 `--replay`를 씁니다. 저장된 응답을 그대로 재사용합니다.
- 페르소나 파일은 sha256 매니페스트로 고정됩니다. 고친 뒤에는 `autopilot personas rehash`를 실행합니다.

## 백테스트 (§14)

```bash
autopilot backtest --start 2026-07-01 --end 2026-11-01 --cutoff 2026-06-30 --controls --estimate-only
```

통제군(01_default, 04_minimal, 05_hold_llm), 강건성 스윕(과제 문구 4종 × 프레이밍 × 목표함수 × 상위 N 격자), 전체 파이프라인을 같은 엔진으로 비교합니다. 결정은 직전 거래일 데이터로 하고 체결은 다음 날 시가로 합니다. 모델 지식 마감일 이전 구간은 실행을 거부합니다(LLM이 결과를 이미 알기 때문). 웹검색은 백테스트에서 끕니다. LLM 호출이 많으므로 `--estimate-only`로 규모를 먼저 확인하십시오.

## 테스트

```bash
pytest
```

네트워크와 API 키 없이 돕니다. 가짜 LLM으로 5단계 파이프라인 전체를 끝까지 실행하는 통합 테스트(`tests/test_pipeline.py`)가 포함됩니다. 실제 데이터 원천 점검은 `AUTOPILOT_LIVE_TESTS=1 pytest -m live`입니다.
