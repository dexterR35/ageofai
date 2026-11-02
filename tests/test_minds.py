"""Each kingdom's model chooses its own character; prompts give facts only; each model's calls and tokens are counted."""
import io
import json
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ai import self_mind
from ai.council import ADVISOR_BRIEF, KING_SYSTEM, Council
from ai.genome import Genome
from ai.llm import FakeLLM, OllamaClient, Usage
from engine.rules import default_rules
from engine.store import Store
from engine.world import World

TZ = ZoneInfo("Europe/Bucharest")
R = default_rules()
SPRING = datetime(2027, 4, 5, 9, 0, tzinfo=TZ)


def world(**kw):
    kw.setdefault("start", SPRING)
    return World(R, tz=TZ, **kw)


# ---------------------------------------------------------------------------- facts, no advice
def test_the_prompts_give_facts_not_advice():
    assert "personality" not in KING_SYSTEM and not hasattr(Council, "personality")
    text = " ".join(ADVISOR_BRIEF.values()) + KING_SYSTEM
    for advice in ("pays only", "you are bold", "you value", "impatient", "careful and keep", "you care about"):
        assert advice not in text.lower()
    assert "nobody tells you how to play" in KING_SYSTEM.lower()


# ---------------------------------------------------------------------------- what each model costs
def test_usage_is_counted_per_model_and_taken_once():
    u = Usage()
    u.add("qwen3:8b", calls=1, tokens_in=100, tokens_out=20, seconds=1.5)
    u.add("qwen3:8b", calls=1, tokens_in=50, tokens_out=10, seconds=0.5)
    u.add("gemma3:12b", failures=1)
    got = u.take()
    assert got["qwen3:8b"]["calls"] == 2 and got["qwen3:8b"]["tokens_in"] == 150 and got["gemma3:12b"]["failures"] == 1
    assert u.take() == {}


def test_ollama_tokens_come_from_its_answer(monkeypatch):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    answer = {"message": {"content": '{"ok": 1}'}, "prompt_eval_count": 321, "eval_count": 45}
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: Resp(json.dumps(answer).encode()))
    c = OllamaClient(model="gemma3:12b")
    assert c.chat_json("s", "u") == {"ok": 1}
    row = c.usage.take()["gemma3:12b"]
    assert row["calls"] == 1 and row["tokens_in"] == 321 and row["tokens_out"] == 45


def test_usage_is_saved_per_day_and_model(tmp_path):
    s = Store(tmp_path / "u.db")
    s.add_model_usage("2026-10-30", {"qwen3:8b": {"calls": 2, "tokens_in": 100, "tokens_out": 30, "seconds": 3.0}})
    s.add_model_usage("2026-10-30", {"qwen3:8b": {"calls": 1, "tokens_in": 50, "tokens_out": 10, "seconds": 1.0}})
    s.add_model_usage("2026-10-31", {"qwen3:8b": {"calls": 1, "tokens_in": 10, "tokens_out": 5, "seconds": 1.0}})
    row = s.model_usage("2026-10-31")[0]
    assert row["model"] == "qwen3:8b" and row["calls"] == 4 and row["tokens_in"] == 160 and row["tokens_today"] == 15


# ---------------------------------------------------------------------------- the model chooses its character
def test_the_models_choice_is_clamped():
    w = world(ai=True)
    civ = w.civs[0]
    civ.genome = Genome.default(R)
    ctx = self_mind.prepare(w, civ)
    assert set(ctx["genes"]) == set(R.gene_ranges) and all("changes" in g for g in ctx["genes"].values())
    lo, hi = R.gene_ranges["theta_attack"]
    raw = {"genes": {"theta_attack": 0.0, "honesty": 0.71, "made_up_gene": 5, "g_food": "lots"},
           "reason": "South attacked us twice."}
    g, changes, reason = self_mind.apply(raw, civ, R, "qwen3:8b", ctx)
    cur = civ.genome["theta_attack"]
    assert g["theta_attack"] == pytest.approx(max(lo, cur - 0.25 * (hi - lo)))   # one step of its range at most
    assert "made_up_gene" not in g.genes and g["g_food"] == civ.genome["g_food"]
    assert g.id == "llm-qwen3-8b-d1" and g.parents == (civ.genome.id,) and reason.startswith("South")
    assert any(c.startswith("theta_attack") for c in changes)
    assert self_mind.apply({"genes": {}}, civ, R, "m", ctx)[0] is None


def test_each_kingdom_asks_its_own_model_for_its_character(tmp_path):
    from ai.live_mind import LiveMind

    def responder(system, user):
        if "choose your own character" in system:
            assert "Nobody tells you" in system
            return {"genes": {"worker_soldier_ratio": 0.9}, "reason": "We need more soldiers."}
        return {}

    w = world(ai=True)
    north_llm, south_llm = FakeLLM(responder, model="qwen3:8b"), FakeLLM(lambda s, u: {}, model="gemma3:12b")
    mind = LiveMind(w, Store(tmp_path / "m.db"), FakeLLM(lambda s, u: {}), llms={"north": north_llm, "south": south_llm})
    try:
        before = w.civs[0].genome["worker_soldier_ratio"]
        for _ in range(4):
            mind.tick()
            for fut, _ in list(mind.jobs):
                fut.result(timeout=30)
        mind.tick()
        north, south = w.civs
        assert north.genome.id.startswith("llm-qwen3-8b") and north.genome["worker_soldier_ratio"] > before
        assert any(e.kind == "mind" and "We need more soldiers" in e.text for e in w.journal.events)
        assert any("choose your own character" in s for s, _ in north_llm.prompts)
        assert not any("choose your own character" in s for s, _ in mind.llm.prompts)   # not the shared model
        assert north.mind_next_t > w.clock.t + 6 * 86400                                  # next week
        assert south.genome.id == "default"                                                # it chose nothing
    finally:
        mind.close()


@pytest.mark.parametrize("who", ["llm", "mixed", "genes"])
def test_the_morning_handoff_respects_who_sets_the_character(tmp_path, monkeypatch, who):
    from server.runner import LiveRunner

    r = LiveRunner(db=tmp_path / f"{who}.db", llm=FakeLLM(up=False), pictures=False, speed=60,
                   start=datetime(2027, 4, 5, 7, 30, tzinfo=TZ), evolve=False)
    try:
        r.rules.llm["mind"] = who
        mine = [Genome(dict(c.genome.genes), id=f"chosen-{c.id}") for c in r.w.civs]
        for c, g in zip(r.w.civs, mine):
            c.genome = g
        night = [Genome(dict(c.genome.genes), id=f"n1g{i}", meta={"fitness": 0.5}) for i, c in enumerate(r.w.civs)]
        monkeypatch.setattr(r.store, "morning_genomes", lambda: night)
        monkeypatch.setattr(r.store, "last_night", lambda: 1)
        r._applied_night = 0
        r.schedule()
        ids = [c.genome.id for c in r.w.civs]
        if who == "llm":
            assert ids == ["chosen-north", "chosen-south"]
        else:
            assert ids == ["n1g0", "n1g1"]
        due = all(getattr(c, "mind_next_t", None) == r.w.clock.t for c in r.w.civs)
        assert due == (who == "mixed")
    finally:
        r.mind.close()
