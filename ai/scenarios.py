"""Improvements §2: mid-game starts for the night's training.

Games from the very start never reach armies in time, so some rounds start from a saved world in
mid-game: both kingdoms played by the default genome until castle 4 (barracks, armies possible). The
night's genomes then take over both kingdoms. The scenarios are made once and kept in data/scenarios;
when rules.yaml changes, new ones are made (the file name carries a fingerprint of the rules).
Both kingdoms are identical at the start of a scenario (same genome, mirrored map), so it stays fair.
"""
from __future__ import annotations

import gzip
import hashlib
import pickle
import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from engine.rules import DATA_DIR, Rules, default_rules

SCEN_DIR = DATA_DIR / "scenarios"


@dataclass(frozen=True)
class ScenarioSpec:
    index: int
    map_seed: int
    event_seed: int
    start: float
    until_castle: int
    max_days: int
    path: str


def fingerprint(rules: Rules) -> str:
    raw = {k: v for k, v in rules.raw.items() if k not in ("llm", "council", "web", "commentator", "images",
                                                            "notify", "doctor", "timelapse", "persistence")}
    return hashlib.sha1(yaml.safe_dump(raw, sort_keys=True).encode("utf-8")).hexdigest()[:10]


def scenario_specs(rules: Rules, tz, folder: Path = SCEN_DIR) -> list[ScenarioSpec]:
    cfg = rules.evolution.get("midgame", {})
    rng = random.Random(int(cfg.get("seed", 4242)))
    key = fingerprint(rules)
    out = []
    for i in range(int(cfg.get("scenarios", 4))):
        month = (4, 7, 10, 1)[i % 4]
        start = datetime(2027, month, rng.randint(1, 25), 7, 0, tzinfo=tz).timestamp()
        m, e = rng.randrange(1 << 30), rng.randrange(1 << 30)
        out.append(ScenarioSpec(i, m, e, start, int(cfg.get("until_castle", 4)), int(cfg.get("max_days", 200)),
                                str(folder / f"mid_{key}_{i}.save")))
    return out


def make_scenario(spec: ScenarioSpec) -> str:
    """Play the default genome until both kingdoms reach the castle level (or max_days). Any process."""
    from engine.world import World

    w = World(default_rules(), seed=spec.map_seed, start=spec.start, event_seed=spec.event_seed)
    for _ in range(spec.max_days):
        w.run(86400)
        if all(c.castle_level >= spec.until_castle for c in w.civs if c.alive):
            break
    hooks = w.detach_hooks()
    path = Path(spec.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(pickle.dumps(w, protocol=pickle.HIGHEST_PROTOCOL), 3))
    tmp.replace(path)
    w.attach_hooks(hooks)
    return spec.path


def load_scenario(path: str):
    return pickle.loads(gzip.decompress(Path(path).read_bytes()))
