"""Version 7: neutral bandits. A camp in a corner of the map raids every 10–20 days.

* Target: the richest, worst-defended kingdom (stock value ÷ (1 + army strength + towers)).
* Size: N = 3 + ⌊day / 15⌋, at most 30, with militia stats.
* Reaching the castle they take 20% of its stock and go home; beaten, they leave 50 gold.
* Friends (R ≥ 60) send their soldiers to help each other.

The camp stands on the line that is equally far from both castles, so neither kingdom is closer.
Bandits want loot: they fight soldiers who stand in their way, but they don't burn houses or kill workers.
"""
from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

from .units import Unit

if TYPE_CHECKING:
    from .civ import Civilization
    from .world import World


class BanditCamp:
    """Looks enough like a kingdom for the combat code (units, soldiers, kills, losses, rng)."""

    id = "bandits"
    name = "Bandits"
    color = "#2b2b2b"
    alive = True
    castle = None
    mirror = False
    people: tuple = ()                    # a camp has no villagers, buildings or spies (read-only, shared)
    buildings: tuple = ()
    spies: tuple = ()

    def __init__(self, world: "World"):
        self.w = world
        self.cfg = world.rules.bandits
        self.rng = random.Random(world.seed * 31337 + 7)
        self.units: list[Unit] = []
        self.kills = 0
        self.losses = 0
        self.x, self.y = self._place()
        lo, hi = self.cfg.get("every_days", [10, 20])
        self.next_raid = world.start_t + (float(self.cfg.get("first_raid_days", 14)) + self.rng.uniform(lo, hi)) * 86400
        self.raid: dict | None = None         # {"target", "size", "kills": {civ: n}, "helpers": [civ ids]}
        self.raids = 0
        self.loot: dict[str, float] = {}

    # ------------------------------------------------------------------ the kingdom-like bits
    @property
    def soldiers(self) -> list[Unit]:
        return self.units

    def tool_level(self, craft: str) -> int:
        return 0

    def store(self, res: str, amount: float) -> float:
        self.loot[res] = self.loot.get(res, 0.0) + amount
        return amount

    # ------------------------------------------------------------------ where the camp is
    def _place(self) -> tuple[float, float]:
        """Walk from the centre along the line equally far from both castles, to near the map's edge."""
        w = self.w
        m = w.map
        n = m.size
        if len(w.civs) < 2:
            return 6.0, float(n - 7)
        (ax, ay), (bx, by) = w.civs[0].castle.center, w.civs[1].castle.center
        mx, my = (ax + bx) / 2, (ay + by) / 2
        dx, dy = -(by - ay), bx - ax                          # perpendicular to the castles' line
        d = math.hypot(dx, dy) or 1.0
        dx, dy = dx / d, dy / d
        # improvements §7: among spots near the edge on that line (both ends), pick the one whose WALK to the two
        # castles is most nearly equal — the land around it isn't mirrored, so straight distance isn't enough
        cands: list[tuple[int, int]] = []
        for sign in (1, -1):
            t = 0.0
            while 6 <= mx + sign * dx * (t + 1) <= n - 7 and 6 <= my + sign * dy * (t + 1) <= n - 7:
                t += 1
            found = 0
            for back in range(0, int(t), 2):
                x, y = int(mx + sign * dx * (t - back)), int(my + sign * dy * (t - back))
                if m.walkable(x, y):
                    cands.append((x, y))
                    found += 1
                    if found >= 5:
                        break
        best, best_gap = None, math.inf
        for x, y in cands:
            lengths = []
            for civ in w.civs[:2]:
                goals = m.access_tiles(civ.castle.x, civ.castle.y, civ.castle.size, False)
                path = w.paths.find((x, y), ("camp", civ.id), goals)
                if not path:
                    break
                lengths.append(sum(math.dist(a, b) for a, b in zip(((x, y),) + tuple(path[:-1]), path)))
            if len(lengths) < 2:
                continue
            gap = abs(lengths[0] - lengths[1])
            if gap < best_gap:
                best, best_gap = (x, y), gap
                if gap <= 1.0:
                    break
        if best is not None:
            return float(best[0]), float(best[1])
        return float(int(mx)), float(int(my))

    # ------------------------------------------------------------------ raids
    def tick(self) -> None:
        w = self.w
        now = w.clock.t
        if self.raid is None:
            if now >= self.next_raid:
                self.start_raid()
            return
        target = w.civ_by_id(self.raid["target"])
        raiders = [u for u in self.units]
        if not raiders:
            self.end_raid(beaten=True)
            return
        if target is None or not target.alive or target.castle is None:
            for u in raiders:
                u.order, u.goal, u.target = "return", None, None
        for u in raiders:
            if u.order == "attack" and target is not None and target.castle is not None and \
                    w.military.dist(u, target.castle) <= 2.0:
                self.plunder(target)
                break
        home = [u for u in self.units if u.order in ("return", "defend") and math.hypot(u.x - self.x, u.y - self.y) < 2.5]
        for u in home:
            self.units.remove(u)                              # back in the camp
        if self.raid is not None and not self.units:
            self.end_raid(beaten=False)

    def pick_target(self) -> "Civilization | None":
        w = self.w
        base = w.rules.market.get("base_price", {})
        scored = []
        for civ in w.civs:
            if not civ.alive or civ.castle is None or civ.castle_level < int(self.cfg.get("min_castle", 2)):
                continue
            wealth = sum(v * base.get(k, 1.0) for k, v in civ.stock.items())
            guard = w.military.strength(civ.soldiers, civ) + 500 * sum(1 for b in civ.buildings if b.type == "tower" and b.built)
            scored.append((wealth / (1 + guard / 1000), civ))
        if not scored:
            return None
        top = max(s for s, _ in scored)
        tied = [c for s, c in scored if s >= top * 0.99]          # equally tempting: the dice decide (fairness)
        return tied[0] if len(tied) == 1 else self.rng.choice(tied)

    def start_raid(self) -> None:
        w = self.w
        lo, hi = self.cfg.get("every_days", [10, 20])
        self.next_raid = w.clock.t + self.rng.uniform(lo, hi) * 86400
        target = self.pick_target()
        if target is None:
            return
        n = min(int(self.cfg.get("max", 30)), int(self.cfg.get("base", 3)) + w.day // int(self.cfg.get("per_days", 15)))
        hp = w.rules.units["militia"].hp
        for i in range(n):
            u = Unit(w.next_id(), self.id, "militia", self.x + (i % 3) * 0.5, self.y + (i // 3) * 0.5, hp, w.clock.t)
            u.name = f"bandit #{u.id}"
            u.order, u.goal = "attack", target.castle
            self.units.append(u)
        self.raids += 1
        self.raid = {"target": target.id, "size": n, "kills": {}, "helpers": []}
        w.log(target, "bandits", f"{n} bandits leave their camp to raid {target.name}!", 8)
        friend_R = float(self.cfg.get("friends_help_R", 60))
        for other in w.civs:
            if other is target or not other.alive or w.diplomacy.rel(other.id, target.id) < friend_R:
                continue
            helpers = [u for u in other.soldiers if u.order == "defend"]
            for u in helpers:
                u.order, u.goal, u.target = "help", target.castle, None
            if helpers:
                self.raid["helpers"].append(other.id)
                w.log(other, "bandits", f"{other.name} sends {len(helpers)} soldiers to help its friend {target.name}.", 6)

    def plunder(self, civ: "Civilization") -> None:
        share = float(self.cfg.get("loot_share", 0.2))
        taken = {k: v * share for k, v in civ.stock.items() if v > 0}
        for k, v in taken.items():
            civ.stock[k] -= v
            self.store(k, v)
        if self.raid is not None:
            self.raid["taken"] = taken                         # carried home; lost to the village unless caught
        what = ", ".join(f"{v:.0f} {k}" for k, v in taken.items() if v >= 1)
        self.w.log(civ, "bandits", f"The bandits reached {civ.name}'s castle and took {what}.", 8)
        for u in self.units:
            u.order, u.goal, u.target = "return", None, None

    def killed_by(self, civ: "Civilization") -> None:
        if self.raid is not None:
            k = self.raid["kills"]
            k[civ.id] = k.get(civ.id, 0) + 1

    def end_raid(self, beaten: bool) -> None:
        w = self.w
        raid, self.raid = self.raid, None
        for cid in raid.get("helpers", []):
            c = w.civ_by_id(cid)
            if c is not None:
                for u in c.soldiers:
                    if u.order == "help":
                        u.order, u.goal, u.target = "return", None, None
        if not beaten:
            return
        taken = raid.get("taken")
        if taken:                                              # they robbed the castle, then fell on the way home
            victim = w.civ_by_id(raid["target"])
            if victim is not None and victim.alive:
                for k, v in taken.items():
                    victim.store(k, v)
                    self.loot[k] = max(0.0, self.loot.get(k, 0.0) - v)
                w.log(victim, "bandits", f"The bandits who robbed {victim.name} were caught on the way home; "
                                         f"the stolen goods are back.", 7)
            return                                             # no reward: they reached the castle
        kills = raid.get("kills", {})
        winner = w.civ_by_id(max(kills, key=kills.get)) if kills else w.civ_by_id(raid["target"])
        if winner is not None and winner.alive:
            gold = float(self.cfg.get("reward_gold", 50))
            winner.store("gold", gold)
            w.log(winner, "bandits", f"{winner.name} beat the bandits; they left {gold:.0f} gold behind.", 7)
