# 한국형 투자 에이전트 설계안 v1.3
## — Claude Portfolio 오케스트레이션 실측 명세 + 97개 변수 한국 데이터 수집 설계 (구현 표준)

**작성일**: 2026-11-13
**기반**: Claude Portfolio 공개 오케스트레이션(5단계, LinkedIn/Scribd/Autopilot 공식) + GPT/DeepSeek 화이트페이퍼 97개 변수 원문 + 23개 페르소나 프롬프트
**구현 대상**: Claude Code / Codex가 본 문서만으로 100% 구현 가능하도록 작성

---

## 버전 이력

| 버전 | 일자 | 변경 내용 |
|------|------|-----------|
| v1.0 | 2026-11-11 | 23개 페르소나 한국형 변환 |
| v1.1 | 2026-11-12 | 오케스트레이션·스코어링·포트폴리오 규칙 7개 섹션 추가 |
| v1.2 | 2026-11-12 | §4를 화이트페이퍼 97개 변수 원문 명세로 교체 |
| **v1.3** | **2026-11-13** | **§3을 Claude Portfolio 실측 5단계 오케스트레이션으로 전면 재작성(공개 자료 확보), §4에 97개 변수별 한국 데이터 소스·수집 방법 상세 설계 추가, §17 구현 가이드(Claude Code/Codex용) 신설** |

---

## 0. 설계 원칙

1. **페르소나 = 데이터 파일**: 23종 `.md` ([prompts_kr/](prompts_kr/README.md)), sha256 해시 고정
2. **한국 데이터 원천**: DART Open API(공시), pykrx(시세), 금융감독원(공매도), FnGuide(컨센서스), 한국은행(매크로)
3. **규제 내장**: 유사투자자문업 가드레일 모든 페르소나에 주입, compliance_check 출력 강제
4. **재현성**: 온도 0, 시드 고정, 구조화 출력(JSON), 프롬프트 전문 로깅
5. **통제형 우선, 자율형 단계 전환**: GPT형 고정 파이프라인 → Claude형 자율형

---

## 1. 전체 아키텍처

```
[데이터 레이어 — §4 상세]
 pykrx (KRX 시세·시총·유동성)
 DART Open API (공시·재무제표·배당·지배구조)
 금융감독원 (공매도 잔고)
 FnGuide (컨센서스·목표주가)
 한국은행 경제통계 (매크로)
        ↓
[오케스트레이터 — §3, Claude Portfolio 5단계 실측 구조]
 S1 스크리닝 (Russell 1000 → KOSPI200+KOSDAQ150)
 S2 적대적 리서치 (30 에이전트: 불 15 vs 베어 15)
 S3 시나리오 모델링 (15 에이전트: 확률가중 불/베이스/베어)
 S4 포트폴리오 구성 (옵티마이저 에이전트)
 S5 상시 리밸런싱 (마스터 오케스트레이터)
        ↓
[컴플라이언스 필터 + 리스크 게이트 (§7)]
        ↓
[실행 엔진] 한국투자증권 모의투자 OpenAPI
        ↓
[성과 추적 + 감사 로그]
```

**운영 케이던스** (Claude Portfolio 실측):
- 주 2회 전체 파이프라인 재실행 (Claude 실측: 주 2회 리서치, 거래는 그 이하)
- 실시간: DART 주요사항보고 모니터링, 커뮤니티 감성 레이더(분석 전용)

---

## 2. 에이전트 페르소나 23종 (요약)

전문은 [prompts_kr/](prompts_kr/README.md). 운영 승인 패널: 가치(07), 모멘텀(08), 역발상(09), 공시(11), 적응형 학습자(21) + 낙관/비관 쌍둥이(18/19). 감성 전용(16·17·20)은 시장 감성 레이더 용도.

---

## 3. 오케스트레이션 — Claude Portfolio 실측 5단계 명세 (v1.3 전면 재작성)

> **이전 버전(v1.1~1.2)의 한계**: Claude Portfolio의 오케스트레이션은 "비공개"로 알고 자체 설계로 대체했습니다. **v1.3에서는 공개 자료로 실측 명세를 확보해 그대로 반영**합니다.

### 3.1 공개 자료로 확보한 Claude Portfolio 오케스트레이션 원문 명세

**출처**: [Chris Josephs(Autopilot CEO) LinkedIn](https://www.linkedin.com/posts/chris-josephs-677641114_the-claude-autonomous-agents-have-officially-activity-7445481891756986368-IN6d), [Luca D. LinkedIn](https://www.linkedin.com/posts/deangelisluca1_the-50000-claude-portfolio-is-live-trading-activity-7445088500225400835-FHjB), [The Claude Portfolio Philosophy Methodology (Scribd)](https://www.scribd.com/document/1063238622/The-Claude-Portfolio-Philosophy-Methodology), [Autopilot 공식 Claude Portfolio 페이지](https://start.joinautopilot.com/strategies/claude-portfolio-950048), [ai-street 인터뷰](https://www.ai-street.co/p/how-ai-runs-200-million-in-portfolios), [explainx.ai 분석](https://www.explainx.ai/blog/claude-portfolio-autopilot-ai-trading-experiment-august-2026)

**시스템 구성 (실측)**:
- **런타임**: Claude Code 에이전트, Opus 4.6 (1M context), `~/autopilot` 워킹디렉터리
- **초기 프롬프트**: "You are managing a $50,000 portfolio. Beat the S&P 500. You in?" (단 한 줄)
- **유니버스**: Russell 1000 (한국형: KOSPI200 + KOSDAQ150)
- **구성**: ~15개 포지션 (집중 포트폴리오)
- **거래 빈도**: Claude가 자율 결정, 상한 주 2회 (실측 평균은 그 이하; GPT/DeepSeek/Grok는 월 1회)

### 3.2 5단계 파이프라인 (실측 명세)

**Stage 1 — 스크리닝 (Screening)**
- 역할: 전체 유니버스 종목을 재무·뉴스·애널리스트 데이터·퀀트 지표로 스코어링
- 출력: 최고 스코어 상위 ~50개 종목 (객관적·데이터 기반 단기명단, 인간 사전 필터링 없음)
- 한국형: §4의 97개 변수로 KOSPI200+KOSDAQ150 전 종목 스코어링 → 상위 50종목

**Stage 2 — 적대적 리서치 (Adversarial Research)**
- 30개 AI 에이전트 병렬 실행: **15개는 불 케이스("왜 매수해야 하는가")**, **15개는 베어 케이스("왜 매도/기피해야 하는가")**
- 각 에이전트가 **독립적으로 웹검색** 수행
- **최근 7일 뉴스만 유효 정보로 인정** (그 이전 정보는 이미 가격에 반영됐다고 간주)
- 효과: 진짜 토론 강제 → 숨겨진 리스크 발굴

**Stage 3 — 시나리오 모델링 (Scenario Modeling)**
- 15+ 에이전트가 각 후보 종목에 대해 **확률 가중 불(base)/베이스/베어 시나리오 모델** 구축
- **1, 3, 6, 12개월 목표주가** 산출
- 시스템이 내부적으로 자기 토론 → 과도하게 낙관적인 모델링 방지

**Stage 4 — 포트폴리오 구성 (Portfolio Construction)**
- **"Agent of agents"** — 옵티마이저 에이전트가 ~15개 포지션 구성
- **엄격한 제약**: ① 섹터 비중 35% 초과 금지 ② 모든 포지션 양(+)의 기대수익 필수 ③ 포트폴리오 전체가 SPY(KOSPI200) 박치기 목표
- 정확한 비중은 확신도·분산·리스크 기반

**Stage 5 — 상시 리밸런싱 (Ongoing Rebalancing)**
- 마스터 오케스트레이터가 주기적으로 전체 파이프라인 재실행
- 보유 종목 vs 갱신된 스코어 상위 종목 비교 → 완전한 근거와 함께 매매 추천
- 기준 미달 종목은 더 높은 스코어 종목으로 스왑

**인간의 역할 (실측)**:
- Lopez-Lira 팀이 전략·리스크 파라미터 설정, 투자 프로세스 통제
- 파라미터: 포지션 사이즈 상한, 회전율 상한, **반증 가능한 서면 투자 논지(thesis) 요구**
- "레일 안의 모든 픽은 내 것" — Claude의 출력은 리서치 입력이며, 인간이 최종 서명

### 3.3 한국형 오케스트레이션 구현 (실측 명세의 한국 변환)

```
[한국형 5단계 — Claude Portfolio 실측 구조의 한국 구현]

Stage 1 스크리닝 (주 2회)
  입력: KOSPI200+KOSDAQ150 전 종목의 97개 변수(§4) + 최근 7일 뉴스
  실행: Claude Opus (병렬 종목당 1회 호출) 또는 Sonnet
  출력: 종목당 1~100점 → 상위 50종목 + 각 종목 1줄 스코어 근거
  저장: screening_results 테이블 (스코어, 근거, 타임스탬프)

Stage 2 적대적 리서치 (주 2회, Stage 1 직후)
  입력: 상위 50종목 × 최근 7일 뉴스 (웹검색: 네이버 뉴스 + DART 공시)
  실행: 30 에이전트 병렬 — 불 15 (매수 논거) / 베어 15 (매도 논거)
        각 에이전트 = 페르소나 프롬프트(prompts_kr/) + 웹검색 도구
  출력: 종목당 불 케이스 요약 + 베어 케이스 요약 (구조화 JSON)
  집계: 종목당 불/베어 강도 비교 → divergence 플래그

Stage 3 시나리오 모델링 (주 2회, Stage 2 직후)
  입력: Stage 2 결과 + 97개 변수
  실행: 15 에이전트 (종목당 1개) — 확률 가중 불/베이스/베어 시나리오
  출력: 종목당 {불 확률, 베이스 확률, 베어 확률,
              목표주가 1/3/6/12개월, 기대수익률, 리스크}
  저장: scenario_models 테이블

Stage 4 포트폴리오 구성 (주 2회, Stage 3 직후)
  입력: Stage 1~3 결과 + 현재 포트폴리오
  실행: 옵티마이저 에이전트 (Claude Opus) — "Agent of agents"
  제약(하드 코딩 + 프롬프트 이중 강제):
    - 섹터 비중 ≤ 35% (GICS 11개 섹터)
    - 모든 포지션 기대수익 > 0
    - 포트폴리오 전체 KOSPI200 대비 초과 수익 목표
    - 종목당 최대 10% (자체 규칙, 원본은 명시 안 됨)
    - 15개 포지션 (현금성 ETF 포함 가능, 최대 20%)
    - 동점 시 시총 큰 종목
  출력: 목표 포지션 15종 + 각 종목 thesis + 리밸런싱 주문 목록

Stage 5 상시 리밸런싱 (주 2회 + 이벤트 트리거)
  마스터 오케스트레이터: 전체 파이프라인 재실행 후
    현재 보유 종목 스코어 vs 신규 상위 종목 스코어 비교
    하락 종목 → 교체 추천 (완전한 근거 포함)
  이벤트 트리거: 일일 DART 주요사항보고 감지 시 해당 종목 즉시 재스코어링
```

### 3.4 집계·충돌 해결 규칙

- Stage 2의 불/베어 대립: 각 15 에이전트 의견의 중앙값으로 종목당 불 강도/베어 강도 산출 → 불 > 베어 시 후보 유지, 베어 > 불 시 관찰 플래그
- Stage 3 시나리오: 확률 가중 기대수익 = Σ(시나리오 확률 × 시나리오 수익률)
- Stage 4 제약 위반 시: 옵티마이저 에이전트에 제약 위반 피드백 후 재생성 (최대 3회)
- Stage 5 스왑 기준: 보유 종목 스코어가 신규 후보 최저 스코어보다 10점 이상 낮으면 스왑 검토

---

## 4. 97개 변수 한국 데이터 소스·수집 방법 상세 설계 (v1.3 신설)

> 화이트페이퍼 원문(Exhibit 2B)의 97개 변수를 한국 데이터로 수집하는 구체적 설계. 각 변수에 대해: 원문 번호, 원문 변수명, 한국 대응 변수, 데이터 소스(구체 API/엔드포인트), 수집 방법·주기, 비고를 명시합니다.

### 4.1 데이터 소스 스택 요약

| 소스 | 용도 | 인증 | 비고 |
|------|------|------|------|
| **pykrx** | KRX 시세·시총·거래량·가격변동 | 무료 | `get_market_ohlcv_by_date`, `get_market_cap_by_ticker` 등 |
| **DART Open API** | 재무제표·공시·배당·지배구조·자사주 | API 키 (무료 발급) | `fnlttSinglAcntAll`, `mptrtCsSttus` 등 |
| **금융감독원 공매도 API** | 공매도 잔고·거래량 | 공개 | KRX 공매도 데이터 |
| **FnGuide** | 애널리스트 컨센서스·목표주가 | 무료 | 컨센서스 API |
| **FinanceDataReader** | 보조 시세·재무 | 무료 | pykrx 대체/보완 |
| **네이버 금융 뉴스 API** | 뉴스 헤드라인 | 무료 | 최근 7일 뉴스 |
| **한국은행 경제통계 API** | 매크로 (금리·CPI·실업률·환율) | 무료 | 매크로 예측 모듈 입력 |

### 4.2 변수별 상세 매핑표 (97개 전체)

#### A. 시세·호가 (17개) — pykrx

| # | 원문 변수명 | 한국 대응 | 소스/엔드포인트 | 수집 방법 |
|---|------------|-----------|-----------------|-----------|
| 1 | Previous Close Price | 전일 종가 | pykrx `get_market_ohlcv_by_date(fromdate, todate, ticker)` | 일 1회 (장 마감 후) |
| 2 | Opening Price | 시가 | 〃 | 〃 |
| 3 | Lowest Price of the Day | 저가 | 〃 | 〃 |
| 4 | Highest Price of the Day | 고가 | 〃 | 〃 |
| 5 | Previous Close Price (Regular Market) | 전일 종가 (정규장) | pykrx `get_market_ohlcv_by_date` (정규장만) | 〃 |
| 6 | Opening Price (Regular Market) | 시가 (정규장) | 〃 | 〃 |
| 7 | Lowest Price of the Day (Regular Market) | 저가 (정규장) | 〃 | 〃 |
| 8 | Highest Price of the Day (Regular Market) | 고가 (정규장) | 〃 | 〃 |
| 16 | Daily Volume of Shares Traded | 일 거래량 | pykrx `get_market_ohlcv_by_date` (volume) | 〃 |
| 17 | Daily Volume of Shares Traded (Regular Market) | 일 거래량 (정규장) | 〃 | 〃 |
| 18 | Average Volume of Shares Traded | 평균 거래량 | pykrx OHLCV → 20일 이동평균 자체 계산 | 〃 |
| 19 | Average Volume of Shares Traded (Last 10 Days) | 평균 거래량 (10일) | 〃 10일 이동평균 | 〃 |
| 20 | Average Daily Volume of Shares Traded (Last 10 Days) | 일평균 거래량 (10일) | 〃 | 〃 |
| 21 | Bid Price | 매수 호가 | pykrx `get_market_price_by_ticker` 또는 KIS OpenAPI 호가 | 실시간 (스코어링 시점) |
| 22 | Ask Price | 매도 호가 | 〃 | 〃 |
| 23 | Bid Size | 매수 호가 잔량 | 〃 | 〃 |
| 24 | Ask Size | 매도 호가 잔량 | 〃 | 〃 |

**수집 구현**: `src/collectors/price_collector.py` — pykrx 일괄 호출, 종목 리스트(KOSPI200+KOSDAQ150) 순회, PostgreSQL `price_data` 테이블 upsert. 장 마감 후 1회 + 스코어링 시점 실시간 호가.

#### B. 배당 (8개) — DART + pykrx

| # | 원문 변수명 | 한국 대응 | 소스/엔드포인트 | 수집 방법 |
|---|------------|-----------|-----------------|-----------|
| 9 | Annual Dividend Rate | 연간 배당금 | DART `getDividendInfo` (배당공시) + pykrx | 연 1~2회 (결산 배당공시 시) |
| 10 | Dividend Yield | 배당수익률 | 배당금 ÷ 현재가 자체 계산 | 주 1회 갱신 |
| 11 | Payout Ratio | 배당성향 | DART 재무제표 (당기순이익, 배당금) → 자체 계산 | 결산 후 갱신 |
| 12 | Five-Year Average Dividend Yield | 5년 평균 배당수익률 | DART 배당공시 5년치 → 자체 계산 | 연 1회 |
| 31 | Annual Dividend Rate (TTM) | 연간 배당금 (TTM) | 최근 4분기 배당금 합산 자체 계산 | 분기별 갱신 |
| 32 | Dividend Yield (TTM) | 배당수익률 (TTM) | TTM 배당금 ÷ 현재가 | 주 1회 |
| 64 | Last Dividend Value | 직전 배당금 | DART 최근 배당공시 | 분기별 |
| 65 | Last Dividend Payment Date | 직전 배당 지급일 | 〃 | 〃 |

**수집 구현**: `src/collectors/dividend_collector.py` — DART Open API `listDividendInfo(corp_code)` → 배당금·지급일 파싱 → `dividend_data` 테이블.

#### C. 밸류에이션 (10개) — pykrx + DART 자체 계산

| # | 원문 변수명 | 한국 대응 | 소스/엔드포인트 | 수집 방법 |
|---|------------|-----------|-----------------|-----------|
| 14 | P/E Ratio (TTM) | PER (TTM) | pykrx `get_market_cap_by_ticker` + DART EPS → 자체 계산, 또는 FnGuide PER | 주 1회 |
| 15 | P/E Ratio (Forward) | 예상 PER | FnGuide 컨센서스 예상 EPS + 현재가 → 자체 계산 | 월 1회 |
| 28 | P/S Ratio (TTM) | PSR | 시총 ÷ 매출 (DART 재무제표) | 결산/분기 갱신 |
| 34 | Enterprise Value | EV | 시총 + 총차입 − 현금 (DART 재무제표) | 분기 갱신 |
| 48 | Book Value per Share | BPS | DART 재무제표 (자본총계 ÷ 발행주식) | 분기 갱신 |
| 49 | P/B Ratio | PBR | 현재가 ÷ BPS 자체 계산 (또는 FnGuide) | 주 1회 |
| 57 | PEG Ratio | PEG | PER ÷ 이익성장률 자체 계산 | 분기 갱신 |
| 60 | EV/Revenue | EV/매출 | EV ÷ 매출 | 분기 갱신 |
| 61 | EV/EBITDA | EV/EBITDA | EV ÷ EBITDA (DART) | 분기 갱신 |
| 92 | PEG Ratio (Trailing) | PEG (Trailing) | PER(TTM) ÷ TTM 이익성장률 | 분기 갱신 |

**수집 구현**: `src/collectors/valuation_collector.py` — pykrx 시총 + DART 재무제표 결합 자체 계산. FnGuide 컨센서스로 보완.

#### D. 모멘텀·기술지표 (7개) — pykrx 자체 계산

| # | 원문 변수명 | 한국 대응 | 소스/엔드포인트 | 수집 방법 |
|---|------------|-----------|-----------------|-----------|
| 13 | Beta | 베타 | pykrx 일별 수익률 vs KOSPI200 수익률 → 250일 회귀 자체 계산 | 주 1회 |
| 26 | 52-Week Low | 52주 저가 | pykrx OHLCV 252거래일 최저가 | 〃 |
| 27 | 52-Week High | 52주 고가 | 〃 최고가 | 〃 |
| 29 | 50-Day Moving Average | 50일 이평 | pykrx OHLCV → 50일 SMA 자체 계산 | 〃 |
| 30 | 200-Day Moving Average | 200일 이평 | 〃 200일 SMA | 〃 |
| 62 | 52-Week Price Change % | 52주 등락률 | (현재가 − 252일 전 종가) ÷ 252일 전 종가 | 〃 |
| 63 | S&P 500 52-Week Change % | KOSPI200 52주 등락률 | pykrx KOSPI200 지수 동일 계산 | 〃 |

**수집 구현**: `src/collectors/momentum_collector.py` — pykrx 시계열 → pandas 계산.

#### E. 공매도·지분구조 (11개) — 금융감독원 + DART

| # | 원문 변수명 | 한국 대응 | 소스/엔드포인트 | 수집 방법 |
|---|------------|-----------|-----------------|-----------|
| 36 | Float Shares | 유통주식수 | 발행주식수 − 최대주주 지분 (DART 최대주주 지분공시) | 분기 갱신 |
| 37 | Shares Outstanding | 발행주식총수 | pykrx `get_market_cap_by_ticker` 또는 DART 발행주식 | 〃 |
| 38 | Shares Sold Short | 공매도 거래량 | 금융감독원 공매도 API (KRX 공매도 데이터) | 일 1회 |
| 39 | Shares Sold Short (Previous Month) | 전월 공매도 거래량 | 〃 월 집계 | 월 1회 |
| 40 | Short Interest Date (Prev Month) | 전월 공매도 기준일 | 〃 | 〃 |
| 41 | Most Recent Short Interest Date | 최근 공매도 기준일 | 〃 | 〃 |
| 42 | Short Interest % of Outstanding | 공매도 비중 (발행주식 대비) | 〃 비율 데이터 | 일 1회 |
| 43 | Insider Holdings % | 최대주주 지분율 | DART `getOwnershipStatus` (최대주주 지분) | 분기 갱신 |
| 44 | Institutional Holdings % | 기관 보유 비중 | DART 기관 보유 지분공시 | 〃 |
| 45 | Short Ratio (Days to Cover) | 숏커버 소요일 | 공매도 잔고 ÷ 일평균 거래량 자체 계산 | 주 1회 |
| 46 | Short Interest % of Float | 공매도 비중 (유통주식 대비) | 〃 ÷ 유통주식수 | 〃 |

**수집 구현**: `src/collectors/short_interest_collector.py` — 금융감독원 공매도 데이터 + DART 지분구조.

#### F. 기업 기본정보 (9개) — pykrx + DART

| # | 원문 변수명 | 한국 대응 | 소스/엔드포인트 | 수집 방법 |
|---|------------|-----------|-----------------|-----------|
| 25 | Market Capitalization | 시가총액 | pykrx `get_market_cap_by_ticker` | 일 1회 |
| 33 | Trading Currency | 거래 통화 | "KRW" 고정 (모든 종목) | 정적 |
| 47 | Implied Shares Outstanding | 시가총액 역산 발행주식 | 시총 ÷ 현재가 자체 계산 | 일 1회 |
| 50 | Last Fiscal Year End Date | 직전 결산일 | DART `getCompanyOverview` (결산일) | 연 1회 |
| 51 | Next Fiscal Year End Date | 차기 결산일 | 〃 | 〃 |
| 52 | Most Recent Quarter Date | 최근 분기 기준일 | DART 분기보고서 공시일 | 분기 갱신 |
| 58 | Last Stock Split Factor | 최근 액면분할 비율 | DART `getStockSplit` (액면변경 공시) | 이벤트 감지 |
| 59 | Last Stock Split Date | 최근 액면분할 일자 | 〃 | 〃 |
| 91 | Financial Currency | 재무 통화 | "KRW" 고정 | 정적 |

#### G. 수익성·현금흐름 (14개) — DART 재무제표

| # | 원문 변수명 | 한국 대응 | 소스/엔드포인트 | 수집 방법 |
|---|------------|-----------|-----------------|-----------|
| 35 | Profit Margins | 순이익률 | DART `fnlttSinglAcntAll` (당기순이익 ÷ 매출) | 분기 갱신 |
| 53 | Earnings Growth (Quarterly) | 분기 이익 성장률 | 전년동분기 대비 자체 계산 | 〃 |
| 54 | Net Income to Common | 지배주주 순이익 | DART 재무제표 (지배기업지분 순이익) | 〃 |
| 55 | EPS (TTM) | EPS (TTM) | TTM 순이익 ÷ 발행주식 자체 계산 | 〃 |
| 56 | EPS (Forward) | 예상 EPS | FnGuide 컨센서스 예상 EPS | 월 1회 |
| 82 | ROA | ROA | 당기순이익 ÷ 자산총계 | 분기 갱신 |
| 83 | ROE | ROE | 당기순이익 ÷ 자본총계 | 〃 |
| 84 | Free Cash Flow | FCF | 영업현금흐름 − 투자현금흐름 | 〃 |
| 85 | Operating Cash Flow | 영업현금흐름 | DART 현금흐름표 | 〃 |
| 86 | Earnings Growth Rate | 이익 성장률 | 3년 CAGR 자체 계산 | 연 갱신 |
| 87 | Revenue Growth Rate | 매출 성장률 | 〃 | 〃 |
| 88 | Gross Profit Margins | 매출총이익률 | (매출 − 매출원가) ÷ 매출 | 분기 갱신 |
| 89 | EBITDA Margins | EBITDA 마진 | EBITDA ÷ 매출 | 〃 |
| 90 | Operating Margins | 영업이익률 | 영업이익 ÷ 매출 | 〃 |

**수집 구현**: `src/collectors/financials_collector.py` — DART `fnlttSinglAcntAll(corp_code, bsns_year)` → 재무제표 계정과목 파싱 → `financial_data` 테이블. 계정과목 매핑 테이블(`account_mapping.json`) 필요.

#### H. 재무안정성 (9개) — DART 재무제표

| # | 원문 변수명 | 한국 대응 | 소스/엔드포인트 | 수집 방법 |
|---|------------|-----------|-----------------|-----------|
| 73 | Total Cash | 현금성자산 | DART 재무제표 (당좌자산 or 현금및현금성자산) | 분기 갱신 |
| 74 | Cash per Share | 주당 현금 | 총현금 ÷ 발행주식 | 〃 |
| 75 | EBITDA | EBITDA | 영업이익 + 감가상각 (DART 현금흐름표 감가상각) | 〃 |
| 76 | Total Debt | 총차입 | 단기차입 + 장기차입 + 사채 (DART) | 〃 |
| 77 | Quick Ratio | 당좌비율 | 당좌자산 ÷ 유동부채 | 〃 |
| 78 | Current Ratio | 유동비율 | 유동자산 ÷ 유동부채 | 〃 |
| 79 | Total Revenue | 매출액 | DART 손익계산서 | 〃 |
| 80 | Debt-to-Equity Ratio | 부채비율 | 부채총계 ÷ 자본총계 | 〃 |
| 81 | Revenue per Share | 주당 매출 | 매출 ÷ 발행주식 | 〃 |

#### I. 애널리스트 (7개) — FnGuide 컨센서스

| # | 원문 변수명 | 한국 대응 | 소스/엔드포인트 | 수집 방법 |
|---|------------|-----------|-----------------|-----------|
| 66 | Highest Target Price | 최고 목표주가 | FnGuide 컨센서스 API | 월 1회 |
| 67 | Lowest Target Price | 최저 목표주가 | 〃 | 〃 |
| 68 | Mean Target Price | 평균 목표주가 | 〃 | 〃 |
| 69 | Median Target Price | 중앙 목표주가 | 〃 (제공 시) 또는 개별 의견 중앙값 | 〃 |
| 70 | Recommendation Mean | 컨센서스 평균 | FnGuide 종목 의견 (매수/중립/매도 → 수치 변환) | 〃 |
| 71 | Recommendation Key | 컨센서스 등급 | 〃 (Strong Buy/Buy/Hold/Sell) | 〃 |
| 72 | Number of Analyst Opinions | 컨센서스 의견 수 | 〃 | 〃 |

**수집 구현**: `src/collectors/consensus_collector.py` — FnGuide API (또는 유료 컨센서스: 한국경제·FnGuide Pro). 무료 FnGuide는 주요 종목만 커버하므로 커버리지 플래그 관리 필요.

#### J. 지배구조 리스크 (5개) — DART 자체 산출

| # | 원문 변수명 | 한국 대응 | 소스/엔드포인트 | 수집 방법 |
|---|------------|-----------|-----------------|-----------|
| 93 | Audit Risk Level | 감사 리스크 | DART 감사보고서 감사의견 (적정의견=저위험, 한정=고위험) → 1~5등급 자체 산출 | 연 1회 (결산 감사보고서) |
| 94 | Board Risk Level | 이사회 리스크 | DART 이사회 구성 공시 (사외이사 비율, 이사회 규모) → 자체 점수화 | 연 1회 |
| 95 | Compensation Risk Level | 보상 리스크 | DART 이사 보수 공시 (보수 총액, 주주총회 승인 여부) → 자체 점수화 | 연 1회 |
| 96 | Shareholder Rights Risk | 주주권 리스크 | DART 정관·주주총회 공시 (의결권 제한, 우선주 발행 여부) → 자체 점수화 | 연 1회 |
| 97 | Overall Risk Level | 종합 리스크 | 93~96 가중 평균 자체 산출 | 연 1회 |

**수집 구현**: `src/collectors/governance_collector.py` — DART 감사보고서·이사회·보수·정관 공시 파싱 → 자체 1~5 리스크 등급 산출. 원본(Yahoo Finance governance)의 직접 대응이 없어 **한국형 자체 산출 설계**임을 명시.

### 4.3 수집 인프라 설계

```
src/collectors/
├── price_collector.py        # A: 시세·호가 (pykrx)
├── dividend_collector.py     # B: 배당 (DART)
├── valuation_collector.py    # C: 밸류에이션 (pykrx+DART+FnGuide)
├── momentum_collector.py     # D: 모멘텀 (pykrx 자체 계산)
├── short_interest_collector.py # E: 공매도·지분구조 (금감원+DART)
├── info_collector.py         # F: 기업 기본정보 (pykrx+DART)
├── financials_collector.py   # G: 수익성 (DART 재무제표)
├── balance_sheet_collector.py # H: 재무안정 (DART)
├── consensus_collector.py    # I: 애널리스트 (FnGuide)
├── governance_collector.py   # J: 지배구조 리스크 (DART 자체 산출)
└── news_collector.py         # 뉴스 헤드라인 (네이버 금융)
```

**스케줄러 (Celery Beat)**:
- 일 1회 (장 마감 후 18:00): price, momentum, short_interest 갱신
- 주 1회 (일요일): valuation, dividend 갱신
- 분기 갱신 (결산 공시 감지 시): financials, balance_sheet, governance
- 월 1회: consensus
- 수집 실패 시: 재시도 3회 → 알림 → 직전 스냅샷 사용 (데이터 신선도 플래그 기록)

**DB 스키마 (PostgreSQL)**:
```sql
price_data (ticker, date, open, high, low, close, volume, market_cap, updated_at)
financial_data (ticker, quarter, revenue, net_income, ebitda, total_debt, ..., updated_at)
dividend_data (ticker, ex_date, pay_date, dividend_amount, updated_at)
short_interest (ticker, date, short_volume, short_balance, short_ratio, updated_at)
consensus_data (ticker, date, target_high, target_low, target_mean, rec_mean, n_analysts, updated_at)
governance_data (ticker, year, audit_risk, board_risk, comp_risk, shareholder_risk, overall_risk, updated_at)
screening_results (date, ticker, score, score_reasoning, stage, updated_at)
scenario_models (date, ticker, bull_prob, base_prob, bear_prob, target_1m, target_3m, target_6m, target_12m, expected_return, updated_at)
portfolio_positions (date, ticker, weight, thesis, edge, risk, updated_at)
audit_log (timestamp, agent, prompt_rendered, response, compliance_check, updated_at)
```

---

## 5. 포트폴리오 구성 규칙 (Claude 실측 + 한국형 보완)

| 규칙 | 값 | 근거 |
|------|-----|------|
| 포지션 수 | 15개 (주식 13~14 + 현금성 ETF 1~2) | Claude Portfolio 실측 "~15 positions" |
| 종목당 최대 비중 | 10% (자체 보완) | 실측은 "position-size limits"만 명시, 수치 미공개 |
| **섹터당 최대 비중** | **35%** | Claude Portfolio 실측 명시 "no sector >35%" |
| 기대수익 조건 | 모든 포지션 기대수익 > 0 | 실측 명시 |
| 벤치마크 | KOSPI200 대비 초과 수익 (SPY→KOSPI200) | 실측 "beat SPY" |
| 회전율 상한 | 자체 설정 (예: 월 30%) | 실측 "turnover ceiling" 수치 미공개 |
| 동점 처리 | 시총 큰 종목 | GPT 화이트페이퍼 규칙 |
| 현금성 ETF | MMF/초단기채 허용, 최대 20% | Claude BOXX 보유 패턴 |
| 리밸런싱 | 주 2회 파이프라인 + 이벤트 트리거 | Claude 실측 "twice a week, trades less often" |

---

## 6. 모델 라우팅·비용 (Claude Code 실측 반영)

| 단계 | 모델 | 실측 근거 |
|------|------|-----------|
| 오케스트레이터 (마스터) | Claude Opus 4.6 (1M context) | 실측: Claude Code v2.1.87, Opus 4.6 |
| Stage 1 스크리닝 | Claude Sonnet (병렬 종목당 호출) | 비용 최적화 |
| Stage 2 적대적 리서치 | Claude Sonnet × 30 에이전트 (페르소나별) | 15 불 + 15 베어 |
| Stage 3 시나리오 모델링 | Claude Sonnet × 15 에이전트 | 〃 |
| Stage 4 포트폴리오 구성 | Claude Opus (옵티마이저, "Agent of agents") | 실측 |
| 일일 DART 모니터링 | Claude Haiku (1차 분류) → Opus (high 재분석) | 비용 최적화 |
| 거래 실행 | 마스터 오케스트레이터가 승인 후 한투 OpenAPI 실행 | 실측: 인간이 파라미터 설정, Claude는 리서치 |

**초기 프롬프트 (실측 원문)**: "You are managing a $50,000 portfolio. Beat the S&P 500. You in?" — 한국형: "당신은 5,000만 원 포트폴리오를 관리합니다. KOSPI200을 이기세요. 시작할까요?"

---

## 7. 리스크 관리 레이어

1. **포트폴리오**: MDD -15% 시 전체 재검토, 현금성 비중 상향
2. **종목**: -25% 시 thesis 재평가 플래그
3. **실행 전 게이트**: risk_flags 확인, KOREA_REGULATION 게이트
4. **유동성**: 20일 평균 거래대금 하위 제외, 일 목표 거래대금 5% 초과 주문 분할
5. **섹터 상한 35% 하드 제약** (Stage 4 옵티마이저 이중 강제)
6. **회전율 상한** (월 30% 등 자체 설정)

---

## 8. 상태·메모리·감사

1. **투자 논지(thesis) 저널**: 모든 보유 종목의 진입 근거·목표·리스크 저장 — Claude 실측 "written investment thesis that can be proven wrong" 요구
2. **에이전트 메모리**: MEMORY 토글, 최근 10개 노트
3. **렌더링된 프롬프트 전문 로깅**: `audit_log` 테이블
4. **sha256 페르소나 해시 고정**: 무결성 테스트
5. **성과 시계열**: 일별 NAV·포지션 스냅샷

---

## 9. 구조화 출력 스키마

```json
{
  "valuation_reasoning": "기업가치 수치 분석",
  "valuation": 28.0,
  "price_prediction_reasoning": "가격 예측 근거",
  "price_prediction_t": "이번 라운드 예측",
  "price_prediction_t1": "다음 라운드 예측",
  "price_prediction_t2": "다다음 라운드 예측",
  "stock_score": 78,
  "score_reasoning": "97변수 스코어링 근거",
  "bull_case": "불 케이스 요약 (Stage 2)",
  "bear_case": "베어 케이스 요약 (Stage 2)",
  "scenario": {
    "bull_prob": 0.3, "base_prob": 0.5, "bear_prob": 0.2,
    "target_1m": 31000, "target_3m": 32000, "target_6m": 34000, "target_12m": 38000,
    "expected_return": 0.15
  },
  "orders": [{"decision": "Buy|Sell", "ticker": "005930", "quantity": 1000, "order_type": "market|limit", "price_limit": 29500}],
  "replace_decision": "Add|Cancel|Replace",
  "reasoning": "매매 결정 설명",
  "risk_flags": ["공매도_규제_확인필요"],
  "compliance_check": {"loss_guarantee_mention": false, "individual_recommendation": false, "disclaimer_included": true}
}
```

---

## 10. 기능 토글

MEMORY, SOCIAL, LAST_REASONING, SELF_MODIFY(운영 금지), CONFIDENCE, SECOND_ORDER, KOREA_REGULATION — [v1.1 §10과 동일, prompts_kr/README.md 참조]

---

## 11. 가드레일 (유사투자자문업)

모든 페르소나에 공통 고지 주입 (23개 파일 구현 완료). 자기 계정 자동화는 규제 부담 없이 합법 범위. 서비스화는 유사투자자문업 등록 전제.

---

## 12. 데이터 레이어 한국 대응 (§4 상세와 연동)

| 원본 | 한국 대응 | 상태 |
|------|-----------|------|
| Yahoo Finance (97변수) | pykrx + DART + 금감원 + FnGuide | §4.2 전수 매핑 완료 |
| stocknewsapi.com | 네이버 금융 뉴스 API | 구현 가능 |
| Wikipedia 현재사건 | 한국 뉴스 종합 + DART | 구현 가능 |
| Alpaca 페이퍼 트레이딩 | 한국투자증권 모의투자 OpenAPI | 구현 가능 |
| FRED 매크로 | 한국은행 경제통계 API | 구현 가능 |

---

## 13. 운영 케이던스 (Claude 실측 기준)

| 주기 | 작업 | 단계 |
|------|------|------|
| 주 2회 | 전체 파이프라인 (Stage 1~5) | Claude 실측 "twice a week" |
| 일 1회 | 데이터 수집 (시세·공매도·모멘텀) | §4.3 스케줄러 |
| 일 1회 (실시간) | DART 주요사항보고 모니터링 | Haiku 1차 분류 |
| 월 1회 | 컨센서스 갱신, 매크로 예측 | S1 매크로 모듈 |
| 실시간 (분석 전용) | 커뮤니티 감성 레이더 | 페르소나 16·17·20 |

---

## 14. 평가·백테스트 프레임워크

[v1.1 §14와 동일 — 통제군(01_default, 04_minimal, 05_hold_llm), 룩어헤드 바이어스 차단, CAGR/Sharpe/MDD/회전율, 강건성 스윕(paraphrase×4, 프레이밍, 목표함수, 격자)]

---

## 15. 실패 처리·운영 안정성

[v1.1 §15와 동일 — API 재시도 3회→폴백→스킵, 데이터 소스 장애 시 직전 스냅샷+신선도 플래그, 회로 차단기, 미체결 3거래일 후 취소]

---

## 16. 구현 로드맵

| 단계 | 기간 | 내용 |
|------|------|------|
| **P0 기술 검증** | ~2주 | §4 콜렉터 10종 구현 + pykrx/DART 연동 테스트 + 97개 변수 수집 검증 |
| **P1 스크리닝 파이프라인** | 1개월 | Stage 1 구현 (97변수 스코어링 → 상위 50종목), SQLite |
| **P2 적대적 리서치+시나리오** | 1~2개월 | Stage 2~3 구현 (30+15 에이전트), 페르소나 프롬프트 연동 |
| **P3 포트폴리오+리밸런싱** | 1개월 | Stage 4~5 구현 (옵티마이저, 제약, 스왑), 한투 모의투자 실행 |
| **P4 자율형 전환** | 이후 | Claude Code 에이전트 자율화, 실전 전환, 서비스화 검토 |

---

## 17. 구현 가이드 — Claude Code / Codex용 (v1.3 신설)

### 17.1 프로젝트 구조

```
autopilot-korea/
├── CLAUDE.md / AGENTS.md          # 이 문서의 핵심 규칙 요약 (에이전트가 먼저 읽음)
├── README.md
├── pyproject.toml                 # Python 3.11+, 의존성: pykrx, dart-fss, FinanceDataReader, anthropic, openai, celery, redis, sqlalchemy, pydantic
├── .env.example                   # DART_API_KEY, ANTHROPIC_API_KEY, KIS_APP_KEY, KIS_APP_SECRET, DB_URL
├── src/
│   ├── collectors/                # §4.3 — 10개 콜렉터
│   ├── agents/
│   │   ├── prompts/prompts_kr/   # 23개 페르소나 .md (이 프로젝트 저장소에서 복사)
│   │   ├── orchestrator.py       # §3.3 5단계 마스터 오케스트레이터
│   │   ├── screener.py           # Stage 1
│   │   ├── adversarial.py        # Stage 2 (30 에이전트 관리)
│   │   ├── scenario.py           # Stage 3 (15 에이전트)
│   │   ├── optimizer.py          # Stage 4 (포트폴리오 구성)
│   │   └── rebalancer.py         # Stage 5
│   ├── compliance/               # 가드레일 필터, 고지 삽입, compliance_check 검증
│   ├── execution/                # 한투 OpenAPI 주문 실행
│   ├── db/                       # SQLAlchemy 모델 (§4.3 스키마)
│   └── scheduler/                # Celery Beat 스케줄 (§4.3, §13)
├── tests/
│   ├── test_persona_sha256.py   # 페르소나 해시 무결성
│   ├── test_collectors.py       # 콜렉터 단위 테스트
│   └── test_pipeline.py         # 5단계 통합 테스트
└── config/
    ├── personas.yaml             # 페르소나 활성화/비활성화, 패널 구성
    ├── constraints.yaml         # §5 제약 (섹터 35%, 종목 10% 등)
    └── schedule.yaml            # 케이던스 (주 2회 등)
```

### 17.2 핵심 구현 명세

**Stage 1 스크리닝 (screener.py)**:
```python
# 입력: universe (KOSPI200+KOSDAQ150 종목 리스트)
# 97개 변수 팩트시트 생성 → Claude Sonnet 종목당 1회 호출
# 프롬프트: prompts_kr/ 기반 + 97개 변수 JSON + 최근 7일 뉴스 헤드라인
# 출력: 종목당 stock_score (1~100) + score_reasoning
# 상위 50종목 선발 → screening_results 테이블
```

**Stage 2 적대적 리서치 (adversarial.py)**:
```python
# 30 에이전트 = 15 불 (페르소나: 낙관론자18 + 가치07 + 모멘텀08 ...)
#             + 15 베어 (페르소나: 비관론자19 + 역발상09 + 공매도14 ...)
# 각 에이전트: 페르소나 프롬프트 + 웹검색 도구(네이버 뉴스, DART) + 최근 7일 제한
# 종목당 bull_case / bear_case 집계 (중앙값)
```

**Stage 4 옵티마이저 (optimizer.py)**:
```python
# 입력: Stage 1~3 결과 + 현재 포트폴리오 + constraints.yaml
# 제약 (프롬프트 + 코드 이중 검증):
#   - 섹터 비중 ≤ 35%
#   - 모든 포지션 expected_return > 0
#   - 종목 비중 ≤ 10%
#   - 15 포지션, 현금성 ETF ≤ 20%
#   - KOSPI200 대비 초과 수익 목표
# 출력: 목표 포지션 + thesis + 주문 목록
# 제약 위반 시 최대 3회 재생성
```

**컴플라이언스 필터 (compliance/)**:
```python
# 모든 LLM 출력에 대해:
#   1. 금지 표현 검색 (손실보전, 이익보장, 확정 수익, 개별 권유)
#   2. 고지 문구 자동 삽입: "본 정보는 참고용이며 개별 투자 권유가 아닙니다."
#   3. compliance_check 필드 검증
#   4. 위반 시 해당 출력 차단 + 재생성
```

### 17.3 CLAUDE.md / AGENTS.md 핵심 내용 (구현 에이전트가 읽을 파일)

```markdown
# Autopilot Korea — 한국형 투자 에이전트

## 프로젝트 개요
Claude Portfolio(Autopilot, Dr. Lopez-Lira)의 공개 오케스트레이션 5단계를
한국 시장(KOSPI200+KOSDAQ150)으로 구현.

## 핵심 규칙
1. 페르소나는 prompts_kr/의 .md 파일 (데이터 파일, 코드 아님). sha256 해시로 무결성 검증.
2. 모든 LLM 호출: 온도 0, 시드 42, 구조화 JSON 출력 강제.
3. 모든 출력에 컴플라이언스 필터 통과 (금지 표현 검열 + 고지 삽입).
4. 섹터 상한 35%, 종목 상한 10%, 15 포지션 — 코드+프롬프트 이중 강제.
5. 최근 7일 뉴스만 유효 정보 (그 이전은 가격 반영 간주).
6. 데이터 수집은 src/collectors/의 10개 콜렉터가 담당 (§4.3 스키마 참조).
7. 유사투자자문업 가드레일: 손실보전/이익보장/개별 권유 금지 문구 모든 출력에 포함.

## 5단계 파이프라인 (주 2회 실행)
Stage 1 스크리닝 → Stage 2 적대적 리서치(30 에이전트) → Stage 3 시나리오(15 에이전트)
→ Stage 4 포트폴리오 구성(옵티마이저) → Stage 5 리밸런싱(마스터 오케스트레이터)

## 데이터 (97개 변수)
pykrx(시세), DART(재무·공시), 금감원(공매도), FnGuide(컨센서스), 한은(매크로).
상세 매핑은 설계안 §4.2 참조.
```

---

## 18. 완성도 자가진단 (v1.3)

| 구성 요소 | v1.2 | v1.3 |
|-----------|------|------|
| 97개 변수 원문 명세 | 완료 | 완료 (§4.2 전수 매핑표) |
| **97개 변수 한국 데이터 수집 설계** | 카테고리 요약만 | **완료 — 변수별 소스·엔드포인트·수집 주기·구현 파일 명시** |
| **Claude Portfolio 오케스트레이션** | 자체 설계(비공개 간주) | **완료 — 실측 5단계 명세(공개 자료) 반영** |
| 프롬프트 원문 | 완료 | 완료 (화이트페이퍼 원문 + 실측 초기 프롬프트) |
| 포트폴리오 규칙 | 완료 | 완료 + 실측 제약(섹터 35%, 기대수익 양수, SPY 박치기) |
| 구현 가이드 (Claude Code/Codex용) | 미비 | **신설 (§17: 프로젝트 구조, 구현 명세, CLAUDE.md)** |
| 컴플라이언스·가드레일 | 완료 | 완료 |
| 데이터 레이어 | 완료 | 완료 (§4.2 상세화) |

**결론**: v1.3으로 Claude Code/Codex가 본 문서만으로 ① 데이터 콜렉터 10종, ② 5단계 오케스트레이터, ③ 23개 페르소나 연동, ④ 컴플라이언스 필터, ⑤ 한투 실행 엔진을 구현하는 데 필요한 명세가 모두 갖춰졌습니다.

---

## 19. 출처

1. [Chris Josephs(Autopilot CEO) LinkedIn — Claude 5단계 오케스트레이션 공개](https://www.linkedin.com/posts/chris-josephs-677641114_the-claude-autonomous-agents-have-officially-activity-7445481891756986368-IN6d)
2. [Luca D. LinkedIn — Claude Agentic Portfolio 5단계 상세](https://www.linkedin.com/posts/deangelisluca1_the-50000-claude-portfolio-is-live-trading-activity-7445088500225400835-FHjB)
3. [The Claude Portfolio Philosophy Methodology (Scribd)](https://www.scribd.com/document/1063238622/The-Claude-Portfolio-Philosophy-Methodology)
4. [Autopilot Claude Portfolio 공식 페이지](https://start.joinautopilot.com/strategies/claude-portfolio-950048) — "orchestrated multi-agent" 공식 선언
5. [ai-street — How AI Runs $200 Million in Portfolios (Lopez-Lira 인터뷰)](https://www.ai-street.co/p/how-ai-runs-200-million-in-portfolios) — Claude의 반복적·검증형 프로세스, 주 2회 거래 상한
6. [explainx.ai — Claude Portfolio 분석](https://www.explainx.ai/blog/claude-portfolio-autopilot-ai-trading-experiment-august-2026) — Claude Code Opus 4.6, 초기 프롬프트 원문, 인간 감독 구조
7. [The GPT Portfolio White Paper (Google Docs 원문)](https://docs.google.com/document/d/1BMCAyP6f4RYk5oPuB-2CXRBxRe5uyz3CIavX311E5K4/edit) — 97개 변수(Exhibit 2B), 프롬프트(Exhibit 1)
8. [The DeepSeek Portfolio White Paper V2](https://docs.google.com/document/d/1RYo6BL4osdbQch-s6HS-lnhzb7OxAX2v7ffWighW17M/edit) — 매크로 프롬프트(2D), 배분 프롬프트(2E)
9. [Autopilot Journal — AI model portfolios](https://start.joinautopilot.com/journal/ai-model-portfolios) — Claude Opus/Fable, 15~20종, 자율 거래
10. 본 프로젝트: [GPT_Portfolio_화이트페이퍼_97변수_전수조사.md](GPT_Portfolio_화이트페이퍼_97변수_전수조사.md), [whitepaper/ 원문보존본](whitepaper/GPT_Portfolio_WhitePaper_원문보존.md), [prompts_kr/](prompts_kr/README.md)
