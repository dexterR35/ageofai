"""What a person does each tick: walk, gather, carry, deliver, build, eat, sleep.

Work loop:  IDLE → WALK(gather) → GATHER → WALK(deliver) → deposit → WALK(gather) …
Builders:   IDLE → WALK(build) → BUILD
Clock interruptions: lunch (eat at home or the castle), evening (go home), night (sleep).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import roads, tech
from .clock import Phase
from .entities import Building, Intent, Person, State
from .pathfinding import step_along
from .rules import Season
from .worldmap import Node

if TYPE_CHECKING:
    from .civ import Civilization
    from .world import World


@dataclass(slots=True)
class TickCtx:
    now: float
    phase: Phase
    working: bool
    season: Season
    year: int
    month: int

    @property
    def farming_year(self) -> int:
        """Farms are sown each March; Jan–Feb still belong to last year's harvest."""
        return self.year if self.month >= 3 else self.year - 1


class Behavior:
    def __init__(self, world: "World"):
        self.w = world
        self.rules = world.rules
        self.m = world.map
        g = self.rules.gathering
        self.base_speed = g["walk_tiles_per_hour"] / 3600.0
        self.carry = float(g["carry"])
        self.depot_bonus = g["depot_bonus_per_level"]
        xp = self.rules.people["xp"]
        self.xp_day_seconds = self.rules.seasons["spring"].hours * 3600.0   # "a day worked" = 11 h
        self.xp_days_per_step, self.xp_step, self.xp_max = xp["days_per_step"], xp["step"], xp["max"]
        self._goal_cache: dict[tuple, list[tuple[int, int]]] = {}
        self._goal_version = -1

    # ================================================================== multipliers
    def work_factor(self, civ: "Civilization", p: Person, now: float) -> float:
        f = civ.stage(p, now)["work"]
        if p.trait:
            f *= self.rules.traits[p.trait].get("work", 1.0)
        return f

    def xp(self, p: Person, job: str, speed: float = 1.0) -> float:
        days = p.job_seconds.get(job, 0.0) * speed / self.xp_day_seconds
        return min(self.xp_max, self.xp_step * math.floor(days / self.xp_days_per_step))

    def carry_cap(self, p: Person) -> float:
        if p.cart:                                            # version 7: a cart carries 50
            return float(self.rules.carts.get("carry", 50))
        if p.trait:
            return float(self.rules.traits[p.trait].get("carry", self.carry))
        return self.carry

    def speed(self, p: Person, season: Season) -> float:
        s = self.base_speed * season.walk
        if p.trait:
            s *= self.rules.traits[p.trait].get("walk", 1.0)
        on_road = self.m.road[self.m.idx(*p.tile)] if self.m.inside(*p.tile) else 0
        if p.cart:                                            # version 7: carts ×1.2 on fields, ×1.8 on roads
            s *= self.rules.carts.get("speed_road", 1.8) if on_road else self.rules.carts.get("speed_field", 1.2)
        elif on_road:
            s *= self.rules.roads.get("speed", 1.5)
        return s

    def gather_mult(self, civ: "Civilization", p: Person, job: str, ctx: TickCtx) -> float:
        """m = m_T · m_M · (1 + 0.1·n_dep) · (1 + XP) · work factor · season."""
        spec = self.rules.jobs[job]
        m = self.rules.tool_mult(civ.tool_level(spec.tool))
        m *= civ.morale
        m *= 1 + self.depot_bonus * civ.depot_level(spec.resource)
        m *= 1 + self.xp(p, job, tech.xp_speed(self.rules, civ))
        m *= self.work_factor(civ, p, ctx.now)
        m *= ctx.season.mult(spec.season)
        m *= tech.food_mult(self.rules, civ, spec.node, spec.resource, ctx.season)
        from .development import bonus
        m *= 1 + bonus(civ, spec.resource)
        if civ.castle_level >= self.rules.castle_max_level:
            m *= 1 + self.rules.castle["kingdom_production_bonus"]
        return m

    def farm_mult(self, farm: Building, ctx: TickCtx) -> float:
        spec = self.rules.buildings["farm"]
        m = 1 + spec.level_bonus * (farm.level - 1)
        if farm.sown_season_year != ctx.farming_year:
            m *= self.rules.sowing["unsown_multiplier"]
        return m

    def needs_sowing(self, farm: Building, ctx: TickCtx) -> bool:
        return ctx.month == self.rules.sowing["month"] and farm.sown_season_year != ctx.farming_year

    # ================================================================== targets
    def goals(self, obj: Node | Building) -> list[tuple[int, int]]:
        if self._goal_version != self.m.version:
            self._goal_cache.clear()
            self._goal_version = self.m.version
        key = (type(obj).__name__, obj.id)
        g = self._goal_cache.get(key)
        if g is None:
            if isinstance(obj, Node):
                g = self.m.access_tiles(obj.x, obj.y, obj.size, not obj.blocks)
            else:
                g = self.m.access_tiles(obj.x, obj.y, obj.size, obj.walkable)
            self._goal_cache[key] = g
        return g

    def release(self, p: Person) -> None:
        """Free the job slot the person holds (tree, deposit or farm)."""
        t = p.target
        if isinstance(t, Node) or (isinstance(t, Building) and t.type == "farm"):
            t.workers = max(0, t.workers - 1)
        p.target = None

    def claim(self, p: Person, target: Node | Building) -> None:
        self.release(p)
        p.target = target
        if isinstance(target, Node) or target.type == "farm":
            target.workers += 1

    def valid_target(self, civ: "Civilization", p: Person, job: str) -> bool:
        t = p.target
        spec = self.rules.jobs[job]
        if spec.node == "farm":
            return isinstance(t, Building) and t.type == "farm" and t.built and t in civ.buildings
        return isinstance(t, Node) and t.kind == spec.node and t.amount > 0

    def find_target(self, civ: "Civilization", p: Person, job: str) -> Node | Building | None:
        spec = self.rules.jobs[job]
        if spec.node == "farm":
            slots = self.rules.buildings["farm"].workers
            farms = [b for b in civ.of_type("farm") if b.workers < slots]
            return min(farms, key=lambda b: (b.center[0] - p.x) ** 2 + (b.center[1] - p.y) ** 2, default=None)
        depot = civ.nearest_depot(spec.resource, p.x, p.y) or civ.castle
        if depot is None:
            return None
        ax, ay = depot.center
        cx, cy = civ.castle.center
        r2 = self.rules.ai["tactical"]["node_search_radius"] ** 2
        best, best_d = None, math.inf
        bad = civ.unreachable
        fog = self.w.fog
        for n in self.m.by_kind[spec.node]:
            if n.amount <= 0 or n.workers >= n.max_workers or n.id in bad or not fog.usable(civ, n):
                continue
            nx, ny = n.x, n.y
            if (nx - cx) ** 2 + (ny - cy) ** 2 > r2:
                continue
            d = (nx - ax) ** 2 + (ny - ay) ** 2
            if d < best_d:
                best, best_d = n, d
        return best

    # ================================================================== movement
    def go(self, p: Person, obj: Node | Building, intent: Intent) -> bool:
        path = self.w.paths.find(p.tile, (type(obj).__name__, obj.id), self.goals(obj))
        if path is None:
            return False
        p.path, p.path_i, p.path_version = path, 0, self.m.version
        p.goal_key = obj
        p.intent = intent
        p.state = State.WALK
        return True

    def walk(self, p: Person, dt: float, ctx: TickCtx) -> bool:
        """Advance along the path. Returns True on arrival."""
        if p.path_version != self.m.version:          # the map changed under our feet
            path = self.w.paths.find(p.tile, (type(p.goal_key).__name__, p.goal_key.id), self.goals(p.goal_key))
            if path is None:
                p.state = State.IDLE
                return False
            p.path, p.path_i, p.path_version = path, 0, self.m.version
        p.x, p.y, p.path_i = step_along(p.path, p.path_i, p.x, p.y, self.speed(p, ctx.season) * dt)
        return p.path_i >= len(p.path)

    # ================================================================== main update
    def update(self, civ: "Civilization", p: Person, dt: float, ctx: TickCtx) -> None:
        now = ctx.now
        if p.job == "fire":                                   # fires are fought day and night
            self._work(civ, p, dt, ctx)
        elif p.away_until > now or civ.storm_from <= now < civ.storm_until:
            self._off_duty(civ, p, dt, ctx)                   # sick, injured, on strike, or a storm
        elif ctx.working or (ctx.phase == Phase.EVENING and p.overtime_until > now):
            self._work(civ, p, dt, ctx)
        else:
            self._off_duty(civ, p, dt, ctx)

    def _work(self, civ: "Civilization", p: Person, dt: float, ctx: TickCtx) -> None:
        if p.job is None and p.carry_amt <= 0:     # nothing to do: rest at home
            if p.state == State.WALK and p.intent == Intent.HOME:
                if self.walk(p, dt, ctx):
                    self.arrive(civ, p, ctx)
            elif p.state != State.REST:
                self.go_home(civ, p, ctx)
            return
        if p.state == State.REST or (p.state == State.WALK and p.intent == Intent.HOME):
            p.state = State.IDLE
        if p.state == State.IDLE:
            self.start_work(civ, p, ctx)
        if p.state == State.WALK:
            if self.walk(p, dt, ctx):
                self.arrive(civ, p, ctx)
        elif p.state == State.GATHER:
            self.gather(civ, p, dt, ctx)
        elif p.state == State.BUILD:
            self.build(civ, p, dt, ctx)

    def _off_duty(self, civ: "Civilization", p: Person, dt: float, ctx: TickCtx) -> None:
        if p.state == State.REST:
            return
        if p.state == State.WALK and p.intent in (Intent.HOME, Intent.DELIVER):
            if self.walk(p, dt, ctx):
                self.arrive(civ, p, ctx)
            return
        if p.carry_amt > 0 and ctx.phase != Phase.LUNCH:
            self.go_deliver(civ, p)
        else:
            self.go_home(civ, p, ctx)
        if p.state == State.WALK and self.walk(p, dt, ctx):
            self.arrive(civ, p, ctx)

    def arrive(self, civ: "Civilization", p: Person, ctx: TickCtx) -> None:
        intent = p.intent
        p.intent = Intent.NONE
        p.arrived_t = ctx.now
        if intent == Intent.GATHER:
            p.state = State.GATHER
        elif intent == Intent.BUILD:
            p.state = State.BUILD
        elif intent == Intent.DELIVER:
            if p.carry_amt > 0 and p.carry_res:
                civ.deposit(p.carry_res, p.carry_amt)
            p.carry_amt, p.carry_res = 0.0, None
            p.state = State.IDLE
            if ctx.working:
                self.start_work(civ, p, ctx)
            else:
                self.go_home(civ, p, ctx)
        elif intent == Intent.HOME:
            p.state = State.REST
        else:
            p.state = State.IDLE

    # ================================================================== work
    def start_work(self, civ: "Civilization", p: Person, ctx: TickCtx) -> None:
        job = p.job
        if job == "fire":
            site = p.target
            if not (isinstance(site, Building) and site.on_fire and site in civ.buildings):
                site = next((b for b in civ.buildings if b.on_fire), None)
            if site is None:
                p.job, p.target = None, None
                civ.dirty = True
                p.state = State.IDLE
                return
            p.target = site
            if not self.go(p, site, Intent.BUILD):
                p.state = State.IDLE
            return
        if p.carry_amt > 0:
            wrong = job is None or job in ("build", "road") or self.rules.jobs[job].resource != p.carry_res
            if wrong or p.carry_amt >= self.carry_cap(p) - 1e-9:
                self.go_deliver(civ, p)
                return
        if job is None:
            p.state = State.IDLE
            return
        if job == "road":                                     # version 7: lay the next tile of the road
            tile = roads.next_tile(civ)
            if tile is None:
                p.job, p.target = None, None
                civ.dirty = True
                p.state = State.IDLE
                return
            p.target = tile
            if not self.go(p, tile, Intent.BUILD):
                civ.road_job["i"] += 1                        # a tile we can't reach: skip it
                p.state = State.IDLE
            return
        if job == "build":
            site = p.target
            if not (isinstance(site, Building) and site.under_construction and site in civ.buildings):
                site = self.w.pick_site(civ)
            if site is None:
                p.job, p.target = None, None
                civ.dirty = True
                p.state = State.IDLE
                return
            p.target = site
            if not self.go(p, site, Intent.BUILD):
                p.state = State.IDLE
            return
        if not self.valid_target(civ, p, job):
            target = self.find_target(civ, p, job)
            if target is None:
                self.release(p)
                p.job = None
                civ.dirty = True
                p.state = State.IDLE
                return
            self.claim(p, target)
        if not self.go(p, p.target, Intent.GATHER):
            if isinstance(p.target, Node):
                civ.unreachable.add(p.target.id)
            self.release(p)
            civ.dirty = True
            p.state = State.IDLE

    def gather(self, civ: "Civilization", p: Person, dt: float, ctx: TickCtx) -> None:
        job = p.job
        if job is None or job in ("build", "fire", "road") or not self.valid_target(civ, p, job):
            self.release(p)
            p.state = State.IDLE
            if p.carry_amt > 0:
                self.go_deliver(civ, p)
            return
        spec = self.rules.jobs[job]
        target = p.target
        p.job_seconds[job] = p.job_seconds.get(job, 0.0) + dt
        if spec.node == "farm" and self.needs_sowing(target, ctx):
            target.sow_progress += dt * self.work_factor(civ, p, ctx.now)
            if target.sow_progress >= self.rules.sowing["work_hours"] * 3600:
                target.sown_season_year = ctx.farming_year
                target.sow_progress = 0.0
                self.w.log(civ, "sow", f"{p.name} sowed farm #{target.id}.", 2)
            return
        mult = self.gather_mult(civ, p, job, ctx)
        if spec.node == "farm":
            mult *= self.farm_mult(target, ctx)
            if civ.harvest_until > ctx.now:
                mult *= self.rules.events["harvest"]["farm_mult"]
        if mult <= 0:          # e.g. fruit in winter: nothing to pick
            civ.dirty = True
            return
        cap = self.carry_cap(p)
        amount = min(spec.rate * mult * dt / 3600.0, cap - p.carry_amt)
        if isinstance(target, Node):
            amount = min(amount, target.amount)
            target.amount -= amount
        p.carry_res = spec.resource
        p.carry_amt += amount
        depleted = isinstance(target, Node) and target.amount <= 1e-9
        if depleted:
            self.w.deplete(target)
            self.release(p)
        if p.carry_amt >= cap - 1e-9 or depleted:
            self.go_deliver(civ, p)

    def build(self, civ: "Civilization", p: Person, dt: float, ctx: TickCtx) -> None:
        site = p.target
        if p.job == "fire":
            if isinstance(site, Building) and site.on_fire and site in civ.buildings:
                civ.firefighters_at[site.id] = civ.firefighters_at.get(site.id, 0.0) + 1.0
            else:
                p.job, p.target = None, None
                civ.dirty = True
                p.state = State.IDLE
            return
        if p.job == "road":
            if not isinstance(site, roads.RoadTile) or civ.road_job is None:
                p.state = State.IDLE
                return
            power = self.work_factor(civ, p, ctx.now) * civ.morale * self.rules.tool_mult(civ.tool_level("builder"))
            p.job_seconds["build"] = p.job_seconds.get("build", 0.0) + dt
            if roads.work(self.w, civ, dt * power):
                p.state = State.IDLE                          # on to the next tile
            return
        if not (isinstance(site, Building) and site.under_construction and site in civ.buildings):
            p.state = State.IDLE
            return
        power = self.work_factor(civ, p, ctx.now) * civ.morale
        civ.builders_at[site.id] = civ.builders_at.get(site.id, 0.0) + power
        p.job_seconds["build"] = p.job_seconds.get("build", 0.0) + dt

    # ================================================================== trips
    def go_deliver(self, civ: "Civilization", p: Person) -> None:
        depot = civ.nearest_depot(p.carry_res, p.x, p.y) if p.carry_res else None
        if depot is None or not self.go(p, depot, Intent.DELIVER):
            if p.carry_res:
                civ.lost[p.carry_res] += p.carry_amt     # nowhere to put it
            p.carry_amt, p.carry_res = 0.0, None
            p.state = State.IDLE

    def go_home(self, civ: "Civilization", p: Person, ctx: TickCtx) -> None:
        dest = p.home or civ.castle
        if ctx.phase == Phase.LUNCH and civ.castle is not None:
            options = [b for b in (p.home, civ.castle) if b is not None]
            dest = min(options, key=lambda b: (b.center[0] - p.x) ** 2 + (b.center[1] - p.y) ** 2)
        if dest is None or not self.go(p, dest, Intent.HOME):
            p.state = State.REST
            return
        if not p.path:
            p.state = State.REST
            p.intent = Intent.NONE
