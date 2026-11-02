"""People, buildings and the event journal."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

SECONDS_PER_DAY = 86400.0


class State(IntEnum):
    IDLE = 0
    WALK = 1
    GATHER = 2
    BUILD = 3
    REST = 4      # at home / eating / sleeping


class Intent(IntEnum):
    NONE = 0
    GATHER = 1    # walking to a resource
    DELIVER = 2   # walking to a depot with a full sack
    BUILD = 3     # walking to a construction site
    HOME = 4      # walking home (lunch / evening / night)


STATE_LABEL = {State.IDLE: "idle", State.WALK: "walking", State.GATHER: "gathering",
               State.BUILD: "building", State.REST: "resting"}


class Building:
    __slots__ = ("id", "civ_id", "type", "x", "y", "size", "level", "target_level", "progress",
                 "paid", "hp", "walkable", "workers", "sown_season_year", "sow_progress",
                 "unsown_penalty_year", "started_t", "builders_now",
                 "family_name", "on_fire", "fire_out", "closed_until", "max_hp")

    def __init__(self, id: int, civ_id: str, btype: str, x: int, y: int, size: int, walkable: bool,
                 started_t: float):
        self.id = id
        self.civ_id = civ_id
        self.type = btype
        self.x = x
        self.y = y
        self.size = size
        self.level = 0              # 0 = foundation, not usable yet
        self.target_level = 1       # > level while a construction or upgrade runs
        self.progress = 0.0         # 0..1 of the current construction
        self.paid: dict[str, float] = {}   # materials delivered so far (progressive sites)
        self.hp = 0.0
        self.walkable = walkable
        self.workers = 0            # occupied job slots (farms)
        self.sown_season_year = -1  # year in which the farm was sown
        self.sow_progress = 0.0     # seconds of sowing work this year
        self.unsown_penalty_year = -1
        self.started_t = started_t
        self.builders_now = 0.0
        self.family_name: str | None = None   # houses: "familia Morar"
        self.on_fire = False
        self.fire_out = 0.0                   # 0..1 progress of putting the fire out
        self.closed_until = 0.0               # mines closed after a collapse
        self.max_hp = 0.0

    @property
    def built(self) -> bool:
        return self.level >= 1

    @property
    def under_construction(self) -> bool:
        return self.target_level > self.level

    @property
    def center(self) -> tuple[float, float]:
        h = (self.size - 1) / 2
        return self.x + h, self.y + h

    @property
    def stage(self) -> int:
        """0 foundation, 1 scaffold, 2 walls, 3 roof, 4 done (for the map view)."""
        if not self.under_construction:
            return 4
        return min(3, int(self.progress * 4))

    def __repr__(self) -> str:
        tag = f"{self.level}->{self.target_level} {self.progress:.0%}" if self.under_construction else f"L{self.level}"
        return f"Building({self.type}#{self.id} {tag} @{self.x},{self.y})"


class Person:
    __slots__ = ("id", "civ_id", "first", "last", "sex", "birth_t", "trait", "x", "y",
                 "job", "target", "state", "intent", "path", "path_i", "path_version", "goal_key",
                 "carry_res", "carry_amt", "home", "job_seconds", "assigned_t", "history",
                 "alive", "arrived_t",
                 "happiness", "shock", "friends", "spouse", "parents", "children",
                 "low_days", "very_low_days", "away_until", "away_reason", "overtime_until", "evening", "hp", "cart",
                 "road_work")

    def __init__(self, id: int, civ_id: str, first: str, last: str, sex: str, birth_t: float,
                 trait: str | None, x: float, y: float):
        self.id = id
        self.civ_id = civ_id
        self.first = first
        self.last = last
        self.sex = sex
        self.birth_t = birth_t
        self.trait = trait
        self.x = x
        self.y = y
        self.job: str | None = None
        self.target: Any = None             # Node or Building
        self.state = State.IDLE
        self.intent = Intent.NONE
        self.path: tuple = ()
        self.path_i = 0
        self.path_version = -1
        self.goal_key: Any = None
        self.carry_res: str | None = None
        self.carry_amt = 0.0
        self.home: Building | None = None
        self.job_seconds: dict[str, float] = {}
        self.assigned_t = -1e18
        self.history: list[str] = []
        self.alive = True
        self.arrived_t = 0.0
        # version 2: wishes, friends and family
        self.happiness = 72.0
        self.shock = 0.0                      # happiness shocks waiting for tonight's update
        self.friends: dict[int, float] = {}   # person id → friendship 0..100
        self.spouse: int | None = None
        self.parents: tuple[int, ...] = ()
        self.children: list[int] = []
        self.low_days = 0                     # days in a row with H < 25 (strike)
        self.very_low_days = 0                # days in a row with H < 15 (flight)
        self.away_until = 0.0                 # sick, injured or on strike until then
        self.away_reason = ""
        self.overtime_until = 0.0             # evening overtime
        self.evening = ""                     # what they chose tonight
        self.hp = 25.0                        # version 5: an enemy army can hurt workers
        self.cart = False                     # version 7: drives one of the village's carts
        self.road_work = 0.0                  # seconds of work on the current road tile

    @property
    def name(self) -> str:
        return f"{self.first} {self.last}"

    def away(self, now: float) -> bool:
        return self.away_until > now

    def friend_count(self, threshold: float) -> int:
        return sum(1 for v in self.friends.values() if v >= threshold)

    def badge(self, green: float = 60, yellow: float = 25) -> str:
        """Happiness sign above the head on the map."""
        return "green" if self.happiness >= green else ("yellow" if self.happiness >= yellow else "red")

    @property
    def tile(self) -> tuple[int, int]:
        return int(self.x + 0.5), int(self.y + 0.5)

    def age(self, now: float, days_per_year: float) -> float:
        return (now - self.birth_t) / (SECONDS_PER_DAY * days_per_year)

    def __repr__(self) -> str:
        return f"Person({self.name}, {self.job}, {STATE_LABEL[self.state]})"


@dataclass(slots=True)
class Event:
    t: float
    day: int
    civ_id: str | None
    kind: str
    text: str
    importance: int = 3


@dataclass
class Journal:
    """Text log of everything that happens. Later versions store it in SQLite."""
    events: list[Event] = field(default_factory=list)
    echo: bool = False
    max_events: int = 50000
    sink: Any = None          # optional callable(Event), e.g. the SQLite store in live mode
    count: int = 0            # events ever added (the list drops old ones; the browser view counts on this)

    def add(self, ev: Event) -> None:
        self.events.append(ev)
        self.count += 1
        if self.sink is not None:
            self.sink(ev)
        if len(self.events) > self.max_events:
            del self.events[: len(self.events) - self.max_events]
        if self.echo:
            print(format_event(ev))

    def of(self, kind: str) -> list[Event]:
        return [e for e in self.events if e.kind == kind]


def format_event(ev: Event, tz=None) -> str:
    from datetime import datetime
    when = datetime.fromtimestamp(ev.t, tz).strftime("%d %b %H:%M")
    who = f"[{ev.civ_id}] " if ev.civ_id else ""
    return f"d{ev.day:>3} {when}  {who}{ev.text}"
