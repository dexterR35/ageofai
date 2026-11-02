from datetime import datetime
from zoneinfo import ZoneInfo

from engine.clock import Clock, Phase
from engine.rules import default_rules

TZ = ZoneInfo("Europe/Bucharest")
R = default_rules()


def at(y, m, d, h, mi=0) -> Clock:
    return Clock(R, datetime(y, m, d, h, mi, tzinfo=TZ).timestamp(), TZ)


def test_spring_day_phases():
    assert at(2027, 4, 10, 6, 59).phase == Phase.NIGHT
    assert at(2027, 4, 10, 7, 0).phase == Phase.WORK
    assert at(2027, 4, 10, 12, 30).phase == Phase.LUNCH
    assert at(2027, 4, 10, 13, 0).phase == Phase.WORK
    assert at(2027, 4, 10, 19, 0).phase == Phase.EVENING
    assert at(2027, 4, 10, 22, 0).phase == Phase.NIGHT


def test_winter_has_shorter_days():
    assert at(2027, 1, 10, 7, 30).phase == Phase.NIGHT
    assert at(2027, 1, 10, 8, 0).phase == Phase.WORK
    assert at(2027, 1, 10, 17, 0).phase == Phase.EVENING


def test_seasons_follow_calendar():
    assert at(2026, 10, 30, 12).season.name == "autumn"
    assert at(2026, 12, 1, 12).season.name == "winter"
    assert at(2027, 3, 1, 12).season.name == "spring"
    assert at(2027, 6, 1, 12).season.name == "summer"


def test_day_rollover_and_next_change():
    c = at(2026, 10, 30, 21, 0)
    assert c.next_change() == datetime(2026, 10, 30, 22, 0, tzinfo=TZ).timestamp()
    c.advance(c.next_change() - c.t)
    assert c.phase == Phase.NIGHT
    changed = c.advance(c.next_change() - c.t)      # → midnight
    assert changed and c.date.day == 31


def test_daylight_saving_day_is_23_hours():
    # Europe/Bucharest springs forward on the last Sunday of March 2027 (28 March)
    c = at(2027, 3, 28, 12)
    assert c.day_end - c.day_start == 23 * 3600
    # 07:00 local still starts work on that day
    work = datetime(2027, 3, 28, 7, 0, tzinfo=TZ).timestamp()
    c2 = Clock(R, work, TZ)
    assert c2.phase == Phase.WORK
