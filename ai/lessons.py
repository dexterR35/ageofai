"""Morning lessons (06:00): what the night's games taught, in up to 10 short sentences.

Code computes the facts (which genes the winners had, which seasons were hard, what killed
villages); the local AI turns them into lessons. The lessons go into the council's summary.
"""
from __future__ import annotations

import json
from datetime import date

from engine.rules import Rules

from .llm import LLM, LLMError

GENE_WORDS = {
    "g_food": "food", "g_wood": "wood", "g_stone": "stone", "g_ore": "ore", "g_gold": "gold",
    "g_houses": "building houses early", "g_castle": "growing the castle", "g_tools": "tools",
    "g_defense": "defense", "food_reserve_days": "food reserves", "winter_prep": "winter preparation",
    "prudence": "prudence (wells, reserves)", "theta_attack": "attack threshold",
    "worker_soldier_ratio": "soldier share", "diplomacy": "diplomacy", "honesty": "honesty",
    "deception": "deception",
}
SEASON_OF_MONTH = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
                   6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}

SYSTEM = """You turn statistics from a night of simulated games into short lessons for a medieval king.
Use ONLY the facts given. Each lesson is one plain English sentence, at most 20 words, practical.
"winners_vs_losers" compares GENE VALUES (how much weight a strategy puts on something), not survival rates.
Never compute percentages or new numbers: if you quote a number, copy it exactly from the facts.
Do not repeat a lesson in other words, and never contradict the facts.
Answer ONLY with JSON: {"lessons": ["...", "..."]} with at most 8 lessons."""


def numbers(text: str) -> set[str]:
    import re
    return {n.rstrip(".0") or "0" for n in re.findall(r"\d+(?:\.\d+)?", text)}


def grounded(lesson: str, facts_text: str) -> bool:
    """Every number in a lesson must appear in the facts (the AI may not invent statistics)."""
    return numbers(lesson) <= numbers(facts_text)


def night_facts(store, rules: Rules, night: int) -> dict:
    """Statistics of one night, from the database."""
    pop = [g for g in store.population(night) if g.meta.get("fitness") is not None]
    facts: dict = {"night": night, "genomes": len(pop)}
    if len(pop) >= 4:
        pop.sort(key=lambda g: g.meta["fitness"], reverse=True)
        q = max(1, len(pop) // 4)
        top, bottom = pop[:q], pop[-q:]
        diffs = []
        for gene, (lo, hi) in rules.gene_ranges.items():
            t = sum(g.genes[gene] for g in top) / len(top)
            b = sum(g.genes[gene] for g in bottom) / len(bottom)
            diffs.append(((t - b) / (hi - lo), gene, t, b))
        diffs.sort(key=lambda d: abs(d[0]), reverse=True)
        facts["winners_vs_losers"] = [
            {"gene": GENE_WORDS.get(gene, gene), "winners": round(t, 2), "losers": round(b, 2),
             "direction": "more" if d > 0 else "less"} for d, gene, t, b in diffs[:4] if abs(d) > 0.08]
    rows = store.db.execute("SELECT kind, start, fa, fb, la, lb, pa, pb FROM matches WHERE night=?", (night,)).fetchall()
    if rows:
        by_season: dict[str, list[float]] = {}
        dead = 0
        for kind, start, fa, fb, la, lb, pa, pb in rows:
            season = SEASON_OF_MONTH[date.fromisoformat(start).month]
            by_season.setdefault(season, []).extend([fa, fb])
            dead += (pa == 0) + (pb == 0)
        facts["average_score_by_start_season"] = {s: round(sum(v) / len(v), 3) for s, v in by_season.items()}
        facts["villages_that_died"] = dead
        facts["games"] = len(rows)
        facts["average_castle_level"] = round(sum(r[4] + r[5] for r in rows) / (2 * len(rows)), 2)
    return facts


def plain_lessons(facts: dict) -> list[str]:
    out = []
    for w in facts.get("winners_vs_losers", []):
        out.append(f"Winners put {w['direction']} weight on {w['gene']} ({w['winners']} vs {w['losers']}).")
    seasons = facts.get("average_score_by_start_season", {})
    if len(seasons) >= 2:
        worst = min(seasons, key=seasons.get)
        best = max(seasons, key=seasons.get)
        out.append(f"Games that started in {worst} scored lowest ({seasons[worst]}); {best} starts scored best.")
    if facts.get("villages_that_died"):
        out.append(f"{facts['villages_that_died']} villages died out last night: never let food run out.")
    return out[:10]


def write_lessons(store, rules: Rules, night: int, llm: LLM | None) -> tuple[list[str], bool]:
    """Returns (lessons, written_by_ai)."""
    facts = night_facts(store, rules, night)
    fallback = plain_lessons(facts)
    if llm is None or not llm.available() or not fallback:
        return fallback, False
    facts_text = json.dumps(facts, ensure_ascii=False)
    try:
        raw = llm.chat_json(SYSTEM, facts_text, temperature=0.2)
    except LLMError:
        return fallback, False
    lessons = [str(x).strip()[:200] for x in raw.get("lessons", []) if str(x).strip()]
    lessons = [x for x in lessons if grounded(x, facts_text)][:10]
    return (lessons, True) if lessons else (fallback, False)
