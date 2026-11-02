"""Version 2: people with their own wishes, friends and families.

Every night each person's happiness H moves toward a target that depends on how they live.
The village morale is the average happiness. Unhappy people strike or flee; couples in the
same house become a family and may have children.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

from . import formulas as F
from .entities import Building, Person, State

if TYPE_CHECKING:
    from .civ import Civilization
    from .world import World


class Society:
    def __init__(self, world: "World"):
        self.w = world
        self.rules = world.rules

    # ================================================================== helpers
    def work_spot(self, p: Person) -> tuple[float, float] | None:
        t = p.target
        if t is None or p.job is None:
            return None
        return t.center

    def works_far(self, p: Person) -> bool:
        spot = self.work_spot(p)
        if spot is None or p.home is None:
            return False
        hx, hy = p.home.center
        return math.hypot(spot[0] - hx, spot[1] - hy) > self.rules.happiness["far_tiles"]

    def is_adult(self, civ: "Civilization", p: Person) -> bool:
        return civ.stage(p, self.w.clock.t)["name"] in ("adult", "elder")

    def residents(self, civ: "Civilization", house: Building) -> list[Person]:
        return [p for p in civ.people if p.home is house]

    # ================================================================== nightly update
    def daily(self, civ: "Civilization") -> None:
        """Called at midnight, before the day's hunger flag is reset."""
        if self.rules.feature("happiness"):
            self.friendships(civ)
            self.happiness(civ)
            self.strikes_and_flight(civ)
        if self.rules.feature("families"):
            self.form_families(civ)
            self.births(civ)

    def friendships(self, civ: "Civilization") -> None:
        cfg = self.rules.friendship
        near2 = cfg["together_tiles"] ** 2
        people = civ.people
        spots = {p.id: self.work_spot(p) for p in people}
        for i, a in enumerate(people):
            sa = spots[a.id]
            for b in people[i + 1:]:
                sb = spots[b.id]
                together = (sa is not None and sb is not None and a.job == b.job
                            and (sa[0] - sb[0]) ** 2 + (sa[1] - sb[1]) ** 2 <= near2)
                if together:
                    v = min(cfg["max"], a.friends.get(b.id, 0.0) + cfg["gain_per_day"])
                    a.friends[b.id] = b.friends[a.id] = v
                elif b.id in a.friends:
                    v = a.friends[b.id] - cfg["loss_per_day"]
                    if v <= 0:
                        a.friends.pop(b.id, None)
                        b.friends.pop(a.id, None)
                    else:
                        a.friends[b.id] = b.friends[a.id] = v

    def happiness(self, civ: "Civilization") -> None:
        h = self.rules.happiness
        friend_from = self.rules.friendship["friend_from"]
        fed = not civ.starved_today
        for p in civ.people:
            cold = p.home is not None and p.home.id in civ.cold_homes
            target = F.happiness_target(fed, p.home is not None, p.friend_count(friend_from),
                                        self.works_far(p), cold, h)
            from .development import bonus
            target += bonus(civ, "happiness")
            p.happiness = F.happiness_step(p.happiness, target, h["pull"], p.shock)
            p.shock = 0.0
        if civ.people:
            mean = sum(p.happiness for p in civ.people) / len(civ.people)
            mo = self.rules.morale
            civ.morale = F.morale_from_happiness(mean, h["morale_base"], h["morale_per_point"], mo["min"], mo["max"])

    def strikes_and_flight(self, civ: "Civilization") -> None:
        s, fl = self.rules.strike, self.rules.flight
        now, end_of_day = self.w.clock.t, self.w.clock.day_end
        striking = []
        for p in list(civ.people):
            p.low_days = p.low_days + 1 if p.happiness < s["below"] else 0
            p.very_low_days = p.very_low_days + 1 if p.happiness < fl["below"] else 0
            if civ.stage(p, now)["work"] <= 0:
                continue
            if p.very_low_days >= fl["days"] and civ.rng.random() < fl["chance_per_day"]:
                self.flee(civ, p)
                continue
            if p.low_days >= s["days"] and not p.away(now):
                self.w.send_away(civ, p, end_of_day, "on strike")
                p.low_days = 0
                striking.append(p)
        if len(striking) >= s["event_min_people"]:
            names = ", ".join(p.first for p in striking)
            self.w.log(civ, "strike", f"Strike! {len(striking)} people refuse to work today ({names}).", 6)
        elif striking:
            for p in striking:
                self.w.log(civ, "strike", f"{p.name} is too unhappy to work today.", 3)

    def flee(self, civ: "Civilization", p: Person) -> None:
        fl = self.rules.flight
        for other in self.w.civs:
            if other is civ or not other.alive:
                continue
            room = other.pop < other.beds() and len(other.workers(self.w.clock.t)) < \
                self.rules.workers_max(other.castle_level)
            if room and civ.relation.get(other.id, 0.0) > fl["min_relation"]:
                self.w.transfer_person(civ, other, p)
                return
        civ.fled += 1
        self.w.remove_person(civ, p, f"{p.name} ran off into the forest, too unhappy to stay.", "flight", 6)

    # ================================================================== evenings
    def evening(self, civ: "Civilization") -> None:
        """19:00: everyone picks visit / rest / overtime, depending on their trait."""
        ev = self.rules.evening
        now = self.w.clock.t
        rng = civ.rng
        for p in civ.people:
            if civ.stage(p, now)["work"] <= 0 or p.away(now):
                p.evening = ""
                continue
            weights = dict(ev["weights"])
            for k, m in ev["trait_weights"].get(p.trait or "", {}).items():
                weights[k] *= m
            if p.trait not in ev["negative_traits"]:
                weights["visit"] *= ev["no_negative_visit_bonus"]
            if p.job is None or p.job == "fire":
                weights["overtime"] = 0.0
            choice = rng.choices(list(weights), weights=list(weights.values()))[0]
            if choice == "visit":
                friends = [q for q in civ.people if q is not p and p.friends.get(q.id, 0) > 0]
                if not friends:
                    choice = "rest"
                else:
                    q = max(friends, key=lambda q: p.friends[q.id])
                    v = min(self.rules.friendship["max"], p.friends[q.id] + ev["visit_friendship"])
                    p.friends[q.id] = q.friends[p.id] = v
            if choice == "rest":
                p.happiness = min(100.0, p.happiness + ev["rest_happiness"])
            elif choice == "overtime":
                p.overtime_until = now + ev["overtime_hours"] * 3600
                p.happiness = max(0.0, p.happiness + ev["overtime_happiness"])
                if p.state == State.REST:
                    p.state = State.IDLE
            p.evening = choice

    # ================================================================== families
    def form_families(self, civ: "Civilization") -> None:
        for house in civ.of_type("house"):
            single = [p for p in self.residents(civ, house) if p.spouse is None and self.is_adult(civ, p)]
            men = [p for p in single if p.sex == "m"]
            women = [p for p in single if p.sex == "f"]
            for a, b in zip(men, women):
                if house.family_name is None:
                    house.family_name = a.last
                a.spouse, b.spouse = b.id, a.id
                a.last = b.last = house.family_name
                day = self.w.day
                a.history.append(f"day {day}: married {b.first}")
                b.history.append(f"day {day}: married {a.first}")
                self.w.log(civ, "family", f"{a.first} and {b.first} started the {house.family_name} family.", 4)

    def births(self, civ: "Civilization") -> None:
        fam = self.rules.families
        if civ.morale < fam["min_morale"]:
            return
        if civ.stock["food"] < fam["min_food_days"] * civ.food_per_day(self.w.clock.t):
            return
        by_id = {p.id: p for p in civ.people}
        for a in list(civ.people):
            b = by_id.get(a.spouse) if a.spouse is not None else None
            if b is None or a.id > b.id or a.home is None or a.home is not b.home or a.home.type != "house":
                continue
            house = a.home
            if len(self.residents(civ, house)) >= civ.beds_in(house) or civ.pop >= self.rules.people["pop_cap"]:
                continue
            if civ.rng.random() < fam["birth_chance_per_day"]:
                self.w.spawn_child(civ, a, b)

    # ================================================================== losses
    def on_loss(self, civ: "Civilization", p: Person, died: bool) -> None:
        """Someone left the village: unlink family and friends; a death saddens those close."""
        shock = self.rules.happiness.get("shock_close_death", -15)
        friend_from = self.rules.friendship["friend_from"]
        for q in civ.people:
            if q is p:
                continue
            close = (q.id == p.spouse or q.id in p.parents or q.id in p.children
                     or p.friends.get(q.id, 0) >= friend_from)
            if died and close:
                q.shock += shock
            if q.spouse == p.id:
                q.spouse = None
            q.friends.pop(p.id, None)
        p.spouse = None
        p.friends.clear()
