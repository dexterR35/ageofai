"""Version 5: training soldiers, marching, combat, towers, loot, a kingdom's fall and rebirth, spies.

Combat follows the doc: every 2 s each soldier hits the nearest enemy in reach for
D = max(1, A·k + U_weapons − (Ar + U_armor))·ε. Watchtowers shoot too. Destroying a building takes 30% of
its cost; destroying the castle ends the kingdom, and a new one is born there 7 days later.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Callable

from . import formulas as F
from .entities import Building, Person
from .pathfinding import step_along
from .units import Unit, count_types

if TYPE_CHECKING:
    from .civ import Civilization
    from .world import World

UNIT_NAMES = {"militia": "militia", "spearman": "spearmen", "archer": "archers", "swordsman": "swordsmen",
              "knight": "knights", "ram": "rams", "spy": "spies"}


class Military:
    def __init__(self, world: "World"):
        self.w = world
        self.r = world.rules
        self.cfg = world.rules.army
        self._acc = 0.0
        self._heal_acc = 0.0
        self.battles: dict[str, dict] = {}         # defender id → {"start", "last", "losses": {civ: n}, "updates"}
        self.effects: list[dict] = []              # hits for the page (cleared by the snapshot)
        self.rebirth_genome: Callable[["Civilization"], object] | None = None
        self._front: dict[str, tuple[float, float, float, float] | None] = {}   # army plan: x, y, dir x, dir y

    # ================================================================== helpers
    def faction(self, fid: str | None):
        """A kingdom, or the bandit camp (version 7)."""
        if fid == "bandits":
            return self.w.bandits
        return self.w.civ_by_id(fid)

    def enemies(self, civ: "Civilization") -> list["Civilization"]:
        return sorted((c for c in self.w.civs if c is not civ and c.alive),
                      key=lambda c: (math.dist(civ.castle.center, c.castle.center)
                                     if civ.castle is not None and c.castle is not None else math.inf, c.id))

    def home_point(self, civ: "Civilization") -> tuple[float, float]:
        if civ is self.w.bandits:
            return civ.x, civ.y
        if civ.castle is None:
            return (0.0, 0.0)
        cx, cy = civ.castle.center
        toward = 1 if not civ.mirror else -1                     # stand on the side facing the enemy
        return cx + 2 * toward, cy + 4 * toward

    def unit_damage(self, u: Unit, owner: "Civilization", target, target_owner: "Civilization | None") -> float:
        spec = self.r.units.get(u.type)
        if spec is None:
            return 0.0
        k = 1.0
        armor = 0.0
        ups = 0
        if isinstance(target, Unit):
            k = spec.bonus.get(target.type, 1.0)
            ts = self.r.units.get(target.type)
            armor = ts.armor if ts else 0.0
            ups = target_owner.tool_level("armor") if target_owner else 0
        lo, hi = self.cfg.get("luck", [0.9, 1.1])
        eps = owner.rng.uniform(lo, hi)
        d = F.damage(spec.attack, k, owner.tool_level("weapons"), armor, ups, eps) * self.veteran(u)
        if isinstance(target, Building) and not spec.buildings_only:
            d *= float(self.cfg.get("vs_buildings", 0.1))         # swords against stone walls
        return d

    def veteran(self, u: Unit) -> float:
        """Army plan: a soldier with 5 kills hits +10%, with 15 kills +20%."""
        bonus = 1.0
        for kills, extra in zip(self.cfg.get("veteran_kills", [5, 15]), self.cfg.get("veteran_bonus", [0.1, 0.2])):
            if u.kills >= kills:
                bonus = 1.0 + float(extra)
        return bonus

    @staticmethod
    def dist(a, b) -> float:
        bx, by = b.center if isinstance(b, Building) else (b.x, b.y)
        if isinstance(b, Building):
            # distance to the footprint's edge, not its middle
            h = b.size / 2
            dx = max(0.0, abs(a.x - bx) - h)
            dy = max(0.0, abs(a.y - by) - h)
            return math.hypot(dx, dy)
        return math.hypot(a.x - bx, a.y - by)

    def strength(self, units: list[Unit], owner: "Civilization | None" = None) -> float:
        """Lanchester S = (Σ HP)·(Σ D / 2 s), against an average target with no armor."""
        rows = []
        w = owner.tool_level("weapons") if owner else 0
        for u in units:
            s = self.r.units.get(u.type)
            if s is None or s.buildings_only:
                continue
            rows.append((u.hp, F.damage(s.attack, 1.0, w, 0, 0)))
        return F.army_strength(rows, float(self.cfg.get("combat_seconds", 2)))

    def strength_of_counts(self, counts: dict[str, float], weapons: int = 0) -> float:
        rows = []
        for t, n in counts.items():
            s = self.r.units.get(t)
            if s is None or s.buildings_only or n <= 0:
                continue
            rows += [(s.hp, F.damage(s.attack, 1.0, weapons, 0, 0))] * int(round(n))
        return F.army_strength(rows, float(self.cfg.get("combat_seconds", 2)))

    # ================================================================== training (barracks, range, stable, workshop, market)
    def unit_info(self, utype: str) -> dict:
        """Cost, training place, hours, health and speed of a unit type: spies and scouts come from their own
        sections of rules.yaml, soldiers from `units`."""
        if utype in ("spy", "scout"):
            spy = utype == "spy"
            cfg = self.r.spies if spy else self.r.scout
            return {"cost": dict(cfg["cost"]),
                    "building": cfg.get("building", "market") if spy else "castle",
                    "hours": float(cfg.get("hours", 2 if spy else 1)),
                    "hp": float(cfg.get("hp", 20)),
                    "speed": float(cfg.get("speed", 600 if spy else 900))}
        spec = self.r.units[utype]
        return {"cost": self.r.unit_cost(utype), "building": spec.building, "hours": spec.hours,
                "hp": spec.hp, "speed": spec.speed}

    def can_train(self, civ: "Civilization", utype: str) -> bool:
        if utype == "scout":
            sc = self.r.scout
            return self.r.feature("fog") and civ.castle is not None and \
                len(civ.scouts) + sum(1 for t in civ.training if t["type"] == "scout") < sc.get("max", 2)
        if utype == "spy":
            sp = self.r.spies
            return civ.castle_level >= sp.get("castle", 5) and any(
                b.type == sp.get("building", "market") and b.built for b in civ.buildings) and \
                len(civ.spies) + sum(1 for t in civ.training if t["type"] == "spy") < sp.get("max_active", 3)
        spec = self.r.units.get(utype)
        if spec is None or civ.castle_level < spec.castle:
            return False
        if not any(b.type == spec.building and b.built for b in civ.buildings):
            return False
        return civ.soldier_room(self.w.clock.t) > 0

    def start_training(self, civ: "Civilization", utype: str, reason: str = "") -> bool:
        if not self.can_train(civ, utype):
            return False
        cost = self.unit_info(utype)["cost"]
        if not civ.can_afford(cost):
            return False
        civ.pay(cost)
        civ.training.append({"type": utype, "progress": 0.0})
        label = utype if utype in ("spy", "scout") else self.r.units[utype].label.lower()
        self.w.log(civ, "train", f"Training a {label}." + (f" {reason}" if reason else ""), 2)
        return True

    def promotion(self, civ: "Civilization", utype: str) -> str | None:
        """Army plan: the better unit this type can become now (militia → swordsman, spearman → knight)."""
        to = self.cfg.get("promote", {}).get(utype)
        if not to or to not in self.r.units:
            return None
        spec = self.r.units[to]
        if civ.castle_level < spec.castle or not any(b.type == spec.building and b.built for b in civ.buildings):
            return None
        return to

    def promotion_cost(self, utype: str, to: str) -> dict[str, float]:
        a, b = self.r.unit_cost(utype), self.r.unit_cost(to)
        return {k: v - a.get(k, 0) for k, v in b.items() if v - a.get(k, 0) > 0}

    def promote(self, civ: "Civilization", utype: str, reason: str = "") -> bool:
        """Send one soldier of this type (the most experienced, at home) to become the better unit."""
        to = self.promotion(civ, utype)
        if to is None:
            return False
        home = [u for u in civ.soldiers if u.type == utype and u.order == "defend" and u.target is None]
        cost = self.promotion_cost(utype, to)
        if not home or not civ.can_afford(cost):
            return False
        u = max(home, key=lambda x: (x.kills, x.hp))
        civ.pay(cost)
        civ.units.remove(u)                              # he is at the barracks until it's done
        civ.training.append({"type": to, "progress": 0.0, "promote": utype, "kills": u.kills})
        self.w.refresh_food_rate(civ)
        self.w.log(civ, "promote", f"A {self.r.units[utype].label.lower()} is trained to become a "
                                   f"{self.r.units[to].label.lower()}." + (f" {reason}" if reason else ""), 3)
        return True

    def _train(self, civ: "Civilization", dt: float) -> None:
        """Each training building works on one recruit at a time (work hours, like recruiting)."""
        busy: set[str] = set()
        for job in list(civ.training):
            info = self.unit_info(job["type"])
            where = info["building"]
            if where in busy:
                continue
            busy.add(where)
            hours = info["hours"]
            if job.get("promote"):
                hours *= float(self.cfg.get("promote_time_share", 0.5))
            job["progress"] += dt / (hours * 3600)
            if job["progress"] >= 1.0:
                civ.training.remove(job)
                u = self.spawn_unit(civ, job["type"], promoted=bool(job.get("promote")))
                u.kills = int(job.get("kills", 0))

    def spawn_unit(self, civ: "Civilization", utype: str, promoted: bool = False) -> Unit:
        hp = self.unit_info(utype)["hp"]
        hx, hy = self.home_point(civ)
        u = Unit(self.w.next_id(), civ.id, utype, hx, hy, hp, self.w.clock.t)
        u.name = f"{UNIT_NAMES.get(utype, utype)[:-1] if utype != 'militia' else 'militiaman'} #{u.id}"
        if utype == "spy":
            u.order = "spy"
        if utype == "scout":
            u.order = "explore"
        civ.units.append(u)
        self.w.refresh_food_rate(civ)
        civ.dirty = True
        if utype == "spy":
            self.w.log(civ, "spy", "A spy is ready at the market.", 3)
        elif utype == "scout":
            self.w.log(civ, "scout", "A scout sets out to explore.", 3)
        elif promoted:
            self.w.log(civ, "train_done", f"A soldier returns to the army as a {self.r.units[utype].label.lower()}.", 3)
        else:
            self.w.log(civ, "train_done", f"A new {self.r.units[utype].label.lower()} joins the army "
                                          f"({len(civ.soldiers)} soldiers).", 3)
        return u

    # ================================================================== orders (from the AI)
    def rally_point(self, civ: "Civilization", enemy: "Civilization") -> tuple[float, float]:
        """Where the army gathers: a little way from our castle, toward the enemy."""
        (ax, ay), (bx, by) = civ.castle.center, enemy.castle.center
        d = math.hypot(bx - ax, by - ay) or 1.0
        k = min(float(self.cfg.get("rally_tiles", 8)), d / 3)
        return ax + (bx - ax) / d * k, ay + (by - ay) / d * k

    def order_attack(self, civ: "Civilization", enemy: "Civilization", share: float = 0.8) -> int:
        """Army plan: most of the army gathers at a rally point, then marches together.
        Returns how many were called up."""
        if enemy.castle is None or civ.castle is None or not self.w.diplomacy.may_attack(civ.id, enemy.id):
            return 0
        army = [u for u in civ.soldiers if u.order not in ("attack", "muster")]
        keep = int(round(len(army) * (1 - share)))
        # the first `keep` stay home: archers first (they defend well beside the towers), then the weakest
        army.sort(key=lambda u: (self.r.units[u.type].range <= 1, u.hp))
        go = army[keep:]
        if not go:
            return 0
        rally = self.rally_point(civ, enemy)
        for u in go:
            u.order, u.goal, u.target, u.path = "muster", rally, None, ()
        civ.muster = {"enemy": enemy.id, "rally": rally, "until": self.w.clock.t +
                      float(self.cfg.get("muster_minutes", 30)) * 60}
        self.w.log(civ, "muster", f"{civ.name}'s army gathers to march on {enemy.name} ({len(go)} soldiers).", 6)
        return len(go)

    def _muster(self, civ: "Civilization") -> None:
        """Once most of the army has gathered (or the time is up), it marches together."""
        m = civ.muster
        if not m:
            return
        enemy = self.w.civ_by_id(m["enemy"])
        band = [u for u in civ.soldiers if u.order == "muster"]
        if enemy is None or not enemy.alive or enemy.castle is None or not band:
            for u in band:
                u.order, u.goal = "defend", None
            civ.muster = None
            return
        rx, ry = m["rally"]
        there = sum(1 for u in band if math.hypot(u.x - rx, u.y - ry) <= 3)
        if there < float(self.cfg.get("muster_share", 0.8)) * len(band) and self.w.clock.t < m["until"]:
            return
        for u in band:
            u.order, u.goal, u.target, u.path = "attack", enemy.castle, None, ()
        civ.muster = None
        civ.attack_started = self.w.clock.t
        civ.attack_hp0 = sum(u.hp for u in civ.soldiers if u.order == "attack")
        civ.attack_size0 = len(band)
        self.w.diplomacy.attacked(civ, enemy)
        n = count_types(band)
        what = ", ".join(f"{v} {UNIT_NAMES.get(k, k)}" for k, v in n.items())
        self.w.log(civ, "attack", f"{civ.name} attacks {enemy.name} with {what}!", 9)

    def order_return(self, civ: "Civilization", why: str = "") -> None:
        civ.muster = None
        back = [u for u in civ.soldiers if u.order in ("attack", "muster")]
        for u in back:
            u.order, u.goal, u.target, u.path = "return", None, None, ()
        if back:
            civ.attack_started = None
            self.w.log(civ, "retreat", f"{civ.name}'s army turns back home." + (f" {why}" if why else ""), 6)

    # ================================================================== every tick
    def tick(self, dt: float, working: bool) -> None:
        for civ in self.w.civs:
            if not civ.alive:
                continue
            if working and civ.training:
                self._train(civ, dt)
            if civ.muster:
                self._muster(civ)
            self._front[civ.id] = self._front_line(civ)
            self._wounded(civ)
            for u in list(civ.units):
                if u.is_spy:
                    self._spy(civ, u, dt)
                elif u.is_scout:
                    self._scout(civ, u, dt)
                else:
                    self._move(civ, u, dt)
        camp = self.w.bandits
        if camp is not None:
            for u in list(camp.units):
                self._move(camp, u, dt)
        self._acc += dt
        cs = float(self.cfg.get("combat_seconds", 2))
        rounds = int(self._acc // cs)
        self._acc -= rounds * cs
        for _ in range(min(rounds, 10)):
            if not self._round():
                break
        self._heal_acc += dt
        if self._heal_acc >= 600:
            self._heal(self._heal_acc / 3600)
            self._heal_acc = 0.0
        self._battle_news()
        self._rebirths()
        if camp is not None:
            camp.tick()

    # ------------------------------------------------------------------ movement
    def _speed(self, u: Unit) -> float:
        return self.unit_info(u.type)["speed"] / 3600

    def _front_line(self, civ) -> tuple[float, float, float, float] | None:
        """The attacking army's front: the soldier nearest the enemy castle, and the direction of the march."""
        band = [u for u in civ.soldiers if u.order == "attack" and self.r.units[u.type].range <= 1
                and not self.r.units[u.type].buildings_only and isinstance(u.goal, Building)]
        if not band:
            return None
        goal = band[0].goal
        gx, gy = goal.center
        lead = min(band, key=lambda u: math.hypot(u.x - gx, u.y - gy))
        d = math.hypot(gx - lead.x, gy - lead.y) or 1.0
        return lead.x, lead.y, (gx - lead.x) / d, (gy - lead.y) / d

    def _formation_goal(self, civ, u: Unit):
        """Archers keep behind the front line; cavalry rides on the flanks; the rest go for the castle."""
        front = self._front.get(civ.id)
        if front is None or not isinstance(u.goal, Building):
            return u.goal
        fx, fy, dx, dy = front
        spec = self.r.units[u.type]
        if spec.range > 1:
            back = float(self.cfg.get("archer_behind", 3))
            return round(fx - dx * back), round(fy - dy * back)
        if u.type == "knight":
            side = float(self.cfg.get("flank_tiles", 4)) * (1 if u.id % 2 else -1)
            return round(fx - dy * side + dx * 2), round(fy + dx * side + dy * 2)
        return u.goal

    def _wounded(self, civ) -> None:
        """Army plan: a soldier below his kingdom's retreat_hp gene goes home to heal (attackers only)."""
        if civ is self.w.bandits:
            return
        limit = civ.genome.get("retreat_hp", 0.25)
        for u in civ.soldiers:
            if u.order == "attack" and u.hp < limit * u.max_hp:
                u.order, u.goal, u.target, u.path = "return", None, None, ()

    def _goal_for(self, civ: "Civilization", u: Unit):
        """Where the unit walks now: its target, its order's goal (in formation), or home."""
        if u.target is not None:
            return u.target
        if u.order == "attack" and u.goal is not None:
            return self._formation_goal(civ, u)
        if u.order in ("help", "muster") and u.goal is not None:
            return u.goal
        if u.order == "defend":
            return None
        return self.home_point(civ)

    def _move(self, civ: "Civilization", u: Unit, dt: float) -> None:
        goal = self._goal_for(civ, u)
        if goal is None:
            hx, hy = self.home_point(civ)
            if math.hypot(u.x - hx, u.y - hy) > 6:
                goal = (hx, hy)
            else:
                u.state = "idle"
                return
        if u.target is not None:
            reach = self.r.units[u.type].range
            if self.dist(u, u.target) <= reach + 0.5:
                u.state = "fight"
                return
        if isinstance(goal, tuple) and math.hypot(u.x - goal[0], u.y - goal[1]) < 1.0:
            if u.order == "return":
                u.order = "defend"
            u.state = "idle"
            return
        self._walk_to(u, goal, dt)

    def _goal_tiles(self, goal) -> tuple[object, list[tuple[int, int]]]:
        m = self.w.map
        if isinstance(goal, Building):
            return ("bld", goal.id), m.access_tiles(goal.x, goal.y, goal.size, goal.walkable)
        if isinstance(goal, (Unit, Person)):
            t = (int(goal.x + 0.5), int(goal.y + 0.5))
            return ("at", t), [t] if m.walkable(*t) else self._near_walkable(t)
        t = (int(goal[0] + 0.5), int(goal[1] + 0.5))
        return ("at", t), [t] if m.walkable(*t) else self._near_walkable(t)

    def _near_walkable(self, t: tuple[int, int]) -> list[tuple[int, int]]:
        return self.w.map.nearest_walkable(t[0], t[1], 5)

    def _walk_to(self, u: Unit, goal, dt: float) -> None:
        now = self.w.clock.t
        key, tiles = self._goal_tiles(goal)
        soldier = not u.is_spy                       # a spy's state is its mission (go / stay / back)
        if not tiles:
            if soldier:
                u.state = "idle"
            return
        moved = u.path_goal != key and not (isinstance(goal, tuple) and u.path and u.path_i < len(u.path)
                                            and now < u.repath_at)          # a formation spot moves a little
        if moved or u.path_version != self.w.map.version or now >= u.repath_at or u.path_i >= len(u.path):
            start = u.tile
            if not self.w.map.walkable(*start):
                near = self._near_walkable(start)
                if near:
                    u.x, u.y = float(near[0][0]), float(near[0][1])
                    start = near[0]
            path = self.w.paths.find(start, key, tiles)
            if not path:
                if soldier:
                    u.state = "idle"
                u.repath_at = now + float(self.cfg.get("repath_seconds", 30))
                return
            u.path, u.path_i, u.path_goal, u.path_version = path, 0, key, self.w.map.version
            u.repath_at = now + (10.0 if isinstance(goal, tuple) else float(self.cfg.get("repath_seconds", 30)))
        u.x, u.y, u.path_i = step_along(u.path, u.path_i, u.x, u.y, self._speed(u) * dt)
        if soldier:
            u.state = "walk"

    # ------------------------------------------------------------------ one round of blows (every 2 s)
    def _round(self) -> bool:
        """Returns False when nobody is near an enemy (nothing to do)."""
        civs = [c for c in self.w.civs if c.alive]
        camp = self.w.bandits
        raiding = camp is not None and bool(camp.units)
        if len(civs) < 2 and not raiding:
            return False
        aggro = float(self.cfg.get("aggro_tiles", 8))
        fought = False
        hits: list[tuple[object, float, "Civilization", "Civilization | None", Unit | None]] = []
        for civ in civs + ([camp] if raiding else []):
            if civ is camp:
                foes = civs                                   # bandits against everyone
            else:
                foes = [c for c in civs if c is not civ and (self.w.diplomacy.at_war(civ.id, c.id) or
                                                              any(u.order == "attack" for u in c.soldiers))]
                if raiding:
                    foes = foes + [camp]
            if not foes:
                continue
            enemy_units = [(u, c) for c in foes for u in c.soldiers]
            for u in civ.soldiers:
                spec = self.r.units[u.type]
                tgt, owner = self._pick_target(civ, u, spec, enemy_units, foes, aggro, loot_only=civ is camp)
                u.target = tgt
                if tgt is None:
                    continue
                if self.dist(u, tgt) <= spec.range + 0.5:
                    hits.append((tgt, self.unit_damage(u, civ, tgt, owner), civ, owner, u))
                    fought = True
            # watchtowers shoot at enemy soldiers on our land
            tw = self.cfg.get("tower", {"attack": 6, "range": 7})
            for b in civ.buildings if civ is not camp else ():
                if b.type != "tower" or not b.built:
                    continue
                near = [(e, c) for e, c in enemy_units if self.dist(e, b) <= tw["range"]]
                if near:
                    e, c = min(near, key=lambda ec: self.dist(ec[0], b))
                    lo, hi = self.cfg.get("luck", [0.9, 1.1])
                    ts = self.r.units[e.type]
                    d = F.damage(tw["attack"], 1.0, 0, ts.armor, c.tool_level("armor"), civ.rng.uniform(lo, hi))
                    hits.append((e, d, civ, c, None))
                    fought = True
        for tgt, d, civ, owner, by in hits:
            self._hit(tgt, d, civ, owner, by)
        return fought

    def _pick_target(self, civ, u: Unit, spec, enemy_units, foes, aggro, loot_only: bool = False):
        """Keep fighting the current target; else the nearest enemy soldier nearby; else (attacking) people
        and buildings near the army; rams only ever hit buildings."""
        cur = u.target
        if cur is not None:
            alive = cur.hp > 0 if not isinstance(cur, Building) else cur in self._owner_buildings(cur)
            if alive and self.dist(u, cur) <= max(aggro, spec.range + 1):
                return cur, self._owner_of(cur)
        if not spec.buildings_only:
            near = [(e, c) for e, c in enemy_units if self.dist(u, e) <= aggro]
            if near:
                return self._best_target(u, spec, near)
            if u.order == "defend" and civ is not self.w.bandits and civ.castle is not None:
                # army plan: an enemy army close to our castle — go out and meet it
                watch = float(self.cfg.get("watch_tiles", 30))
                cx, cy = civ.castle.center
                coming = [(e, c) for e, c in enemy_units if e.order in ("attack", "muster")
                          and math.hypot(e.x - cx, e.y - cy) <= watch]
                if coming:
                    return self._best_target(u, spec, coming)
        if u.order != "attack" or loot_only:                  # bandits fight soldiers, then march to the castle
            return None, None
        best, best_d, best_c = None, aggro, None
        for c in foes:
            if not spec.buildings_only:
                for p in c.people:
                    d = math.hypot(p.x - u.x, p.y - u.y)
                    if d < min(best_d, 3.0):
                        best, best_d, best_c = p, d, c
            for b in c.buildings:
                d = self.dist(u, b)
                if d < best_d:
                    best, best_d, best_c = b, d, c
        return best, best_c

    def _best_target(self, u: Unit, spec, near: list):
        """Army plan: hit what we are strong against, finish off the weakest one in reach."""
        reach = spec.range + 0.5

        def score(ec):
            e = ec[0]
            k = spec.bonus.get(e.type, 1.0)
            d = self.dist(u, e)
            if d <= reach:
                return (0, -k, e.hp / max(1.0, e.max_hp), d)
            return (1, -k, d, 0)

        return min(near, key=score)

    def _owner_buildings(self, b: Building) -> list[Building]:
        c = self.w.civ_by_id(b.civ_id)
        return c.buildings if c else []

    def _owner_of(self, obj) -> "Civilization | None":
        return self.faction(obj.civ_id)

    def _hit(self, tgt, d: float, civ: "Civilization", owner: "Civilization | None", by: Unit | None) -> None:
        if owner is None or not owner.alive:
            return
        if isinstance(tgt, Building) and not tgt.built:
            if tgt.type == self.r.wonder.get("type", "wonder"):  # a Wonder site is as strong as it is high
                if tgt.max_hp <= 0:                             # its walls are counted at the first blow only
                    full = self.r.building_hp(tgt.type, 1)
                    tgt.max_hp = full
                    tgt.hp = full * max(float(self.r.wonder.get("site_hp_from", 0.05)), tgt.progress)
            else:                                               # a site has no walls yet: one blow ends it
                tgt.hp = d
        if tgt.hp <= 0:
            return
        tgt.hp -= d
        self.effects.append({"x": round(tgt.center[0] if isinstance(tgt, Building) else tgt.x, 2),
                             "y": round(tgt.center[1] if isinstance(tgt, Building) else tgt.y, 2),
                             "civ": civ.id, "kind": "tower" if by is None else ("ram" if by.type == "ram" else "hit")})
        battle = self._battle(owner, civ)
        if tgt.hp > 0:
            return
        if by is not None:
            by.kills += 1
        if isinstance(tgt, Unit):
            owner.units.remove(tgt)
            owner.losses += 1
            civ.kills += 1
            battle["losses"][owner.id] = battle["losses"].get(owner.id, 0) + 1
            if owner is self.w.bandits:
                owner.killed_by(civ)
            else:
                self.w.refresh_food_rate(owner)
        elif isinstance(tgt, Person):
            if tgt in owner.people:
                owner.deaths += 1
                self.w.remove_person(owner, tgt, f"{tgt.name} was killed by {civ.name}'s soldiers.", "death", 7)
        elif isinstance(tgt, Building):
            self._destroy(owner, tgt, civ)

    def _destroy(self, owner: "Civilization", b: Building, by: "Civilization") -> None:
        if b not in owner.buildings:
            return
        share = float(self.r.war.get("loot_share", 0.3))
        if b.type == "castle":
            cost = self.r.castle_cost(max(1, b.level - 1))
        elif not b.built and self.r.progressive(b.type):        # only what was delivered can be carried off
            cost = dict(b.paid)
        else:
            cost = self.r.building_cost(b.type, max(1, b.level))
        loot = {k: v * share for k, v in cost.items()}
        for k, v in loot.items():
            by.store(k, v)
        label = "castle" if b.type == "castle" else self.r.buildings[b.type].label
        what = ", ".join(f"{v:.0f} {k}" for k, v in loot.items() if v >= 1)
        self.w.log(by, "loot", f"{by.name} destroyed {owner.name}'s {label} and took {what}.", 8)
        if b.type == "castle":
            self.fall(owner, by)
        else:
            self.w.destroy_building(owner, b, f"{owner.name}'s {label} was destroyed by {by.name}.")

    # ------------------------------------------------------------------ a kingdom falls (castle destroyed)
    def fall(self, civ: "Civilization", by: "Civilization | None", text: str | None = None) -> None:
        """The kingdom ends: its castle was destroyed (`by`), or `text` says why (it has no people left)."""
        w = self.w
        w.log(civ, "fall", text or (f"{civ.name} has fallen! Its castle was destroyed"
                                    + (f" by {by.name}." if by else ".")), 10)
        rival = by if by is not None and by is not w.bandits else None
        w.rival("fallen", civ.id, other=rival.id if rival else None, text=text or "castle destroyed")
        if rival is not None:
            w.rival("castle_destroyed", rival.id, other=civ.id, text=f"{rival.name} destroyed {civ.name}'s castle")
        for other in w.civs:                                   # its wars end: whoever it fought wins them
            if other is not civ:
                w.diplomacy.end_war(civ.id, other.id, other.id, "fall")
        if by is not None:
            for p in list(civ.people):
                if by.alive and by.pop < by.beds() and len(by.workers(w.clock.t)) < self.r.workers_max(by.castle_level):
                    w.transfer_person(civ, by, p)
        for p in list(civ.people):
            w.behavior.release(p)
            p.alive = False
        civ.people.clear()
        civ.units.clear()
        civ.training.clear()
        civ.research.clear()
        civ.recruiting.clear()
        for b in list(civ.buildings):
            civ.buildings.remove(b)
            w.map.remove_building(b.x, b.y, b.size)
        civ.castle = None
        civ.invalidate_depots()
        civ.alive = False
        civ.fallen_at = w.clock.t
        live = w.rules.war.get("fast_mode_rebirth", False) or getattr(w, "live", False)
        if live:
            civ.rebirth_at = w.clock.t + float(w.rules.war.get("rebirth_days", 7)) * 86400
        if by is not None:
            by.attack_started = None
            for u in by.soldiers:
                if u.order == "attack":
                    u.order, u.goal, u.target = "return", None, None
        for key in [k for k in self.battles if civ.id in k.split("|")]:
            self.battles.pop(key)

    def _rebirths(self) -> None:
        for i, civ in enumerate(self.w.civs):
            if not civ.alive and civ.rebirth_at is not None and self.w.clock.t >= civ.rebirth_at:
                genome = self.rebirth_genome(civ) if self.rebirth_genome else civ.genome
                self.w.rebirth(i, genome)

    # ------------------------------------------------------------------ healing at home
    def _heal(self, hours: float) -> None:
        rate = float(self.cfg.get("heal_per_hour", 0.05))
        for civ in self.w.civs:
            for u in civ.soldiers:
                if u.order == "defend" and u.target is None:
                    u.hp = min(u.max_hp, u.hp + u.max_hp * rate * hours)

    # ------------------------------------------------------------------ battles: news for the journal and the radio
    def _battle(self, defender: "Civilization", attacker: "Civilization") -> dict:
        """One battle per pair of kingdoms; it ends after 2 minutes without a blow."""
        now = self.w.clock.t
        key = "|".join(sorted((defender.id, attacker.id)))
        b = self.battles.get(key)
        if b is None or now - b["last"] > 120:
            # the attacker is whoever marched out (whoever took the first blow doesn't matter)
            if any(u.order in ("attack", "muster") for u in defender.soldiers) and \
                    not any(u.order in ("attack", "muster") for u in attacker.soldiers):
                defender, attacker = attacker, defender
            b = {"start": now, "last": now, "losses": {}, "next_update": now + 30, "a": attacker.id, "d": defender.id}
            self.battles[key] = b
            if attacker is self.w.bandits:
                text = f"Bandits fight {defender.name}'s soldiers!"
            else:
                text = f"Battle: {attacker.name} attacks {defender.name}!"
            self.w.log(defender, "battle", text, 9)
        b["last"] = now
        return b

    def _battle_news(self) -> None:
        now = self.w.clock.t
        for key, b in list(self.battles.items()):
            a, d = self.faction(b["a"]), self.faction(b["d"])
            if a is None or d is None:
                self.battles.pop(key)
                continue
            if now - b["last"] > 120:
                la, ld = b["losses"].get(a.id, 0), b["losses"].get(d.id, 0)
                self.w.log(d, "battle_end", f"The battle is over: {a.name} lost {la} soldiers, {d.name} lost {ld}.", 8)
                self.battles.pop(key)
                if a is not self.w.bandits and d is not self.w.bandits:      # the rivalry scoreboard
                    for x, y, mine, theirs in ((a, d, la, ld), (d, a, ld, la)):
                        self.w.rival("battle", x.id, other=y.id, value=1.0 if mine < theirs else 0.0 if mine > theirs
                                     else 0.5, text=f"{a.name} attacked {d.name}: losses {la} to {ld}")
                size = getattr(a, "attack_size0", 0) or la + len(a.soldiers)
                if d is not self.w.bandits and a is not self.w.bandits and size and \
                        la >= float(self.cfg.get("counter_losses", 0.5)) * size and ld < la:
                    d.counter = {"enemy": a.id, "until": now + float(self.cfg.get("counter_hours", 2)) * 3600}
            elif now >= b["next_update"]:
                b["next_update"] = now + 30
                ra = sum(1 for u in a.soldiers if u.target is not None)
                rd = sum(1 for u in d.soldiers if u.target is not None)
                self.w.log(d, "battle_update", f"The battle goes on: {ra} {a.name} soldiers fight {rd} of {d.name}; "
                                               f"losses {b['losses'].get(a.id, 0)} to {b['losses'].get(d.id, 0)}.", 2)

    # ================================================================== scouts (version 7)
    def _scout(self, civ: "Civilization", u: Unit, dt: float) -> None:
        """Walk to the nearest unknown land; pick a new goal at most once a minute (game time)."""
        fog = self.w.fog
        now = self.w.clock.t
        goal = u.mission.get("goal")
        stale = goal is None or fog.knows(civ, *goal) or now >= u.mission.get("give_up", 0.0)
        if stale:
            if now < u.mission.get("next_pick", 0.0):
                if goal is None:
                    u.state = "idle"
                    return
            else:
                u.mission["next_pick"] = now + 60
                goal = self._frontier(civ, u)
                u.mission["goal"] = goal
                u.mission["give_up"] = now + 3 * 3600         # unreachable land: try elsewhere later
                u.path, u.path_i, u.path_goal = (), 0, None
        if goal is None:
            hx, hy = self.home_point(civ)
            if math.hypot(u.x - hx, u.y - hy) > 3:
                self._walk_to(u, (hx, hy), dt)
            else:
                u.state = "idle"
            return
        self._walk_to(u, goal, dt)
        if u.state == "idle":                                  # no path there
            u.mission["goal"] = None

    def _frontier(self, civ: "Civilization", u: Unit) -> tuple[int, int] | None:
        """The nearest unknown, walkable tile (a little luck between equally near ones)."""
        m = self.w.map
        n = m.size
        seen = self.w.fog.seen[civ.id]
        blocked = m.blocked
        cx, cy = int(u.x + 0.5), int(u.y + 0.5)
        s = -1 if civ.mirror else 1                              # the south searches in mirrored order (fairness)
        for r in range(6, n, 6):
            cand = []
            for k in range(-r, r + 1, 3):
                for dx, dy in ((k, -r), (k, r), (-r, k), (r, k)):
                    x, y = cx + s * dx, cy + s * dy
                    if 0 <= x < n and 0 <= y < n:
                        i = y * n + x
                        if not seen[i] and not blocked[i]:
                            cand.append((x, y))
            if cand:
                return civ.rng.choice(cand)
        return None

    # ================================================================== spies
    def send_spy(self, civ: "Civilization", enemy: "Civilization") -> bool:
        idle = [u for u in civ.spies if u.state in ("idle", "home")]
        if not idle or enemy.castle is None:
            return False
        u = idle[0]
        u.mission = {"enemy": enemy.id, "hours": 0.0, "checked": 0, "turned": False}
        u.state, u.goal = "go", enemy.castle
        self.w.log(civ, "spy", f"A spy leaves for {enemy.name}.", 3)
        return True

    def _spy(self, civ: "Civilization", u: Unit, dt: float) -> None:
        sp = self.r.spies
        if u.state in ("idle", "home"):
            u.state = "home"
            return
        enemy = self.w.civ_by_id(u.mission.get("enemy", ""))
        if u.state == "go":
            if enemy is None or not enemy.alive or enemy.castle is None:
                u.state = "back"
                return
            if self.dist(u, enemy.castle) <= 1.5:
                u.state = "stay"
                u.mission["arrive"] = self.w.clock.t
                return
            self._walk_to(u, enemy.castle, dt)
        elif u.state == "stay":
            u.mission["hours"] += dt / 3600
            while u.mission["checked"] < int(u.mission["hours"]):
                u.mission["checked"] += 1
                if enemy is None or not enemy.alive:
                    break
                towers = sum(1 for b in enemy.buildings if b.type == "tower" and b.built
                             and self.dist(u, b) <= sp.get("tower_radius", 14))
                p = F.spy_catch_per_hour(towers, sp.get("catch_base", 0.01), sp.get("catch_per_tower", 0.02),
                                         sp.get("catch_max", 0.30))
                if enemy.rng.random() < p:
                    if self._caught(civ, u, enemy):
                        return
                    break
            if u.mission["hours"] >= sp.get("stay_hours", 6):
                u.mission["report"] = self.report(enemy, u.mission.get("turned", False), civ) if enemy else None
                u.state = "back"
        elif u.state == "back":
            hx, hy = civ.castle.center if civ.castle else self.home_point(civ)
            if math.hypot(u.x - hx, u.y - hy) <= 3:
                rep = u.mission.get("report")
                if rep:
                    civ.intel[rep["enemy"]] = rep
                    self.w.log(civ, "spy", f"A spy is back from {self.w.civ_by_id(rep['enemy']).name}: about "
                                           f"{rep['soldiers']} soldiers, castle {rep['castle']}.", 4)
                u.state, u.mission = "home", {}
                return
            self._walk_to(u, (hx, hy), dt)

    def _caught(self, civ: "Civilization", u: Unit, enemy: "Civilization") -> bool:
        """The enemy caught our spy. They execute him (R − 10) or try to turn him (30%).
        Returns True if the spy is gone."""
        sp = self.r.spies
        deception = enemy.genome.get("deception", 1.0)
        try_turn = enemy.rng.random() < min(1.0, max(0.0, (deception - 0.5) / 1.5))
        if try_turn and enemy.rng.random() < sp.get("turn_chance", 0.30):
            u.mission["turned"] = True
            u.mission["turned_by"] = deception
            self.w.log(enemy, "spy_caught", f"{enemy.name} caught a spy from {civ.name} and turned him.", 6)
            return False                                     # he goes home with a false report
        civ.units.remove(u)
        self.w.refresh_food_rate(civ)
        if try_turn:
            self.w.log(enemy, "spy_caught", f"{enemy.name} caught a spy from {civ.name}; he would not turn and died.", 6)
        else:
            self.w.diplomacy.change(civ.id, enemy.id, "spy_executed")
            self.w.log(enemy, "spy_caught", f"{enemy.name} caught and executed a spy from {civ.name}.", 6)
        return True

    def report(self, enemy: "Civilization", false: bool, reader: "Civilization") -> dict:
        """What a spy brings home: soldiers and stocks ±20%, buildings and castle exact.
        A turned spy brings the catcher's lie: soldiers × the catcher's deception gene."""
        err = float(self.r.spies.get("error_with_spy", 0.2))
        rng = reader.rng
        counts = count_types(enemy.soldiers)
        factor = enemy.genome.get("deception", 1.0) if false else 1.0
        noisy = {k: max(0, round(v * factor * rng.uniform(1 - err, 1 + err))) for k, v in counts.items()}
        return {"enemy": enemy.id, "t": self.w.clock.t, "source": "spy", "false": false,
                "soldiers": sum(noisy.values()), "units": noisy, "weapons": enemy.tool_level("weapons"),
                "stocks": {k: round(v * rng.uniform(1 - err, 1 + err)) for k, v in enemy.stock.items()},
                "castle": enemy.castle_level,
                "buildings": {t: sum(1 for b in enemy.buildings if b.type == t and b.built)
                              for t in {b.type for b in enemy.buildings}}}

    def rumour(self, enemy: "Civilization", reader: "Civilization") -> dict:
        """Without a spy: the neighbour's power with ±50% error, refreshed once a day."""
        err = float(self.r.spies.get("error_without_spy", 0.5))
        rng = reader.rng
        counts = count_types(enemy.soldiers)
        noisy = {k: max(0, round(v * rng.uniform(1 - err, 1 + err))) for k, v in counts.items()}
        return {"enemy": enemy.id, "t": self.w.clock.t, "source": "rumour", "false": False,
                "soldiers": sum(noisy.values()), "units": noisy, "weapons": 0, "castle": enemy.castle_level}

    def daily(self, civ: "Civilization") -> None:
        """Pay the soldiers (1 coin each), lose some if unpaid or hungry, refresh what we hear of the enemy."""
        r = self.r
        n = len(civ.soldiers)
        if n:
            pay = n * float(self.cfg.get("pay_coins_per_day", 1))
            if civ.stock["coins"] >= pay:
                civ.stock["coins"] -= pay
            else:
                civ.stock["coins"] = 0.0
                self._desert(civ, float(self.cfg.get("unpaid_desert_share", 0.1)), "were not paid")
            if civ.hunger_days > 0:
                self._desert(civ, float(self.cfg.get("hungry_desert_share", 0.1)), "were hungry")
        days = float(r.spies.get("report_days", 3))
        for e in self.enemies(civ):
            rep = civ.intel.get(e.id)
            if rep is None or rep["source"] != "spy" or self.w.clock.t - rep["t"] > days * 86400:
                civ.intel[e.id] = self.rumour(e, civ)

    def _desert(self, civ: "Civilization", share: float, why: str) -> None:
        n = max(1, int(len(civ.soldiers) * share))
        gone = sorted(civ.soldiers, key=lambda u: u.hp)[:n]
        for u in gone:
            civ.units.remove(u)
        self.w.refresh_food_rate(civ)
        self.w.log(civ, "desert", f"{n} soldiers left the army: they {why}.", 6)
