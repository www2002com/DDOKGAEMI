"""schedule.yaml → Celery Beat 스케줄 (§4.3 스케줄러, §13 운영 케이던스).

build_beat_schedule는 순수 함수다(Celery 없이 테스트 가능). crontab 생성기는 주입받는다.
"""

from __future__ import annotations

from typing import Any, Callable

DAY = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 0}  # Celery crontab: 0=일요일


def _days(days: list[str]) -> str:
    return ",".join(str(DAY[d]) for d in days)


def _hm(text: str) -> tuple[str, str]:
    h, m = text.split(":")
    return str(int(h)), str(int(m))


def build_beat_schedule(cfg: dict[str, Any], crontab: Callable[..., Any]) -> dict[str, dict[str, Any]]:
    sched: dict[str, dict[str, Any]] = {}

    def add(name: str, task: str, cron: Any, kwargs: dict | None = None) -> None:
        sched[name] = {"task": task, "schedule": cron, "kwargs": kwargs or {}}

    run = cfg["pipeline"]["full_run"]
    h, m = _hm(run["time"])
    add("pipeline-full-run", "autopilot.run_pipeline", crontab(minute=m, hour=h, day_of_week=_days(run["days"])))

    c = cfg["collectors"]
    for key in ("daily_after_close", "weekly"):
        h, m = _hm(c[key]["time"])
        add(f"collect-{key}", "autopilot.collect", crontab(minute=m, hour=h, day_of_week=_days(c[key]["days"])),
            {"jobs": c[key]["jobs"]})
    q = c["quarterly"]
    h, m = _hm(q["time"])
    # 정기보고서 감지: 매일 저녁 새 정기보고서가 있는 종목만 재무·지배구조를 수집한다.
    add("collect-quarterly-on-filing", "autopilot.collect_on_filing", crontab(minute=m, hour=h, day_of_week="1-5"),
        {"jobs": q["jobs"]})
    add("collect-quarterly-backstop", "autopilot.collect",
        crontab(minute=m, hour=h, day_of_month=str(q["backstop_day"]), month_of_year=",".join(map(str, q["backstop_months"]))),
        {"jobs": q["jobs"]})
    mo = c["monthly"]
    h, m = _hm(mo["time"])
    add("collect-monthly", "autopilot.collect", crontab(minute=m, hour=h, day_of_month=str(mo["day_of_month"])),
        {"jobs": mo["jobs"]})
    if "macro" in mo["jobs"]:
        # 매크로 통계 수집 1시간 뒤 월간 매크로 전망(LLM)을 만든다.
        add("macro-outlook", "autopilot.run_macro", crontab(minute=m, hour=str(int(h) + 1), day_of_month=str(mo["day_of_month"])))

    mon = cfg["monitors"]
    d = mon["dart_major_reports"]
    add("dart-monitor", "autopilot.dart_monitor",
        crontab(minute=f"*/{d['every_minutes']}", hour=d["hours"], day_of_week=_days(d["days"])))
    for key, task in (("sentiment_radar", "autopilot.sentiment_radar"), ("stale_orders", "autopilot.sync_orders"),
                      ("nav_snapshot", "autopilot.snapshot_nav")):
        h, m = _hm(mon[key]["time"])
        add(key.replace("_", "-"), task, crontab(minute=m, hour=h, day_of_week=_days(mon[key]["days"])))
    return sched
