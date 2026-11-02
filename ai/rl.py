"""Reinforcement learning (PPO): a small neural network steers a kingdom, learned only from reward.

This file has no PyTorch, so both sides can use it:
  * the trainer (rl/train_ppo.py, Python 3.12 + PyTorch) uses `observe`, `to_plan` and the action layout;
  * the game (Python 3.15, no PyTorch) runs a trained network with `Policy` in plain Python.

What the network sees (`observe`): about 50 numbers, all scaled to roughly 0…1: the time of year, the kingdom
(castle, people, soldiers, stocks against storage, buildings, tools, morale), what it knows of the neighbour
(castle, people, soldiers as reported, the strength ratio) and diplomacy (relation, trust, war, treaty, peace).
What it decides (`to_plan`), every `rl.step_hours` game hours: a weight for each of 9 areas (0.5, 1 or 2 ×)
and a war intent (none, defend, attack, seek peace). That is the same plan the LLM king gives; the tactical
formulas then do the daily work with it, and the game's rules (14 days of peace, a minimum army…) still hold.
Nobody tells it what is good: it learns from the reward, the training's own score (castle, gathering, people,
survival; ours minus the enemy's) and winning or losing.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.civ import Civilization
    from engine.world import World

KEYS = ("food", "wood", "stone", "ore", "gold", "houses", "castle", "tools", "defense")
LEVELS = (0.5, 1.0, 2.0)
INTENTS = ("none", "defend", "attack", "seek_peace")
HEADS = (len(LEVELS),) * len(KEYS) + (len(INTENTS),)
SEASONS = ("spring", "summer", "autumn", "winter")
BUILDINGS = ("house", "farm", "lumber_camp", "mill", "quarry", "granary", "forge", "iron_mine", "barracks",
             "tower", "market", "gold_mine", "wonder")
RESOURCES = ("food", "wood", "stone", "ore", "gold", "coins")


def _sq(x: float, scale: float) -> float:
    """A count squashed into 0…1 (scale = the value that gives 0.5)."""
    return x / (x + scale) if x > 0 else 0.0


def observe(world: "World", civ: "Civilization") -> list[float]:
    """What the network sees. The order never changes (a trained network depends on it)."""
    now = world.clock.t
    others = [c for c in world.civs if c is not civ]
    enemy = others[0] if others else None
    day = world.clock.date.timetuple().tm_yday
    o = [math.sin(2 * math.pi * day / 365), math.cos(2 * math.pi * day / 365)]
    o += [1.0 if world.clock.season.name == s else 0.0 for s in SEASONS]
    o += [min(1.0, world.day / 120)]
    o += [civ.castle_level / 10, _sq(civ.pop, 20), _sq(len(civ.workers(now)), 15), _sq(len(civ.soldiers), 15),
          min(1.5, civ.morale) / 1.5, min(1.0, civ.hunger_days / 5), 1.0 if civ.cold_homes else 0.0,
          _sq(len(civ.sites()), 3), min(1.0, civ.pop / max(1, civ.beds()))]
    o += [min(1.5, civ.stock.get(r, 0.0) / max(1.0, civ.cap(r))) for r in RESOURCES]
    o += [_sq(len(civ.of_type(b)), 2) for b in BUILDINGS]
    o += [sum(civ.tool_level(c) for c in civ.rules.crafts) / (4 * max(1, len(civ.rules.crafts))),
          civ.tool_level("weapons") / 5, civ.tool_level("armor") / 5]
    if enemy is not None and enemy.alive and civ.ai is not None:
        rep = civ.intel.get(enemy.id, {})
        ratio = civ.ai.army.ratio(enemy)
        dip = world.diplomacy
        o += [1.0, enemy.castle_level / 10, _sq(enemy.pop, 20), _sq(rep.get("soldiers") or 0, 15),
              _sq(min(ratio, 50.0), 1.0),
              (dip.rel(civ.id, enemy.id) + 100) / 200, dip.trust(civ.id, enemy.id), dip.trust(enemy.id, civ.id),
              1.0 if dip.at_war(civ.id, enemy.id) else 0.0, 1.0 if dip.treaty(civ.id, enemy.id) else 0.0,
              1.0 if dip.peace_period() else 0.0]
    else:
        o += [0.0] * 11
    return [float(v) if math.isfinite(v) else 0.0 for v in o]


OBS_SIZE = 7 + 9 + len(RESOURCES) + len(BUILDINGS) + 3 + 11


def to_plan(actions: list[int], civ: "Civilization", world: "World"):
    """The network's choice as the plan the tactical layer follows (the same kind the LLM king gives)."""
    from .council import Plan
    pri = {k: LEVELS[int(a)] for k, a in zip(KEYS, actions[:len(KEYS)])}
    intent = INTENTS[int(actions[len(KEYS)])]
    others = [c for c in world.civs if c is not civ and c.alive]
    target = others[0].id if others and intent in ("attack", "seek_peace") else None
    if intent in ("attack", "seek_peace") and target is None:
        intent = "none"
    if intent == "defend":
        pri["defense"] = max(pri.get("defense", 1.0), 1.5)
    return Plan(thought="", priorities=pri, build_next=None, war=intent == "attack", reason="the network's choice",
                listened_to=None, intent=intent, target=target)


class Policy:
    """A trained network in plain Python (no PyTorch): input scaling, tanh layers, one softmax per head.
    The game takes the most likely choice of each head (no dice)."""

    def __init__(self, data: dict):
        if data.get("obs_size") != OBS_SIZE or tuple(data.get("heads", ())) != HEADS:
            raise ValueError(f"policy was trained for {data.get('obs_size')} inputs / heads {data.get('heads')}, "
                             f"this game has {OBS_SIZE} / {list(HEADS)}")
        self.mean = data.get("obs_mean") or [0.0] * OBS_SIZE
        self.std = data.get("obs_std") or [1.0] * OBS_SIZE
        self.layers = [(layer["W"], layer["b"]) for layer in data["layers"]]
        self.head = (data["head"]["W"], data["head"]["b"])
        self.name = str(data.get("name", "ppo"))
        self.updates = int(data.get("updates", 0))

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @staticmethod
    def _dense(W: list[list[float]], b: list[float], x: list[float]) -> list[float]:
        return [sum(w * v for w, v in zip(row, x)) + bi for row, bi in zip(W, b)]

    def logits(self, obs: list[float]) -> list[float]:
        x = [max(-5.0, min(5.0, (v - m) / (s if s > 1e-8 else 1.0))) for v, m, s in zip(obs, self.mean, self.std)]
        for W, b in self.layers:
            x = [math.tanh(v) for v in self._dense(W, b, x)]
        return self._dense(*self.head, x)

    def act(self, obs: list[float]) -> list[int]:
        out, i = [], 0
        z = self.logits(obs)
        for n in HEADS:
            part = z[i:i + n]
            out.append(max(range(n), key=lambda k: part[k]))
            i += n
        return out


_cache: dict[str, tuple[float, Policy | None]] = {}


def policy_for(rules, civ_id: str) -> Policy | None:
    """The trained network this kingdom plays with (rules.yaml → rl.civs and rl.policy), or None."""
    cfg = rules.raw.get("rl", {}) or {}
    if civ_id not in (cfg.get("civs") or []):
        return None
    path = Path(cfg.get("policy", "data/rl/policy.json"))
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    if not path.exists():
        return None
    stamp = path.stat().st_mtime
    hit = _cache.get(str(path))
    if hit is None or hit[0] != stamp:                       # a newer training export is picked up by itself
        try:
            hit = (stamp, Policy.load(path))
        except (ValueError, KeyError, json.JSONDecodeError):
            hit = (stamp, None)
        _cache[str(path)] = hit
    return hit[1]
