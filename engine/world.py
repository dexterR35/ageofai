"""The world: map, clock, civilizations and the simulation tick.

One `World` serves both modes:
  * live mode  — `step(1.0)` once per real second, following the wall clock;
  * fast mode  — `run(seconds)` with 10 s ticks, skipping quiet nights in one step.
"""
from __future__ import annotations

import math
import random
from datetime import datetime
from typing import Callable

from . import formulas as F
from . import development
from .behavior import Behavior, TickCtx
from .civ import Civilization
from .clock import Clock, Phase
from .bandits import BanditCamp
from .diplomacy import Diplomacy
from .fog import Fog
from .military import Military
from .trade import Trade
from .entities import Building, Event, Journal, Person, State, SECONDS_PER_DAY
from .events import Events
from .pathfinding import PathFinder
from .rules import Rules, default_rules, name_lists
from .society import Society
from .worldmap import Node, castle_positions, find_site, generate_map


class World:
    def __init__(self, rules: Rules | None = None, *, seed: int = 1, start: datetime | float | None = None,
                 genomes: list | None = None, tz=None, echo: bool = False, ai: bool = True,
                 event_seed: int | None = None):
        from ai.genome import Genome
        from ai.tactical import TacticalAI

        self.rules = rules = rules or default_rules()
        self.seed = seed
        self.rng = random.Random(seed * 7919 + 17)
        self.map = generate_map(rules, seed)
        self.paths = PathFinder(self.map)
        if start is None:
            start_t = datetime.now().timestamp()
        elif isinstance(start, datetime):
            start_t = start.timestamp()
        else:
            start_t = float(start)
        self.clock = Clock(rules, start_t, tz)
        self.start_t = start_t
        self.start_date = self.clock.date
        self.journal = Journal(echo=echo)
        self.behavior = Behavior(self)
        self.society = Society(self)
        self.events = Events(self, seed if event_seed is None else event_seed)
        self.military = Military(self)                 # version 5
        self.diplomacy = Diplomacy(self)
        self.trade = Trade(self)
        self.diplomat = None                           # ai.diplomat.Diplomat (set below when the AI plays)
        self.live = False                              # the live world (rebirth after 7 days, the local AI)
        self._last_phase = self.clock.phase
        self._last_id = 0                          # a plain counter (it is saved with the world)
        self._depleted: list[Node] = []
        self.ticks = 0
        self.civs: list[Civilization] = []
        positions = castle_positions(rules)
        for i, spec in enumerate(rules.civs):
            genome = genomes[i] if genomes and i < len(genomes) else Genome.default(rules)
            civ = self._found_civ(spec, positions[i], genome)
            if ai:
                civ.ai = TacticalAI(civ, self)
        self.fog = Fog(self)                           # version 7
        self.bandits = BanditCamp(self) if rules.feature("bandits") and len(self.civs) >= 2 else None
        if ai:
            from ai.diplomat import Diplomat
            self.diplomat = Diplomat(self)
        self._next_tactical = start_t
        # after castle 10: the first finished Wonder wins the era; the live world then starts a new one
        self.era = 1
        self.eras: list[dict] = []                     # past eras: {"era", "winner", "name", "day", "date"}
        self.era_winner: str | None = None
        self.new_era_at: float | None = None
        self._last_decide = -math.inf
        self.rivalry_feed: list[dict] = []             # scoreboard facts; the live runner moves them to SQLite

    # ================================================================== saving (version 6)
    def __getstate__(self) -> dict:
        """What a save keeps: everything but the path cache and the live hooks (they belong to the
        running server and are put back after loading)."""
        state = self.__dict__.copy()
        state["paths"] = None
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        self.paths = PathFinder(self.map)

    def adopt(self, other: "World") -> None:
        """Become `other` (a world loaded from a save) while keeping this object, so everything that
        holds a reference to it (the server, the page, the local AI) keeps working."""
        self.__dict__.update(other.__dict__)
        # every part that pointed at `other` must point here now (fog, bandits, the AI's planners…);
        # one left behind would read stale counters, e.g. hand out unit ids that are already taken
        parts = list(self.__dict__.values())
        for civ in self.civs:
            if civ.ai is not None:
                parts.append(civ.ai)
                parts += vars(civ.ai).values()
        for part in parts:
            if getattr(part, "w", None) is other:
                part.w = self

    def detach_hooks(self) -> dict:
        """Take the live hooks off before pickling; returns them so they can be put back."""
        hooks = {"sink": self.journal.sink, "on_message": self.diplomacy.on_message,
                 "rebirth": self.military.rebirth_genome}
        if self.diplomat is not None:
            hooks["writer"], hooks["answerer"] = self.diplomat.writer, self.diplomat.answerer
            self.diplomat.writer = self.diplomat.answerer = None
        self.journal.sink = None
        self.diplomacy.on_message = None
        self.military.rebirth_genome = None
        return hooks

    def attach_hooks(self, hooks: dict) -> None:
        self.journal.sink = hooks.get("sink")
        self.diplomacy.on_message = hooks.get("on_message")
        self.military.rebirth_genome = hooks.get("rebirth")
        if self.diplomat is not None:
            self.diplomat.writer, self.diplomat.answerer = hooks.get("writer"), hooks.get("answerer")

    # ================================================================== setup
    def next_id(self) -> int:
        self._last_id += 1
        return self._last_id

    @property
    def day(self) -> int:
        return (self.clock.date - self.start_date).days + 1

    def log(self, civ: Civilization | None, kind: str, text: str, importance: int = 3) -> None:
        self.journal.add(Event(self.clock.t, self.day, civ.id if civ else None, kind, text, importance))

    def rival(self, kind: str, civ_id: str | None, other: str | None = None, value: float | None = None,
              text: str = "", days: float | None = None) -> None:
        """A fact for the rivalry scoreboard (wars, battles, castles, Wonders). Kept small: the live runner
        drains it into SQLite; in the night's fast games nobody reads it."""
        feed = self.__dict__.setdefault("rivalry_feed", [])        # saves made before the scoreboard
        feed.append({"t": self.clock.t, "day": self.day, "era": getattr(self, "era", 1), "kind": kind,
                     "civ": civ_id, "other": other, "value": value, "days": days, "text": text})
        del feed[:-1000]

    def civ_by_id(self, cid: str | None) -> Civilization | None:
        for c in self.civs:
            if c.id == cid:
                return c
        return None

    def _found_civ(self, spec: dict, pos: tuple[int, int], genome, index: int | None = None) -> Civilization:
        r = self.rules
        civ = Civilization(r, spec, self.clock.t, genome)
        civ.rng = random.Random(self.seed * 7919 + 17)        # identical luck for every civ
        slot = len(self.civs) if index is None else index
        civ.mirror = slot % 2 == 1                            # the south half is the north half rotated
        cs = r.castle_spec
        castle = Building(self.next_id(), civ.id, "castle", pos[0], pos[1], cs.size, False, self.clock.t)
        castle.level = castle.target_level = 1
        castle.hp = castle.max_hp = r.building_hp("castle", 1)
        self.map.place_building(castle.id, pos[0], pos[1], cs.size, False)
        civ.buildings.append(castle)
        civ.castle = castle
        if index is None:
            self.civs.append(civ)
        else:
            self.civs[index] = civ
        if self._in_winter_bonus_window():
            civ.stock["food"] += r.winter["start_bonus_food"]    # may exceed the cap at birth
            self.log(civ, "start", f"{civ.name} is born before winter: +{r.winter['start_bonus_food']} food.", 4)
        sexes = ["m", "f"]
        for i, age in enumerate(r.people["start_ages"]):
            self.spawn_person(civ, float(age), sexes[i % 2], announce=False)
        self.log(civ, "start", f"{civ.name} was founded by {' and '.join(p.name for p in civ.people)}.", 6)
        return civ

    def rebirth(self, index: int, genome) -> Civilization:
        """Version 5: 7 days after a kingdom falls, a new one is born in its place with 2 people."""
        from ai.tactical import TacticalAI

        old = self.civs[index]
        spec = self.rules.civs[index]
        size = self.rules.castle_spec.size
        pos = castle_positions(self.rules)[index]
        if not self.map.can_place(pos[0], pos[1], size, margin=0):
            pos = find_site(self.map, (pos[0] + size / 2, pos[1] + size / 2), size, radius=12) or pos
        civ = self._found_civ(spec, pos, genome, index=index)
        civ.generation = old.generation + 1
        self.fog.seen.pop(civ.id, None)                       # a new kingdom knows only its own valley
        self.fog.found_nodes.pop(civ.id, None)
        self.fog.found(civ)
        civ.ai = TacticalAI(civ, self)
        civ.ai.book = list(old.ai.book) if old.ai else []
        for other in self.civs:                               # a newborn kingdom gets its own 14 days of peace
            if other is not civ:
                p = self.diplomacy.pair(civ.id, other.id)
                self.diplomacy.treaty_until[p] = self.clock.t + float(self.rules.war.get("peace_days", 14)) * 86400
                self.diplomacy.end_war(civ.id, other.id, None, "rebirth")
                self.diplomacy.R[p] = float(self.rules.diplomacy.get("R_start", 0))
        self.refresh_food_rate(civ)
        self.log(civ, "rebirth", f"A new {civ.name} is born where the old one fell (generation {civ.generation}).", 10)
        return civ

    def _in_winter_bonus_window(self) -> bool:
        w = self.rules.winter
        md = (self.clock.date.month, self.clock.date.day)
        a, b = tuple(w["start_bonus_from"]), tuple(w["start_bonus_to"])
        return md >= a or md <= b   # the window wraps around New Year

    @staticmethod
    def _names(civ: Civilization) -> dict:
        return name_lists().get(civ.id) or next(iter(name_lists().values()))

    def _first_name(self, civ: Civilization, sex: str) -> str:
        return civ.rng.choice(self._names(civ)["male" if sex == "m" else "female"])

    def spawn_person(self, civ: Civilization, age: float, sex: str | None = None,
                     announce: bool = True) -> Person:
        r = self.rules
        sex = sex or civ.rng.choice("mf")
        first = self._first_name(civ, sex)
        last = civ.rng.choice(self._names(civ)["surnames"])
        trait = None if civ.rng.random() < r.trait_none_chance else civ.rng.choice(sorted(r.traits))
        birth = self.clock.t - age * r.people["days_per_year"] * SECONDS_PER_DAY
        spot = civ.rng.choice(self.spots(civ, civ.castle)) if civ.castle else (0, 0)
        p = Person(self.next_id(), civ.id, first, last, sex, birth, trait, float(spot[0]), float(spot[1]))
        civ.people.append(p)
        civ.assign_homes()
        self.refresh_food_rate(civ)
        civ.dirty = True
        trait_label = r.traits[trait]["label"].lower() if trait else "no trait"
        p.history.append(f"day {self.day}: joined the village aged {age:.0f}")
        if announce:
            self.log(civ, "arrival", f"{p.name} ({age:.0f}, {trait_label}) joined the village.", 4)
        return p

    def spots(self, civ: Civilization, b: Building) -> list[tuple[int, int]]:
        """Tiles next to a building, listed in the civ's own (mirrored) order, so both sides match."""
        return sorted(self.behavior.goals(b), key=lambda t: (t[1], t[0]), reverse=civ.mirror)

    def remove_person(self, civ: Civilization, p: Person, text: str, kind: str, importance: int = 6) -> None:
        self.behavior.release(p)
        p.alive = False
        civ.people.remove(p)
        self.society.on_loss(civ, p, died=kind in ("death", "wolves", "mine"))
        civ.assign_homes()
        self.refresh_food_rate(civ)
        civ.dirty = True
        self.log(civ, kind, text, importance)

    def spawn_child(self, civ: Civilization, mother: Person, father: Person) -> Person:
        r = self.rules
        house = mother.home
        sex = civ.rng.choice("mf")
        first = self._first_name(civ, sex)
        if civ.rng.random() < r.families["inherit_trait_chance"]:
            trait = civ.rng.choice([mother.trait, father.trait])
        else:
            trait = None if civ.rng.random() < r.trait_none_chance else civ.rng.choice(sorted(r.traits))
        x, y = civ.rng.choice(self.spots(civ, house) or [(house.x, house.y)])
        child = Person(self.next_id(), civ.id, first, house.family_name or mother.last, sex, self.clock.t,
                       trait, float(x), float(y))
        child.parents = (mother.id, father.id)
        child.home = house
        child.state = State.REST
        child.history.append(f"day {self.day}: born into the {child.last} family")
        for parent in (mother, father):
            parent.children.append(child.id)
            parent.shock += r.happiness.get("shock_child", 10)
        civ.people.append(child)
        civ.births += 1
        civ.assign_homes()
        self.refresh_food_rate(civ)
        self.log(civ, "birth", f"{child.first} was born into the {child.last} family.", 4)
        return child

    def send_away(self, civ: Civilization, p: Person, until: float, reason: str) -> None:
        """Sick, injured or on strike: no work until `until`."""
        self.behavior.release(p)
        p.job = None
        p.away_until = max(p.away_until, until)
        p.away_reason = reason
        p.history.append(f"day {self.day}: {reason}")
        civ.dirty = True

    def transfer_person(self, src: Civilization, dst: Civilization, p: Person) -> None:
        """A very unhappy person runs away to the other civilization, experience included."""
        self.behavior.release(p)
        src.people.remove(p)
        self.society.on_loss(src, p, died=False)
        src.fled += 1
        p.civ_id = dst.id
        p.job, p.target, p.home = None, None, None
        p.carry_amt, p.carry_res = 0.0, None
        p.state = State.IDLE
        p.happiness = self.rules.happiness["start"]
        p.low_days = p.very_low_days = 0
        x, y = dst.rng.choice(self.spots(dst, dst.castle))
        p.x, p.y = float(x), float(y)
        p.history.append(f"day {self.day}: fled from {src.name} to {dst.name}")
        dst.people.append(p)
        dst.arrived_fugitives += 1
        for c in (src, dst):
            c.assign_homes()
            self.refresh_food_rate(c)
            c.dirty = True
        self.log(src, "flight", f"{p.name} fled to {dst.name}.", 6)
        self.log(dst, "flight", f"{p.name} fled from {src.name} and joined us.", 6)

    def destroy_building(self, civ: Civilization, b: Building, text: str) -> None:
        for p in civ.people:
            if p.target is b:
                self.behavior.release(p)
                p.state = State.IDLE
            if p.home is b:
                p.home = None
        civ.buildings.remove(b)
        self.map.remove_building(b.x, b.y, b.size)
        civ.invalidate_depots()
        civ.assign_homes()
        civ.dirty = True
        self.log(civ, "destroyed", text, 6)

    def refresh_food_rate(self, civ: Civilization) -> None:
        civ.food_rate = civ.food_per_day(self.clock.t) / SECONDS_PER_DAY

    # ================================================================== actions (called by the AI)
    def start_building(self, civ: Civilization, btype: str, x: int, y: int, reason: str = "") -> Building:
        spec = self.rules.buildings[btype]
        if not development.unlocked(civ, btype):
            raise ValueError(f"{spec.label} requires a later era, more people or prerequisite buildings")
        if not self.rules.progressive(btype):            # the Wonder's materials arrive while it is built
            civ.pay(self.rules.building_cost(btype, 1))
        b = Building(self.next_id(), civ.id, btype, x, y, spec.size, spec.walkable, self.clock.t)
        self.map.place_building(b.id, x, y, spec.size, spec.walkable)
        civ.buildings.append(b)
        civ.dirty = True
        self.log(civ, "build_start", f"Started a {spec.label}." + (f" {reason}" if reason else ""), 3)
        return b

    def start_upgrade(self, civ: Civilization, b: Building, reason: str = "") -> None:
        new = b.level + 1
        civ.pay(self.rules.building_cost(b.type, new))
        b.target_level, b.progress = new, 0.0
        civ.dirty = True
        self.log(civ, "build_start", f"Upgrading the {self.rules.buildings[b.type].label} to level {new}."
                 + (f" {reason}" if reason else ""), 3)

    def start_castle_upgrade(self, civ: Civilization, reason: str = "") -> None:
        c = civ.castle
        c.target_level, c.progress, c.paid = c.level + 1, 0.0, {}
        civ.dirty = True
        self.log(civ, "castle_start", f"Started building castle level {c.target_level}."
                 + (f" {reason}" if reason else ""), 6)

    def start_research(self, civ: Civilization, craft: str, reason: str = "") -> None:
        level = civ.tool_level(craft) + 1
        civ.pay(self.rules.research_cost(craft, level))
        civ.research.append({"craft": craft, "level": level, "progress": 0.0})
        label = self.rules.research_label(craft)
        what = f"{label.lower()} level {level} for the army" if craft in self.rules.MILITARY else             f"level {level} tools for the {label}s"
        self.log(civ, "research_start", f"The forge is making {what}." + (f" {reason}" if reason else ""), 3)

    def start_tech(self, civ: Civilization, tech: str, reason: str = "") -> None:
        """Version 7: research a tech at the castle."""
        spec = self.rules.tech[tech]
        civ.pay(spec["cost"])
        civ.tech_job = {"tech": tech, "progress": 0.0}
        self.log(civ, "tech_start", f"The castle studies {spec['label']} ({spec['effect']})." + (f" {reason}" if reason else ""), 3)

    def _tech(self, civ: Civilization, dt: float) -> None:
        job = civ.tech_job
        if job is None:
            return
        spec = self.rules.tech[job["tech"]]
        job["progress"] += dt / (float(spec["hours"]) * 3600.0)
        if job["progress"] >= 1.0:
            civ.tech_job = None
            civ.techs.add(job["tech"])
            civ.dirty = True
            self.log(civ, "tech_done", f"{civ.name} learned {spec['label']}: {spec['effect']}.", 6)

    def start_recruit(self, civ: Civilization, reason: str = "") -> None:
        civ.pay({"food": self.rules.people["recruit"]["food"]})
        civ.recruiting.append(0.0)
        self.log(civ, "recruit_start", "The castle is training a new worker." + (f" {reason}" if reason else ""), 2)

    def site_hours(self, site: Building) -> float:
        """One-builder hours for the whole job on a construction site."""
        if site.type == "castle":
            return self.rules.castle_one_builder_hours(site.level)
        return self.rules.building_hours(site.type, site.target_level)

    def remaining_hours(self, site: Building) -> float:
        """One-builder hours of work left on a construction site."""
        return (1 - site.progress) * self.site_hours(site)

    def pick_site(self, civ: Civilization) -> Building | None:
        """A builder without a site takes an empty site first, shortest job first."""
        sites = civ.sites()
        if not sites:
            return None
        limit = self.rules.construction["max_builders"]
        load = {s.id: 0 for s in sites}
        for p in civ.people:
            if p.job == "build" and isinstance(p.target, Building) and p.target.id in load:
                load[p.target.id] += 1
        open_sites = [s for s in sites if load[s.id] < limit]
        if not open_sites:
            return None
        return min(open_sites, key=lambda s: (load[s.id] > 0, self.remaining_hours(s)))

    def deplete(self, node: Node) -> None:
        node.amount = 0.0
        days = self.rules.nodes[node.kind].regrow_days
        if days:
            node.regrow_at = self.clock.t + days * SECONDS_PER_DAY
            self._depleted.append(node)

    # ================================================================== simulation
    def step(self, dt: float) -> None:
        c = self.clock
        dt = min(dt, c.next_change() - c.t)
        phase = c.phase
        ctx = TickCtx(c.t, phase, phase == Phase.WORK, c.season, c.date.year, c.date.month)
        for civ in self.civs:
            civ.now = c.t
        if phase != self._last_phase:
            if phase == Phase.EVENING and self.rules.feature("happiness"):
                for civ in self.civs:
                    if civ.alive:
                        self.society.evening(civ)
            self._last_phase = phase

        if c.t >= self._next_tactical or (any(cv.dirty for cv in self.civs) and c.t - self._last_decide >= 30):
            for civ in self.civs:
                if civ.alive and civ.ai is not None:
                    civ.ai.decide()
                civ.dirty = False
            self._last_decide = c.t
            self._next_tactical = c.t + self.rules.tactical_interval

        update = self.behavior.update
        for civ in self.civs:
            if not civ.alive:
                continue
            for p in list(civ.people):
                update(civ, p, dt, ctx)
            if ctx.working:
                self._construction(civ, dt, ctx)
                self._research(civ, dt)
                self._recruit(civ, dt)
                self._tech(civ, dt)
            development.tick(self, civ, dt, ctx.working)
            civ.builders_at.clear()
            self.events.tick(civ, dt)
            civ.firefighters_at.clear()
            self._eat(civ, dt)
        self.military.tick(dt, ctx.working)             # version 5: armies, spies, rebirth
        self.fog.tick(dt)                               # version 7: what each kingdom has seen
        self.trade.tick(dt, ctx.working)                # market prices, the mint, trade carts
        if self.diplomat is not None:
            self.diplomat.tick()

        self.ticks += 1
        if c.advance(dt):
            self._new_day()

    def all_resting(self) -> bool:
        """Nobody moves and nothing burns: the night can be skipped in one step."""
        if self.trade.carts or self.military.battles or (self.bandits is not None and self.bandits.units):
            return False
        for civ in self.civs:
            if any(b.on_fire for b in civ.buildings):
                return False
            if any(p.state != State.REST for p in civ.people):
                return False
            if any(u.state in ("walk", "fight", "go", "back") or u.order == "attack" for u in civ.units):
                return False
        return True

    def run(self, seconds: float, tick: float | None = None,
            on_day: Callable[["World"], None] | None = None) -> None:
        """Fast mode: simulate `seconds` as quickly as possible."""
        tick = tick or self.rules.fast_tick
        end = self.clock.t + seconds
        day = self.day
        while self.clock.t < end - 1e-6:
            if not self.clock.working and self.all_resting():
                dt = min(self.clock.next_change(), end) - self.clock.t   # nobody moves: jump ahead
            else:
                dt = min(tick, end - self.clock.t)
            self.step(dt)
            if on_day and self.day != day:
                day = self.day
                on_day(self)

    def run_until(self, t_end: float, tick: float | None = None,
                  on_day: Callable[["World"], None] | None = None) -> None:
        self.run(max(0.0, t_end - self.clock.t), tick, on_day)

    # ================================================================== per-tick systems
    def _eat(self, civ: Civilization, dt: float) -> None:
        need = civ.food_rate * dt
        if civ.stock["food"] >= need:
            civ.stock["food"] -= need
        else:
            civ.stock["food"] = 0.0
            civ.starved_today = True

    def _construction(self, civ: Civilization, dt: float, ctx: TickCtx) -> None:
        if not civ.builders_at:
            for site in civ.sites():
                site.builders_now = 0.0                  # nobody on site: the map says "waiting for builders"
            return
        r = self.rules
        exp = r.construction["builder_exponent"]
        tool = r.tool_mult(civ.tool_level("builder")) * (1 + development.bonus(civ, "construction"))
        for site in civ.sites():
            power = civ.builders_at.get(site.id, 0.0)
            site.builders_now = power                    # the map shows the time left at this pace
            if power <= 0:
                continue
            delta = F.builder_power(power, exp) * tool * ctx.season.build * dt / (self.site_hours(site) * 3600.0)
            if r.progressive(site.type):
                delta = self._deliver_materials(civ, site, delta)
            site.progress = min(1.0, site.progress + delta)
            if site.progress >= 1.0 - 1e-9:
                self._complete(civ, site)

    def _deliver_materials(self, civ: Civilization, site: Building, delta: float) -> float:
        """Progress can't outrun materials on site: progress ≤ delivered / required."""
        cost = self.rules.site_cost(site)
        reach = site.progress + delta
        for res, total in cost.items():
            if total <= 0:
                continue
            have = site.paid.get(res, 0.0) + civ.stock[res]
            reach = min(reach, have / total)
        if reach <= site.progress:
            return 0.0
        for res, total in cost.items():
            need = total * reach - site.paid.get(res, 0.0)
            if need > 0:
                take = min(need, civ.stock[res])
                civ.stock[res] -= take
                site.paid[res] = site.paid.get(res, 0.0) + take
        return reach - site.progress

    def _complete(self, civ: Civilization, b: Building) -> None:
        r = self.rules
        b.level = b.target_level
        b.progress = 0.0
        b.paid = {}
        b.hp = b.max_hp = r.building_hp(b.type, b.level)
        civ.invalidate_depots()
        civ.dirty = True
        if b.type == "castle":
            civ.castle_reached[b.level] = self.clock.t
            age = r.age_name(b.level)
            new_age = age != r.age_name(b.level - 1)
            text = f"The castle reached level {b.level}." + (f" {civ.name} enters the {age}!" if new_age else "")
            self.log(civ, "castle", text, 10)
            self.rival("castle", civ.id, value=b.level, days=round((self.clock.t - civ.born_t) / 86400, 2),
                       text=f"{civ.name} reached castle {b.level}")
            return
        label = r.buildings[b.type].label
        if b.type == r.wonder.get("type", "wonder"):
            self._wonder_done(civ)
            return
        if b.type == "farm" and b.level == 1:
            m = self.clock.date.month
            b.sown_season_year = self.clock.date.year if m >= 3 else self.clock.date.year - 1
        if b.type == "house" or development.building_rule(civ, b.type).get("housing"):
            civ.assign_homes()
        names = [p.first for p in civ.people if p.job == "build" and p.target is b]
        for p in civ.people:
            if p.target is b and p.job == "build":
                p.history.append(f"day {self.day}: helped build the {label}")
        who = f" (builders: {', '.join(names)})" if names else ""
        lvl = f" level {b.level}" if b.level > 1 else ""
        self.log(civ, "build_done", f"{label}{lvl} is finished{who}.", 3)

    def _wonder_done(self, civ: Civilization) -> None:
        """The first Wonder finished wins the era; in the live world a new era begins some days later."""
        if getattr(self, "era_winner", None) is not None:
            self.log(civ, "wonder", f"{civ.name} finished a Wonder too, but the era is already won.", 7)
            self.rival("wonder", civ.id, value=0, text=f"{civ.name} finished a Wonder after the era was won")
            return
        self.era_winner = civ.id
        self.eras = getattr(self, "eras", [])
        self.eras.append({"era": getattr(self, "era", 1), "winner": civ.id, "name": civ.name, "day": self.day,
                          "date": self.clock.date.isoformat()})
        days = float(self.rules.wonder.get("new_era_days", 7))
        text = f"{civ.name} finished its Wonder and wins era {getattr(self, 'era', 1)}!"
        self.rival("wonder", civ.id, value=1, days=round((self.clock.t - civ.born_t) / 86400, 2), text=text)
        if self.live and self.rules.wonder.get("reset_world", True):
            self.new_era_at = self.clock.t + days * SECONDS_PER_DAY
            text += f" A new era begins in {days:.0f} days."
        self.log(civ, "wonder", text, 10)

    def new_era_world(self, genomes: list | None = None) -> "World":
        """The next era: a new map and both kingdoms born again (the caller adopts it). History is kept."""
        era = getattr(self, "era", 1) + 1
        nxt = World(self.rules, seed=self.seed + 1000 * era, start=self.clock.t, tz=self.clock.tz,
                    genomes=genomes or [c.genome for c in self.civs])
        nxt.era = era
        nxt.eras = list(getattr(self, "eras", []))
        nxt.live = self.live
        for civ, old in zip(nxt.civs, self.civs):
            if civ.ai is not None and old.ai is not None:
                civ.ai.book = list(old.ai.book)
        last = nxt.eras[-1] if nxt.eras else None
        nxt.log(None, "era", f"Era {era} begins on a new map." + (f" {last['name']} won the last one." if last else ""), 10)
        return nxt

    def _research(self, civ: Civilization, dt: float) -> None:
        if not civ.research:
            return
        slots = civ.research_slots()
        for job in list(civ.research[:slots]):
            job["progress"] += dt / (self.rules.research_hours(job["craft"], job["level"]) * 3600.0)
            if job["progress"] >= 1.0:
                civ.research.remove(job)
                civ.tools[job["craft"]] = job["level"]
                civ.dirty = True
                label = self.rules.research_label(job["craft"])
                if job["craft"] in self.rules.MILITARY:
                    self.log(civ, "research_done", f"{label} level {job['level']}: the army hits harder." if
                             job["craft"] == "weapons" else f"{label} level {job['level']}: the army is better protected.", 4)
                else:
                    self.log(civ, "research_done", f"New level {job['level']} tools for the {label}s.", 4)

    def _recruit(self, civ: Civilization, dt: float) -> None:
        if not civ.recruiting:
            return
        rec = self.rules.people["recruit"]
        civ.recruiting[0] += dt / (rec["hours"] * 3600.0)
        if civ.recruiting[0] >= 1.0:
            civ.recruiting.pop(0)
            self.spawn_person(civ, civ.rng.uniform(*rec["age"]))

    # ================================================================== daily
    def _new_day(self) -> None:
        now = self.clock.t
        still = []
        for n in self._depleted:
            if n.regrow_at is not None and n.regrow_at <= now:
                n.amount, n.regrow_at = n.max_amount, None
            else:
                still.append(n)
        self._depleted = still
        for civ in self.civs:
            if civ.alive:
                self._daily(civ)
                self.military.daily(civ)
        self.diplomacy.daily()

    def _daily(self, civ: Civilization) -> None:
        development.daily(self, civ)
        r = self.rules
        now = self.clock.t
        civ.now = now
        season = self.clock.season
        mo = r.morale
        pop = civ.pop

        taxed = pop + len(civ.soldiers)                 # every inhabitant pays, soldiers too (doc)
        civ.store("coins", F.coins_per_day(taxed, tax=r.economy["tax_per_person_per_day"]))
        civ.stock["food"] *= 1 - r.storage["food_spoil_per_day"] / (1 + development.bonus(civ, "preservation"))

        civ.cold_homes = set()
        per_house = season.firewood_per_house
        if per_house:
            houses = civ.of_type("house")
            need = per_house * len(houses)
            burn = min(need, civ.stock["wood"])
            civ.stock["wood"] -= burn
            if need - burn > 1e-6:
                cold = math.ceil((need - burn) / per_house)
                civ.cold_homes = {h.id for h in houses[-cold:]}
                self.log(civ, "cold", f"{cold} houses have no firewood.", 4)

        fed = not civ.starved_today
        if fed:
            civ.hunger_days = 0
        else:
            civ.hunger_days += 1
        if self.rules.feature("happiness"):
            self.society.daily(civ)        # morale = average happiness (version 2)
        else:
            housed = pop <= civ.beds()
            delta = 0.0
            if fed and housed and not civ.cold_homes:
                delta += mo["fed_and_housed"]
            if not fed:
                delta += mo["hunger"]
            if not housed:
                delta += mo["no_bed"]
            if civ.cold_homes:
                delta += r.winter["no_firewood_morale_per_day"]
            civ.morale = min(mo["max"], max(mo["min"], civ.morale + delta))
            if self.rules.feature("families"):
                self.society.daily(civ)
        civ.starved_today = False

        if civ.hunger_days >= r.people["hunger_leave_days"] and civ.people:
            p = civ.rng.choice(civ.people)
            civ.departures += 1
            self.remove_person(civ, p, f"{p.name} left the village because of hunger.", "leave", 7)

        dpy = r.people["days_per_year"]
        death = r.people["death"]
        for p in list(civ.people):
            age = p.age(now, dpy)
            from_age = death["from_age"] + (r.tech_effects.get("medicine_years", 5) if civ.has_tech("medicine") else 0)
            q = F.death_chance_per_year(age, from_age, death["base"], death["growth"])
            if q and civ.rng.random() < F.per_day_from_per_year(q, dpy):
                civ.deaths += 1
                self.remove_person(civ, p, f"{p.name} died of old age at {age:.0f}.", "death", 6)

        self.events.daily(civ)
        if any(b.closed_until and b.closed_until <= now for b in civ.buildings):
            for b in civ.buildings:
                if b.closed_until and b.closed_until <= now:
                    b.closed_until = 0.0
            civ.invalidate_depots()

        civ.unreachable.clear()
        self.refresh_food_rate(civ)
        civ.dirty = True
        civ.daily.append(self.civ_row(civ))
        if not civ.people:
            text = f"{civ.name} has no people left."
            if self.live or self.rules.war.get("fast_mode_rebirth", False):
                self.military.fall(civ, None, text)        # clears the land; a new kingdom is born 7 days later
            else:                                          # the night's games: the side simply lost
                civ.alive = False
                civ.fallen_at = now
                self.log(civ, "fall", text, 10)

    # ================================================================== reporting
    def civ_row(self, civ: Civilization) -> dict:
        return {
            "day": self.day, "date": self.clock.date.isoformat(), "castle": civ.castle_level,
            "pop": civ.pop, "workers": len(civ.workers(self.clock.t)), "beds": civ.beds(),
            "morale": round(civ.morale, 3), **{r: round(v, 1) for r, v in civ.stock.items()},
            "buildings": sum(1 for b in civ.buildings if b.built),
        }
