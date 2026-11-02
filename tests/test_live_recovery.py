"""Regression coverage for the live world freezing during nightly benchmarks."""
import sqlite3
import threading
from concurrent.futures import Future
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ai.evolution import Evolution
from ai.llm import FakeLLM
from engine.rules import default_rules
from engine.store import Store


@pytest.mark.parametrize("lineages", [False, True])
def test_benchmark_leaves_the_database_available_to_the_live_world(tmp_path, monkeypatch, lineages):
    store = Store(tmp_path / "shared.db")
    live = sqlite3.connect(store.path, timeout=0.05)
    evo = Evolution(store, processes=1, population=4, days=1, peer_games=1, champion_games=1)
    if not lineages:
        monkeypatch.setattr(evo, "lineages", lambda: [])
    stages = []

    def play(specs, progress=None):
        stages.append(specs[0].kind)
        # A real second connection must be able to save while training computes.
        live.execute("INSERT INTO journal(kind, text) VALUES('test', 'world is advancing')")
        live.commit()
        side = {"castle": 1, "gathered": 100, "pop": 2, "alive": True,
                "departures": 0, "deaths": 0, "births": 0, "buildings": {}, "activations": {}}
        return [{"sides": [dict(side), dict(side)]} for _ in specs]

    monkeypatch.setattr(evo, "play_all", play)
    try:
        evo.run_night()
        assert "benchmark" in stages
        assert store.last_night() == 1
    finally:
        live.close()
        store.close()


def test_ai_failure_survives_a_locked_error_journal(tmp_path, monkeypatch, caplog):
    from ai.live_mind import LiveMind
    from engine.world import World

    start = datetime(2027, 4, 5, 9, tzinfo=ZoneInfo("Europe/Bucharest"))
    w = World(default_rules(), start=start)
    store = Store(tmp_path / "live.db")
    mind = LiveMind(w, store, FakeLLM(up=False))
    failed = Future()
    failed.set_exception(sqlite3.OperationalError("database is locked"))
    mind.jobs.append((failed, lambda value: None))
    store.db.execute("INSERT INTO journal(kind) VALUES('uncommitted')")

    def locked_log(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(w, "log", locked_log)
    try:
        mind.tick()
        assert not store.db.in_transaction
        assert "database is locked" in caplog.text
        assert not mind.jobs
    finally:
        mind.close()
        store.close()


def test_world_thread_failure_reports_unhealthy_and_requests_restart(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.runner import LiveRunner

    r = LiveRunner(db=tmp_path / "runner.db", llm=FakeLLM(up=False), pictures=False,
                   commentator=False, evolve=False, speed=60,
                   start=datetime(2027, 4, 5, 9, tzinfo=ZoneInfo("Europe/Bucharest")))
    restart = threading.Event()

    def crash():
        raise RuntimeError("simulation failed")

    monkeypatch.setattr(r, "run", crash)
    app = create_app(r)
    app.state.on_world_error = restart.set
    try:
        with TestClient(app) as client:
            assert restart.wait(timeout=3)
            response = client.get("/api/health")
            assert response.status_code == 503
            assert response.json()["ok"] is False
            assert response.json()["world_alive"] is False
    finally:
        r.mind.close()
        r.store.close()


@pytest.mark.parametrize("applied", [None, 1, 2])
def test_restart_applies_only_training_missing_from_the_save(tmp_path, monkeypatch, applied):
    from ai.genome import Genome
    from engine import persist
    from engine.world import World
    from server.runner import LiveRunner

    now = datetime(2027, 4, 5, 13, tzinfo=ZoneInfo("Europe/Bucharest"))
    monkeypatch.setattr("server.runner.time.time", lambda: now.timestamp())
    w = World(default_rules(), start=now)
    if applied is not None:
        w.applied_training_night = applied
    old_ids = [c.genome.id for c in w.civs]
    saves = tmp_path / "saves"
    persist.save_world(w, saves)
    store = Store(tmp_path / "handoff.db")
    winners = []
    for civ in w.civs:
        g = Genome.default(default_rules())
        g.id = f"trained-{civ.id}"
        g.meta.update(night=2, lineage=civ.id, fitness=0.7)
        winners.append(g)
    store.save_population(2, winners)
    store.save_night(2, best_id=winners[0].id)
    store.close()

    kwargs = dict(db=tmp_path / "handoff.db", llm=FakeLLM(up=False),
                  commentator=False, pictures=False, evolve=False, save_dir=saves)
    r = LiveRunner(**kwargs)
    try:
        r.schedule()
        expected = old_ids if applied == 2 else [g.id for g in winners]
        assert [c.genome.id for c in r.w.civs] == expected
        assert r.w.applied_training_night == 2
        r.save()
    finally:
        r.mind.close()
        r.store.close()

    again = LiveRunner(**kwargs)
    try:
        assert again._applied_night == 2
        again.schedule()
        assert [c.genome.id for c in again.w.civs] == expected
    finally:
        again.mind.close()
        again.store.close()
