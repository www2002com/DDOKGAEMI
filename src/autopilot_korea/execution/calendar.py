"""KRX 거래일 계산. 주말과 schedule.yaml의 holidays를 제외한다."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from autopilot_korea.settings import Settings

KST = ZoneInfo("Asia/Seoul")


def now_kst() -> datetime:
    return datetime.now(KST)


class TradingCalendar:
    def __init__(self, holidays: set[date] | None = None):
        self.holidays = holidays or set()

    @classmethod
    def from_settings(cls, settings: Settings) -> "TradingCalendar":
        return cls({date.fromisoformat(d) for d in settings.schedule.get("holidays", [])})

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def is_market_open(self, now: datetime, open_hm: str = "09:00", close_hm: str = "15:20") -> bool:
        """거래일의 정규장 주문 시간인가 (한국 시간)."""
        local = now.astimezone(KST) if now.tzinfo else now.replace(tzinfo=KST)
        start, end = (time(*map(int, hm.split(":"))) for hm in (open_hm, close_hm))
        return self.is_trading_day(local.date()) and start <= local.time() < end

    def add_trading_days(self, d: date, n: int) -> date:
        while n > 0:
            d += timedelta(days=1)
            if self.is_trading_day(d):
                n -= 1
        return d

    def trading_days_between(self, start: date, end: date) -> int:
        """start 다음 날부터 end까지(포함) 거래일 수."""
        count, d = 0, start
        while d < end:
            d += timedelta(days=1)
            count += self.is_trading_day(d)
        return count
