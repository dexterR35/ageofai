"""Saved reports must stay accurate across benchmark changes and restarts."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from ai.genome import Genome
from ai.llm import FakeLLM
from engine.rules import default_rules
from server.app import create_app
from server.runner import LiveRunner


@pytest.fixture
def training_client(tmp_path):
    r = LiveRunner(db=tmp_path / "training.db", evolve=True, pictures=False, commentator=False,
                   llm=FakeLLM(up=False), start=datetime(2027, 4, 5, 13, tzinfo=ZoneInfo("Europe/Bucharest")))
    with TestClient(create_app(r, run_world=False)) as client:
        yield client, r
    r.mind.close()
    r.store.close()


def save_round(store, night, benchmark, score):
    store.save_night(night, started="2027-04-05T00:30:00", finished="2027-04-05T01:00:00",
                     best_id=f"g{night}", best_fitness=0.6, mean_fitness=0.5, games=1, seconds=1800,
                     bench_id=f"g{night}", bench_fitness=score, diversity=0.2,
                     config={"bench": benchmark, "days": 120, "population": 32})


def test_empty_history_and_missing_round(training_client):
    client, _ = training_client
    data = client.get("/api/training").json()
    assert data["rounds"] == [] and data["next_population"] == 0
    assert data["status"]["running"] is False
    assert data["status"]["next_start"] == "2027-04-06T00:30:00+03:00"
    assert client.get("/api/training/99").status_code == 404
    assert client.post("/api/training").status_code == 405


def test_history_compares_only_matching_benchmarks_and_reports_live_training(training_client):
    client, r = training_client
    save_round(r.store, 1, "short", 0.4)
    save_round(r.store, 2, "short", 0.5)
    save_round(r.store, 3, "long", 0.7)
    save_round(r.store, 4, "long", 0.65)
    r._evolving = type("Running", (), {"poll": lambda self: None})()
    r._applied_night = 3
    data = client.get("/api/training").json()
    rows = data["rounds"]
    assert rows[0]["benchmark_delta"] is None
    assert rows[1]["benchmark_delta"] == pytest.approx(0.1)
    assert rows[2]["benchmark_delta"] is None
    assert rows[3]["benchmark_delta"] == pytest.approx(-0.05)
    assert data["status"]["running"] is True and data["status"]["applied_night"] == 3
    assert rows[0]["finished"].endswith("+03:00")


def test_round_detail_uses_its_own_lessons_and_actual_match_results(training_client):
    client, r = training_client
    store = r.store
    save_round(store, 1, "fixed", 0.5)
    save_round(store, 2, "fixed", 0.6)
    g = Genome.default(default_rules())
    g.id = "g1"
    store.save_genome(g, 1)
    store.db.commit()
    store.save_lessons(1, ["The first winter needed reserves."], False)
    store.save_lessons(2, ["This is a different round."], False)
    store.save_build_stats(1, {"farm": 7})
    store.save_matches([{"night": 1, "round": 0, "kind": "peer", "a": "g1", "b": "default",
                         "map_seed": 1, "event_seed": 2, "start": "2027-01-03", "days": 120,
                         "fa": 0.4, "fb": 0.4, "score_a": 0.5, "la": 2, "lb": 4, "pa": 4, "pb": 8}])
    data = client.get("/api/training/1").json()
    assert data["round"]["night"] == 1
    assert data["lessons"] == ["The first winter needed reserves."]
    assert data["buildings"] == {"farm": 7}
    assert data["champions"][0]["genes"] == g.genes
    assert data["outcomes"] == {"games": 1, "draws": 1, "average_castle": 3.0,
                                "highest_castle": 4, "average_population": 6.0}
    assert data["seasons"][0]["season"] == "Winter"
    assert client.get("/api/training/2").json()["outcomes"]["average_population"] is None
