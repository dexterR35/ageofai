"""A* on the tile grid (8 directions, no corner cutting) with a path cache.

Workers repeat the same trips all day (tree → depot → tree), so caching paths
by (start, goal, map version) removes almost all of the search cost.
"""
from __future__ import annotations

import heapq
import math
from typing import Iterable

from .worldmap import WorldMap

SQRT2 = math.sqrt(2)
_DIRS = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
         (1, 1, SQRT2), (1, -1, SQRT2), (-1, 1, SQRT2), (-1, -1, SQRT2))

Path = tuple[tuple[int, int], ...]


def step_along(path: Path, i: int, x: float, y: float, budget: float) -> tuple[float, float, int]:
    """Move from (x, y) along `path` (starting at tile i) by `budget` tiles.
    Returns the new position and the index of the next tile to reach (len(path) = arrived).
    Shared by workers, soldiers and trade carts."""
    n = len(path)
    while i < n and budget > 0:
        tx, ty = path[i]
        dx, dy = tx - x, ty - y
        d = math.hypot(dx, dy)
        if d <= budget:
            x, y, budget, i = float(tx), float(ty), budget - d, i + 1
        else:
            x += dx / d * budget
            y += dy / d * budget
            budget = 0.0
    return x, y, i


def _octile(dx: float, dy: float) -> float:
    dx, dy = abs(dx), abs(dy)
    return (dx + dy) + (SQRT2 - 2) * min(dx, dy)


def astar(m: WorldMap, start: tuple[int, int], goals: Iterable[tuple[int, int]],
          max_expand: int = 30000) -> Path | None:
    """Shortest path from `start` to any tile in `goals`. Returns tiles after start, or None."""
    goal_set = set(goals)
    if not goal_set:
        return None
    if start in goal_set:
        return ()
    n = m.size
    blocked = m.blocked
    gx0 = min(g[0] for g in goal_set)
    gx1 = max(g[0] for g in goal_set)
    gy0 = min(g[1] for g in goal_set)
    gy1 = max(g[1] for g in goal_set)

    def h(x: int, y: int) -> float:
        dx = gx0 - x if x < gx0 else (x - gx1 if x > gx1 else 0)
        dy = gy0 - y if y < gy0 else (y - gy1 if y > gy1 else 0)
        return _octile(dx, dy)

    sx, sy = start
    start_i = sy * n + sx
    g_cost = {start_i: 0.0}
    parent: dict[int, int] = {}
    heap = [(h(sx, sy), 0.0, start_i)]
    expanded = 0
    while heap:
        _, g, i = heapq.heappop(heap)
        if g > g_cost.get(i, math.inf):
            continue
        y, x = divmod(i, n)
        if (x, y) in goal_set:
            out = []
            while i != start_i:
                yy, xx = divmod(i, n)
                out.append((xx, yy))
                i = parent[i]
            out.reverse()
            return tuple(out)
        expanded += 1
        if expanded > max_expand:
            return None
        for dx, dy, cost in _DIRS:
            nx, ny = x + dx, y + dy
            if not (0 <= nx < n and 0 <= ny < n):
                continue
            j = ny * n + nx
            if blocked[j]:
                continue
            if dx and dy and (blocked[y * n + nx] or blocked[ny * n + x]):
                continue   # no squeezing between two blocked corners
            ng = g + cost
            if ng < g_cost.get(j, math.inf):
                g_cost[j] = ng
                parent[j] = i
                heapq.heappush(heap, (ng + h(nx, ny), ng, j))
    return None


class PathFinder:
    """A* with a cache keyed by (start, goal key). The cache is dropped when the map changes."""

    def __init__(self, m: WorldMap, max_entries: int = 50000):
        self.m = m
        self.max_entries = max_entries
        self._cache: dict[tuple, Path | None] = {}
        self._version = m.version
        self.searches = 0
        self.hits = 0

    def find(self, start: tuple[int, int], goal_key: object,
             goals: Iterable[tuple[int, int]]) -> Path | None:
        if self.m.version != self._version:
            self._cache.clear()
            self._version = self.m.version
        key = (start, goal_key)
        if key in self._cache:
            self.hits += 1
            return self._cache[key]
        self.searches += 1
        path = astar(self.m, start, goals)
        if len(self._cache) >= self.max_entries:
            self._cache.clear()
        self._cache[key] = path
        return path
