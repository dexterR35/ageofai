"""The game as an environment for reinforcement learning (no PyTorch here, so tests run on the game's Python).

One episode = one new game (`rl.episode_days` game days, a random map, start date and side). The network plays
one kingdom; the other is played by the genes: the default genome or a champion from the Hall (data/world.db).
Every step the network's choice becomes that kingdom's plan for `rl.step_hours`, then the game runs that long
in fast mode.

Reward: the change in the training's own score (`evolution.base_fitness`: castle, gathering, people, survival),
ours minus the enemy's, so a step that helps us or hurts them pays; at the end +1 for a win, −1 for a loss
(the night's rule: within 2% is a draw), and −1 more if our kingdom fell. Nothing else: no hint how to play.
"""
from __future__ import annotations

import copy
import json
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from ai import rl
from ai.evolution import base_fitness, match_score
from ai.genome import Genome
from engine.rules import DATA_DIR, Rules, default_rules
from engine.world import World


def champions(db: Path | None = None) -> list[dict]:
    """Genes of the Hall of Champions (read-only), so the network meets strong opponents too."""
    db = db or DATA_DIR / "world.db"
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        rows = con.execute("SELECT g.genes FROM hall h JOIN genomes g ON g.id = h.genome_id").fetchall()
        con.close()
        return [json.loads(r[0]) for r in rows]
    except sqlite3.Error:
        return []


class AgeEnv:
    def __init__(self, seed: int = 0, rules: Rules | None = None, days: int | None = None,
                 step_hours: float | None = None, opponents: list[dict] | None = None):
        base = rules or default_rules()
        raw = copy.deepcopy(base.raw)
        raw.setdefault("rl", {})["civs"] = []              # inside training nobody loads a policy file
        raw.setdefault("lookahead", {})["enabled"] = False
        self.rules = Rules(raw)
        cfg = self.rules.raw.get("rl", {})
        self.days = int(days or cfg.get("episode_days", 60))
        self.step_s = float(step_hours or cfg.get("step_hours", 6)) * 3600
        self.rng = random.Random(seed)
        self.opponents = champions() if opponents is None else opponents
        self.w: World | None = None
        self.me = self.enemy = None
        self.base: dict[str, float] = {}
        self.score0 = 0.0

    # ------------------------------------------------------------------ helpers
    def _side(self, civ) -> dict:
        return {"castle": civ.castle_level, "gathered": civ.total_gathered() - self.base[civ.id], "days": self.days,
                "pop": civ.pop + len(civ.soldiers), "alive": civ.alive and civ.pop > 0}

    def _score(self) -> float:
        return base_fitness(self._side(self.me), self.rules) - base_fitness(self._side(self.enemy), self.rules)

    # ------------------------------------------------------------------ the gym-like interface
    def reset(self) -> list[float]:
        r = self.rng
        start = datetime(2027, 1, 1, 7, 0) + timedelta(days=r.randrange(365))
        opp = Genome(dict(r.choice(self.opponents)), id="champion") if self.opponents and r.random() < 0.5 \
            else Genome.default(self.rules)
        me = Genome.default(self.rules)
        side = r.randrange(2)                                  # play North or South (fairness)
        genomes = [me, opp] if side == 0 else [opp, me]
        self.w = World(self.rules, seed=r.randrange(1 << 30), start=start, genomes=genomes,
                       event_seed=r.randrange(1 << 30))
        self.me, self.enemy = self.w.civs[side], self.w.civs[1 - side]
        self.base = {c.id: c.total_gathered() for c in self.w.civs}
        self.score0 = self._score()
        return rl.observe(self.w, self.me)

    def step(self, actions: list[int]) -> tuple[list[float], float, bool, dict]:
        w = self.w
        if self.me.ai is not None and self.me.alive:
            self.me.ai.set_directive(rl.to_plan(actions, self.me, w), w.clock.t + self.step_s)
        w.run(self.step_s)
        score = self._score()
        reward = (score - self.score0) * 10.0                  # the change in the game's own score
        self.score0 = score
        done = w.day > self.days or not self.me.alive or not self.enemy.alive
        info = {}
        if done:
            fa, fb = base_fitness(self._side(self.me), self.rules), base_fitness(self._side(self.enemy), self.rules)
            result = match_score(fa, fb, float(self.rules.evolution.get("draw_margin", 0.02)))
            reward += 2.0 * result - 1.0                       # +1 win, 0 draw, −1 loss
            if not self.me.alive:
                reward -= 1.0
            info = {"result": result, "castle": self.me.castle_level, "enemy_castle": self.enemy.castle_level,
                    "day": w.day, "alive": self.me.alive}
        return rl.observe(w, self.me), reward, done, info
