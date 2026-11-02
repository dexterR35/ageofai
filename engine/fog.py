"""Version 7: fog and exploration. Each kingdom knows only what its people have seen.

People see 8 tiles around them, watchtowers 12, scouts 14; a new kingdom knows 22 tiles around its castle.
Iron and gold deposits can't be used until the kingdom has seen them (the doc's rule; trees, stone,
bushes and animals stay usable, so a village never starves for lack of a scout).
"""
from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .civ import Civilization
    from .world import World


@lru_cache(maxsize=8)
def disc(radius: int) -> tuple[tuple[int, int], ...]:
    return tuple((dx, dy) for dy in range(-radius, radius + 1) for dx in range(-radius, radius + 1)
                 if dx * dx + dy * dy <= radius * radius)


class Fog:
    def __init__(self, world: "World"):
        self.w = world
        self.cfg = world.rules.fog
        self.on = world.rules.feature("fog")
        n = world.map.size
        self.seen: dict[str, bytearray] = {c.id: bytearray(n * n) for c in world.civs}
        self.version: dict[str, int] = {c.id: 0 for c in world.civs}
        self._acc = 0.0
        self._hidden = set(self.cfg.get("hidden", ["iron", "gold"]))
        self.found_nodes: dict[str, set[int]] = {}             # civ → hidden deposits it has seen
        for c in world.civs:
            self.found(c)

    def found(self, civ: "Civilization") -> None:
        """A kingdom is born (or reborn): it knows the land around its castle."""
        n = self.w.map.size
        self.seen.setdefault(civ.id, bytearray(n * n))
        self.version.setdefault(civ.id, 0)
        if civ.castle is not None:
            self.reveal(civ, *civ.castle.center, int(self.cfg.get("castle_sight", 22)))

    # ------------------------------------------------------------------ questions
    def knows(self, civ: "Civilization", x: int, y: int) -> bool:
        if not self.on:
            return True
        m = self.w.map
        return bool(self.seen[civ.id][m.idx(min(max(x, 0), m.size - 1), min(max(y, 0), m.size - 1))])

    def usable(self, civ: "Civilization", node) -> bool:
        """Can this kingdom use this resource? Hidden kinds (iron, gold) only once seen."""
        if not self.on or node.kind not in self._hidden:
            return True
        cx, cy = node.center
        return self.knows(civ, int(cx + 0.5), int(cy + 0.5))

    def share_known(self, civ: "Civilization") -> float:
        s = self.seen[civ.id]
        return sum(s) / len(s)

    # ------------------------------------------------------------------ sight
    def reveal(self, civ: "Civilization", x: float, y: float, radius: int) -> bool:
        m = self.w.map
        n = m.size
        seen = self.seen[civ.id]
        cx, cy = int(x + 0.5), int(y + 0.5)
        new = False
        for dx, dy in disc(radius):
            tx, ty = cx + dx, cy + dy
            if 0 <= tx < n and 0 <= ty < n:
                i = ty * n + tx
                if not seen[i]:
                    seen[i] = 1
                    new = True
        if new:
            self.version[civ.id] += 1
            for node in m.by_kind.get("iron", []) + m.by_kind.get("gold", []):
                if node.kind in self._hidden and (node.x - cx) ** 2 + (node.y - cy) ** 2 <= radius * radius:
                    self._announce(civ, node)
        return new

    def _announce(self, civ: "Civilization", node) -> None:
        found = self.found_nodes.setdefault(civ.id, set())
        if node.id in found:
            return
        found.add(node.id)
        if self.w.clock.t > civ.born_t + 60:                     # not the ones next to the castle at birth
            self.w.log(civ, "discovery", f"{civ.name} discovered a {node.kind} deposit.", 5)
            civ.dirty = True

    def tick(self, dt: float) -> None:
        """Every minute of game time: what everyone sees now."""
        if not self.on:
            return
        self._acc += dt
        if self._acc < float(self.cfg.get("reveal_seconds", 60)):
            return
        self._acc = 0.0
        ps, ts, ss = int(self.cfg.get("person_sight", 8)), int(self.cfg.get("tower_sight", 12)), \
            int(self.cfg.get("scout_sight", 14))
        for civ in self.w.civs:
            if not civ.alive:
                continue
            done: set[tuple[int, int]] = set()
            for p in civ.people:
                t = (int(p.x + 0.5) // 3, int(p.y + 0.5) // 3)        # people close together see the same
                if t in done:
                    continue
                done.add(t)
                self.reveal(civ, p.x, p.y, ps)
            for b in civ.buildings:
                if b.type == "tower" and b.built:
                    self.reveal(civ, *b.center, ts)
            for u in civ.units:
                self.reveal(civ, u.x, u.y, ss if u.is_scout else ps)
