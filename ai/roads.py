"""Version 7: the AI builds a road only when the time it saves pays for it (doc):

    Σ_trips/day (t_without − t_with) · R · 30 days  >  cost of the road

t is the walking time of one trip, R the resource a worker gathers per hour, and the cost is the road's
stone plus the work lost while laying it (15 minutes a tile), both valued at market base prices.
"""
from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from engine.roads import start_road

if TYPE_CHECKING:
    from .tactical import Estimate, TacticalAI


class RoadPlanner:
    def __init__(self, tac: "TacticalAI"):
        self.t = tac
        self.civ = tac.civ
        self.w = tac.w
        self.r = tac.rules
        self.last_plan = -1e18

    def plan(self, est: "Estimate") -> str | None:
        """At most once an hour: maybe start a road. Returns a note for the thoughts panel."""
        civ, w, cfg = self.civ, self.w, self.r.roads
        if not self.r.feature("roads") or civ.road_job is not None or est.now - self.last_plan < 3600:
            return None
        if civ.castle_level < int(cfg.get("from_castle", 2)) or est.emergency:
            return None
        self.last_plan = est.now
        best = None
        for job, n in est.counts.items():
            if not job or job not in self.r.jobs or n < 1:
                continue
            route = self.route(job)
            if route is None:
                continue
            tiles, res = route
            gain, cost = self.value(job, n, len(tiles), res, est)
            if gain > cost and (best is None or gain - cost > best[0]):
                best = (gain - cost, tiles, job, res, gain, cost)
        if best is None:
            return None
        _, tiles, job, res, gain, cost = best
        # the road's stone must be spare: not what the next castle level or the current goals need
        need = sum(a.cost.get("stone", 0) for a in self.t.targets)
        if civ.castle is not None and civ.castle_level < self.r.castle_max_level:
            castle = self.r.castle_cost(civ.castle_level)
            need += max(0.0, castle.get("stone", 0) - civ.castle.paid.get("stone", 0))
        if civ.stock["stone"] - float(cfg.get("stone_per_tile", 5)) * len(tiles) < need:
            return None
        why = f"to the {res} ({gain:.0f} worth saved in {cfg.get('payback_days', 30)} days, costs {cost:.0f})"
        if start_road(w, civ, tiles, why):
            return f"a road of {len(tiles)} tiles {why}"
        return None

    def route(self, job: str) -> tuple[list[tuple[int, int]], str] | None:
        """The usual walk of this job: from its depot to where most of its workers go."""
        civ, w = self.civ, self.w
        spec = self.r.jobs[job]
        targets = Counter(p.target for p in civ.people if p.job == job and p.target is not None)
        if not targets:
            return None
        target = targets.most_common(1)[0][0]
        if not hasattr(target, "x"):
            return None
        tx, ty = target.center
        depot = civ.nearest_depot(spec.resource, tx, ty) or civ.castle
        if depot is None:
            return None
        m = w.map
        start = m.access_tiles(depot.x, depot.y, depot.size, depot.walkable)
        goals = w.behavior.goals(target)
        if not start or not goals:
            return None
        first = start[-1] if civ.mirror else start[0]            # the south's mirror of the north's tile
        path = w.paths.find(first, (type(target).__name__, target.id), goals)
        if not path:
            return None
        tiles = [t for t in path if not m.road[m.idx(*t)]][: int(self.r.roads.get("max_tiles", 40))]
        if len(tiles) < 4:
            return None
        return tiles, spec.resource

    def value(self, job: str, workers: int, tiles: int, res: str, est: "Estimate") -> tuple[float, float]:
        cfg = self.r.roads
        v = float(self.r.gathering["walk_tiles_per_hour"])
        hours_saved_per_trip = 2 * tiles * (1 / v - 1 / (v * float(cfg.get("speed", 1.5))))
        per_day = est.yield_day.get(job, 0.0)
        carry = float(self.r.gathering["carry"])
        trips = workers * max(1.0, per_day / carry)
        if trips < float(cfg.get("min_trips_per_day", 6)):
            return 0.0, 1.0
        work_h = max(1.0, self.w.clock.season.hours)
        rate = per_day / work_h                                  # R: one worker's gathering per hour
        price = self.r.market.get("base_price", {})
        days = float(cfg.get("payback_days", 30))
        gain = trips * hours_saved_per_trip * rate * days * price.get(res, 1.0)
        cost = (float(cfg.get("stone_per_tile", 5)) * tiles * price.get("stone", 1.2)
                + float(cfg.get("minutes_per_tile", 15)) / 60 * tiles * rate * price.get(res, 1.0))
        return gain, cost


def give_carts(tac: "TacticalAI") -> None:
    """The village's carts go to the workers with the longest walks (a cart needs a driver)."""
    civ = tac.civ
    if civ.carts <= 0:
        for p in civ.people:
            p.cart = False
        return
    far = {job: tac.job_distance(job) for job in {p.job for p in civ.people} if job in tac.rules.jobs}
    drivers = [p for p in civ.people if p.job in far and p.job != "farm" and far[p.job] < 1e8]
    drivers.sort(key=lambda p: -far[p.job])
    keep = {p.id for p in drivers[: civ.carts]}
    for p in civ.people:
        p.cart = p.id in keep
