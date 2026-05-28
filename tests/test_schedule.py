from datetime import datetime
from zoneinfo import ZoneInfo

from tech_news.main import _seconds_until

ET = ZoneInfo("America/New_York")


def test_waits_until_later_today():
    now = datetime(2026, 5, 28, 8, 0, 0, tzinfo=ET)
    # 08:58:37 - 08:00:00 = 58m37s = 3517s
    assert _seconds_until("08:58:37", "America/New_York", now=now) == 3517.0


def test_zero_when_target_already_passed():
    now = datetime(2026, 5, 28, 9, 30, 0, tzinfo=ET)
    assert _seconds_until("08:58:37", "America/New_York", now=now) == 0.0


def test_accepts_hh_mm_without_seconds():
    now = datetime(2026, 5, 28, 8, 0, 0, tzinfo=ET)
    assert _seconds_until("09:00", "America/New_York", now=now) == 3600.0


def test_converts_now_into_target_timezone():
    # 13:00 UTC is 09:00 ET in summer, so a 09:30 ET target is 30 min away.
    now = datetime(2026, 5, 28, 13, 0, 0, tzinfo=ZoneInfo("UTC"))
    assert _seconds_until("09:30", "America/New_York", now=now) == 1800.0
