"""SQLite storage (data/world.db): genomes, nights of evolution, matches, the Hall of Champions
and the live world's journal. WAL mode lets the live world read while the night's evolution writes.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

from ai.genome import Genome
from .entities import Event
from .rules import DATA_DIR, default_rules

DEFAULT_DB = DATA_DIR / "world.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS genomes(
    id TEXT PRIMARY KEY, night INTEGER, parents TEXT, generation INTEGER, genes TEXT,
    fitness REAL, elo REAL, games INTEGER DEFAULT 0, created TEXT);
CREATE TABLE IF NOT EXISTS population(
    night INTEGER, slot INTEGER, genome_id TEXT, PRIMARY KEY(night, slot));
CREATE TABLE IF NOT EXISTS hall(
    genome_id TEXT PRIMARY KEY, entered_night INTEGER);
CREATE TABLE IF NOT EXISTS nights(
    night INTEGER PRIMARY KEY, started TEXT, finished TEXT, best_id TEXT, best_fitness REAL,
    mean_fitness REAL, diversity REAL, games INTEGER, seconds REAL, config TEXT);
CREATE TABLE IF NOT EXISTS matches(
    id INTEGER PRIMARY KEY AUTOINCREMENT, night INTEGER, round INTEGER, kind TEXT, a TEXT, b TEXT,
    map_seed INTEGER, event_seed INTEGER, start TEXT, days INTEGER, fa REAL, fb REAL, score_a REAL,
    la INTEGER, lb INTEGER, pa INTEGER, pb INTEGER);
CREATE TABLE IF NOT EXISTS journal(
    id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, day INTEGER, civ TEXT, kind TEXT, text TEXT, importance INTEGER);
CREATE INDEX IF NOT EXISTS journal_t ON journal(t);
CREATE TABLE IF NOT EXISTS lessons(night INTEGER, idx INTEGER, text TEXT, by_ai INTEGER, PRIMARY KEY(night, idx));
CREATE TABLE IF NOT EXISTS build_stats(night INTEGER, btype TEXT, count INTEGER, PRIMARY KEY(night, btype));
CREATE TABLE IF NOT EXISTS chronicles(date TEXT PRIMARY KEY, headline TEXT, body TEXT, table_json TEXT, by_ai INTEGER);
CREATE TABLE IF NOT EXISTS council(
    id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, civ TEXT, thought TEXT, reason TEXT, listened_to TEXT,
    proposals TEXT, plan TEXT, seconds REAL, followed_score REAL);
CREATE INDEX IF NOT EXISTS matches_night ON matches(night);
CREATE TABLE IF NOT EXISTS images(
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, kind TEXT, person_id INTEGER, person_name TEXT, civ TEXT,
    caption TEXT, prompt TEXT, seed INTEGER, file TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS comments(
    id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, civ TEXT, text TEXT, audio TEXT, by_ai INTEGER);
CREATE TABLE IF NOT EXISTS doctor(
    id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, wall TEXT, check_name TEXT, civ TEXT, text TEXT, action TEXT);
CREATE TABLE IF NOT EXISTS doctor_reports(date TEXT PRIMARY KEY, text TEXT, bugs TEXT, by_ai INTEGER);
CREATE TABLE IF NOT EXISTS timeline(t REAL PRIMARY KEY, frame BLOB);
CREATE TABLE IF NOT EXISTS cma_state(key TEXT PRIMARY KEY, night INTEGER, state TEXT);
CREATE TABLE IF NOT EXISTS model_usage(
    day TEXT, model TEXT, calls INTEGER DEFAULT 0, failures INTEGER DEFAULT 0, tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0, seconds REAL DEFAULT 0, PRIMARY KEY(day, model));
CREATE TABLE IF NOT EXISTS rivalry(
    id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, day INTEGER, era INTEGER, civ TEXT, model TEXT, mind TEXT,
    kind TEXT, other TEXT, value REAL, days REAL, text TEXT);
CREATE INDEX IF NOT EXISTS rivalry_kind ON rivalry(kind);
"""

# what the rivalry scoreboard counts, per kingdom or per model (rows of the rivalry table)
RIVALRY_SUMS = {
    "wars_started": "kind='war_started'",
    "wars_won": "kind='war_end' AND value=1",
    "wars_lost": "kind='war_end' AND value=0",
    "battles": "kind='battle'",
    "battles_won": "kind='battle' AND value=1",
    "castles_destroyed": "kind='castle_destroyed'",
    "fallen": "kind='fallen'",
    "wonders": "kind='wonder'",
    "eras_won": "kind='wonder' AND value=1",
}


class Store:
    def __init__(self, path: str | Path | None = None, handoff: bool = False):
        """`handoff`: the connection is made here but used by another thread afterwards (one at a time),
        like the live world, which is set up on the main thread and then runs on its own."""
        self.path = Path(path) if path else DEFAULT_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=not handoff)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(nights)")}
        for col, kind in (("bench_id", "TEXT"), ("bench_fitness", "REAL")):
            if col not in cols:
                self.db.execute(f"ALTER TABLE nights ADD COLUMN {col} {kind}")
        if "lineage" not in {r[1] for r in self.db.execute("PRAGMA table_info(genomes)")}:
            self.db.execute("ALTER TABLE genomes ADD COLUMN lineage TEXT")    # rivalry: each kingdom's own line
        self.db.commit()

    @classmethod
    def reader(cls, path: str | Path | None = None) -> "Store":
        """A short-lived connection for one web request (no schema work; SQLite stays on its thread)."""
        s = cls.__new__(cls)
        s.path = Path(path) if path else DEFAULT_DB
        s.db = sqlite3.connect(s.path, timeout=30)
        s.db.row_factory = sqlite3.Row
        return s

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ genomes
    def save_genome(self, g: Genome, night: int) -> None:
        self.db.execute(
            "INSERT INTO genomes(id, night, parents, generation, genes, fitness, elo, games, created, lineage) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET fitness=excluded.fitness, "
            "elo=excluded.elo, games=excluded.games, lineage=COALESCE(excluded.lineage, genomes.lineage)",
            (g.id, night, json.dumps(list(g.parents)), g.generation, json.dumps(g.genes),
             g.meta.get("fitness"), g.meta.get("elo", 1000.0), g.meta.get("games", 0),
             datetime.now().isoformat(timespec="seconds"), g.meta.get("lineage")))

    def _genome(self, row: sqlite3.Row) -> Genome:
        genes = json.loads(row["genes"])
        for k, v in default_rules().genome["default"].items():
            genes.setdefault(k, float(v))            # genomes born before a gene existed (v7: exploration)
        g = Genome(genes, id=row["id"], parents=tuple(json.loads(row["parents"] or "[]")),
                   generation=row["generation"] or 0)
        g.meta.update(fitness=row["fitness"], elo=row["elo"] if row["elo"] is not None else 1000.0,
                      games=row["games"] or 0, night=row["night"],
                      lineage=row["lineage"] if "lineage" in row.keys() else None)
        return g

    def family_tree(self, gid: str, depth: int = 5) -> list[dict]:
        """Version 7: a genome and its ancestors, generation by generation, with the genes that changed
        compared to its parents (crossover picks each gene from one parent; mutation moves some)."""
        out, seen, level = [], set(), [gid]
        for gen in range(depth + 1):
            nxt = []
            for g_id in level:
                if g_id in seen:
                    continue
                seen.add(g_id)
                g = self.genome(g_id)
                if g is None:
                    continue
                parents = [self.genome(p) for p in g.parents]
                parents = [p for p in parents if p is not None]
                changed = {}
                for k, v in g.genes.items():
                    pv = [p.genes.get(k) for p in parents if p.genes.get(k) is not None]
                    if pv and all(abs(v - x) > 1e-6 for x in pv):
                        changed[k] = {"from": round(sum(pv) / len(pv), 3), "to": round(v, 3)}
                out.append({"id": g.id, "depth": gen, "night": g.meta.get("night"), "fitness": g.meta.get("fitness"),
                            "elo": g.meta.get("elo"), "parents": list(g.parents), "changed": changed,
                            "genes": {k: round(v, 3) for k, v in g.genes.items()}})
                nxt += list(g.parents)
            level = nxt
            if not level:
                break
        return out

    def genome(self, gid: str) -> Genome | None:
        row = self.db.execute("SELECT * FROM genomes WHERE id=?", (gid,)).fetchone()
        return self._genome(row) if row else None

    # ------------------------------------------------------------------ populations
    def save_population(self, night: int, genomes: Iterable[Genome]) -> None:
        self.db.execute("DELETE FROM population WHERE night=?", (night,))
        for i, g in enumerate(genomes):
            self.save_genome(g, g.meta.get("night", night))
            self.db.execute("INSERT INTO population(night, slot, genome_id) VALUES(?,?,?)", (night, i, g.id))
        self.db.commit()

    def population(self, night: int) -> list[Genome]:
        rows = self.db.execute(
            "SELECT g.* FROM population p JOIN genomes g ON g.id = p.genome_id WHERE p.night=? ORDER BY p.slot",
            (night,)).fetchall()
        return [self._genome(r) for r in rows]

    # ------------------------------------------------------------------ hall of champions
    def hall(self, lineage: str | None = None) -> list[Genome]:
        """The Hall of Champions; with `lineage`, that kingdom's champions (plus the old shared ones)."""
        q = "SELECT g.*, h.entered_night FROM hall h JOIN genomes g ON g.id = h.genome_id"
        args: tuple = ()
        if lineage is not None:
            q, args = q + " WHERE g.lineage = ? OR g.lineage IS NULL", (lineage,)
        rows = self.db.execute(q + " ORDER BY g.elo DESC", args).fetchall()
        out = []
        for r in rows:
            g = self._genome(r)
            g.meta["entered_night"] = r["entered_night"]
            out.append(g)
        return out

    def hall_add(self, g: Genome, night: int) -> None:
        self.save_genome(g, g.meta.get("night", night))
        self.db.execute("INSERT OR IGNORE INTO hall(genome_id, entered_night) VALUES(?,?)", (g.id, night))

    def hall_remove(self, gid: str) -> None:
        self.db.execute("DELETE FROM hall WHERE genome_id=?", (gid,))

    # ------------------------------------------------------------------ nights and matches
    def last_night(self) -> int:
        row = self.db.execute("SELECT MAX(night) FROM nights").fetchone()
        return row[0] or 0

    def save_night(self, night: int, **fields) -> None:
        cols = ["night", *fields]
        vals = [night, *(json.dumps(v) if isinstance(v, dict) else v for v in fields.values())]
        self.db.execute(f"INSERT OR REPLACE INTO nights({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
        self.db.commit()

    def nights(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM nights ORDER BY night").fetchall()]

    def save_matches(self, rows: list[dict]) -> None:
        if not rows:
            return
        cols = list(rows[0])
        self.db.executemany(f"INSERT INTO matches({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                            [tuple(r[c] for c in cols) for r in rows])
        self.db.commit()

    def morning_genomes(self) -> list[Genome] | None:
        """One genome per civilization of the live world (in rules.yaml → civs order): each kingdom gets the
        best of its own lineage from the last night (rivalry). Nights from before the lineages had one shared
        population: then the 2 best of it, as before."""
        night = self.last_night()
        if not night:
            return None
        rows = self.db.execute(
            "SELECT g.* FROM population p JOIN genomes g ON g.id = p.genome_id "
            "WHERE p.night=? AND g.fitness IS NOT NULL ORDER BY g.fitness DESC", (night,)).fetchall()
        genomes = [self._genome(r) for r in rows]
        civs = [c["id"] for c in default_rules().civs]
        best: dict[str, Genome] = {}
        for g in genomes:
            best.setdefault(g.meta.get("lineage"), g)
        if all(c in best for c in civs):
            return [best[c] for c in civs]
        return genomes[:len(civs)] if len(genomes) >= len(civs) else None

    def lineage_best(self, civ_id: str) -> Genome | None:
        """The best genome of this kingdom's lineage last night (or of the shared population before lineages)."""
        best = self.morning_genomes()
        civs = [c["id"] for c in default_rules().civs]
        return best[civs.index(civ_id)] if best and civ_id in civs else None

    # ------------------------------------------------------------------ version 3
    def save_lessons(self, night: int, lessons: list[str], by_ai: bool) -> None:
        self.db.execute("DELETE FROM lessons WHERE night=?", (night,))
        self.db.executemany("INSERT INTO lessons(night, idx, text, by_ai) VALUES(?,?,?,?)",
                            [(night, i, t, int(by_ai)) for i, t in enumerate(lessons)])
        self.db.commit()

    def lessons(self, night: int | None = None) -> list[str]:
        night = self.last_night() if night is None else night
        return [r[0] for r in self.db.execute("SELECT text FROM lessons WHERE night=? ORDER BY idx", (night,))]

    def save_build_stats(self, night: int, counts: dict[str, int]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO build_stats(night, btype, count) VALUES(?,?,?)",
                            [(night, k, v) for k, v in counts.items()])
        self.db.commit()

    def build_stats(self, from_night: int, to_night: int) -> dict[str, int]:
        rows = self.db.execute("SELECT btype, SUM(count) FROM build_stats WHERE night BETWEEN ? AND ? GROUP BY btype",
                               (from_night, to_night)).fetchall()
        return {r[0]: r[1] for r in rows}

    def save_chronicle(self, ch) -> None:
        self.db.execute("INSERT OR REPLACE INTO chronicles(date, headline, body, table_json, by_ai) VALUES(?,?,?,?,?)",
                        (ch.date, ch.headline, ch.text(), json.dumps(ch.table), int(ch.by_ai)))
        self.db.commit()

    def chronicles(self, limit: int = 30, search: str | None = None) -> list[dict]:
        if search:
            rows = self.db.execute("SELECT * FROM chronicles WHERE body LIKE ? ORDER BY date DESC LIMIT ?",
                                   (f"%{search}%", limit))
        else:
            rows = self.db.execute("SELECT * FROM chronicles ORDER BY date DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows.fetchall()]

    # ------------------------------------------------------------------ version 4: pictures and the radio
    def save_image(self, row: dict) -> int:
        row = {**row, "created": datetime.now().isoformat(timespec="seconds")}
        cols = list(row)
        cur = self.db.execute(f"INSERT INTO images({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                              [row[c] for c in cols])
        self.db.commit()
        return int(cur.lastrowid)

    def images(self, date: str | None = None, person_name: str | None = None, limit: int = 20,
               person_id: int | None = None) -> list[dict]:
        """A person's portraits are matched by id AND name: two people may share a name, and a new world
        starts its ids from 1 again."""
        q, args = "SELECT * FROM images", []
        if date:
            q, args = q + " WHERE date=?", [date]
        elif person_id is not None:
            q, args = q + " WHERE person_id=? AND person_name IS ?", [person_id, person_name]
        elif person_name:
            q, args = q + " WHERE person_name=?", [person_name]
        rows = self.db.execute(q + " ORDER BY id DESC LIMIT ?", [*args, limit]).fetchall()
        return [dict(r) for r in rows]

    def save_comment(self, t: float, civ: str | None, text: str, audio: str | None, by_ai: bool) -> int:
        cur = self.db.execute("INSERT INTO comments(t, civ, text, audio, by_ai) VALUES(?,?,?,?,?)",
                              (t, civ, text, audio, int(by_ai)))
        self.db.commit()
        return int(cur.lastrowid)

    # ------------------------------------------------------------------ version 6: the doctor and the timelapse
    def save_doctor(self, t: float, check: str, civ: str | None, text: str, action: str) -> None:
        self.db.execute("INSERT INTO doctor(t, wall, check_name, civ, text, action) VALUES(?,?,?,?,?,?)",
                        (t, datetime.now().isoformat(timespec="seconds"), check, civ, text, action))
        self.db.commit()

    def doctor_log(self, since: float) -> list[dict]:
        rows = self.db.execute("SELECT * FROM doctor WHERE t >= ? ORDER BY t", (since,)).fetchall()
        return [dict(r) for r in rows]

    def save_doctor_report(self, date: str, text: str, bugs: list[str], by_ai: bool) -> None:
        self.db.execute("INSERT OR REPLACE INTO doctor_reports(date, text, bugs, by_ai) VALUES(?,?,?,?)",
                        (date, text, json.dumps(bugs), int(by_ai)))
        self.db.commit()

    def doctor_report(self, date: str) -> dict | None:
        r = self.db.execute("SELECT * FROM doctor_reports WHERE date=?", (date,)).fetchone()
        return {**dict(r), "bugs": json.loads(r["bugs"] or "[]")} if r else None

    def record_frame(self, t: float, blob: bytes, keep_seconds: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO timeline(t, frame) VALUES(?,?)", (t, blob))
        self.db.execute("DELETE FROM timeline WHERE t < ?", (t - keep_seconds,))
        self.db.commit()

    def frames_since(self, since: float) -> list[tuple[float, bytes]]:
        return [(r[0], r[1]) for r in self.db.execute("SELECT t, frame FROM timeline WHERE t >= ? ORDER BY t", (since,))]

    # ------------------------------------------------------------------ CMA-ES (the night's training)
    def cma_state(self, key: str) -> dict | None:
        row = self.db.execute("SELECT state FROM cma_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_cma_state(self, key: str, night: int, state: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO cma_state(key, night, state) VALUES(?,?,?)",
                        (key, night, json.dumps(state)))
        self.db.commit()

    # ------------------------------------------------------------------ what each model costs (calls, tokens)
    def add_model_usage(self, day: str, counted: dict[str, dict]) -> None:
        for model, u in counted.items():
            if not (u.get("calls") or u.get("failures")):
                continue
            self.db.execute(
                "INSERT INTO model_usage(day, model, calls, failures, tokens_in, tokens_out, seconds) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(day, model) DO UPDATE SET calls=calls+excluded.calls, "
                "failures=failures+excluded.failures, tokens_in=tokens_in+excluded.tokens_in, "
                "tokens_out=tokens_out+excluded.tokens_out, seconds=seconds+excluded.seconds",
                (day, model, int(u.get("calls", 0)), int(u.get("failures", 0)), int(u.get("tokens_in", 0)),
                 int(u.get("tokens_out", 0)), float(u.get("seconds", 0.0))))
        self.db.commit()

    def model_usage(self, today: str | None = None) -> list[dict]:
        """Totals per model (all days), with today's share."""
        rows = self.db.execute(
            "SELECT model, SUM(calls) calls, SUM(failures) failures, SUM(tokens_in) tokens_in, "
            "SUM(tokens_out) tokens_out, SUM(seconds) seconds, "
            "SUM(CASE WHEN day=? THEN tokens_in + tokens_out ELSE 0 END) tokens_today "
            "FROM model_usage GROUP BY model ORDER BY model", (today or "",)).fetchall()
        return [dict(r) for r in rows]

    def save_council(self, d) -> None:
        self.db.execute(
            "INSERT INTO council(t, civ, thought, reason, listened_to, proposals, plan, seconds) VALUES(?,?,?,?,?,?,?,?)",
            (d.t, d.civ, d.plan.thought, d.plan.reason, d.plan.listened_to,
             json.dumps({r: p.__dict__ for r, p in d.proposals.items()}, ensure_ascii=False),
             json.dumps(d.plan.__dict__, ensure_ascii=False), d.seconds))
        self.db.commit()

    # ------------------------------------------------------------------ rivalry scoreboard
    def save_rivalry(self, rows: list[dict]) -> None:
        cols = ("t", "day", "era", "civ", "model", "mind", "kind", "other", "value", "days", "text")
        self.db.executemany(f"INSERT INTO rivalry({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                            [tuple(r.get(c) for c in cols) for r in rows])
        self.db.commit()

    def rivalry(self, by: str = "civ") -> dict[str, dict]:
        """Totals per kingdom (`by="civ"`) or per model (`by="model"`): wars started and won, battles, castles
        destroyed, kingdoms fallen, Wonders and eras won, and the highest castle level with the fewest days a
        kingdom needed to reach it."""
        if by not in ("civ", "model"):
            raise ValueError(by)
        sums = ", ".join(f"SUM(CASE WHEN {cond} THEN 1 ELSE 0 END) AS {name}" for name, cond in RIVALRY_SUMS.items())
        out = {r["who"]: {k: r[k] for k in r.keys() if k != "who"}
               for r in self.db.execute(f"SELECT {by} AS who, {sums} FROM rivalry GROUP BY {by}")}
        for who, level, days in self.db.execute(
                f"SELECT {by}, CAST(value AS INTEGER), MIN(days) FROM rivalry WHERE kind='castle' "
                f"GROUP BY {by}, CAST(value AS INTEGER)"):
            row = out.setdefault(who, {k: 0 for k in RIVALRY_SUMS})
            row.setdefault("castle_days", {})[int(level)] = days
            if level > row.get("best_castle", 0):
                row["best_castle"], row["best_castle_days"] = int(level), days
        return out

    def rivalry_recent(self, limit: int = 20) -> list[dict]:
        rows = self.db.execute("SELECT * FROM rivalry WHERE kind != 'castle' OR value >= 5 "
                               "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ journal (live world)
    def add_event(self, ev: Event) -> None:
        self.db.execute("INSERT INTO journal(t, day, civ, kind, text, importance) VALUES(?,?,?,?,?,?)",
                        (ev.t, ev.day, ev.civ_id, ev.kind, ev.text, ev.importance))
        self.db.commit()

    def events(self, since: float = 0.0, min_importance: int = 0, limit: int = 500) -> list[dict]:
        rows = self.db.execute("SELECT * FROM journal WHERE t>=? AND importance>=? ORDER BY t DESC LIMIT ?",
                               (since, min_importance, limit)).fetchall()
        return [dict(r) for r in rows]
