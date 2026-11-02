"""Read-only reports from saved training results; no simulation work on web requests."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from engine.store import Store


def round_row(row: dict, timezone: str) -> dict:
    result = dict(row)
    config = result.get("config") or {}
    if isinstance(config, str):
        config = json.loads(config)
    result["config"] = config
    result["benchmark_key"] = config.get("bench")
    for field in ("started", "finished"):
        if result.get(field):
            dt = datetime.fromisoformat(result[field])
            result[field] = dt.replace(tzinfo=ZoneInfo(timezone)).isoformat() if dt.tzinfo is None else dt.isoformat()
    return result


def history(store: Store, timezone: str) -> list[dict]:
    rows = [round_row(row, timezone) for row in store.nights()]
    previous = None
    for row in rows:
        same = previous and row["benchmark_key"] and previous["benchmark_key"] == row["benchmark_key"]
        row["benchmark_delta"] = (row["bench_fitness"] - previous["bench_fitness"]) if (
            same and row.get("bench_fitness") is not None and previous.get("bench_fitness") is not None) else None
        previous = row
    return rows


def live_status(runner) -> dict:
    with runner.lock:
        now = runner.w.clock.local
        hour, minute = map(int, runner.rules.evolution["start"].split(":"))
        next_start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_start <= now:
            next_start += timedelta(days=1)
        running = runner._evolving is not None and runner._evolving.poll() is None
        return {"running": running, "enabled": runner.evolve,
                "next_start": next_start.isoformat() if runner.evolve else None,
                "applied_night": runner._applied_night,
                "clock": now.isoformat(timespec="seconds"),
                "kingdoms": [{"name": c.name, "strategy": c.genome.id} for c in runner.w.civs]}


def detail(store: Store, night: int, timezone: str) -> dict | None:
    saved = store.db.execute("SELECT * FROM nights WHERE night=?", (night,)).fetchone()
    if saved is None:
        return None
    row = round_row(dict(saved), timezone)
    games = store.db.execute(
        "SELECT COUNT(*) games, SUM(CASE WHEN score_a=0.5 THEN 1 ELSE 0 END) draws "
        "FROM matches WHERE night=?", (night,)).fetchone()
    outcomes = store.db.execute(
        "SELECT AVG(castle) average_castle, MAX(castle) highest_castle, AVG(pop) average_population "
        "FROM (SELECT la castle, pa pop FROM matches WHERE night=? "
        "UNION ALL SELECT lb, pb FROM matches WHERE night=?)", (night, night)).fetchone()
    seasons = store.db.execute(
        "SELECT CASE WHEN substr(start,6,2) IN ('03','04','05') THEN 'Spring' "
        "WHEN substr(start,6,2) IN ('06','07','08') THEN 'Summer' "
        "WHEN substr(start,6,2) IN ('09','10','11') THEN 'Autumn' ELSE 'Winter' END season, "
        "COUNT(*) games, AVG((la+lb)/2.0) average_castle, AVG((pa+pb)/2.0) average_population "
        "FROM matches WHERE night=? GROUP BY season", (night,)).fetchall()
    winners = row["config"].get("lineages") or {"Best strategy": row["best_id"]}
    champions = []
    for kingdom, gid in winners.items():
        g = store.genome(gid)
        champions.append({"kingdom": kingdom.title(), "id": gid, "genes": g.genes if g else {}})
    tests = []
    if store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rules'").fetchone():
        for rule in store.db.execute("SELECT name, history FROM rules ORDER BY id"):
            notes = [note for note in json.loads(rule["history"] or "[]") if note.startswith(f"night {night}:")]
            if notes:
                tests.append({"name": rule["name"], "notes": notes})
    return {"round": row, "outcomes": {**dict(games), **dict(outcomes)},
            "seasons": [dict(s) for s in seasons], "champions": champions,
            "lessons": store.lessons(night), "buildings": store.build_stats(night, night), "rules": tests}
