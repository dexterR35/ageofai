"""Version 2: random events. P_day = p0 · m_season · (1 − r_defense).

Each civilization draws from its own random stream. In training, both sides of a match get
the same seed, so genomes compared on the same night face the same luck.
"""
from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

from .entities import SECONDS_PER_DAY
from .worldmap import Node

if TYPE_CHECKING:
    from .civ import Civilization
    from .world import World


class Events:
    def __init__(self, world: "World", seed: int):
        self.w = world
        self.rules = world.rules
        self.cfg = world.rules.events
        self.seed = seed
        self._rngs: dict[str, random.Random] = {}

    def rng(self, civ: "Civilization") -> random.Random:
        if civ.id not in self._rngs:
            self._rngs[civ.id] = random.Random(self.seed)
        return self._rngs[civ.id]

    def season_mult(self, spec: dict) -> float:
        season = self.w.clock.season.name
        if "only" in spec and season not in spec["only"]:
            return 0.0
        return spec.get("mult", {}).get(season, 1.0)

    def _wells_near(self, civ: "Civilization", x: float, y: float, radius: float) -> bool:
        return any(math.hypot(b.center[0] - x, b.center[1] - y) <= radius for b in civ.of_type("well"))

    # ================================================================== daily rolls
    def daily(self, civ: "Civilization") -> None:
        if not self.rules.feature("events") or not civ.people:
            return
        rng = self.rng(civ)
        self.fires(civ, rng)
        self.disease(civ, rng)
        self.harvest(civ, rng)
        self.wolves(civ, rng)
        self.mine_collapse(civ, rng)
        self.storm(civ, rng)
        self.merchant(civ, rng)

    def fires(self, civ: "Civilization", rng: random.Random) -> None:
        spec = self.cfg["fire"]
        m = self.season_mult(spec)
        for b in civ.buildings:
            if not b.built or b.on_fire or b.type == "castle":
                continue
            if self.rules.buildings[b.type].material != "wood":
                continue
            p = spec["p0"] * m
            if self._wells_near(civ, *b.center, spec["well_radius"]):
                p *= 1 - spec["well_reduction"]
            if rng.random() < p:
                b.on_fire, b.fire_out = True, 0.0
                civ.dirty = True
                self.w.log(civ, "fire", f"Fire at the {self.rules.buildings[b.type].label}!", 5)

    def disease_chance(self, civ: "Civilization") -> float:
        spec = self.cfg["disease"]
        people = civ.people
        sickly = sum(1 for p in people if p.trait == "sickly") / max(1, len(people))
        p = spec["p0"] * self.season_mult(spec) * (1 + (spec["sickly_mult"] - 1) * sickly)
        if civ.of_type("well"):
            p *= 1 - spec["well_reduction"]
        if civ.has_tech("medicine"):                          # version 7
            p *= self.w.rules.tech_effects.get("medicine_disease", 0.5)
        if civ.morale >= spec["morale_from"]:
            p *= 1 - spec["morale_reduction"]
        return p

    def disease(self, civ: "Civilization", rng: random.Random) -> None:
        spec = self.cfg["disease"]
        people = civ.people
        if rng.random() >= self.disease_chance(civ):
            return
        n = max(1, round(spec["share_sick"] * len(people)))
        pool = list(people)
        weights = [spec["sickly_mult"] if q.trait == "sickly" else 1.0 for q in pool]
        sick = []
        while pool and len(sick) < n:
            q = rng.choices(pool, weights=weights)[0]
            i = pool.index(q)
            pool.pop(i)
            weights.pop(i)
            sick.append(q)
        until = self.w.clock.t + spec["sick_days"] * SECONDS_PER_DAY
        self.w.log(civ, "disease", f"Sickness in the village: {len(sick)} people can't work for {spec['sick_days']} days.", 5)
        for q in sick:
            if rng.random() < spec["death_share"]:
                civ.deaths += 1
                self.w.remove_person(civ, q, f"{q.name} died of the sickness.", "death", 6)
            else:
                self.w.send_away(civ, q, until, "sick")

    def harvest(self, civ: "Civilization", rng: random.Random) -> None:
        spec = self.cfg["harvest"]
        if not civ.of_type("farm") or civ.harvest_until > self.w.clock.t:
            return
        if rng.random() < spec["p0"] * self.season_mult(spec):
            civ.harvest_until = self.w.clock.t + spec["days"] * SECONDS_PER_DAY
            self.w.log(civ, "harvest", f"Rich harvest: farms give ×{spec['farm_mult']} for {spec['days']} days.", 2)

    def wolves(self, civ: "Civilization", rng: random.Random) -> None:
        spec = self.cfg["wolves"]
        m = self.season_mult(spec)
        cx, cy = civ.castle.center
        towers = civ.of_type("tower")
        for p in list(civ.people):
            if not isinstance(p.target, Node):
                continue
            tx, ty = p.target.center
            if math.hypot(tx - cx, ty - cy) <= spec["far_tiles"]:
                continue
            if any(math.hypot(t.center[0] - tx, t.center[1] - ty) <= spec["tower_radius"] for t in towers):
                continue
            if rng.random() >= spec["p0"] * m:
                continue
            n = rng.randint(*spec["count"])
            if rng.random() < spec["death_per_wolf"] * n:
                civ.deaths += 1
                self.w.remove_person(civ, p, f"{n} wolves attacked {p.name} far from the castle. {p.first} did not survive.",
                                     "wolves", 6)
            else:
                self.w.send_away(civ, p, self.w.clock.t + spec["injured_days"] * SECONDS_PER_DAY, "injured")
                self.w.log(civ, "wolves", f"{n} wolves attacked {p.name}, who escaped injured.", 5)

    def mine_collapse(self, civ: "Civilization", rng: random.Random) -> None:
        spec = self.cfg["mine_collapse"]
        for p in list(civ.people):
            if p.job not in ("ore", "gold") or not isinstance(p.target, Node):
                continue
            craft = self.rules.jobs[p.job].tool
            chance = spec["p0"] * (1 - spec["tool_reduction"] if civ.tool_level(craft) >= spec["safe_tool_level"] else 1)
            if rng.random() >= chance:
                continue
            mine = civ.nearest_depot(self.rules.jobs[p.job].resource, *p.target.center)
            if mine is None or mine.type == "castle":
                continue
            mine.closed_until = self.w.clock.t + spec["closed_days"] * SECONDS_PER_DAY
            civ.invalidate_depots()
            civ.dirty = True
            label = self.rules.buildings[mine.type].label
            if rng.random() < spec["death_chance"]:
                civ.deaths += 1
                self.w.remove_person(civ, p, f"The {label} collapsed on {p.name}.", "mine", 6)
                self.w.log(civ, "mine", f"The {label} is closed for {spec['closed_days']} days.", 5)
            else:
                self.w.log(civ, "mine", f"The {label} collapsed and is closed for {spec['closed_days']} days. "
                                        f"{p.first} got out alive.", 5)

    def storm(self, civ: "Civilization", rng: random.Random) -> None:
        spec = self.cfg["storm"]
        if rng.random() >= spec["p0"] * self.season_mult(spec):
            return
        start = self.w.clock.day_start + rng.uniform(8, 14) * 3600
        civ.storm_from, civ.storm_until = start, start + spec["hours"] * 3600
        self.w.log(civ, "storm", f"Storm: no outdoor work for {spec['hours']} hours.", 5)

    def merchant(self, civ: "Civilization", rng: random.Random) -> None:
        spec = self.cfg["merchant"]
        if not civ.of_type(spec["needs"]):
            return
        if rng.random() < spec["p0"]:
            civ.merchant_until = self.w.clock.t + spec["days"] * SECONDS_PER_DAY
            self.w.log(civ, "merchant", f"A foreign merchant arrived: market trades cost ×{spec['price_mult']} today.", 2)

    # ================================================================== fires burn every tick
    def tick(self, civ: "Civilization", dt: float) -> None:
        spec = self.cfg.get("fire")
        if not spec:
            return
        for b in [b for b in civ.buildings if b.on_fire]:
            max_hp = b.max_hp or self.rules.building_hp(b.type, b.level)
            b.hp -= spec["hp_loss_per_hour"] * max_hp * dt / 3600
            crew = civ.firefighters_at.get(b.id, 0.0)
            b.fire_out += dt * crew / (spec["firefighters"] * spec["extinguish_hours"] * 3600)
            label = self.rules.buildings[b.type].label
            if b.hp <= 0:
                self.w.destroy_building(civ, b, f"The {label} burned to the ground.")
            elif b.fire_out >= 1:
                b.on_fire, b.fire_out = False, 0.0
                civ.dirty = True
                self.w.log(civ, "fire_out", f"The fire at the {label} is out ({b.hp / max_hp:.0%} of the building left).", 4)
