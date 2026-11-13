"""Celery 앱과 작업 정의. 실행: `celery -A autopilot_korea.scheduler.celery_app worker -B -l info`

모든 작업은 Orchestrator 메서드를 부른다. 데이터 수집 작업은 §15에 따라 실패 시 재시도 3회 →
직전 스냅샷 폴백(신선도 플래그) → 스킵 처리를 collectors.base.run_job이 맡는다.
"""

from __future__ import annotations

from functools import lru_cache

from celery import Celery
from celery.schedules import crontab

from autopilot_korea.scheduler.beat import build_beat_schedule
from autopilot_korea.settings import get_settings

settings = get_settings()
app = Celery("autopilot_korea", broker=settings.celery_broker_url)
app.conf.update(timezone=settings.schedule.get("timezone", "Asia/Seoul"), enable_utc=False,
                beat_schedule=build_beat_schedule(settings.schedule, crontab),
                task_acks_late=True, worker_prefetch_multiplier=1)


@lru_cache(maxsize=1)
def orchestrator():
    from autopilot_korea.agents.orchestrator import Orchestrator
    from autopilot_korea.db import Database

    return Orchestrator(settings, Database(settings.db_url))


@app.task(name="autopilot.run_pipeline")
def run_pipeline():
    return orchestrator().run_pipeline(trigger="schedule")


@app.task(name="autopilot.collect")
def collect(jobs: list[str]):
    from autopilot_korea.service import collect as do_collect

    return do_collect(orchestrator(), jobs)


@app.task(name="autopilot.collect_on_filing")
def collect_on_filing(jobs: list[str]):
    from autopilot_korea.service import collect_on_new_filings

    return collect_on_new_filings(orchestrator(), jobs)


@app.task(name="autopilot.run_macro")
def run_macro():
    return orchestrator().run_macro()


@app.task(name="autopilot.dart_monitor")
def dart_monitor():
    return orchestrator().run_dart_monitor()


@app.task(name="autopilot.sentiment_radar")
def sentiment_radar():
    return orchestrator().run_sentiment_radar()


@app.task(name="autopilot.sync_orders")
def sync_orders():
    return orchestrator().sync_orders()


@app.task(name="autopilot.snapshot_nav")
def snapshot_nav():
    return orchestrator().snapshot_nav()
