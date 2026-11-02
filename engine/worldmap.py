"""Tile map, resource nodes and map generation.

1 tile = 10 m. The map is 128 × 128 tiles. Each civilization gets one half; the
south half is the north half rotated by 180°, so both starts are equally good.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from functools import lru_cache
from typing import Iterator

from .rules import Rules

GRASS, WATER = 0, 1


class Node:
    """A resource on the map: tree, bush, animal or a stone/iron/gold deposit."""

    __slots__ = ("id", "kind", "x", "y", "size", "amount", "max_amount", "regrow_at", "workers", "blocks",
                 "max_workers")

    def __init__(self, id: int, kind: str, x: int, y: int, size: int, amount: float, blocks: bool,
                 max_workers: int):
        self.id = id
        self.kind = kind
        self.x = x
        self.y = y
        self.size = size
        self.amount = amount
        self.max_amount = amount
        self.regrow_at: float | None = None   # epoch when a depleted node comes back
        self.workers = 0
        self.blocks = blocks
        self.max_workers = max_workers

    @property
    def center(self) -> tuple[float, float]:
        h = (self.size - 1) / 2
        return self.x + h, self.y + h

    @property
    def available(self) -> bool:
        return self.amount > 0 and self.workers < self.max_workers

    def __repr__(self) -> str:
        return f"Node({self.kind}#{self.id} @{self.x},{self.y} {self.amount:.0f})"


class WorldMap:
    def __init__(self, size: int):
        self.size = size
        n = size * size
        self.terrain = bytearray(n)
        self.blocked = bytearray(n)        # 1 = cannot walk here
        self.node_at = [0] * n             # node id + 1
        self.bld_at = [0] * n              # building id
        self.road = bytearray(n)           # version 7: 1 + index of the kingdom that built the road
        self.road_log: list[tuple[int, int, int]] = []   # (x, y, owner) in the order they were built
        self.nodes: list[Node] = []
        self.by_kind: dict[str, list[Node]] = defaultdict(list)
        self.version = 0                   # bumps whenever walkability changes (invalidates paths)

    # ------------------------------------------------------------------ basics
    def idx(self, x: int, y: int) -> int:
        return y * self.size + x

    def inside(self, x: int, y: int) -> bool:
        return 0 <= x < self.size and 0 <= y < self.size

    def walkable(self, x: int, y: int) -> bool:
        return 0 <= x < self.size and 0 <= y < self.size and not self.blocked[y * self.size + x]

    def nearest_walkable(self, x: int, y: int, max_r: int = 5) -> list[tuple[int, int]]:
        """The walkable tiles on the nearest square ring around (x, y) that has any (r = 1…max_r).
        Empty when there are none that close."""
        for r in range(1, max_r + 1):
            ring = [(x + dx, y + dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1)
                    if max(abs(dx), abs(dy)) == r and self.walkable(x + dx, y + dy)]
            if ring:
                return ring
        return []

    def rect(self, x: int, y: int, w: int, h: int | None = None) -> Iterator[tuple[int, int]]:
        h = w if h is None else h
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                yield xx, yy

    # ------------------------------------------------------------------ nodes
    def add_node(self, kind: str, x: int, y: int, size: int, amount: float, blocks: bool,
                 max_workers: int) -> Node:
        node = Node(len(self.nodes), kind, x, y, size, amount, blocks, max_workers)
        self.nodes.append(node)
        self.by_kind[kind].append(node)
        for xx, yy in self.rect(x, y, size):
            i = self.idx(xx, yy)
            self.node_at[i] = node.id + 1
            if blocks:
                self.blocked[i] = 1
        if blocks:
            self.version += 1
        return node

    def tile_free(self, x: int, y: int) -> bool:
        if not self.inside(x, y):
            return False
        i = self.idx(x, y)
        return self.terrain[i] == GRASS and not self.node_at[i] and not self.bld_at[i]

    # ------------------------------------------------------------------ buildings
    def can_place(self, x: int, y: int, size: int, margin: int = 1) -> bool:
        """Footprint must be free grass; a margin ring must have no water or buildings (keeps paths open)."""
        for xx, yy in self.rect(x, y, size):
            if not self.tile_free(xx, yy):
                return False
        for xx, yy in self.rect(x - margin, y - margin, size + 2 * margin):
            if not self.inside(xx, yy):
                return False
            i = self.idx(xx, yy)
            if self.terrain[i] != GRASS or self.bld_at[i]:
                return False
        return True

    def place_building(self, bid: int, x: int, y: int, size: int, walkable: bool) -> None:
        for xx, yy in self.rect(x, y, size):
            i = self.idx(xx, yy)
            self.bld_at[i] = bid
            if not walkable:
                self.blocked[i] = 1
        if not walkable:
            self.version += 1

    def remove_building(self, x: int, y: int, size: int) -> None:
        for xx, yy in self.rect(x, y, size):
            i = self.idx(xx, yy)
            self.bld_at[i] = 0
            self.blocked[i] = 1 if self.terrain[i] == WATER or (
                self.node_at[i] and self.nodes[self.node_at[i] - 1].blocks) else 0
        self.version += 1

    # ------------------------------------------------------------------ access
    def access_tiles(self, x: int, y: int, size: int, walkable_inside: bool) -> list[tuple[int, int]]:
        """Where a person stands to use something: on it if walkable, else on the ring around it."""
        if walkable_inside:
            return [(xx, yy) for xx, yy in self.rect(x, y, size) if self.walkable(xx, yy)]
        ring = []
        for xx, yy in self.rect(x - 1, y - 1, size + 2):
            if x <= xx < x + size and y <= yy < y + size:
                continue
            if self.walkable(xx, yy):
                ring.append((xx, yy))
        return ring


# ---------------------------------------------------------------------------- placement helpers

@lru_cache(maxsize=4)
def spiral_offsets(radius: int) -> list[tuple[int, int]]:
    """All (dx, dy) within `radius`, nearest first. Used to search for building sites."""
    pts = [(dx, dy) for dy in range(-radius, radius + 1) for dx in range(-radius, radius + 1)
           if dx * dx + dy * dy <= radius * radius]
    pts.sort(key=lambda p: (p[0] * p[0] + p[1] * p[1], p[1], p[0]))
    return pts


def find_site(m: WorldMap, anchor: tuple[float, float], size: int, radius: int = 25,
              min_dist: float = 0.0, avoid: tuple[float, float] | None = None,
              avoid_dist: float = 0.0, flip: bool = False) -> tuple[int, int] | None:
    """Nearest free top-left tile for a size×size building around `anchor`.

    `flip` = search in 180°-rotated order (the south civ), so both civs pick mirror-image spots.
    """
    h = (size - 1) / 2
    if flip:
        ax, ay = math.ceil(anchor[0] - h - 0.5), math.ceil(anchor[1] - h - 0.5)
    else:
        ax, ay = math.floor(anchor[0] - h + 0.5), math.floor(anchor[1] - h + 0.5)
    for dx, dy in spiral_offsets(radius):
        if min_dist and dx * dx + dy * dy < min_dist * min_dist:
            continue
        if flip:
            dx, dy = -dx, -dy
        x, y = ax + dx, ay + dy
        if avoid is not None:
            cx, cy = x + (size - 1) / 2, y + (size - 1) / 2
            if (cx - avoid[0]) ** 2 + (cy - avoid[1]) ** 2 < avoid_dist * avoid_dist:
                continue
        if m.can_place(x, y, size):
            return x, y
    return None


# ---------------------------------------------------------------------------- generation

def mirror(n: int, x: int, y: int, size: int = 1) -> tuple[int, int]:
    """180° rotation of a size×size footprint."""
    return n - x - size, n - y - size


def castle_positions(rules: Rules) -> list[tuple[int, int]]:
    n = rules.map["size"]
    size = rules.castle_spec.size
    north = tuple(rules.map["castle_offset"])
    if len(rules.civs) == 4:
        x, y = north
        return [(x, y), (n - y - size, x), (n - x - size, n - y - size), (y, n - x - size)]
    if len(rules.civs) != 2:
        raise ValueError("Map generation supports two or four civilizations")
    return [north, mirror(n, north[0], north[1], size)]


def generate_map(rules: Rules, seed: int) -> WorldMap:
    """Generate the north half, then copy it rotated onto the south half."""
    if len(rules.civs) == 4:
        return _four_regions(rules, seed)
    cfg = rules.map
    n = cfg["size"]
    rng = random.Random(seed)
    m = WorldMap(n)
    csize = rules.castle_spec.size
    cx0, cy0 = castle_positions(rules)[0]
    ccx, ccy = cx0 + (csize - 1) / 2, cy0 + (csize - 1) / 2
    half_limit = n // 2 - 3          # keep features clear of the mirror line

    def in_half(x: int, y: int, size: int = 1) -> bool:
        return 1 <= x and x + size <= n - 1 and 1 <= y and y + size <= half_limit

    def dist_castle(x: float, y: float) -> float:
        return math.hypot(x - ccx, y - ccy)

    def point_at(dmin: float, dmax: float, size: int = 1, tries: int = 200) -> tuple[int, int] | None:
        for _ in range(tries):
            a = rng.uniform(0, 2 * math.pi)
            r = rng.uniform(dmin, dmax)
            x, y = int(round(ccx + r * math.cos(a))), int(round(ccy + r * math.sin(a)))
            if in_half(x, y, size):
                return x, y
        return None

    def castle_zone(x: int, y: int, size: int, pad: float) -> bool:
        return dist_castle(x + (size - 1) / 2, y + (size - 1) / 2) < pad

    placements: list[tuple[str, int, int, int]] = []   # (kind, x, y, size)
    water: list[tuple[int, int]] = []

    # Lakes
    lk = cfg["lakes"]
    for _ in range(lk["count"]):
        p = point_at(lk["min_dist"], 50)
        if not p:
            continue
        r = rng.uniform(*lk["radius"])
        for y in range(int(p[1] - r - 1), int(p[1] + r + 2)):
            for x in range(int(p[0] - r - 1), int(p[0] + r + 2)):
                wobble = r + rng.uniform(-0.8, 0.8)
                if in_half(x, y) and math.hypot(x - p[0], y - p[1]) <= wobble \
                        and dist_castle(x, y) > lk["min_dist"] - 2:
                    i = m.idx(x, y)
                    if m.terrain[i] == GRASS:
                        m.terrain[i] = WATER
                        m.blocked[i] = 1
                        water.append((x, y))

    taken: set[tuple[int, int]] = set()

    def free(x: int, y: int, size: int) -> bool:
        return all(in_half(xx, yy) and m.terrain[m.idx(xx, yy)] == GRASS and (xx, yy) not in taken
                   for xx, yy in m.rect(x, y, size))

    def take(kind: str, x: int, y: int, size: int) -> None:
        for t in m.rect(x, y, size):
            taken.add(t)
        placements.append((kind, x, y, size))

    # Castle footprint + a ring are reserved
    for t in m.rect(cx0 - 1, cy0 - 1, csize + 2):
        taken.add(t)

    # Deposits first (they are the rarest and matter most)
    for kind, specs in cfg["deposits"].items():
        size = rules.nodes[kind].size
        for spec in specs:
            for _ in range(spec.get("count", 1)):
                for _ in range(200):
                    p = point_at(*spec["dist"], size=size)
                    if p and free(p[0] - 1, p[1] - 1, size + 2):
                        take(kind, p[0], p[1], size)
                        break

    # Forests
    clear = cfg["clear_radius"]
    for group in ("near", "far"):
        fs = cfg["forests"][group]
        for _ in range(fs["count"]):
            p = point_at(*fs["dist"])
            if not p:
                continue
            r = rng.uniform(*fs["radius"])
            for y in range(int(p[1] - r), int(p[1] + r) + 1):
                for x in range(int(p[0] - r), int(p[0] + r) + 1):
                    if math.hypot(x - p[0], y - p[1]) <= r and rng.random() < fs["density"] \
                            and not castle_zone(x, y, 1, clear) and free(x, y, 1):
                        take("tree", x, y, 1)

    # Bushes and animals, in small clusters
    def clusters(kind: str, count: int, dist: list, per: list) -> None:
        for _ in range(count):
            p = point_at(*dist)
            if not p:
                continue
            want = rng.randint(*per)
            for _ in range(want * 6):
                if want <= 0:
                    break
                x, y = p[0] + rng.randint(-2, 2), p[1] + rng.randint(-2, 2)
                if free(x, y, 1) and not castle_zone(x, y, 1, 5):
                    take(kind, x, y, 1)
                    want -= 1

    b = cfg["bushes"]
    clusters("bush", b["clusters"], b["dist"], b["per_cluster"])
    a = cfg["animals"]
    clusters("animal", a["herds"], a["dist"], a["per_herd"])

    # Apply to both halves
    for x, y in water:
        mx, my = mirror(n, x, y)
        i = m.idx(mx, my)
        m.terrain[i] = WATER
        m.blocked[i] = 1
    for kind, x, y, size in placements:
        spec = rules.nodes[kind]
        m.add_node(kind, x, y, size, spec.amount, spec.blocks, spec.max_workers)
    for kind, x, y, size in placements:
        spec = rules.nodes[kind]
        mx, my = mirror(n, x, y, size)
        m.add_node(kind, mx, my, size, spec.amount, spec.blocks, spec.max_workers)
    m.version += 1
    return m


def _four_regions(rules: Rules, seed: int) -> WorldMap:
    """Four rotated copies of one region give all civilizations equal deposits."""
    import copy

    n = int(rules.map["size"])
    if n % 2 or n < 128:
        raise ValueError("Four-civilization maps need an even size of at least 128")
    q = n // 2
    raw = copy.deepcopy(rules.raw)
    raw["map"]["size"] = q
    raw["civs"] = raw["civs"][:2]
    template = generate_map(Rules(raw), seed)
    m = WorldMap(n)

    def position(x, y, size, turns):
        for _ in range(turns):
            x, y = q - y - size, x
        ox, oy = ((0, 0), (q, 0), (q, q), (0, q))[turns]
        return x + ox, y + oy

    for turns in range(4):
        for y in range(q):
            for x in range(q):
                xx, yy = position(x, y, 1, turns)
                if template.terrain[template.idx(x, y)] == WATER:
                    m.terrain[m.idx(xx, yy)] = WATER
                    m.blocked[m.idx(xx, yy)] = 1
        for node in template.nodes:
            x, y = position(node.x, node.y, node.size, turns)
            m.add_node(node.kind, x, y, node.size, node.amount, node.blocks, node.max_workers)
    if rules.map.get("rivers", {}).get("enabled", False):
        # Connected border rivers separate the regions. Explicit shallow fords
        # keep every capital reachable; rivers never erase a deposit.
        crossings = (q // 2, q + q // 2)
        for axis in range(n):
            for edge in (q - 1, q):
                for x, y in ((edge, axis), (axis, edge)):
                    i = m.idx(x, y)
                    ford = any(abs(axis - c) <= 1 for c in crossings)
                    if not m.node_at[i] and not ford:
                        m.terrain[i] = WATER
                        m.blocked[i] = 1
    m.version += 1
    return m
