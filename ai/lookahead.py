"""Look-ahead by simulation (Monte Carlo): before the army marches, the kingdom plays out the futures.

When the genes say "attack" (our strength / theirs is above theta_attack), the kingdom copies the world and
plays it forward in fast mode (`lookahead.hours`, default 72 game hours) once with the attack ordered and
once without, `lookahead.samples` times each with different luck. Each future is scored with the training's
own objective (`evolution.base_fitness`: castle, gathering, people, survival), ours minus the enemy's.
The army marches only if attacking scored better; otherwise the kingdom waits `lookahead.hold_hours`.
No rule written by us decides it: the game's own math does.

The futures run in a separate process (the world never waits for them; one at a time). Only the live world
looks ahead (rules.yaml → lookahead.enabled); the night's training games, which already play thousands of
games, don't. A copy never looks ahead again inside its own future.
"""
from __future__ import annotations

import pickle
import random
from concurrent.futures import Future, ProcessPoolExecutor
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.civ import Civilization
    from engine.world import World

OPTIONS = ("attack", "wait")

_pool: ProcessPoolExecutor | None = None
_pending: dict[tuple[int, str], Future] = {}         # (world, civ) → the futures being played
_hold: dict[tuple[int, str], float] = {}             # (world, civ) → don't ask again before this game time
last: dict[tuple[int, str], dict] = {}                # (world, civ) → the last verdict (for the king's facts)


def cfg(world: "World") -> dict:
    return world.rules.raw.get("lookahead", {}) or {}


def enabled(world: "World") -> bool:
    c = cfg(world)
    return bool(c.get("enabled", True)) and bool(getattr(world, "live", False)) \
        and not getattr(world, "simulating", False)


def snapshot(world: "World") -> bytes:
    """The world as bytes, without the live hooks (the local AI, the page)."""
    hooks = world.detach_hooks()
    try:
        return pickle.dumps(world, protocol=pickle.HIGHEST_PROTOCOL)
    finally:
        world.attach_hooks(hooks)


# ---------------------------------------------------------------------------- one future (any process)
def side_score(world: "World", civ, base_gathered: float, days: float) -> float:
    from ai.evolution import base_fitness
    side = {"castle": civ.castle_level, "gathered": civ.total_gathered() - base_gathered, "days": max(days, 1.0),
            "pop": civ.pop + len(civ.soldiers), "alive": civ.alive and civ.pop > 0}
    return base_fitness(side, world.rules)


def play_future(blob: bytes, civ_id: str, enemy_id: str, option: str, hours: float, seed: int) -> float:
    """Play one future: the world from `blob`, with `option` taken now, for `hours` game hours.
    Returns our score minus the enemy's (the training's objective)."""
    w = pickle.loads(blob)
    w.simulating = True                                  # no look-ahead inside a future
    w.live = False                                       # no rebirth, no local AI
    civ, enemy = w.civ_by_id(civ_id), w.civ_by_id(enemy_id)
    for i, c in enumerate(w.civs):                       # a different luck in every sample
        c.rng = random.Random(seed * 7919 + i)
    w.rng = random.Random(seed)
    base = {c.id: c.total_gathered() for c in w.civs}
    if option == "attack" and enemy is not None and enemy.alive:
        w.military.order_attack(civ, enemy)
    w.run(hours * 3600)
    days = hours / 24
    return side_score(w, civ, base[civ.id], days) - (side_score(w, enemy, base[enemy.id], days) if enemy else 0.0)


def play_all(blob: bytes, civ_id: str, enemy_id: str, hours: float, samples: int, seed: int) -> dict[str, float]:
    """Every option, `samples` futures each (same luck for both options, so they are compared fairly)."""
    out = {}
    for opt in OPTIONS:
        scores = [play_future(blob, civ_id, enemy_id, opt, hours, seed + k) for k in range(samples)]
        out[opt] = sum(scores) / len(scores)
    return out


# ---------------------------------------------------------------------------- the live world's side
def _pool_get() -> ProcessPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(max_workers=1)
    return _pool


def shutdown() -> None:
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None
    _pending.clear()


def gate(world: "World", civ: "Civilization", enemy: "Civilization", sync: bool = False) -> str | None:
    """Should the army march now? None = look-ahead is off (march as before); "attack" = the futures say
    yes; "wait" = they say no (or we decided recently); "pending" = the futures are still being played.
    `sync` plays them at once (tests)."""
    if not enabled(world):
        return None
    c = cfg(world)
    key = (id(world), civ.id)
    now = world.clock.t
    if now < _hold.get(key, 0.0):
        return "wait"
    fut = _pending.get(key)
    if fut is None:
        args = (snapshot(world), civ.id, enemy.id, float(c.get("hours", 72)), int(c.get("samples", 2)),
                int(now) % 100000)
        if sync:
            return _decide(world, civ, enemy, play_all(*args))
        _pending[key] = _pool_get().submit(play_all, *args)
        world.log(civ, "lookahead", f"{civ.name} plays out the futures before marching on {enemy.name}.", 2)
        return "pending"
    if not fut.done():
        return "pending"
    _pending.pop(key, None)
    try:
        scores = fut.result()
    except Exception as e:                               # a broken future must not stop the war logic
        world.log(civ, "lookahead", f"{civ.name} could not play out the futures ({e}); it decides as before.", 1)
        return None
    return _decide(world, civ, enemy, scores)


def _decide(world: "World", civ, enemy, scores: dict[str, float]) -> str:
    c = cfg(world)
    key = (id(world), civ.id)
    best = max(scores, key=scores.get)
    hours = float(c.get("hours", 72))
    text = (f"{civ.name} played out {int(c.get('samples', 2))} futures of {hours:.0f} h: attacking {enemy.name} now "
            f"scores {scores['attack']:+.3f}, waiting {scores['wait']:+.3f}. "
            + ("It marches." if best == "attack" else "It waits."))
    world.log(civ, "lookahead", text, 5)
    last[key] = {"t": world.clock.t, "attack": round(scores["attack"], 3), "wait": round(scores["wait"], 3),
                 "choice": best, "against": enemy.name}
    if best == "wait":
        _hold[key] = world.clock.t + float(c.get("hold_hours", 12)) * 3600
    return best


def last_for(world: "World", civ) -> dict | None:
    return last.get((id(world), civ.id))
