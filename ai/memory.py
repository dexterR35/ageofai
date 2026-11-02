"""Long-term memory for the king and the advisors (SQLite + local embeddings).

Saved automatically: every event with importance I ≥ 5, kept/broken promises (I = 9),
and the king's evening reflection at 22:00 (I = 8).

Recall: for the current situation each memory gets
    S = 0.3 · 0.995^hours + 0.4 · I/10 + 0.3 · cos(e_memory, e_situation)
and the best 8 go into the council's summary.

Forgetting: I 5–6 after 60 days, I 7–8 after a year, I ≥ 9 never.
"""
from __future__ import annotations

import math
import sqlite3
from array import array
from dataclasses import dataclass

from .llm import LLM, LLMError

DEFAULTS = {
    "recency_base": 0.995, "w_recency": 0.3, "w_importance": 0.4, "w_similarity": 0.3,
    "recall": 8, "min_importance": 5, "forget_days": {5: 60, 7: 365},  # I ≥ key → kept that many days
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories(
    id INTEGER PRIMARY KEY AUTOINCREMENT, civ TEXT, t REAL, day INTEGER, kind TEXT, text TEXT,
    importance INTEGER, vec BLOB);
CREATE INDEX IF NOT EXISTS memories_civ ON memories(civ, t);
"""


@dataclass
class Memory:
    id: int
    civ: str
    t: float
    day: int
    kind: str
    text: str
    importance: int
    vec: list[float] | None
    score: float = 0.0


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def memory_score(hours_ago: float, importance: float, cos: float, cfg: dict = DEFAULTS) -> float:
    """S = 0.3·0.995^hours + 0.4·I/10 + 0.3·cos."""
    return (cfg["w_recency"] * cfg["recency_base"] ** max(0.0, hours_ago)
            + cfg["w_importance"] * importance / 10 + cfg["w_similarity"] * cos)


def keep_days(importance: int, cfg: dict = DEFAULTS) -> float | None:
    """How long a memory lives. None = forever."""
    if importance >= 9:
        return None
    for floor in sorted(cfg["forget_days"], reverse=True):
        if importance >= floor:
            return cfg["forget_days"][floor]
    return 0.0


class MemoryBank:
    def __init__(self, db: sqlite3.Connection, llm: LLM | None = None, cfg: dict | None = None):
        self.db = db
        self.llm = llm
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.db.executescript(SCHEMA)
        self.db.commit()

    def _embed(self, text: str) -> list[float] | None:
        if self.llm is None or not self.llm.available():
            return None
        try:
            return self.llm.embed([text])[0]
        except LLMError:
            return None

    def embed_many(self, texts: list[str]) -> list[list[float] | None]:
        """Vectors for several texts in one call; Nones when the local AI is away. Any thread (no SQLite)."""
        if not texts or self.llm is None or not self.llm.available():
            return [None] * len(texts)
        try:
            return list(self.llm.embed(texts))
        except LLMError:
            return [None] * len(texts)

    def add(self, civ: str, t: float, day: int, kind: str, text: str, importance: int,
            vec: list[float] | None = None, embed: bool = True) -> int:
        """`embed=False` stores the memory with the given vector (or none) without calling the local AI;
        the live world does this so the game thread never waits for the GPU."""
        if vec is None and embed:
            vec = self._embed(text)
        blob = array("f", vec).tobytes() if vec else None
        cur = self.db.execute("INSERT INTO memories(civ, t, day, kind, text, importance, vec) VALUES(?,?,?,?,?,?,?)",
                              (civ, t, day, kind, text, int(importance), blob))
        self.db.commit()
        return cur.lastrowid

    def wants(self, ev) -> bool:
        """Only what matters (I ≥ 5) becomes a memory."""
        return ev.civ_id is not None and ev.importance >= self.cfg["min_importance"]

    @staticmethod
    def event_text(ev) -> str:
        return f"Day {ev.day}: {ev.text}"

    def remember_event(self, ev) -> int | None:
        """Journal hook: keep only what matters (I ≥ 5)."""
        if not self.wants(ev):
            return None
        return self.add(ev.civ_id, ev.t, ev.day, ev.kind, self.event_text(ev), ev.importance)

    def all(self, civ: str) -> list[Memory]:
        rows = self.db.execute("SELECT id, civ, t, day, kind, text, importance, vec FROM memories WHERE civ=?",
                               (civ,)).fetchall()
        out = []
        for r in rows:
            vec = list(array("f", r[7])) if r[7] else None
            out.append(Memory(r[0], r[1], r[2], r[3], r[4], r[5], r[6], vec))
        return out

    def recall(self, civ: str, situation: str, now: float, k: int | None = None) -> list[Memory]:
        """The k memories that best fit the situation right now."""
        return self.rank(self.all(civ), self._embed(situation), now, k)

    def rank(self, mems: list[Memory], situation_vec: list[float] | None, now: float,
             k: int | None = None) -> list[Memory]:
        """Score memories already read from SQLite. Any thread (no SQLite, no AI call)."""
        k = k or self.cfg["recall"]
        sit = situation_vec
        for m in mems:
            m.score = memory_score((now - m.t) / 3600, m.importance, cosine(m.vec, sit), self.cfg)
        mems.sort(key=lambda m: m.score, reverse=True)
        return mems[:k]

    def forget(self, now: float) -> int:
        """Delete memories past their lifetime. Returns how many were removed."""
        removed = 0
        for floor, days in self.cfg["forget_days"].items():
            nxt = [f for f in self.cfg["forget_days"] if f > floor]
            upper = min(nxt) if nxt else 9
            cur = self.db.execute("DELETE FROM memories WHERE importance>=? AND importance<? AND t<?",
                                  (floor, upper, now - days * 86400))
            removed += cur.rowcount
        cur = self.db.execute("DELETE FROM memories WHERE importance<?", (min(self.cfg["forget_days"]),))
        removed += cur.rowcount
        self.db.commit()
        return removed
