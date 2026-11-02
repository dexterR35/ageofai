"""Version 5: the tactical layer's army — how many soldiers, which kind, towers, upgrades, spies, war.

Doc rules used here:
  * strength (Lanchester): S = (Σ HP)·(Σ D / 2 s); attack when S_me / S_enemy > θ_attack (a gene, 1–3)
  * soldiers wanted ≈ worker_soldier_ratio × workers, more when the enemy looks stronger (g_defense)
  * the enemy is seen through a spy's report (±20%) or, without one, the daily rumour (±50%)
  * the king (live world, local AI) may decide war and peace himself: "attack" marches on his target whatever
    the strength ratio, "defend" and "seek_peace" keep the army home; the game's rules still apply
    (no attack in the first 14 days, a minimum army, a broken treaty costs trust and relation)
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tactical import Action, Estimate, TacticalAI

COUNTER = {"knight": "spearman", "archer": "knight", "spearman": "archer", "militia": "swordsman",
           "swordsman": "archer"}


class ArmyPlanner:
    def __init__(self, tac: "TacticalAI"):
        self.t = tac
        self.civ = tac.civ
        self.w = tac.w
        self.r = tac.rules

    # ------------------------------------------------------------------ what we know
    def enemy(self):
        foes = self.w.military.enemies(self.civ)
        return foes[0] if foes else None

    def my_strength(self) -> float:
        home = [u for u in self.civ.soldiers]
        return self.w.military.strength(home, self.civ)

    def enemy_strength(self, enemy) -> float:
        rep = self.civ.intel.get(enemy.id)
        if rep is None:
            rep = self.w.military.rumour(enemy, self.civ)
            self.civ.intel[enemy.id] = rep
        return self.w.military.strength_of_counts(rep.get("units", {}), rep.get("weapons", 0))

    def ratio(self, enemy) -> float:
        mine, theirs = self.my_strength(), self.enemy_strength(enemy)
        if theirs <= 0:
            return float("inf") if mine > 0 else 0.0
        return mine / theirs

    def keeps_treaty(self, enemy) -> bool:
        """Do we keep this peace treaty? The honesty gene is rolled once per treaty, not at every decision
        (a roll every 5 minutes would break almost any treaty within the hour)."""
        dip = self.w.diplomacy
        until = dip.treaty_until.get(dip.pair(self.civ.id, enemy.id), 0.0)
        word = self.__dict__.setdefault("word", {})            # saves made before this existed lack it
        key = (enemy.id, until)
        if key not in word:
            for k in [k for k in word if k[0] == enemy.id]:     # an older treaty with them is over
                del word[k]
            word[key] = self.civ.rng.random() < self.t.G("honesty", 0.7)
        return word[key]

    def intent(self) -> tuple[str, object]:
        """The king's war intent while his plan lasts: (attack | defend | seek_peace | none, plan)."""
        d = self.t.directive
        if d is None or self.t.directive_until <= self.w.clock.t:
            return "none", None
        intent = getattr(d, "intent", None)
        if intent is None or (intent == "none" and getattr(d, "war", False)):     # plans from older saves
            intent = "attack" if getattr(d, "war", False) else "none"
        return intent, d

    def war_push(self) -> bool:
        return self.intent()[0] == "attack"

    def kings_war(self, plan, enemy) -> str | None:
        """The king ordered an attack: march if the game's rules allow it (once per plan)."""
        civ, w = self.civ, self.w
        dip = w.diplomacy
        target = w.civ_by_id(getattr(plan, "target", None)) or enemy
        if target is None or not target.alive or target.castle is None:
            return None
        if dip.peace_period() or not dip.may_attack(civ.id, target.id):
            return f"the king wants war on {target.name}, but nobody may attack yet"
        need = int(self.r.war.get("min_army", 5))
        if len(civ.soldiers) < need:
            return f"the king wants war on {target.name}: arming ({len(civ.soldiers)}/{need} soldiers)"
        plan.war_done = True
        reason = getattr(plan, "war_reason", "") or "By order of the king."
        if not dip.at_war(civ.id, target.id) and self.t.G("honesty", 0.7) >= 0.5 and w.diplomat is not None:
            w.diplomat.declare_war(civ, target, reason)
        n = w.military.order_attack(civ, target)
        return f"the king's war: {n} soldiers march on {target.name} ({reason})" if n else None

    # ------------------------------------------------------------------ how many, which
    def wanted(self, est: "Estimate") -> int:
        civ = self.civ
        if civ.castle_level < min(u.castle for u in self.r.units.values()):
            return 0
        base = round(self.t.G("worker_soldier_ratio", 0.5) * est.W)
        enemy = self.enemy()
        if enemy is not None:
            seen = civ.intel.get(enemy.id, {}).get("soldiers", 0)
            if seen > len(civ.soldiers):
                base += round((seen - len(civ.soldiers)) * min(1.0, self.t.G("g_defense", 1.0) / 2))
        for req in self.r.castle_requirements.get(civ.castle_level + 1, []):
            if "soldiers" in req:
                base = max(base, req["soldiers"])
        return min(base, len(civ.soldiers) + civ.soldier_room(est.now))

    def unit_choice(self) -> str | None:
        civ, mil = self.civ, self.w.military
        ok = [k for k in self.r.units if not self.r.units[k].buildings_only and mil.can_train(civ, k)]
        if not ok:
            return None
        enemy = self.enemy()
        seen = civ.intel.get(enemy.id, {}).get("units", {}) if enemy else {}
        if seen:
            top = max(seen, key=seen.get)
            c = COUNTER.get(top)
            if c in ok and civ.can_afford(self.r.unit_cost(c)):
                return c
        mine: dict[str, int] = {}
        for u in civ.soldiers:
            mine[u.type] = mine.get(u.type, 0) + 1
        # army plan: about half front line, 30% archers, 20% cavalry; the strongest we can afford in each
        n = max(1, len(civ.soldiers))
        if "archer" in ok and mine.get("archer", 0) / n < 0.3 and civ.can_afford(self.r.unit_cost("archer")):
            return "archer"
        if "knight" in ok and mine.get("knight", 0) / n < 0.2 and civ.can_afford(self.r.unit_cost("knight")):
            return "knight"
        for k in ("swordsman", "spearman", "militia"):
            if k in ok and civ.can_afford(self.r.unit_cost(k)):
                return k
        return min(ok, key=lambda k: sum(self.r.unit_cost(k).values()))

    # ------------------------------------------------------------------ actions for the utility formula
    def actions(self, est: "Estimate") -> list["Action"]:
        from .tactical import Action

        civ, r = self.civ, self.r
        out: list[Action | None] = []
        L = est.L
        enemy = self.enemy()
        want = self.wanted(est)
        have = len(civ.soldiers) + sum(1 for t in civ.training if t["type"] != "spy")
        threat = 0.0
        if enemy is not None:
            seen = civ.intel.get(enemy.id, {}).get("soldiers", 0)
            threat = min(1.0, max(0.0, (seen - len(civ.soldiers)) / max(4, seen)))
            if any(u.order == "attack" for u in enemy.soldiers):
                threat = 1.0
        if L >= r.buildings["barracks"].castle and not civ.of_type("barracks", built_only=False) and want > 0:
            out.append(self.t.build_action("barracks", {"defense": 0.5 + threat}, "no barracks, no army", tag="army"))
        if L >= r.buildings["archery_range"].castle and not civ.of_type("archery_range", built_only=False) \
                and len(civ.soldiers) >= 4:
            out.append(self.t.build_action("archery_range", {"defense": 0.35}, "archers would cover our soldiers",
                                           tag="army"))
        if L >= r.buildings["stable"].castle and not civ.of_type("stable", built_only=False) and len(civ.soldiers) >= 8:
            out.append(self.t.build_action("stable", {"defense": 0.3}, "knights hit hardest", tag="army"))
        if have < want:
            utype = self.unit_choice()
            if utype is not None:
                urgency = min(1.5, 0.3 + (want - have) / max(1, want) + threat)
                out.append(Action("train", ("train", utype), r.units[utype].label, r.unit_cost(utype),
                                  r.units[utype].hours, {"defense": urgency},
                                  [f"soldiers {len(civ.soldiers)}/{want}"], btype=utype))
        mil = self.w.military
        for utype in {u.type for u in civ.soldiers}:                    # army plan: promote soldiers
            to = mil.promotion(civ, utype)
            if to is None or not any(u.type == utype and u.order == "defend" for u in civ.soldiers):
                continue
            cost = mil.promotion_cost(utype, to)
            hours = r.units[to].hours * float(r.army.get("promote_time_share", 0.5))
            out.append(Action("promote", ("promote", utype), f"{r.units[utype].label} → {r.units[to].label}", cost,
                              hours, {"defense": 0.25 + 0.5 * threat},
                              [f"a {r.units[to].label.lower()} is worth more than a {r.units[utype].label.lower()}"],
                              btype=utype))
        if L >= r.buildings["siege_workshop"].castle and not civ.of_type("siege_workshop", built_only=False) \
                and len(civ.soldiers) >= 10:
            out.append(self.t.build_action("siege_workshop", {"defense": 0.3}, "rams break walls and castles",
                                           tag="army"))
        rams = sum(1 for u in civ.units if u.type == "ram") + sum(1 for t in civ.training if t["type"] == "ram")
        if enemy is not None and rams < int(r.army.get("siege_rams", 2)) and mil.can_train(civ, "ram") \
                and self.ratio(enemy) > 0.7 * self.t.G("theta_attack", 2.0):
            out.append(Action("train", ("train", "ram"), r.units["ram"].label, r.unit_cost("ram"), r.units["ram"].hours,
                              {"defense": 0.4}, ["a ram for the siege"], btype="ram"))
        towers = civ.of_type("tower", built_only=False)
        if L >= r.buildings["tower"].castle and len(towers) < min(8, round(self.t.G("g_defense", 1.0) * 2 + threat * 2)) \
                and not any(b.level == 0 for b in towers):
            out.append(self.t.build_action("tower", {"defense": 0.25 + 0.5 * threat}, "a tower watches the border",
                                           tag="army"))
        mu = r.military_upgrades
        if len(civ.soldiers) >= 4 and L >= mu.get("from_castle", 5) and civ.of_type("forge"):
            forge = max(b.level for b in civ.of_type("forge"))
            for craft in r.MILITARY:
                lvl = civ.tool_level(craft) + 1
                if lvl <= mu.get("max_level", 3) and lvl <= forge and not civ.researching(craft):
                    out.append(Action("research", ("research", craft), f"{r.research_label(craft)} {lvl}",
                                      r.research_cost(craft, lvl), r.research_hours(craft, lvl),
                                      {"defense": 0.25 * len(civ.soldiers) / max(4, want)},
                                      [f"{len(civ.soldiers)} soldiers would fight better"], craft=craft))
        if self.w.military.can_train(civ, "scout"):          # version 7: scouts lift the fog
            fog = self.w.fog
            known = fog.share_known(civ)
            # a scout is worth it when we'll soon need a mine we haven't found, or (from castle 2) out of curiosity
            hidden = any(L + 1 >= r.buildings[mine].castle and
                         not any(fog.usable(civ, n) for n in self.w.map.by_kind.get(kind, []))
                         for kind, mine in (("iron", "iron_mine"), ("gold", "gold_mine")))
            curious = L >= 2 and known < 0.5 and self.t.G("exploration", 0.5) > 0.3
            if hidden or curious:
                why = "we need a mine we haven't found" if hidden else f"we know {known:.0%} of the land"
                out.append(Action("train", ("train", "scout"), "Scout", dict(r.scout["cost"]), r.scout["hours"],
                                  {"explore": (0.6 if hidden else 0.0) + 0.3 * (1 - known)}, [why], btype="scout"))
        if enemy is not None and self.w.military.can_train(civ, "spy"):
            rep = civ.intel.get(enemy.id)
            stale = rep is None or rep.get("source") != "spy" or \
                est.now - rep["t"] > r.spies.get("report_days", 3) * 86400
            if stale and not civ.spies:
                out.append(Action("train", ("train", "spy"), "Spy", dict(r.spies["cost"]), r.spies["hours"],
                                  {"defense": 0.3 + 0.3 * self.t.G("prudence", 0.5)},
                                  ["we don't know what the enemy has"], btype="spy"))
        return [a for a in out if a is not None]

    def for_soldiers(self, req: dict, share: float, est: "Estimate", level: int) -> list["Action"]:
        """A castle level that needs soldiers (castle 7: 15, castle 10: 50)."""
        from .tactical import Action

        civ, r = self.civ, self.r
        out = []
        if not civ.of_type("barracks", built_only=False):
            a = self.t.build_action("barracks", {"castle": share}, f"needed for castle {level}: soldiers", tag="req")
            return [a] if a else []
        have = len(civ.soldiers) + sum(1 for t in civ.training if t["type"] != "spy")
        if have < req["soldiers"]:
            utype = self.unit_choice()
            if utype is not None:
                out.append(Action("train", ("train", utype), r.units[utype].label, r.unit_cost(utype),
                                  r.units[utype].hours, {"castle": share},
                                  [f"needed for castle {level}: {have}/{req['soldiers']} soldiers"], btype=utype))
        return out

    # ------------------------------------------------------------------ war: attack, retreat, spies
    def war(self, est: "Estimate") -> str | None:
        """Called after the actions run. Returns a short note for the thoughts panel."""
        civ, w = self.civ, self.w
        mil, dip = w.military, w.diplomacy
        enemy = self.enemy()
        if enemy is None or enemy.castle is None:
            return None
        for u in civ.spies:
            if u.state == "home":
                rep = civ.intel.get(enemy.id)
                if rep is None or rep.get("source") != "spy" or est.now - rep["t"] > self.r.spies.get("report_days", 3) * 86400:
                    mil.send_spy(civ, enemy)
                break
        if civ.muster:
            return "gathering the army"
        attacking = [u for u in civ.soldiers if u.order == "attack"]
        if attacking:
            hp = sum(u.hp for u in attacking)
            defenders = enemy.soldiers                       # nobody left to fight: march on to the castle
            weaker = mil.strength(attacking, civ) < mil.strength(defenders, enemy)
            if civ.attack_hp0 and defenders and weaker and \
                    hp < float(self.r.army.get("retreat_below", 0.35)) * civ.attack_hp0:
                mil.order_return(civ, "Too many fell.")
                return "retreat"
            return "attacking"
        intent, plan = self.intent()
        if intent == "attack" and not getattr(plan, "war_done", False):
            note = self.kings_war(plan, enemy)
            if note:
                return note
        counter = civ.counter
        if counter and est.now < counter["until"] and counter["enemy"] == enemy.id and not dip.peace_period() \
                and intent != "seek_peace":
            civ.counter = None                                  # one roll per won defense
            if civ.rng.random() < self.t.G("counter_attack", 0.5) and len(civ.soldiers) >= 3 \
                    and self.ratio(enemy) > 0.8:
                if not dip.at_war(civ.id, enemy.id) and w.diplomat is not None:
                    w.diplomat.declare_war(civ, enemy, "You attacked us; now we answer.")
                n = mil.order_attack(civ, enemy)
                if n:
                    return f"counter-attack with {n} soldiers after a won defense"
        if dip.peace_period() or len(civ.soldiers) < int(self.r.war.get("min_army", 5)):
            return None
        if intent in ("defend", "seek_peace"):
            return None                                       # the king keeps the army home
        theta = self.t.G("theta_attack", 2.0)
        ratio = self.ratio(enemy)
        if ratio <= theta:
            return None
        if dip.treaty(civ.id, enemy.id) and self.keeps_treaty(enemy):
            return None                                       # we keep our word
        from . import lookahead                               # play out the futures before marching (live world)
        seen = lookahead.gate(w, civ, enemy)
        if seen == "pending":
            return f"playing out the futures before attacking (we look {ratio:.1f}× stronger)"
        if seen == "wait":
            return "the futures said wait: the army stays home for now"
        if not dip.at_war(civ.id, enemy.id) and self.t.G("honesty", 0.7) >= 0.5 and w.diplomat is not None:
            w.diplomat.declare_war(civ, enemy, f"Our army is {ratio:.1f} times stronger.")
        n = mil.order_attack(civ, enemy)
        return f"attack with {n} soldiers (we look {ratio:.1f}× stronger, we attack above {theta:.1f}×)" if n else None

    # ------------------------------------------------------------------ the market fills a gap
    def market(self, est: "Estimate") -> None:
        """If the next goal waits only for one resource and we have plenty of another, trade at the market."""
        civ, tr = self.civ, self.w.trade
        if not tr.has_market(civ) or not self.t.targets:
            return
        chunk = float(self.r.market.get("chunk", 100))
        goal = self.t.targets[0]
        short = [(k, v - civ.stock.get(k, 0)) for k, v in goal.cost.items() if civ.stock.get(k, 0) < v]
        if len(short) != 1:
            return
        need_res, need = short[0]
        spare = [(k, civ.stock[k] - 0.6 * civ.cap(k)) for k in ("food", "wood", "stone", "ore", "gold")
                 if k != need_res and k not in goal.cost and civ.stock[k] > 0.6 * civ.cap(k)]
        if not spare:
            return
        give, extra = max(spare, key=lambda kv: kv[1])
        amount = min(extra, chunk * 3)
        if tr.quote(civ, give, need_res, amount) >= min(need, chunk) * 0.5:
            tr.exchange(civ, give, need_res, amount)
