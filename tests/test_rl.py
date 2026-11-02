"""Reinforcement learning (PPO): what the network sees and decides, running it in the game, the environment.
These run on the game's Python (no PyTorch); the trainer itself runs in .venv-rl."""
import copy
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ai import rl
from ai.llm import FakeLLM
from engine.rules import Rules, default_rules
from engine.store import Store
from engine.world import World
from rl.env import AgeEnv

TZ = ZoneInfo("Europe/Bucharest")
SPRING = datetime(2027, 4, 5, 9, 0, tzinfo=TZ)


def handmade_policy(path, choice=(2,) * 9 + (1,), hidden=4) -> None:
    """A network whose answer is fixed by its last biases (the rest is zero)."""
    bias = []
    for n, c in zip(rl.HEADS, choice):
        bias += [5.0 if k == c else 0.0 for k in range(n)]
    data = {"name": "handmade", "updates": 7, "obs_size": rl.OBS_SIZE, "heads": list(rl.HEADS),
            "obs_mean": [0.0] * rl.OBS_SIZE, "obs_std": [1.0] * rl.OBS_SIZE,
            "layers": [{"W": [[0.0] * rl.OBS_SIZE for _ in range(hidden)], "b": [0.0] * hidden}],
            "head": {"W": [[0.0] * hidden for _ in range(sum(rl.HEADS))], "b": bias}}
    path.write_text(json.dumps(data), encoding="utf-8")


def rules_with(**rl_cfg) -> Rules:
    raw = copy.deepcopy(default_rules().raw)
    raw.setdefault("rl", {}).update(rl_cfg)
    return Rules(raw)


def test_what_the_network_sees():
    w = World(default_rules(), tz=TZ, start=SPRING)
    o = rl.observe(w, w.civs[0])
    assert len(o) == rl.OBS_SIZE and all(math.isfinite(v) and -1.5 <= v <= 1.5 for v in o)


def test_what_the_network_decides_becomes_a_plan():
    w = World(default_rules(), tz=TZ, start=SPRING)
    north = w.civs[0]
    plan = rl.to_plan([0, 1, 2, 1, 1, 1, 2, 1, 1, 2], north, w)
    assert plan.priorities["food"] == 0.5 and plan.priorities["stone"] == 2.0 and plan.priorities["castle"] == 2.0
    assert plan.intent == "attack" and plan.war and plan.target == "south"
    defend = rl.to_plan([1] * 9 + [1], north, w)
    assert defend.intent == "defend" and defend.priorities["defense"] >= 1.5


def test_the_game_runs_a_trained_network_without_pytorch(tmp_path):
    p = tmp_path / "policy.json"
    handmade_policy(p)
    pol = rl.Policy.load(p)
    assert pol.act([0.3] * rl.OBS_SIZE) == [2] * 9 + [1]
    bad = json.loads(p.read_text())
    bad["obs_size"] = 12
    with pytest.raises(ValueError):
        rl.Policy(bad)


def test_a_kingdom_listed_in_the_rules_is_steered_by_the_network(tmp_path):
    p = tmp_path / "policy.json"
    handmade_policy(p, choice=(2,) * 9 + (0,))
    w = World(rules_with(policy=str(p), civs=["south"]), tz=TZ, start=SPRING)
    north, south = w.civs
    w.run(600)
    assert south.ai.directive is not None and all(v == 2.0 for v in south.ai.directive.priorities.values())
    assert any("The network (handmade" in t for _, t in south.ai.thoughts)
    assert north.ai.directive is None or north.ai.directive.reason != "the network's choice"


def test_no_llm_council_for_a_network_kingdom(tmp_path):
    from ai.live_mind import LiveMind

    p = tmp_path / "policy.json"
    handmade_policy(p)
    w = World(rules_with(policy=str(p), civs=["south"]), tz=TZ, start=SPRING)
    north_llm, south_llm = FakeLLM(lambda s, u: {}, model="n"), FakeLLM(lambda s, u: {}, model="s")
    mind = LiveMind(w, Store(tmp_path / "m.db"), FakeLLM(lambda s, u: {}), llms={"north": north_llm, "south": south_llm})
    try:
        assert mind.by_network(w.civs[1]) and not mind.by_network(w.civs[0])
        mind.tick()
        for fut, _ in list(mind.jobs):
            fut.result(timeout=30)
        assert "council:south" not in mind.busy and "mind:south" not in mind.busy
        assert not south_llm.prompts                         # the network kingdom never asked its LLM
    finally:
        mind.close()


def test_the_training_environment_plays_a_short_game():
    env = AgeEnv(seed=3, days=2, opponents=[])
    obs = env.reset()
    assert len(obs) == rl.OBS_SIZE
    total, steps, done, info = 0.0, 0, False, {}
    while not done:
        obs, r, done, info = env.step([1] * 9 + [0])
        total += r
        steps += 1
        assert math.isfinite(r)
    assert steps >= 6 and info["result"] in (0.0, 0.5, 1.0) and "castle" in info
    assert env.rules.raw["rl"]["civs"] == [] and not env.rules.raw["lookahead"]["enabled"]
