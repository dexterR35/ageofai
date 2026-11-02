"""A civilization: its people, buildings, stock and the bookkeeping around them."""
from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING, Iterable

from . import formulas as F
from .entities import Building, Person
from .rules import RESOURCES, Rules
from .development import Development

if TYPE_CHECKING:
    from ai.genome import Genome
    from ai.tactical import TacticalAI


class Civilization:
    def __init__(self, rules: Rules, spec: dict, born_t: float, genome: "Genome"):
        self.rules = rules
        self.id: str = spec["id"]
        self.name: str = spec["name"]
        self.color: str = spec["color"]
        self.genome = genome
        self.ai: "TacticalAI | None" = None
        self.born_t = born_t
        self.stock: dict[str, float] = dict(rules.start_stock)
        self.buildings: list[Building] = []
        self.castle: Building | None = None
        self.people: list[Person] = []
        self.tools: dict[str, int] = {c: 0 for c in rules.crafts}
        self.research: list[dict] = []        # {"craft", "level", "progress"}
        self.recruiting: list[float] = []     # progress 0..1 per recruit in training
        self.morale: float = rules.morale["start"]
        self.hunger_days = 0
        self.starved_today = False
        self.gathered: dict[str, float] = {r: 0.0 for r in RESOURCES}
        self.lost: dict[str, float] = {r: 0.0 for r in RESOURCES}
        self.unreachable: set[int] = set()     # node ids this civ cannot walk to
        self.dirty = True                      # ask the tactical AI to decide on the next tick
        self.alive = True
        self.deaths = 0
        self.departures = 0
        self.daily: list[dict] = []            # one stats row per day
        self.castle_reached: dict[int, float] = {1: born_t}
        self.builders_at: dict[int, float] = {}   # site id → builder power this tick
        self.food_rate = 0.0                       # food eaten per second (refreshed on changes)
        self.rng = random.Random(0)                # set by the world: same seed for every civ (same luck)
        self.mirror = False                        # True for the civ on the rotated half of the map
        # version 2
        self.now = born_t                          # updated by the world every tick
        self.relation: dict[str, float] = {}       # R toward other civs (diplomacy comes in v5)
        self.harvest_until = 0.0
        self.storm_from = 0.0
        self.storm_until = 0.0
        self.merchant_until = 0.0
        self.cold_homes: set[int] = set()          # houses without firewood today
        self.firefighters_at: dict[int, float] = {}
        self.births = 0
        self.fled = 0
        self.arrived_fugitives = 0
        self._depot_cache: dict[str, list[Building]] | None = None
        # version 5: army, spies, intelligence, market
        self.units: list = []                      # engine.units.Unit (soldiers, rams, spies)
        self.training: list[dict] = []             # {"type", "progress"} soldiers and spies being trained
        self.intel: dict[str, dict] = {}           # enemy id → what we believe about them (spy report or rumour)
        self.market_price: dict[str, float] = dict(rules.market.get("base_price", {}))
        self.kills = 0
        self.losses = 0                            # soldiers lost
        self.fallen_at: float | None = None
        self.rebirth_at: float | None = None
        self.generation = 1                        # 2, 3… after each rebirth
        self.attack_started: float | None = None   # when our army set out
        self.attack_hp0 = 0.0                      # the army's HP when it set out (retreat rule)
        self.attack_size0 = 0                      # army plan: how many set out (counter-attack rule)
        self.muster: dict | None = None            # army plan: the army is gathering before it marches
        self.counter: dict | None = None           # army plan: a won defense: strike back until "until"
        # version 7
        self.techs: set[str] = set()
        self.tech_job: dict | None = None          # {"tech", "progress"} researched at the castle
        self.carts = 0
        self.road_job: dict | None = None          # {"tiles", "i", "goal"} a road being built
        self.roads_built = 0
        self.development = Development()

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        if "development" not in state:
            self.development = Development()

    def balance(self, resource: str) -> float:
        return self.stock.get(resource, self.development.materials.get(resource, 0.0))

    # ------------------------------------------------------------------ army (version 5)
    @property
    def soldiers(self) -> list:
        return [u for u in self.units if u.fights]

    @property
    def scouts(self) -> list:
        return [u for u in self.units if u.is_scout]

    def has_tech(self, name: str) -> bool:
        return name in self.techs and self.rules.feature("tech")

    @property
    def spies(self) -> list:
        return [u for u in self.units if u.is_spy]

    def soldier_room(self, now: float) -> int:
        """Army plan: 300 per castle, villagers and soldiers together — minus soldiers we have or train."""
        cap = self.rules.people["pop_cap"] - self.pop
        training = sum(1 for t in self.training if t["type"] not in ("spy", "scout"))
        return max(0, cap - len(self.soldiers) - training)

    # ------------------------------------------------------------------ buildings
    @property
    def castle_level(self) -> int:
        return self.castle.level if self.castle else 0

    def of_type(self, btype: str, built_only: bool = True) -> list[Building]:
        return [b for b in self.buildings if b.type == btype and (b.built or not built_only)]

    def count(self, btype: str, min_level: int = 1) -> int:
        return sum(1 for b in self.buildings if b.type == btype and b.level >= min_level)

    def sites(self) -> list[Building]:
        return [b for b in self.buildings if b.under_construction]

    def invalidate_depots(self) -> None:
        self._depot_cache = None

    def depots(self, res: str) -> list[Building]:
        if self._depot_cache is None:
            cache: dict[str, list[Building]] = {r: [] for r in RESOURCES}
            for b in self.buildings:
                if not b.built or b.closed_until > self.now:
                    continue
                spec = self.rules.castle_spec if b.type == "castle" else self.rules.buildings[b.type]
                for r in spec.depot:
                    cache[r].append(b)
            self._depot_cache = cache
        return self._depot_cache[res]

    def nearest_depot(self, res: str, x: float, y: float) -> Building | None:
        best, best_d = None, math.inf
        for b in self.depots(res):
            cx, cy = b.center
            d = (cx - x) ** 2 + (cy - y) ** 2
            if d < best_d:
                best, best_d = b, d
        return best

    def depot_level(self, res: str) -> int:
        """Highest level of a specialized depot (not the castle) for this resource → +10%/level."""
        return max((b.level for b in self.depots(res) if b.type != "castle"), default=0)

    # ------------------------------------------------------------------ storage
    def cap(self, res: str) -> float:
        s = self.rules.storage
        levels = sum(b.level for b in self.buildings if b.type == "granary")
        extra = s["granary_food_per_level"] * levels if res == "food" else 0
        return s["castle_cap"] + s["granary_per_level"] * levels + extra

    def store(self, res: str, amount: float) -> float:
        """Put goods in storage. What does not fit is lost (the doc's rule). Returns what was stored."""
        room = max(0.0, self.cap(res) - self.stock[res])
        stored = min(amount, room)
        self.stock[res] += stored
        self.lost[res] += amount - stored
        return stored

    def deposit(self, res: str, amount: float) -> float:
        """A worker drops a full sack at a depot."""
        self.gathered[res] += amount
        return self.store(res, amount)

    def can_afford(self, cost: dict[str, float], reserved: dict[str, float] | None = None) -> bool:
        reserved = reserved or {}
        return all(self.balance(r) - reserved.get(r, 0) >= v - 1e-9 for r, v in cost.items())

    def pay(self, cost: dict[str, float]) -> None:
        if not self.can_afford(cost):
            raise ValueError(f"{self.id}: cannot afford {cost}")
        for r, v in cost.items():
            account = self.stock if r in self.stock else self.development.materials
            account[r] = account.get(r, 0.0) - v
            if account[r] < 0:
                if account[r] < -1e-6:
                    raise ValueError(f"{self.id}: negative {r} after paying {cost}")
                account[r] = 0.0

    # ------------------------------------------------------------------ people
    def age(self, p: Person, now: float) -> float:
        return p.age(now, self.rules.people["days_per_year"])

    def stage(self, p: Person, now: float) -> dict:
        return self.rules.stage_of(self.age(p, now))

    def workers(self, now: float) -> list[Person]:
        """Everyone of working age (children don't work)."""
        return [p for p in self.people if self.stage(p, now)["work"] > 0]

    def available_workers(self, now: float) -> list[Person]:
        """Who can be given a job right now: of working age, not sick/injured/on strike,
        and at most W_max of them (the doc's limit counts only people who work)."""
        ready = [p for p in self.workers(now) if not p.away(now)]
        cap = self.rules.workers_max(self.castle_level)
        if len(ready) > cap:
            ready.sort(key=lambda p: (p.job is None, -sum(p.job_seconds.values())))
            ready = ready[:cap]
        return ready

    @property
    def pop(self) -> int:
        return len(self.people)

    def food_per_day(self, now: float) -> float:
        soldiers = len(self.soldiers) * self.rules.army.get("food_per_day", 48) if self.units else 0
        return sum(self.stage(p, now)["food_per_day"] for p in self.people) + soldiers

    def homes(self) -> list[Building]:
        """Castle first, then built houses."""
        out = [self.castle] if self.castle else []
        from .development import building_rule
        out += [b for b in self.buildings if b.built and (b.type == "house" or building_rule(self, b.type).get("housing"))]
        return out

    def beds_in(self, b: Building) -> int:
        p = self.rules.people
        from .development import building_rule
        return p["castle_beds"] if b.type == "castle" else building_rule(self, b.type).get("housing", p["house_beds_base"]) + b.level

    def beds(self) -> int:
        p = self.rules.people
        return min(p["pop_cap"], sum(self.beds_in(b) for b in self.homes()))

    def assign_homes(self) -> None:
        """Give everyone a bed, filling the castle then houses. People without a bed have home=None."""
        slots: list[Building] = []
        for h in self.homes():
            slots += [h] * self.beds_in(h)
        keep: dict[int, int] = {}
        for h in slots:
            keep[h.id] = keep.get(h.id, 0) + 1
        unhoused = []
        for p in self.people:
            if p.home is not None and keep.get(p.home.id, 0) > 0 and p.home in slots:
                keep[p.home.id] -= 1
            else:
                p.home = None
                unhoused.append(p)
        for p in unhoused:
            for h in self.homes():
                if keep.get(h.id, 0) > 0:
                    keep[h.id] -= 1
                    p.home = h
                    break

    # ------------------------------------------------------------------ tools
    def tool_level(self, craft: str) -> int:
        return self.tools.get(craft, 0)

    def researching(self, craft: str) -> bool:
        return any(r["craft"] == craft for r in self.research)

    def research_slots(self) -> int:
        forge = self.of_type("forge")
        if not forge:
            return 0
        n = max(b.level for b in forge)
        return 1 + n // self.rules.tools["parallel_every_forge_levels"]

    # ------------------------------------------------------------------ castle requirements
    def requirement_met(self, req: dict, now: float) -> bool:
        if "building" in req:
            return self.count(req["building"], req.get("level", 1)) >= req.get("count", 1)
        if "workers" in req:
            return min(len(self.workers(now)), self.rules.workers_max(self.castle_level)) >= req["workers"]
        if "soldiers" in req:
            return len(self.soldiers) >= req["soldiers"]
        if "tools" in req:
            return sum(1 for c in self.rules.crafts if self.tool_level(c) >= req["level"]) >= req["tools"]
        if "tools_all" in req:
            return all(self.tool_level(c) >= req["tools_all"] for c in self.rules.crafts)
        if "economic_all" in req:
            lvl = req["economic_all"]
            eco = [b for b in self.buildings if b.type in self.rules.buildings
                   and self.rules.buildings[b.type].economic]
            return bool(eco) and all(b.level >= lvl for b in eco)
        raise ValueError(f"unknown requirement {req}")

    def missing_requirements(self, level: int, now: float) -> list[dict]:
        return [r for r in self.rules.castle_requirements.get(level, []) if not self.requirement_met(r, now)]

    # ------------------------------------------------------------------ summary
    def total_gathered(self) -> float:
        return sum(v for r, v in self.gathered.items() if r != "coins")

    def iter_tools(self) -> Iterable[tuple[str, int]]:
        return self.tools.items()
