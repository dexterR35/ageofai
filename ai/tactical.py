"""Tactical layer: every 5 minutes, decide what to build and who works where.

Doc formulas used here:
  * worker split:  N_r = W · g_r(0.1 + deficit_r) / Σ g_s(0.1 + deficit_s)
  * next action:   U(a) = Σ g_j·benefit_j / (1 + (t_wait + t_build)/24h)
  * food safety:   enough food workers to feed everyone always comes first
  * winter:        fill R_food and R_wood (× winter_prep gene) before 1 December
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from engine import formulas as F
from engine import tech as T
from engine import development
from engine.entities import Building, Intent, Person, State
from engine.worldmap import find_site

from .military import ArmyPlanner
from .roads import RoadPlanner, give_carts

if TYPE_CHECKING:
    from engine.civ import Civilization
    from engine.world import World

INF = 1e9
GATHER_RES = ("food", "wood", "stone", "ore", "gold")
RES_JOB = {"wood": "wood", "stone": "stone", "ore": "ore", "gold": "gold"}
FOOD_JOBS = ("farm", "hunt", "fruit")
GENE_FOR = {"food": "g_food", "wood": "g_wood", "stone": "g_stone", "ore": "g_ore", "gold": "g_gold",
            "houses": "g_houses", "castle": "g_castle", "tools": "g_tools", "defense": "g_defense",
            "storage": "g_food", "safety": "prudence", "explore": "exploration"}
DEPOT_FOR = {"wood": "lumber_camp", "stone": "quarry", "food": "mill"}
RES_LABEL = {"food": "food", "wood": "wood", "stone": "stone", "ore": "ore", "gold": "gold", "coins": "coins"}


@dataclass
class Action:
    kind: str                       # build | upgrade | castle | research | recruit
    key: tuple
    label: str
    cost: dict[str, float]
    hours: float
    benefit: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    btype: str | None = None
    building: Building | None = None
    craft: str | None = None
    utility: float = 0.0
    wait: float = 0.0

    def add(self, other: "Action") -> None:
        for k, v in other.benefit.items():
            self.benefit[k] = self.benefit.get(k, 0.0) + v
        self.reasons += other.reasons


@dataclass
class Estimate:
    now: float
    month: int
    L: int
    W: int
    workers: list[Person]
    pop: int
    food_day: float
    counts: Counter
    yield_day: dict[str, float]
    slots: dict[str, int]
    income_day: dict[str, float]
    free_beds: int
    food_plan: dict[str, int]
    food_need_day: float
    food_unmet_day: float
    food_capacity_day: float
    food_target: float
    winter_food: float
    winter_wood: float
    emergency: bool
    firewood_workers: int = 0       # winter: lumberjacks kept for firewood before anything else
    can_feed: bool = True           # one more person is safe for the next weeks (seasons included)


class TacticalAI:
    def __init__(self, civ: "Civilization", world: "World"):
        self.civ = civ
        self.w = world
        self.rules = world.rules
        self.cfg = self.rules.ai["tactical"]
        self.thoughts: deque[tuple[float, str]] = deque(maxlen=200)
        self.targets: list[Action] = []
        self.last_alloc: dict[str, int] = {}
        self._dist_cache: dict[str, float] = {}
        self._dist_key: tuple | None = None
        # version 3: AI-written rules and the council's plan nudge the formulas
        self.book: list = []                                  # rulebook.Rule objects in force
        self.activations: dict[str, int] = {}                 # rule text → times it fired
        self.gene_boost: dict[str, tuple[float, float]] = {}  # gene → (factor, until)
        self.directive = None                                 # council Plan
        self.directive_until = 0.0
        self._moves: list[tuple[str, int]] = []
        self.army = ArmyPlanner(self)                         # version 5: soldiers, towers, spies, war
        self.war_note: str | None = None
        self.roads = RoadPlanner(self)                        # version 7: roads and carts
        self.road_note: str | None = None

    @property
    def g(self):
        return self.civ.genome

    def G(self, name: str, default: float = 1.0) -> float:
        """A gene as the AI uses it right now: the genome × any 24 h multiplier from a rule."""
        v = self.g.get(name, default)
        boost = self.gene_boost.get(name)
        if boost and boost[1] > self.w.clock.t:
            v *= boost[0]
        return v

    def gene(self, key: str) -> float:
        """Weight of a benefit (food, castle…): gene × the council's priority for it."""
        v = self.G(GENE_FOR.get(key, key))
        if self.directive is not None and self.directive_until > self.w.clock.t:
            v *= self.directive.priorities.get(key, 1.0)
        return v

    def rl_steer(self) -> bool:
        """A trained network (rules.yaml → rl) sets this kingdom's plan every rl.step_hours.
        Returns True when this kingdom is played by a network."""
        from . import rl
        pol = rl.policy_for(self.rules, self.civ.id)
        if pol is None:
            return False
        now = self.w.clock.t
        if now >= getattr(self, "_rl_next", 0.0):
            step = float((self.rules.raw.get("rl", {}) or {}).get("step_hours", 6)) * 3600
            acts = pol.act(rl.observe(self.w, self.civ))
            plan = rl.to_plan(acts, self.civ, self.w)
            self.set_directive(plan, now + step)
            self._rl_next = now + step
            up = [k for k, v in plan.priorities.items() if v > 1.0]
            self.thoughts.append((now, f"The network ({pol.name}, {pol.updates} updates): more "
                                       f"{', '.join(up) or 'of nothing'}; war: {plan.intent.replace('_', ' ')}."))
        return True

    def set_directive(self, plan, until: float) -> None:
        self.directive, self.directive_until = plan, until
        self.civ.dirty = True

    # ================================================================== main
    def decide(self) -> None:
        civ = self.civ
        if not civ.people or civ.castle is None:
            return
        self.rl_steer()                                       # a kingdom played by a trained network (PPO)
        fired = self.fire_rules()
        est = self.estimate()
        acts = self.candidates(est)
        for a in acts:
            self.score(a, est)
        self.apply_bonuses(acts, fired, est)
        acts.sort(key=lambda a: a.utility, reverse=True)
        done, self.targets = self.execute(acts, est)
        self.war_note = self.army.war(est)
        self.army.market(est)
        if done:
            est = self.estimate()       # stock and sites changed
        alloc = self.apply_moves(self.allocate(est), est)
        self.assign(alloc, est)
        self.rebalance_builders(est.now)
        self.road_note = self.roads.plan(est)
        self.road_worker(est)
        give_carts(self)
        self.last_alloc = alloc
        self.thoughts.append((self.w.clock.t, self.explain(est, alloc, done)))

    # ================================================================== estimates
    def job_distance(self, job: str) -> float:
        """Typical depot ↔ resource distance in tiles for this job (cached per hour/depot change)."""
        civ = self.civ
        key = (int(self.w.clock.t // 3600), tuple(sorted((b.id, b.built) for b in civ.buildings)))
        if key != self._dist_key:
            self._dist_cache.clear()
            self._dist_key = key
        if job in self._dist_cache:
            return self._dist_cache[job]
        spec = self.rules.jobs[job]
        cx, cy = civ.castle.center
        if spec.node == "farm":
            farms = civ.of_type("farm")
            if not farms:
                d = 9.0
            else:
                d = sum(self._nearest_depot_dist("food", *f.center) for f in farms) / len(farms)
        else:
            depots = civ.depots(spec.resource)
            sources = depots or [civ.castle]
            r2 = self.cfg["node_search_radius"] ** 2
            d = INF
            fog = self.w.fog
            for n in self.w.map.by_kind[spec.node]:
                if n.amount <= 0 or (n.x - cx) ** 2 + (n.y - cy) ** 2 > r2 or not fog.usable(civ, n):
                    continue
                for b in sources:
                    bx, by = b.center
                    d = min(d, math.hypot(n.x - bx, n.y - by))
            if d < INF:
                d = max(1.0, d - 1.5)
        self._dist_cache[job] = d
        return d

    def _nearest_depot_dist(self, res: str, x: float, y: float) -> float:
        b = self.civ.nearest_depot(res, x, y)
        return math.hypot(b.center[0] - x, b.center[1] - y) if b else 30.0

    def yield_per_day(self, job: str, season=None) -> float:
        """Daily output of one average worker at this job, travel included (doc: Y = R·h)."""
        civ, r = self.civ, self.rules
        season = season or self.w.clock.season
        spec = r.jobs[job]
        d = self.job_distance(job)
        if d >= INF:
            return 0.0
        mult = r.tool_mult(civ.tool_level(spec.tool)) * civ.morale * season.mult(spec.season)
        mult *= 1 + r.gathering["depot_bonus_per_level"] * civ.depot_level(spec.resource)
        mult *= 1 + development.bonus(civ, spec.resource)
        mult *= T.food_mult(r, civ, spec.node, spec.resource, season)
        if spec.node == "farm":
            farms = civ.of_type("farm")
            if farms:
                fb = r.buildings["farm"].level_bonus
                mult *= sum(1 + fb * (f.level - 1) for f in farms) / len(farms)
        v = r.gathering["walk_tiles_per_hour"] * season.walk
        return F.collection_rate(r.gathering["carry"], spec.rate, mult, d, v) * season.hours

    def node_slots(self, job: str, limit: int) -> int:
        civ = self.civ
        spec = self.rules.jobs[job]
        cx, cy = civ.castle.center
        r2 = self.cfg["node_search_radius"] ** 2
        n = 0
        for node in self.w.map.by_kind[spec.node]:
            if node.amount > 0 and node.id not in civ.unreachable and (node.x - cx) ** 2 + (node.y - cy) ** 2 <= r2:
                n += 1
                if n >= limit:
                    break
        return n

    def estimate(self) -> Estimate:
        civ, r, cfg = self.civ, self.rules, self.cfg
        now = self.w.clock.t
        date = self.w.clock.date
        season = self.w.clock.season
        workers = civ.available_workers(now)      # not sick, injured or on strike
        W = len(workers)
        counts = Counter(p.job for p in workers)
        yields = {job: self.yield_per_day(job) for job in r.jobs}

        has_depot = {res: bool(civ.depots(res)) for res in GATHER_RES}
        slots = {
            "farm": r.buildings["farm"].workers * len(civ.of_type("farm")),
            "hunt": self.node_slots("hunt", cfg["hunt_max_workers"]) if yields["hunt"] > 0 else 0,
            "fruit": self.node_slots("fruit", cfg["fruit_max_workers"]) if yields["fruit"] > 0 else 0,
        }
        for res, job in RES_JOB.items():
            slots[job] = W if has_depot[res] and yields[job] > 0 else 0

        income = defaultdict(float)
        for job, n in counts.items():
            if job and job in r.jobs:
                income[r.jobs[job].resource] += n * yields[job]
        income["coins"] += civ.pop * r.economy["tax_per_person_per_day"]

        # ---------------- food plan (safety rule first)
        food_day = civ.food_per_day(now)
        stock_food = civ.stock["food"]
        cap_food = civ.cap("food")
        target = self.G("food_reserve_days", 2) * food_day
        horizon = float(cfg["reserve_horizon_days"])
        winter_food = winter_wood = 0.0
        prep_from = cfg["winter_prep_from_month"]
        if prep_from <= date.month <= 11:
            winter = r.seasons["winter"]
            per_person = food_day / max(1, civ.pop)
            wf = min(slots["farm"], math.ceil(civ.pop * cfg["winter_farmer_share"]))
            winter_food = self.G("winter_prep", 1.0) * F.winter_food_reserve(
                civ.pop, wf, r.winter["days"], per_person, self.yield_per_day("farm", winter))
            winter_wood = self.G("winter_prep", 1.0) * F.winter_wood_reserve(
                len(civ.of_type("house")), r.winter["days"], winter.firewood_per_house)
            if winter_food > target:
                target = min(winter_food, cap_food * 0.95)
                days_left = (date.replace(month=12, day=1) - date).days
                horizon = max(horizon, float(days_left))
        target = min(target, cap_food * 0.95)
        # A surplus above the target is eaten first; food brought to a full store is wasted.
        need_day = max(0.0, food_day * (1 + cfg["food_safety_margin"]) + (target - stock_food) / max(1.0, horizon))
        if stock_food >= cap_food * cfg["cap_full_ratio"]:
            need_day = 0.0
        emergency = stock_food < food_day

        # Cold houses hurt morale, and low morale halves all work: firewood comes first in winter.
        firewood_workers = 0
        per_house = season.firewood_per_house
        houses = len(civ.of_type("house"))
        if per_house and houses and yields["wood"] > 0:
            burn = per_house * houses
            if civ.stock["wood"] < burn * cfg["firewood_days"]:
                firewood_workers = min(W, math.ceil(burn * 1.3 / yields["wood"]))

        plan = {j: 0 for j in FOOD_JOBS}
        left = need_day
        capacity = 0.0
        assigned = firewood_workers
        for job in sorted(FOOD_JOBS, key=lambda j: yields[j], reverse=True):
            capacity += slots[job] * yields[job]
            while plan[job] < slots[job] and left > 0 and assigned < W and yields[job] > 0:
                plan[job] += 1
                assigned += 1
                left -= yields[job]
        unmet = max(0.0, left)

        free_beds = civ.beds() - civ.pop - len(civ.recruiting)
        est = Estimate(now, date.month, civ.castle_level, W, workers, civ.pop, food_day, counts, yields,
                       slots, dict(income), free_beds, plan, need_day, unmet, capacity, target,
                       winter_food, winter_wood, emergency, firewood_workers)
        est.can_feed = self.food_outlook(est, extra=1)
        return est

    # ================================================================== food outlook
    def node_supply(self, kind: str) -> float:
        """Food per day that bushes/animals near us can give forever (they regrow)."""
        civ = self.civ
        spec = self.rules.nodes[kind]
        if not spec.regrow_days:
            return 0.0
        cx, cy = civ.castle.center
        r2 = self.cfg["node_search_radius"] ** 2
        total = sum(n.max_amount for n in self.w.map.by_kind[kind] if (n.x - cx) ** 2 + (n.y - cy) ** 2 <= r2)
        return total / spec.regrow_days

    def sustainable_food(self, people: int, season) -> float:
        """Daily food if everyone who has to works on food, with the farms we have or can pay for now."""
        civ, r, cfg = self.civ, self.rules, self.cfg
        per_farm = r.buildings["farm"].workers
        farm_slots = per_farm * len(civ.of_type("farm", built_only=False))
        if self._unlocked("farm") and civ.stock["wood"] >= r.building_cost("farm", 1)["wood"]:
            farm_slots += per_farm
        food = 0.0
        left = people
        for job, kind, limit in (("hunt", "animal", cfg["hunt_max_workers"]),
                                 ("fruit", "bush", cfg["fruit_max_workers"])):
            y = self.yield_per_day(job, season)
            supply = self.node_supply(kind) * season.mult(r.jobs[job].season)
            if y <= 0 or supply <= 0 or left <= 0:
                continue
            n = min(limit, left, math.ceil(supply / y))
            food += min(n * y, supply)
            left -= n
        food += min(left, farm_slots) * self.yield_per_day("farm", season)
        return food * 0.9      # people also walk home, eat, and do other jobs

    def food_outlook(self, est: Estimate, extra: int = 1) -> bool:
        """Day-by-day forecast: with `extra` more people, does food stay above one day's need?"""
        civ, r = self.civ, self.rules
        per = r.stages[2]["food_per_day"]
        eat = est.food_day + per * extra
        prod = {name: self.sustainable_food(est.W + extra, s) for name, s in r.seasons.items()}
        stock = civ.stock["food"] - r.people["recruit"]["food"] * extra
        cap = max(civ.cap("food"), civ.stock["food"])
        day = self.w.clock.date
        for i in range(int(self.cfg["food_outlook_days"])):
            season = r.season_of((day + timedelta(days=i)).month).name
            stock = min(cap, stock + prod[season] - eat)
            if stock < eat:
                return False
        return True

    # ================================================================== candidates
    def candidates(self, est: Estimate) -> list[Action]:
        civ, r = self.civ, self.rules
        acts: dict[tuple, Action] = {}

        def add(a: Action | None) -> None:
            if a is None:
                return
            if a.key in acts:
                acts[a.key].add(a)
            else:
                acts[a.key] = a

        L = est.L
        castle = civ.castle
        if L < r.castle_max_level:
            if not castle.under_construction:
                missing = civ.missing_requirements(L + 1, est.now)
                if not missing:
                    a = Action("castle", ("castle",), f"Castel {L + 1}", r.castle_cost(L), r.castle_hours(L),
                               {"castle": 1.5}, [f"every requirement for level {L + 1} is met"])
                    add(a)
                else:
                    share = 1.0 / len(missing)
                    for req in missing:
                        for a in self.for_requirement(req, share, est, L + 1):
                            add(a)
            elif L + 2 <= r.castle_max_level:
                # Plan ahead while the castle is being built (only with what is unlocked now).
                missing = civ.missing_requirements(L + 2, est.now)
                share = 0.5 / max(1, len(missing))
                for req in missing:
                    for a in self.for_requirement(req, share, est, L + 2):
                        add(a)
        else:
            add(self.wonder_action())                    # after castle 10: the Wonder wins the era
        for a in self.economy_actions(est):
            add(a)
        for a in self.army.actions(est):
            add(a)
        if development.enabled(civ) and not est.emergency:
            for btype, entry in development.config(civ).get("buildings", {}).items():
                # Build useful institutions once before buying extra copies.
                if civ.count(btype) == 0:
                    add(self.build_action(btype, entry.get("benefit", {}), "develop the settlement's institutions"))
                elif civ.balance("coins") > 300:
                    for building in civ.of_type(btype):
                        add(self.upgrade_action(building, entry.get("benefit", {}), "improve the existing institution"))
        return list(acts.values())

    def wonder_action(self) -> Action | None:
        r = self.rules
        wt = r.wonder.get("type", "wonder")
        if wt not in r.buildings or getattr(self.w, "era_winner", None) is not None or not self._unlocked(wt):
            return None
        return Action("build", ("build", wt, "wonder"), r.buildings[wt].label, r.building_cost(wt, 1),
                      r.building_hours(wt, 1), {"castle": 2.0}, ["castle 10: the first Wonder wins the era"], btype=wt)

    def _unlocked(self, btype: str) -> bool:
        spec = self.rules.buildings[btype]
        if spec.since_version > self.rules.raw.get("version", 1):
            return False
        if spec.castle > self.civ.castle_level:
            return False
        if not development.unlocked(self.civ, btype):
            return False
        return spec.max is None or len(self.civ.of_type(btype, built_only=False)) < spec.max

    def build_action(self, btype: str, benefit: dict, reason: str, tag: object = 0) -> Action | None:
        if not self._unlocked(btype):
            return None
        r = self.rules
        return Action("build", ("build", btype, tag), r.buildings[btype].label, r.building_cost(btype, 1),
                      r.building_hours(btype, 1), dict(benefit), [reason], btype=btype)

    def upgrade_action(self, b: Building, benefit: dict, reason: str) -> Action | None:
        r = self.rules
        new = b.level + 1
        if b.under_construction or not b.built or new > r.max_building_level(self.civ.castle_level):
            return None
        return Action("upgrade", ("upgrade", b.id), f"{r.buildings[b.type].label} {new}",
                      r.building_cost(b.type, new), r.building_hours(b.type, new), dict(benefit), [reason],
                      btype=b.type, building=b)

    def research_action(self, craft: str, benefit: dict, reason: str) -> Action | None:
        civ, r = self.civ, self.rules
        level = civ.tool_level(craft) + 1
        if level > r.tools["max_level"] or civ.researching(craft):
            return None
        req = r.tool_requires(level)
        forge = max((b.level for b in civ.of_type("forge")), default=0)
        if forge < req["forge"] or civ.castle_level < req["castle"]:
            return None
        return Action("research", ("research", craft), f"Level {level} tools ({r.tools['labels'][craft]})",
                      r.tool_cost(level), r.tool_hours(level), dict(benefit), [reason], craft=craft)

    def farm_slots_planned(self) -> int:
        return self.rules.buildings["farm"].workers * len(self.civ.of_type("farm", built_only=False))

    def recruit_action(self, benefit: dict, reason: str) -> Action | None:
        rec = self.rules.people["recruit"]
        return Action("recruit", ("recruit",), "New worker", {"food": rec["food"]}, rec["hours"],
                      dict(benefit), [reason])

    def craft_workers(self, est: Estimate) -> Counter:
        out = Counter()
        for job, n in est.counts.items():
            if job == "build":
                out["builder"] += n
            elif job in self.rules.jobs:
                out[self.rules.jobs[job].tool] += n
        return out

    def for_requirement(self, req: dict, share: float, est: Estimate, level: int) -> list[Action]:
        civ, r = self.civ, self.rules
        why = f"needed for castle {level}"
        ben = {"castle": share}
        out: list[Action | None] = []
        if "building" in req:
            btype, count, ml = req["building"], req.get("count", 1), req.get("level", 1)
            if r.buildings[btype].castle > civ.castle_level:
                return []
            all_b = civ.of_type(btype, built_only=False)
            if len(all_b) < count:
                for i in range(min(2, count - len(all_b))):
                    out.append(self.build_action(btype, ben, why, tag=("req", i)))
            if ml > 1:
                ok = sum(1 for b in all_b if b.level >= ml or (b.under_construction and b.target_level >= ml))
                below = sorted((b for b in all_b if b.built and b.level < ml and not b.under_construction),
                               key=lambda b: -b.level)
                for b in below[: max(0, count - ok)]:
                    out.append(self.upgrade_action(b, ben, why))
        elif "workers" in req:
            if est.W + len(civ.recruiting) < min(req["workers"], r.workers_max(civ.castle_level)):
                if est.free_beds > 0:
                    if est.can_feed:
                        out.append(self.recruit_action({"castle": share, "houses": 0.3}, why))
                    elif self.farm_slots_planned() < est.W + 1:
                        out.append(self.build_action("farm", {"castle": share, "food": 0.5},
                                                     f"{why}: first food for one more person", tag="feed"))
                else:
                    out.append(self.build_action("house", ben, f"{why}: not enough beds", tag="beds"))
        elif "tools" in req or "tools_all" in req:
            ml = req.get("level", req.get("tools_all"))
            want = req.get("tools", len(r.crafts))
            have = sum(1 for c in r.crafts if civ.tool_level(c) >= ml)
            busy = sum(1 for j in civ.research if j["level"] >= ml)
            forges = civ.of_type("forge", built_only=False)
            if not forges:
                out.append(self.build_action("forge", ben, f"{why}: tools"))
            elif ml <= r.tools["max_level"]:
                need_forge = r.tool_requires(ml)["forge"]
                built = [b for b in forges if b.built]
                if built and max(b.level for b in built) < need_forge:
                    out.append(self.upgrade_action(max(built, key=lambda b: b.level), ben,
                                                   f"{why}: the forge must be level {need_forge}"))
            by_use = self.craft_workers(est)
            crafts = sorted((c for c in r.crafts if civ.tool_level(c) < ml and not civ.researching(c)),
                            key=lambda c: -by_use[c])
            for c in crafts[: max(0, want - have - busy)]:
                out.append(self.research_action(c, ben, why))
        elif "economic_all" in req:
            ml = req["economic_all"]
            eco = [b for b in civ.buildings if b.type in r.buildings and r.buildings[b.type].economic
                   and b.built and b.level < ml]
            for b in sorted(eco, key=lambda b: b.level)[:2]:
                out.append(self.upgrade_action(b, ben, why))
        elif "soldiers" in req:
            out += self.army.for_soldiers(req, share, est, level)
        return [a for a in out if a is not None]

    def economy_actions(self, est: Estimate) -> list[Action]:
        civ, r, cfg = self.civ, self.rules, self.cfg
        out: list[Action | None] = []
        L = est.L
        wmax = r.workers_max(L)
        house_sites = [b for b in civ.of_type("house", built_only=False) if b.level == 0]
        future_beds = est.free_beds + sum(r.people["house_beds_base"] + 1 for _ in house_sites)

        # Beds for the next recruits
        if future_beds <= 1 and est.W < wmax and civ.pop + len(civ.soldiers) < r.people["pop_cap"]:
            out.append(self.build_action("house", {"houses": 1.0 if future_beds <= 0 else 0.6},
                                         f"free beds: {max(0, est.free_beds)}", tag="econ"))

        # More people, if we can feed them through the coming months
        per_person = r.stages[2]["food_per_day"]
        feedable = est.can_feed
        if est.W + len(civ.recruiting) < wmax and est.free_beds > 0 and not civ.recruiting                 and civ.pop + len(civ.soldiers) < r.people["pop_cap"]:
            if feedable:
                out.append(self.recruit_action({"houses": 0.9}, f"workers {est.W}/{wmax}"))

        # Food capacity
        farm_slots = est.slots["farm"]
        farm_sites = len([b for b in civ.of_type("farm", built_only=False) if b.level == 0])
        y_farm = max(1.0, est.yield_day["farm"])
        if farm_sites == 0:
            want_slots = math.ceil((est.food_day + per_person * 2) * 1.1 / y_farm)
            more_farmers_possible = farm_slots < est.W + 1     # a farm nobody can work is wasted wood
            if more_farmers_possible and (est.food_unmet_day > 0 or not feedable):
                ben = min(1.2, 0.5 + est.food_unmet_day / y_farm)
                out.append(self.build_action("farm", {"food": ben}, "not enough food for everyone", tag="econ"))
            elif farm_slots < want_slots:
                out.append(self.build_action("farm", {"food": 0.5}, "more farms to grow", tag="econ"))

        # Depots near work that is far away
        for res, btype in DEPOT_FOR.items():
            job = "farm" if res == "food" else res
            n = est.counts.get(job, 0) + (est.counts.get("hunt", 0) if res == "food" else 0)
            d = self.job_distance(job)
            if n >= 2 and cfg["depot_far_tiles"] < d < INF and \
                    not any(b.level == 0 for b in civ.of_type(btype, built_only=False)):
                v = r.gathering["walk_tiles_per_hour"]
                spec = r.jobs[job]
                gain = F.collection_rate(10, spec.rate, 1, 3, v) / F.collection_rate(10, spec.rate, 1, d, v) - 1
                ben = min(1.0, gain * 3 * n / max(1, est.W))
                out.append(self.build_action(btype, {res: ben}, f"long walk to the depot ({d:.0f} tiles)",
                                             tag="econ"))

        # Mines open new resources
        if L >= r.buildings["iron_mine"].castle and not civ.of_type("iron_mine", built_only=False):
            out.append(self.build_action("iron_mine", {"ore": 0.6}, "we need ore", tag="econ"))
        if L >= r.buildings["gold_mine"].castle and not civ.of_type("gold_mine", built_only=False):
            out.append(self.build_action("gold_mine", {"gold": 0.6}, "we need gold", tag="econ"))

        # Storage: winter reserve or goals larger than the barns
        if L >= r.buildings["granary"].castle and not any(b.level == 0 for b in civ.of_type("granary", built_only=False)):
            cap_food = civ.cap("food")
            pressure = 0.0
            if est.winter_food > cap_food:
                pressure = max(pressure, self.G("winter_prep", 1.0) * min(1.0, (est.winter_food - cap_food) / 2000))
            for t in self.targets:
                for res, v in t.cost.items():
                    if v > civ.cap(res) * 0.9:
                        pressure = max(pressure, 0.8)
            if pressure > 0:
                granaries = civ.of_type("granary")
                if self._unlocked("granary"):
                    out.append(self.build_action("granary", {"storage": pressure}, "the granaries are too small", tag="econ"))
                elif granaries:
                    b = min(granaries, key=lambda b: b.level)
                    out.append(self.upgrade_action(b, {"storage": pressure}, "the granaries are too small"))

        # Wells cut the risk of fire and disease (how much we care: the prudence gene)
        if self._unlocked("well") and r.feature("events") and not any(
                b.level == 0 for b in civ.of_type("well", built_only=False)):
            wooden = [b for b in civ.buildings if b.built and b.type in r.buildings
                      and r.buildings[b.type].material == "wood"]
            covered = [b for b in wooden if any(math.dist(b.center, w.center) <= r.events["fire"]["well_radius"]
                                                for w in civ.of_type("well", built_only=False))]
            uncovered = len(wooden) - len(covered)
            if uncovered >= 3 or not civ.of_type("well", built_only=False):
                ben = min(1.0, 0.2 + uncovered / 10)
                out.append(self.build_action("well", {"safety": ben}, f"{uncovered} wooden buildings without a well",
                                             tag="econ"))

        # Tools
        if L >= r.buildings["forge"].castle and not civ.of_type("forge", built_only=False):
            out.append(self.build_action("forge", {"tools": 0.5}, "no forge, no tools", tag="econ"))
        if civ.of_type("forge"):
            by_use = self.craft_workers(est)
            for craft, n in by_use.items():
                if n <= 0:
                    continue
                t = civ.tool_level(craft)
                gain = r.tools["bonus_per_level"] / r.tool_mult(t)
                out.append(self.research_action(craft, {"tools": gain * 3 * n / max(1, est.W)},
                                                f"{n} people use these tools"))
        out += self.later_actions(est)
        return [a for a in out if a is not None]

    # ================================================================== version 7: techs and carts
    TECH_BENEFIT = {"plow": {"food": 0.45}, "wheel": {"wood": 0.15, "stone": 0.15}, "writing": {"tools": 0.25},
                    "irrigation": {"food": 0.3}, "crop_rotation": {"food": 0.3}, "water_mill": {"food": 0.45},
                    "medicine": {"safety": 0.5}}

    def later_actions(self, est: Estimate) -> list[Action]:
        civ, r = self.civ, self.rules
        out: list[Action] = []
        farmers = est.counts.get("farm", 0) + est.counts.get("hunt", 0) + est.counts.get("fruit", 0)
        share = farmers / max(1, est.W)
        for name in T.available(r, civ):
            spec = r.tech[name]
            ben = dict(self.TECH_BENEFIT.get(name, {"tools": 0.2}))
            if "food" in ben:
                ben["food"] *= 0.5 + share                      # worth more when many people grow food
            if name == "wheel" and not civ.of_type("lumber_camp"):
                ben = {k: v * 0.5 for k, v in ben.items()}
            out.append(Action("tech", ("tech", name), spec["label"], dict(spec["cost"]), float(spec["hours"]),
                              ben, [spec["effect"]], craft=name))
        c = r.carts
        camps = civ.of_type(c.get("building", "lumber_camp"))
        if r.feature("roads") and civ.has_tech(c.get("needs_tech", "wheel")) and civ.carts < c.get("max", 6) \
                and any(b.level >= c.get("building_level", 2) for b in camps):
            far = [j for j in ("wood", "stone", "ore", "gold") if est.counts.get(j, 0) and self.job_distance(j) < INF]
            if far:
                j = max(far, key=self.job_distance)
                gain = min(1.0, self.job_distance(j) / 40)
                out.append(Action("cart", ("cart",), "Cart", dict(c["cost"]), 0.0,
                                  {r.jobs[j].resource: gain}, [f"a cart carries 50 on the long walk to the {j}"]))
        return out

    def road_worker(self, est: Estimate) -> None:
        """While a road is being built, one worker lays it (taken from the builders if we can)."""
        civ = self.civ
        if civ.road_job is None or any(p.job == "road" for p in est.workers):
            return
        if est.emergency or len(est.workers) < 4:
            return
        builders = [p for p in est.workers if p.job == "build"]
        pick = builders[-1] if len(builders) >= 2 else None
        if pick is None:
            counts = Counter(p.job for p in est.workers if p.job in ("wood", "stone", "ore", "gold"))
            if counts:
                job = counts.most_common(1)[0][0]
                pick = next(p for p in reversed(est.workers) if p.job == job)
        if pick is not None:
            self.set_job(pick, "road", est.now)

    # ================================================================== scoring
    def wait_hours(self, cost: dict[str, float], est: Estimate, progressive: bool = False) -> float:
        civ = self.civ
        worst = 0.0
        for res, v in cost.items():
            short = v - civ.balance(res)
            if short <= 0:
                continue
            if not progressive and v > civ.cap(res) + 1e-9:
                return INF
            per_hour = est.income_day.get(res, 0.0) / 24
            if res not in civ.stock:
                per_hour = development.material_rate(civ, res)
            if per_hour <= 1e-9:
                if res == "coins":
                    return INF
                job = "farm" if res == "food" else RES_JOB.get(res)
                pot = est.yield_day.get(job, 0.0) if job and est.slots.get(job, 0) > 0 else 0.0
                if pot <= 0:
                    return INF
                per_hour = pot / 24 * 0.5
            worst = max(worst, short / per_hour)
        return worst

    def score(self, a: Action, est: Estimate) -> None:
        r = self.rules
        builders = max(1, min(3, est.W // 3))
        tool = r.tool_mult(self.civ.tool_level("builder"))
        season = self.w.clock.season
        if a.kind == "castle":
            hours = r.castle_one_builder_hours(est.L) / (F.builder_power(max(builders, 2)) * tool * season.build)
            wait = 0.0 if r.castle["progressive_materials"] else self.wait_hours(a.cost, est)
            # Materials still arrive over time: count part of the wait.
            wait = max(wait, 0.5 * self.wait_hours(a.cost, est, progressive=True))
        elif a.kind in ("build", "upgrade"):
            hours = a.hours / (F.builder_power(builders) * tool * season.build)
            if a.kind == "build" and r.progressive(a.btype):      # the Wonder: materials arrive while building
                wait = 0.5 * self.wait_hours(a.cost, est, progressive=True)
            else:
                wait = self.wait_hours(a.cost, est)
        else:
            hours = a.hours
            wait = self.wait_hours(a.cost, est)
        a.wait = wait
        genes = {k: self.gene(k) for k in a.benefit}
        a.utility = 0.0 if wait >= INF else F.utility(a.benefit, genes, wait, hours)

    # ================================================================== execution
    def place(self, btype: str) -> tuple[int, int] | None:
        civ, r, m = self.civ, self.rules, self.w.map
        spec = r.buildings[btype]
        cc = civ.castle.center
        if spec.near or btype in ("quarry", "lumber_camp"):
            kind = spec.near or ("stone" if btype == "quarry" else "tree")
            node = self._nearest_node(kind, cc)
            if node is None:
                return None
            anchor = node.center
            if kind == "tree":
                close = [n for n in m.by_kind["tree"] if n.amount > 0
                         and (n.x - node.x) ** 2 + (n.y - node.y) ** 2 <= 36]
                anchor = (sum(n.x for n in close) / len(close), sum(n.y for n in close) / len(close))
            return find_site(m, anchor, spec.size, radius=8, flip=civ.mirror)
        if btype == "mill":
            farms = civ.of_type("farm")
            anchor = (sum(f.center[0] for f in farms) / len(farms), sum(f.center[1] for f in farms) / len(farms)) \
                if farms else cc
            return find_site(m, anchor, spec.size, radius=12, flip=civ.mirror)
        if btype == "farm":
            return find_site(m, cc, spec.size, radius=22, min_dist=4, flip=civ.mirror)
        if btype == "house":
            return find_site(m, cc, spec.size, radius=20, min_dist=3, flip=civ.mirror)
        return find_site(m, cc, spec.size, radius=22, min_dist=3, flip=civ.mirror)

    def _nearest_node(self, kind: str, at: tuple[float, float]):
        civ = self.civ
        r2 = self.cfg["node_search_radius"] ** 2
        best, best_d = None, INF
        for n in self.w.map.by_kind[kind]:
            if n.amount <= 0 or n.id in civ.unreachable or not self.w.fog.usable(civ, n):
                continue
            d = (n.x - at[0]) ** 2 + (n.y - at[1]) ** 2
            if d <= r2 and d < best_d:
                best, best_d = n, d
        return best

    def execute(self, acts: list[Action], est: Estimate) -> tuple[list[Action], list[Action]]:
        civ, w, cfg = self.civ, self.w, self.cfg
        reserved: dict[str, float] = defaultdict(float)
        castle = civ.castle
        if castle.under_construction:
            # Scarce materials go to the castle first; wood and stone are easy to gather again.
            for res, total in self.rules.castle_cost(castle.level).items():
                if res in ("coins", "ore", "gold"):
                    reserved[res] += min(civ.stock[res], max(0.0, total - castle.paid.get(res, 0.0)))
        for site in civ.sites():                     # the Wonder's scarce materials are kept for it too
            if site.type != "castle" and self.rules.progressive(site.type):
                for res, total in self.rules.site_cost(site).items():
                    if res in ("coins", "ore", "gold"):
                        reserved[res] += min(max(0.0, civ.stock[res] - reserved[res]),
                                             max(0.0, total - site.paid.get(res, 0.0)))
        max_sites = cfg["max_sites_base"] + est.W // cfg["workers_per_extra_site"]
        sites = len([s for s in civ.sites() if s.type != "castle"])
        done: list[Action] = []
        targets: list[Action] = []
        for a in acts:
            if a.utility <= 1e-6:
                continue
            if a.kind in ("build", "upgrade") and sites >= max_sites:
                continue
            if a.kind == "research" and len(civ.research) >= civ.research_slots():
                continue
            if a.kind == "recruit" and civ.recruiting:
                continue
            if a.kind == "train" and len(civ.training) >= self.rules.army.get("queue_per_building", 5):
                continue
            if a.kind == "tech" and civ.tech_job is not None:
                continue
            reason = "; ".join(dict.fromkeys(a.reasons))
            if a.kind == "castle":
                w.start_castle_upgrade(civ, reason)
                done.append(a)
                continue
            extra = {"food": est.food_day} if "food" in a.cost else {}
            need = {k: reserved.get(k, 0.0) + extra.get(k, 0.0) for k in set(reserved) | set(extra)}
            progressive = a.kind == "build" and self.rules.progressive(a.btype)
            if progressive or civ.can_afford(a.cost, need):
                if a.kind == "build":
                    spot = self.place(a.btype)
                    if spot is None:
                        continue
                    w.start_building(civ, a.btype, spot[0], spot[1], reason)
                    sites += 1
                elif a.kind == "upgrade":
                    w.start_upgrade(civ, a.building, reason)
                    sites += 1
                elif a.kind == "research":
                    w.start_research(civ, a.craft, reason)
                elif a.kind == "recruit":
                    w.start_recruit(civ, reason)
                elif a.kind == "train":
                    if not w.military.start_training(civ, a.btype, reason):
                        continue
                elif a.kind == "tech":
                    w.start_tech(civ, a.craft, reason)
                elif a.kind == "promote":
                    if not w.military.promote(civ, a.btype, reason):
                        continue
                elif a.kind == "cart":
                    civ.pay(a.cost)
                    civ.carts += 1
                    w.log(civ, "cart", f"A new cart at the lumber camp ({civ.carts} in all). {reason}", 3)
                done.append(a)
            elif len(targets) < 2:
                targets.append(a)
                for res, v in a.cost.items():
                    reserved[res] += min(v, max(0.0, civ.stock.get(res, 0.0) - reserved[res]))
        return done, targets

    # ================================================================== allocation
    def deficits(self, est: Estimate) -> dict[str, float]:
        civ, r = self.civ, self.rules
        need: dict[str, float] = defaultdict(float)
        for a in self.targets:
            for res, v in a.cost.items():
                need[res] += v
        castle = civ.castle
        if castle.under_construction:
            for res, total in r.castle_cost(castle.level).items():
                need[res] += max(0.0, total - castle.paid.get(res, 0.0))
        need["food"] += est.food_target
        if est.winter_wood:
            need["wood"] += min(est.winter_wood, civ.cap("wood") * 0.9)
        fw = self.w.clock.season.firewood_per_house
        if fw:
            need["wood"] += fw * len(civ.of_type("house")) * 7
        out = {}
        for res in GATHER_RES:
            n = need.get(res, 0.0)
            out[res] = min(1.0, max(0.0, (n - civ.stock[res]) / n)) if n > 0 else 0.0
        return out

    def builder_slots(self) -> tuple[int, bool]:
        """How many builders the open sites can use, and whether the castle waits for materials."""
        civ, r = self.civ, self.rules
        limit = r.construction["max_builders"]
        total = 0
        starved = False
        for s in civ.sites():
            if s.type == "castle":
                cost = r.castle_cost(s.level)
                reach = min(((s.paid.get(res, 0.0) + civ.stock[res]) / v for res, v in cost.items() if v > 0),
                            default=1.0)
                starved = reach - s.progress < 0.01       # less than 1% of materials on hand
                total += 1 if starved else limit
            else:
                total += min(3, limit)
        return total, starved

    def allocate(self, est: Estimate) -> dict[str, int]:
        civ, cfg = self.civ, self.cfg
        W = est.W
        alloc: dict[str, int] = {job: 0 for job in self.rules.jobs}
        alloc["build"] = 0
        # A fire comes before everything: 2 people per burning building.
        burning = sum(1 for b in civ.buildings if b.on_fire)
        alloc["fire"] = min(W, burning * self.rules.events.get("fire", {}).get("firefighters", 2))
        left = W - alloc["fire"]
        alloc["wood"] = min(left, est.firewood_workers)
        left -= alloc["wood"]
        for job, n in est.food_plan.items():
            alloc[job] = min(n, left)
            left -= alloc[job]
        rem = left

        slots, starved = self.builder_slots()
        if est.emergency:
            # Hungry: everyone on food, except one builder to finish a farm that would feed us.
            if rem > 0 and any(s.type == "farm" for s in civ.sites()):
                alloc["build"] = 1
                rem -= 1
        elif slots and rem > 0:
            share = min(0.7, cfg["builder_share_base"] + cfg["builder_share_per_gene"] * self.G("g_castle", 1.0))
            nb = max(1, round(rem * share))
            if civ.castle.under_construction and not starved:
                nb = max(nb, min(cfg["castle_min_builders"], rem))
            alloc["build"] = min(nb, slots, rem)
            rem -= alloc["build"]

        if rem <= 0:
            return alloc
        deficit = self.deficits(est)
        weights = {}
        for res in GATHER_RES:
            if civ.stock[res] >= civ.cap(res) * cfg["cap_full_ratio"]:
                continue   # nowhere to put it
            if res == "food":
                spare = sum(max(0, est.slots[j] - alloc[j]) for j in FOOD_JOBS if est.yield_day[j] > 0)
                if spare <= 0:
                    continue
            elif est.slots.get(RES_JOB[res], 0) <= 0:
                continue
            weights[res] = self.gene(res) * (0.1 + deficit[res])
        split = F.allocate_proportional(rem, weights) if weights else {}
        for res, n in split.items():
            if res == "food":
                for job in sorted(FOOD_JOBS, key=lambda j: est.yield_day[j], reverse=True):
                    take = min(n, max(0, est.slots[job] - alloc[job])) if est.yield_day[job] > 0 else 0
                    alloc[job] += take
                    n -= take
                if n > 0:   # no free food slots: give them to the emptiest other store
                    others = [r for r in weights if r != "food"]
                    if others:
                        best = min(others, key=lambda r: civ.stock[r] / civ.cap(r))
                        alloc[RES_JOB[best]] += n
            else:
                alloc[RES_JOB[res]] += n
        return alloc

    def assign(self, alloc: dict[str, int], est: Estimate) -> None:
        now = est.now
        dwell = self.cfg["min_dwell_minutes"] * 60
        beh = self.w.behavior
        by_job: dict[str | None, list[Person]] = defaultdict(list)
        for p in est.workers:
            by_job[p.job].append(p)
        pool: list[Person] = list(by_job.pop(None, []))
        road = by_job.pop("road", [])                         # version 7: one worker keeps laying the road
        if self.civ.road_job is not None and road:
            pool += road[1:]
        else:
            pool += road
        fire = alloc.get("fire", 0) > 0
        for job, ppl in by_job.items():
            want = alloc.get(job, 0)
            extra = len(ppl) - want
            if extra <= 0:
                continue
            urgent = want == 0 or est.emergency or fire or job not in alloc
            ppl.sort(key=lambda p: (now - p.assigned_t < dwell, beh.xp(p, job), p.assigned_t))
            for p in ppl:
                if extra <= 0:
                    break
                if not urgent and now - p.assigned_t < dwell:
                    continue
                pool.append(p)
                extra -= 1
        have = Counter(p.job for p in est.workers if p not in pool)
        order = ["fire", "wood", *FOOD_JOBS, "build", "stone", "ore", "gold"]
        for job in order:
            need = alloc.get(job, 0) - have.get(job, 0)
            while need > 0 and pool:
                p = max(pool, key=lambda q: self.fit(q, job))
                pool.remove(p)
                self.set_job(p, job, now)
                have[job] += 1
                need -= 1
        for p in pool:   # not needed anywhere useful (e.g. all stores full): rest at home
            self.set_job(p, None, now)

    # ================================================================== version 3: rules and council
    def fire_rules(self) -> list:
        """Rules whose conditions hold right now. Gene multipliers take effect immediately (24 h)."""
        from .rulebook import rule_state

        self._moves = []
        if not self.book:
            return []
        state = rule_state(self.w, self.civ)
        now = self.w.clock.t
        fired = []
        for rule in self.book:
            if not rule.holds(state):
                continue
            fired.append(rule)
            key = rule.text()
            self.activations[key] = self.activations.get(key, 0) + 1
            act = rule.action
            if act.kind == "gene":
                self.gene_boost[act.target] = (act.amount, now + 86400)
            elif act.kind == "move":
                self._moves.append((act.target, int(act.amount)))
        return fired

    def apply_bonuses(self, acts: list[Action], fired: list, est: Estimate) -> None:
        """U'(a) = U(a) + PRIORITY · U_max for the action a rule (or the king) asks for."""
        u_max = max((a.utility for a in acts), default=0.0) or 0.1
        wishes: list[tuple[str, str, float, str]] = []     # (kind, target, priority, why)
        for rule in fired:
            if rule.action.kind in ("build", "upgrade_tool"):
                wishes.append((rule.action.kind, rule.action.target, rule.priority, f'rule "{rule.name}"'))
        plan = self.directive if self.directive_until > self.w.clock.t else None
        if plan is not None and plan.build_next:
            wishes.append(("build", plan.build_next, 0.5, "the king's order"))
        for kind, target, prio, why in wishes:
            if kind == "build":
                match = [a for a in acts if a.btype == target and a.kind in ("build", "upgrade")]
                if not match:
                    extra = self.build_action(target, {}, why, tag=("wish", why))
                    if extra is not None:
                        self.score(extra, est)
                        if extra.wait < INF:
                            acts.append(extra)
                            match = [extra]
            else:
                match = [a for a in acts if a.kind == "research" and a.craft == target]
                if not match:
                    extra = self.research_action(target, {}, why)
                    if extra is not None:
                        self.score(extra, est)
                        if extra.wait < INF:
                            acts.append(extra)
                            match = [extra]
            for a in match:
                a.utility += prio * u_max
                a.reasons.append(why)

    def apply_moves(self, alloc: dict[str, int], est: Estimate) -> dict[str, int]:
        """'move N workers to X': shift workers, but never below the food, firewood or fire minimum."""
        if not self._moves:
            return alloc
        floor = {j: n for j, n in est.food_plan.items()}
        floor["wood"] = est.firewood_workers
        floor["fire"] = alloc.get("fire", 0)
        for res, n in self._moves:
            if res == "food":
                job = max(FOOD_JOBS, key=lambda j: (est.slots[j] - alloc[j], est.yield_day[j]))
            else:
                job = RES_JOB[res]
            room = est.slots.get(job, 0) - alloc.get(job, 0)
            n = min(n, max(0, room))
            donors = sorted((j for j in ("build", "gold", "ore", "stone", "wood", *FOOD_JOBS) if j != job),
                            key=lambda j: alloc.get(j, 0) - floor.get(j, 0), reverse=True)
            for d in donors:
                while n > 0 and alloc.get(d, 0) > floor.get(d, 0):
                    alloc[d] -= 1
                    alloc[job] = alloc.get(job, 0) + 1
                    n -= 1
        return alloc

    def rebalance_builders(self, now: float) -> None:
        """Spread builders over sites: every site gets one (shortest job first), then fill up."""
        civ = self.civ
        sites = sorted(civ.sites(), key=self.w.remaining_hours)
        builders = [p for p in civ.people if p.job == "build"]
        if not sites or not builders:
            return
        limit = self.rules.construction["max_builders"]
        quota = {s.id: 0 for s in sites}
        left = len(builders)
        for s in sites:                       # round 1: one each
            if left:
                quota[s.id] += 1
                left -= 1
        for s in sites:                       # round 2: castle up to 5, others up to 3
            cap = limit if s.type == "castle" else min(3, limit)
            add = min(left, cap - quota[s.id])
            quota[s.id] += add
            left -= add
        by_id = {s.id: s for s in sites}
        movers = []
        for p in builders:
            t = p.target
            if isinstance(t, Building) and t.id in quota and quota[t.id] > 0:
                quota[t.id] -= 1
            else:
                movers.append(p)
        for p in movers:
            sid = next((sid for sid, q in quota.items() if q > 0), None)
            if sid is None:
                break
            quota[sid] -= 1
            p.target = by_id[sid]
            if p.state in (State.BUILD, State.WALK) and p.intent in (Intent.BUILD, Intent.NONE):
                p.state = State.IDLE

    def fit(self, p: Person, job: str) -> float:
        score = 10 * self.w.behavior.xp(p, job)
        if p.job == job:
            score += 1.0
        t = p.trait
        if t == "strong" and job in ("stone", "ore", "gold"):
            score += 0.3
        elif t == "quick" and job in ("wood", "hunt"):
            score += 0.2
        elif t == "lazy" and job == "build":
            score -= 0.2
        return score

    def set_job(self, p: Person, job: str | None, now: float) -> None:
        if p.job == job:
            return
        self.w.behavior.release(p)
        p.job = job
        p.assigned_t = now
        if p.state in (State.GATHER, State.BUILD) or (p.state == State.WALK and p.intent in (Intent.GATHER, Intent.BUILD)):
            p.state = State.IDLE

    # ================================================================== explanation
    def explain(self, est: Estimate, alloc: dict[str, int], done: list[Action]) -> str:
        parts = []
        jobs = ", ".join(f"{n} {j}" for j, n in alloc.items() if n)
        parts.append(f"Workers: {jobs or 'none'}.")
        parts.append(f"Food: {self.civ.stock['food']:.0f} (eating {est.food_day:.0f}/day, target {est.food_target:.0f}).")
        if done:
            parts.append("Started: " + ", ".join(a.label for a in done) + ".")
        if self.targets:
            t = self.targets[0]
            short = {k: v - self.civ.stock.get(k, 0) for k, v in t.cost.items() if v > self.civ.stock.get(k, 0)}
            miss = ", ".join(f"{v:.0f} {RES_LABEL.get(k, k)}" for k, v in short.items())
            parts.append(f"Saving for {t.label}" + (f" (missing {miss})." if miss else "."))
        if self.road_note:
            parts.append(f"Planned {self.road_note}.")
        if self.civ.soldiers or self.war_note:
            army = f"Army: {len(self.civ.soldiers)} soldiers"
            parts.append(army + (f", {self.war_note}." if self.war_note else "."))
        return " ".join(parts)
