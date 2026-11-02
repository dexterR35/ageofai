"""Each kingdom's model chooses its own character (the genome's genes).

At birth (a new world, a rebirth, a new era) and then about once a week in the live world, the kingdom's own
model sees its situation, what happened to it, and every gene: its range, what it changes and its value now.
It answers which genes it wants to change and why. Nobody tells it which values are good: it learns from
what happened. Code keeps every value inside its range and moves each gene at most a share of its range
(rules.yaml → llm.mind_step), so a character changes over weeks, not in one jump.

rules.yaml → llm.mind decides who sets the live genome:
  genes  the night's training only (as before; the model never changes its genes)
  llm    the model only (the night's winners don't replace it at 07:00)
  mixed  the night's best at 07:00, then the model adjusts it (default)
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from .genome import Genome
from .llm import LLM, LLMError

if TYPE_CHECKING:
    from engine.civ import Civilization
    from engine.world import World

# What each gene changes in the formulas: facts, never advice.
GENE_MEANING = {
    "g_food": "weight of food when choosing what to build and where workers go",
    "g_wood": "weight of wood in the same choices",
    "g_stone": "weight of stone",
    "g_ore": "weight of iron ore",
    "g_gold": "weight of gold",
    "g_houses": "weight of houses (beds, so more people)",
    "g_castle": "weight of the castle's next level and its requirements",
    "g_tools": "weight of better tools at the forge (faster work)",
    "g_defense": "weight of soldiers, towers and weapon/armour upgrades",
    "theta_attack": "the army attacks when our strength divided by theirs is above this number",
    "food_reserve_days": "days of food kept in store before food workers are moved elsewhere",
    "worker_soldier_ratio": "soldiers wanted per worker",
    "winter_prep": "multiplier on the food and firewood kept for winter",
    "prudence": "share of effort kept for safety: reserves, wells, defending instead of trading away",
    "diplomacy": "chance to send a message to the neighbour each time messages are written",
    "honesty": "chance to keep a promise (trades, treaties); a broken promise lowers the other side's trust",
    "deception": "how much a caught enemy spy is turned and how much his false report exaggerates",
    "exploration": "how much the kingdom trains scouts to find iron and gold in the fog",
    "retreat_hp": "a soldier goes home to heal below this share of his health",
    "counter_attack": "chance to strike back after winning a defense",
}

SYSTEM = """You are the mind of {kingdom}, a medieval kingdom in a simulation, and you choose your own character.
Your character is a set of numbers ("genes") that the kingdom's formulas use for every everyday decision:
who works where, what to build, how many soldiers, when the army attacks, whether promises are kept.
Below: each gene's range, what it changes and its value now; your situation; what happened to you lately.
Nobody tells you which values are good. Decide from what happened.
You may change at most {max_genes} genes, each by at most {step_pct}% of its range this time.
Answer ONLY with JSON: {{"genes": {{"<gene>": <new number>, ...}}, "reason": "<one sentence in plain English>"}}"""


def mode(rules) -> str:
    m = str((getattr(rules, "llm", {}) or {}).get("mind", "mixed")).lower()
    return m if m in ("genes", "llm", "mixed") else "mixed"


def slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(model).lower()).strip("-") or "model"


def prepare(world: "World", civ: "Civilization", score: dict | None = None) -> dict:
    """Main thread: read the world once (the model is asked later, on the AI thread)."""
    from .council import Council

    rules = world.rules
    cfg = rules.llm or {}
    genes = {k: {"range": [lo, hi], "now": round(civ.genome.get(k, (lo + hi) / 2), 3),
                 "changes": GENE_MEANING.get(k, k.replace("_", " "))}
             for k, (lo, hi) in rules.gene_ranges.items()}
    since = world.clock.t - 7 * 86400
    happened = [f"day {e.day}: {e.text}" for e in world.journal.events
                if e.civ_id == civ.id and e.t >= since and e.importance >= 5][-25:]
    return {"kingdom": civ.name, "civ": civ.id, "day": world.day,
            "summary": Council(None).summary(world, civ), "genes": genes, "happened_lately": happened,
            "scoreboard": score or {}, "max_genes": int(cfg.get("mind_max_genes", 6)),
            "step": float(cfg.get("mind_step", 0.25))}


def ask(llm: LLM, ctx: dict) -> dict | None:
    """Any thread: only the AI call."""
    system = SYSTEM.format(kingdom=ctx["kingdom"], max_genes=ctx["max_genes"], step_pct=round(ctx["step"] * 100))
    user = json.dumps({k: ctx[k] for k in ("summary", "genes", "happened_lately", "scoreboard")}, ensure_ascii=False)
    try:
        return llm.chat_json(system, user)
    except LLMError:
        return None


def apply(raw: dict | None, civ: "Civilization", rules, model: str, ctx: dict) -> tuple[Genome | None, list[str], str]:
    """Check the answer: known genes only, inside their ranges, each moved at most `step` of its range,
    at most `max_genes` of them. Returns (the new genome or None, the changes as text, the reason)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("genes"), dict):
        return None, [], ""
    old = civ.genome
    new = dict(old.genes)
    changes = []
    for k, v in raw["genes"].items():
        key = str(k).strip()
        if key not in rules.gene_ranges or len(changes) >= ctx["max_genes"]:
            continue
        try:
            want = float(v)
        except (TypeError, ValueError):
            continue
        lo, hi = rules.gene_ranges[key]
        cur = old.get(key, (lo + hi) / 2)
        most = ctx["step"] * (hi - lo)
        val = min(hi, max(lo, min(cur + most, max(cur - most, want))))
        if abs(val - cur) < 1e-6:
            continue
        new[key] = val
        changes.append(f"{key} {cur:.2f}→{val:.2f}")
    if not changes:
        return None, [], str(raw.get("reason", "")).strip()[:300]
    g = Genome(new, id=f"llm-{slug(model)}-d{ctx['day']}", parents=(old.id,), generation=old.generation + 1,
               meta={k: v for k, v in old.meta.items() if k in ("night", "lineage")})
    return g, changes, str(raw.get("reason", "")).strip()[:300]
