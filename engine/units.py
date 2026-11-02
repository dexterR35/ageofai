"""Version 5: things that move on the map besides workers — soldiers, spies and trade carts."""
from __future__ import annotations

from typing import Any


class Unit:
    """A soldier, a ram or a spy. Soldiers are not Persons: they live in the barracks, eat and get paid."""

    __slots__ = ("id", "civ_id", "type", "x", "y", "hp", "max_hp", "order", "target", "goal", "path", "path_i",
                 "path_version", "path_goal", "repath_at", "state", "mission", "born_t", "kills", "name")

    def __init__(self, id: int, civ_id: str, utype: str, x: float, y: float, hp: float, born_t: float):
        self.id = id
        self.civ_id = civ_id
        self.type = utype
        self.x = x
        self.y = y
        self.hp = hp
        self.max_hp = hp
        self.order = "defend"           # defend | attack | return (spies: spy)
        self.target: Any = None         # Unit, Building or Person being fought
        self.goal: Any = None           # where an order sends it: a Building, or (x, y)
        self.path: tuple = ()
        self.path_i = 0
        self.path_version = -1
        self.path_goal: Any = None
        self.repath_at = 0.0
        self.state = "idle"             # idle | walk | fight (spies: go | stay | back)
        self.mission: dict = {}         # spies: {"enemy", "arrive_t", "hours_checked", "turned", ...}
        self.born_t = born_t
        self.kills = 0
        self.name = ""

    @property
    def tile(self) -> tuple[int, int]:
        return int(self.x + 0.5), int(self.y + 0.5)

    @property
    def is_spy(self) -> bool:
        return self.type == "spy"

    @property
    def is_scout(self) -> bool:
        return self.type == "scout"

    @property
    def fights(self) -> bool:
        return self.type not in ("spy", "scout")

    @property
    def alive(self) -> bool:
        return self.hp > 0

    def __repr__(self) -> str:
        return f"Unit({self.type}#{self.id} {self.civ_id} {self.order}/{self.state} hp {self.hp:.0f})"


def count_types(units) -> dict[str, int]:
    """How many of each unit type: {"militia": 4, "archer": 2}."""
    out: dict[str, int] = {}
    for u in units:
        out[u.type] = out.get(u.type, 0) + 1
    return out


class Cart:
    """A trade cart carrying one side of a deal from castle to castle (visible on the map)."""

    __slots__ = ("id", "deal_id", "src", "dst", "x", "y", "path", "path_i", "load", "started_t")

    def __init__(self, id: int, deal_id: int, src: str, dst: str, x: float, y: float, path: tuple,
                 load: dict[str, float], started_t: float):
        self.id = id
        self.deal_id = deal_id
        self.src = src
        self.dst = dst
        self.x = x
        self.y = y
        self.path = path
        self.path_i = 0
        self.load = load
        self.started_t = started_t
