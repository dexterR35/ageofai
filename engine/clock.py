"""Game clock. Game time is real time: an epoch timestamp read in the local timezone.

The same clock runs the live world (1 s ticks, follows the wall clock) and fast mode
(10 s ticks, as fast as the CPU allows). Phase boundaries are computed per local day,
so daylight-saving days (23 h / 25 h) are handled correctly.
"""
from __future__ import annotations

import bisect
from datetime import date, datetime, time, timedelta, tzinfo
from enum import IntEnum
from zoneinfo import ZoneInfo

from .rules import Rules, Season


class Phase(IntEnum):
    NIGHT = 0     # 22:00 – work start: everyone sleeps
    WORK = 1
    LUNCH = 2     # 12:00 – 13:00: eat at home or at the castle
    EVENING = 3   # work end – 22:00: go home


PHASE_LABEL = {Phase.NIGHT: "night", Phase.WORK: "work", Phase.LUNCH: "lunch", Phase.EVENING: "evening"}


def day_schedule(rules: Rules, season: Season) -> list[tuple[int, Phase]]:
    """(seconds after midnight, phase starting then), sorted."""
    (am_start, am_end), (pm_start, pm_end) = season.work
    lunch_start, lunch_end = rules.lunch
    assert am_end == lunch_start and pm_start == lunch_end, "work windows must surround lunch"
    return [
        (0, Phase.NIGHT),
        (am_start, Phase.WORK),
        (lunch_start, Phase.LUNCH),
        (lunch_end, Phase.WORK),
        (pm_end, Phase.EVENING),
        (rules.evening_end, Phase.NIGHT),
    ]


class Clock:
    def __init__(self, rules: Rules, start: float, tz: tzinfo | str | None = None):
        self.rules = rules
        self.tz: tzinfo = ZoneInfo(tz or rules.timezone) if not isinstance(tz, tzinfo) else tz
        self.t: float = float(start)
        self._load_day()

    # ------------------------------------------------------------------ internals
    def _local(self, d: date, seconds: int) -> float:
        h, rem = divmod(seconds, 3600)
        dt = datetime.combine(d, time(h, rem // 60), self.tz)
        return dt.timestamp()

    def _load_day(self) -> None:
        local = datetime.fromtimestamp(self.t, self.tz)
        self.date: date = local.date()
        self.season: Season = self.rules.season_of(self.date.month)
        self.day_start: float = self._local(self.date, 0)
        self.day_end: float = self._local(self.date + timedelta(days=1), 0)
        sched = day_schedule(self.rules, self.season)
        self._bounds: list[float] = [self._local(self.date, s) for s, _ in sched]
        self._phases: list[Phase] = [p for _, p in sched]

    # ------------------------------------------------------------------ API
    def advance(self, dt: float) -> bool:
        """Move time forward. Returns True when a new local day starts."""
        self.t += dt
        if self.t >= self.day_end:
            self._load_day()
            return True
        return False

    @property
    def phase(self) -> Phase:
        i = bisect.bisect_right(self._bounds, self.t) - 1
        return self._phases[max(i, 0)]

    @property
    def working(self) -> bool:
        return self.phase == Phase.WORK

    def next_change(self) -> float:
        """Epoch of the next phase boundary or midnight, strictly after now."""
        i = bisect.bisect_right(self._bounds, self.t)
        return self._bounds[i] if i < len(self._bounds) else self.day_end

    @property
    def local(self) -> datetime:
        return datetime.fromtimestamp(self.t, self.tz)

    def hhmm(self) -> str:
        return self.local.strftime("%H:%M")

    def __repr__(self) -> str:
        return f"Clock({self.local:%Y-%m-%d %H:%M}, {self.season.name}, {PHASE_LABEL[self.phase]})"
