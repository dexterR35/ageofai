"""Morning editions must survive late starts, missing models and restarts."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ai.chronicle import Chronicle
from ai.llm import FakeLLM
from engine import persist
from engine.rules import default_rules
from engine.world import World
from server.runner import LiveRunner


@pytest.mark.parametrize("saved_draft", [False, True])
def test_late_live_start_recovers_and_publishes_todays_chronicle(tmp_path, monkeypatch, saved_draft):
    now = datetime(2027, 4, 5, 13, tzinfo=ZoneInfo("Europe/Bucharest"))
    monkeypatch.setattr("server.runner.time.time", lambda: now.timestamp())
    kwargs = dict(db=tmp_path / "live.db", save_dir=tmp_path / "saves", llm=FakeLLM(up=False),
                  pictures=False, commentator=False, evolve=False, frames=False)
    persist.save_world(World(default_rules(), start=now), kwargs["save_dir"])
    if saved_draft:
        from engine.store import Store
        store = Store(kwargs["db"])
        store.save_chronicle(Chronicle(now.date().isoformat(), "The saved morning draft", [], "", [], False, 0))
        store.close()
    r = LiveRunner(**kwargs)
    try:
        r.mind.tick()
        for future, _ in list(r.mind.jobs):
            future.result(timeout=3)
        r.mind.tick()
        entries = r.store.chronicles()
        assert len(entries) == 1 and entries[0]["date"] == "2027-04-05"
        assert entries[0]["by_ai"] == 0
        if saved_draft:
            assert entries[0]["headline"] == "The saved morning draft"
        assert len(r.w.journal.of("chronicle")) == 1
        assert r.w.chronicle_published_on == "2027-04-05"
        r.save()
    finally:
        r.mind.close()
        r.store.close()

    again = LiveRunner(**kwargs)
    try:
        again.mind.tick()
        assert len(again.w.journal.of("chronicle")) == 1
        assert len(again.store.chronicles()) == 1
    finally:
        again.mind.close()
        again.store.close()
