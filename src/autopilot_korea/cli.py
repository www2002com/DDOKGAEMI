"""명령줄 진입점: `autopilot <명령>`.

운영 흐름(주 2회): autopilot collect → autopilot run → (보고서 확인) → autopilot orders approve --run <ID> --by <이름>
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from typing import Any

from sqlalchemy import select


def _orch(args: argparse.Namespace):
    from autopilot_korea.agents.llm import AnthropicLLM
    from autopilot_korea.agents.orchestrator import Orchestrator
    from autopilot_korea.db import Database
    from autopilot_korea.settings import get_settings

    settings = get_settings()
    db = Database(settings.db_url)
    db.create_all()
    llm = AnthropicLLM(settings, db, replay=getattr(args, "replay", False))
    return Orchestrator(settings, db, llm=llm)


def _date(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_init_db(args):
    from autopilot_korea.db import Database
    from autopilot_korea.settings import get_settings

    Database(get_settings().db_url).create_all()
    print("DB 테이블을 만들었습니다.")


def cmd_personas(args):
    from autopilot_korea.agents.personas import PersonaLibrary
    from autopilot_korea.settings import get_settings

    lib = PersonaLibrary.from_settings(get_settings())
    if args.action == "rehash":
        print(f"매니페스트를 갱신했습니다: {lib.rehash()}")
        return
    problems = lib.verify()
    print("페르소나 23종 무결성 정상" if not problems else "\n".join(problems))
    sys.exit(1 if problems else 0)


def cmd_variables(args):
    from autopilot_korea.variables import CATEGORIES, VARIABLES

    for v in sorted(VARIABLES, key=lambda v: v.no):
        if not args.category or v.category == args.category:
            print(f"#{v.no:>2} [{v.category} {CATEGORIES[v.category]}] {v.name_kr} ({v.name_en}) — {v.source} / {v.frequency}")


def cmd_universe(args):
    from autopilot_korea.service import refresh_universe

    tickers = refresh_universe(_orch(args), _date(args.as_of))
    print(f"유니버스 {len(tickers)}종목을 갱신했습니다.")


def cmd_collect(args):
    from autopilot_korea.service import collect

    _print(collect(_orch(args), args.jobs.split(","), args.tickers.split(",") if args.tickers else None, _date(args.as_of)))


def cmd_run(args):
    orch = _orch(args)
    summary = orch.run_pipeline(as_of=_date(args.as_of), trigger="manual",
                                universe=args.tickers.split(",") if args.tickers else None)
    _print({k: summary.get(k) for k in ("run_id", "stage1", "stage2", "stage3", "stage4", "stage5", "report")})
    if summary["stage5"].get("awaiting_human_approval"):
        print(f"\n승인 대기 주문이 있습니다. 보고서를 확인한 뒤: autopilot orders approve --run {summary['run_id']} --by <이름>")


def cmd_rescore(args):
    r = _orch(args).rescore(args.ticker, _date(args.as_of), trigger="manual")
    _print({"ticker": r.ticker, "score": r.score, "reasoning": r.reasoning})


def cmd_simple(method: str):
    def run(args):
        _print(getattr(_orch(args), method)())
    return run


def cmd_orders(args):
    from autopilot_korea.db.models import Order

    orch = _orch(args)
    if args.action == "list":
        with orch.db.session() as s:
            q = select(Order).order_by(Order.id.desc()).limit(args.limit)
            if args.status:
                q = q.where(Order.status == args.status)
            if args.run:
                q = q.where(Order.run_id == args.run)
            for o in s.scalars(q):
                flag = " [사람 검토 필요]" if o.needs_review else ""
                print(f"{o.id:>5} {o.run_id} {o.ticker} {o.side} {o.quantity}주 @{o.price_limit or '시장가'} "
                      f"{o.status}{flag} {o.error or ''}")
        return
    if args.action in ("approve", "reject"):
        if not args.by:
            sys.exit("--by <승인자 이름>이 필요합니다 (감사 기록).")
        ids = [int(x) for x in args.ids]
        if args.run:
            with orch.db.session() as s:
                ids += list(s.scalars(select(Order.id).where(Order.run_id == args.run, Order.status == "proposed")).all())
        fn = orch.orders.approve if args.action == "approve" else orch.orders.reject
        print(f"{fn(ids, args.by)}건 {'승인' if args.action == 'approve' else '거절'}")
        if args.action == "approve" and orch.settings.mode != "dry_run":
            _print(orch.sync_orders())
        return
    if args.action == "sync":
        _print(orch.sync_orders())


def cmd_halt(args):
    """비상 정지: 자동 승인과 주문 제출을 즉시 멈춘다. 이미 낸 주문은 취소하지 않는다."""
    from autopilot_korea.settings import get_settings

    path = get_settings().halt_file
    if args.action == "halt":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{date.today().isoformat()} {args.reason or ''}\n", encoding="utf-8")
        print(f"비상 정지를 켰습니다 ({path}). 자동 승인과 주문 제출이 멈춥니다. 다시 켜려면: autopilot resume")
        print("이미 제출된 미체결 주문은 그대로입니다. 필요하면 토스증권 앱에서 취소하십시오.")
    else:
        path.unlink(missing_ok=True)
        print("비상 정지를 해제했습니다. 다음 스케줄부터 자동 매매가 다시 동작합니다.")


def cmd_backtest(args):
    from autopilot_korea.backtest import BacktestConfig, PriceBook, run_backtest
    from autopilot_korea.backtest.strategies import PipelineStrategy, control_groups, estimate_llm_calls, sweep_strategies

    orch = _orch(args)
    cfg = BacktestConfig(start=_date(args.start), end=_date(args.end), model_knowledge_cutoff=_date(args.cutoff))
    strategies = []
    if args.controls:
        strategies += control_groups(orch)
    if args.sweep:
        strategies += sweep_strategies(orch, framings=("excess", "risk_adjusted"), objectives=("score", "score_x_confidence"),
                                       grid_top_n=(10, 15, 20))
    if args.pipeline:
        strategies.append(PipelineStrategy(orch))
    prices = PriceBook(orch.db, cfg.start, cfg.end)
    rebalances = sum(1 for d in prices.dates[1:] if d.weekday() in cfg.rebalance_weekdays)
    calls = estimate_llm_calls(len([s for s in strategies if not isinstance(s, PipelineStrategy)]), rebalances,
                               len(orch.universe(cfg.start)))
    print(f"전략 {len(strategies)}개, 리밸런싱 {rebalances}회, Stage 1 기준 예상 LLM 호출 약 {calls:,}회"
          + (" (+ 전체 파이프라인 전략은 회당 수천 회)" if args.pipeline else ""))
    if args.estimate_only:
        return
    for st in strategies:
        res = run_backtest(prices, st, cfg)
        _print({"strategy": res.strategy, **{k: round(v, 4) for k, v in res.metrics.items()}})


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="autopilot", description="한국형 투자 에이전트 (설계안 v1.3)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="DB 테이블 생성").set_defaults(fn=cmd_init_db)
    sp = sub.add_parser("personas", help="페르소나 sha256 무결성 검증·갱신")
    sp.add_argument("action", choices=["verify", "rehash"])
    sp.set_defaults(fn=cmd_personas)
    sp = sub.add_parser("variables", help="97개 변수 레지스트리 출력")
    sp.add_argument("--category")
    sp.set_defaults(fn=cmd_variables)
    sp = sub.add_parser("universe", help="KOSPI200+KOSDAQ150 유니버스 갱신")
    sp.add_argument("--as-of")
    sp.set_defaults(fn=cmd_universe)
    sp = sub.add_parser("collect", help="데이터 수집 (§4.3 콜렉터)")
    sp.add_argument("--jobs", default="price,momentum,short_interest,info,news,valuation,dividend,financials,balance_sheet,consensus,governance,macro")
    sp.add_argument("--tickers")
    sp.add_argument("--as-of")
    sp.set_defaults(fn=cmd_collect)
    sp = sub.add_parser("run", help="5단계 파이프라인 1회 실행")
    sp.add_argument("--as-of")
    sp.add_argument("--tickers", help="유니버스를 이 종목들로 제한 (시험용)")
    sp.add_argument("--replay", action="store_true", help="같은 입력이면 저장된 LLM 응답을 재사용 (재현)")
    sp.set_defaults(fn=cmd_run)
    sp = sub.add_parser("rescore", help="한 종목 즉시 재스코어링")
    sp.add_argument("ticker")
    sp.add_argument("--as-of")
    sp.set_defaults(fn=cmd_rescore)
    for name, method, help_ in (("dart-monitor", "run_dart_monitor", "DART 주요사항보고 모니터링"),
                                ("radar", "run_sentiment_radar", "감성 레이더 (분석 전용)"),
                                ("macro", "run_macro", "월간 매크로 전망"), ("nav", "snapshot_nav", "NAV 스냅샷")):
        sub.add_parser(name, help=help_).set_defaults(fn=cmd_simple(method))
    sp = sub.add_parser("orders", help="주문 목록·승인·거절·동기화")
    sp.add_argument("action", choices=["list", "approve", "reject", "sync"])
    sp.add_argument("ids", nargs="*")
    sp.add_argument("--run")
    sp.add_argument("--status")
    sp.add_argument("--by", help="승인자 이름 (감사 기록)")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(fn=cmd_orders)
    sp = sub.add_parser("halt", help="비상 정지 — 자동 승인·주문 제출 중단")
    sp.add_argument("--reason")
    sp.set_defaults(fn=cmd_halt, action="halt")
    sub.add_parser("resume", help="비상 정지 해제").set_defaults(fn=cmd_halt, action="resume", reason=None)
    sp = sub.add_parser("backtest", help="백테스트 (§14)")
    sp.add_argument("--start", required=True)
    sp.add_argument("--end", required=True)
    sp.add_argument("--cutoff", help="모델 지식 마감일 (이전 구간은 거부)")
    sp.add_argument("--controls", action="store_true", help="통제군 01_default·04_minimal·05_hold_llm")
    sp.add_argument("--sweep", action="store_true", help="강건성 스윕 (paraphrase×4·프레이밍·목표함수·격자)")
    sp.add_argument("--pipeline", action="store_true", help="전체 5단계 파이프라인")
    sp.add_argument("--estimate-only", action="store_true", help="LLM 호출 수만 추정")
    sp.add_argument("--replay", action="store_true")
    sp.set_defaults(fn=cmd_backtest)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
