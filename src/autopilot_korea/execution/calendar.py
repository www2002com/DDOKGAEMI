"""KRX 거래일 계산. 주말과 schedule.yaml의 holidays를 제외한다."""

from __future__ import annotations

from datetime import date, timedelta

from autopilot_korea.settings import Settings


class TradingCalendar:
    def __init__(self, holidays: set[date] | None = None):
        self.holidays = holidays or set()

    @classmethod
    def from_settings(cls, settings: Settings) -> "TradingCalendar":
        return cls({date.fromisoformat(d) for d in settings.schedule.get("holidays", [])})

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

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
