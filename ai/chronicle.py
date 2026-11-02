"""The daily "Chronicle of the Kingdoms" (written 06:15, published 07:00).

Code picks the 30 most important events of the last 24 hours with
    I = weight(type) · (1 + log10(1 + size))
and builds the numbers table itself (so no number is invented). The local AI writes the
headline, three short stories and the portrait of the day (150–250 words).
Without Ollama a plain chronicle is still written from the events.
"""
from __future__ import annotations

import json
import math
import random
import re
from dataclasses import dataclass, field

from .llm import LLM, LLMError

# journal kind → weight from the design doc
KIND_WEIGHT = {
    "castle": 10, "attack": 9, "battle": 9,
    "death": 6, "wolves": 6, "mine": 5, "flight": 6, "leave": 6, "fall": 10,
    "fire": 5, "disease": 5, "storm": 5, "strike": 5, "destroyed": 5, "merchant": 5,
    "message": 5, "trade": 2, "harvest": 2,
    "birth": 4, "family": 3, "build_done": 3, "research_done": 3, "genome": 3,
}

SYSTEM = """You write "The Chronicle of the Kingdoms", a short medieval newspaper about two simulated kingdoms.
Write in plain, vivid English. Use ONLY the facts given; never invent numbers, names or events.
The three stories cover the most important EVENTS (both kingdoms if possible), not the person of the day.
The portrait is the only part about the person of the day. If there are few events, keep stories short.
Answer ONLY with JSON:
{"headline": "<one line>", "stories": ["<2-3 sentences>", "<2-3 sentences>", "<2-3 sentences>"],
 "portrait": "<3-4 sentences about the person of the day>"}
Together the stories and the portrait are 150-250 words."""


def event_size(text: str) -> float:
    """'Size' of an event: the biggest number in its text (dead, resources, soldiers…), else 0."""
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
    return max(nums) if nums else 0.0


def importance(kind: str, text: str) -> float:
    w = KIND_WEIGHT.get(kind, 1)
    return w * (1 + math.log10(1 + event_size(text)))


@dataclass
class Chronicle:
    date: str
    headline: str
    stories: list[str]
    portrait: str
    table: list[dict]
    by_ai: bool
    events_used: int
    meta: dict = field(default_factory=dict)

    def text(self) -> str:
        lines = [f"THE CHRONICLE OF THE KINGDOMS — {self.date}", "", self.headline.upper(), ""]
        lines += [f"• {s}" for s in self.stories]
        lines += ["", "Portrait of the day: " + self.portrait, "", "Numbers of the day:"]
        if self.table:
            cols = list(self.table[0])
            lines.append("  " + " | ".join(f"{c:>10}" for c in cols))
            for row in self.table:
                lines.append("  " + " | ".join(f"{str(row[c]):>10}" for c in cols))
        return "\n".join(lines)


def pick_events(events: list, since: float, limit: int = 30) -> list:
    recent = [e for e in events if e.t >= since and e.kind in KIND_WEIGHT]
    recent.sort(key=lambda e: importance(e.kind, e.text), reverse=True)
    return recent[:limit]


def numbers_table(world) -> list[dict]:
    rows = []
    for civ in world.civs:
        rows.append({"kingdom": civ.name, "castle": civ.castle_level, "people": civ.pop,
                     "food": round(civ.stock["food"]), "wood": round(civ.stock["wood"]),
                     "stone": round(civ.stock["stone"]), "coins": round(civ.stock["coins"]),
                     "morale": round(civ.morale, 2)})
    return rows


def portrait_subject(world, rng: random.Random) -> dict | None:
    people = [(c, p) for c in world.civs for p in c.people]
    if not people:
        return None
    civ, p = rng.choice(people)
    dpy = world.rules.people["days_per_year"]
    trait = world.rules.traits[p.trait]["label"] if p.trait else "no particular trait"
    return {"id": p.id, "civ_id": civ.id, "name": p.name, "kingdom": civ.name, "age": round(p.age(world.clock.t, dpy)), "job": p.job or "resting",
            "trait": trait, "happiness": round(p.happiness), "recent_life": p.history[-4:]}


def plain_chronicle(date: str, events: list, subject: dict | None, table: list[dict]) -> Chronicle:
    """Written by code when the local AI is not available."""
    head = events[0].text if events else "A quiet day in both kingdoms"
    stories = [f"{e.civ_id.title() if e.civ_id else 'The world'}: {e.text}" for e in events[1:4]]
    while len(stories) < 3:
        stories.append("Nothing else of note happened.")
    portrait = (f"{subject['name']} of {subject['kingdom']}, {subject['age']} years old, works as {subject['job']} "
                f"({subject['trait'].lower()})." if subject else "")
    return Chronicle(date, head, stories, portrait, table, by_ai=False, events_used=len(events))


def gather_facts(world, since: float, seed: int = 0) -> dict:
    """Main thread: read the world once."""
    rich = any(c.has_tech("writing") for c in world.civs)          # version 7: Writing makes a richer Chronicle
    events = pick_events(world.journal.events, since, limit=40 if rich else 30)
    return {"date": world.clock.date.isoformat(), "events": events, "rich": rich,
            "subject": portrait_subject(world, random.Random(seed or int(world.clock.t))),
            "table": numbers_table(world)}


def compose(facts: dict, llm: LLM | None) -> Chronicle:
    """Any thread: only the AI call (or the plain fallback)."""
    date, events, subject, table = facts["date"], facts["events"], facts["subject"], facts["table"]
    if llm is None or not llm.available():
        return plain_chronicle(date, events, subject, table)
    payload = {
        "date": date,
        "events": [{"kingdom": e.civ_id, "day": e.day, "what": e.text,
                    "importance": round(importance(e.kind, e.text), 1)} for e in events],
        "person_of_the_day": {k: v for k, v in subject.items() if k not in ("id", "civ_id")} if subject else None,
        "numbers_of_the_day": table,
    }
    try:
        system = SYSTEM.replace("150-250 words", "250-350 words") if facts.get("rich") else SYSTEM
        raw = llm.chat_json(system, json.dumps(payload, ensure_ascii=False))
    except LLMError:
        return plain_chronicle(date, events, subject, table)
    stories = [str(s)[:600] for s in raw.get("stories", []) if str(s).strip()][:3]
    if not raw.get("headline") or len(stories) < 3:
        return plain_chronicle(date, events, subject, table)
    return Chronicle(date, str(raw["headline"])[:160], stories, str(raw.get("portrait", ""))[:900], table,
                     by_ai=True, events_used=len(events))


def write_chronicle(world, llm: LLM | None, since: float, seed: int = 0) -> Chronicle:
    return compose(gather_facts(world, since, seed), llm)
