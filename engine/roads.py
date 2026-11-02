"""Version 7: roads. A road costs 5 stone and 15 minutes of one builder's work per tile; people walk ×1.5
on it, carts ×1.8. The AI plans a road (ai/roads.py); one worker with the job "road" lays it tile by tile.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .civ import Civilization
    from .world import World


class RoadTile:
    """Where the road worker stands next. Looks enough like a Building for walking and goals."""

    __slots__ = ("id", "x", "y", "size", "walkable", "type", "center")

    def __init__(self, x: int, y: int):
        self.id = -(y * 1000 + x) - 1                 # never clashes with buildings or nodes
        self.x, self.y = x, y
        self.size = 1
        self.walkable = True
        self.type = "road"
        self.center = (float(x), float(y))


def start_road(w: "World", civ: "Civilization", tiles: list[tuple[int, int]], why: str) -> bool:
    cost = {"stone": float(w.rules.roads.get("stone_per_tile", 5)) * len(tiles)}
    if not tiles or not civ.can_afford(cost):
        return False
    civ.pay(cost)
    civ.road_job = {"tiles": list(tiles), "i": 0, "work": 0.0, "why": why}
    w.log(civ, "road_start", f"{civ.name} starts a road of {len(tiles)} tiles: {why}.", 3)
    return True


def next_tile(civ: "Civilization") -> RoadTile | None:
    job = civ.road_job
    if job is None or job["i"] >= len(job["tiles"]):
        return None
    return RoadTile(*job["tiles"][job["i"]])


def work(w: "World", civ: "Civilization", seconds: float) -> bool:
    """A builder worked `seconds` (power-weighted) on the current tile. True when the tile is done."""
    job = civ.road_job
    if job is None:
        return False
    job["work"] += seconds
    need = float(w.rules.roads.get("minutes_per_tile", 15)) * 60
    if job["work"] < need:
        return False
    x, y = job["tiles"][job["i"]]
    m = w.map
    owner = w.civs.index(civ) + 1
    if m.inside(x, y) and not m.road[m.idx(x, y)]:
        m.road[m.idx(x, y)] = owner
        m.road_log.append((x, y, owner))
    job["i"] += 1
    job["work"] = 0.0
    civ.roads_built += 1
    if job["i"] >= len(job["tiles"]):
        w.log(civ, "road_done", f"{civ.name} finished a road of {len(job['tiles'])} tiles ({job['why']}).", 4)
        civ.road_job = None
        civ.dirty = True
    return True
