"""Reconnects restore map deltas and idle observers release server resources."""
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from ai.llm import FakeLLM
from server.app import Hub, create_app
from server.runner import LiveRunner


@pytest.fixture
def observer(tmp_path):
    runner = LiveRunner(db=tmp_path / "observer.db", persistent=False,
                        start=datetime(2027, 4, 5, 9, tzinfo=ZoneInfo("Europe/Bucharest")),
                        llm=FakeLLM(up=False), evolve=False, pictures=False, commentator=False)
    try:
        with TestClient(create_app(runner, run_world=False)) as client:
            yield client, runner
    finally:
        runner.mind.close()
        runner.store.close()


def test_reconnect_greeting_contains_current_map_even_without_a_new_frame(observer):
    client, runner = observer
    node = runner.w.map.nodes[0]
    with client.websocket_connect("/ws") as socket:
        before = socket.receive_json()["map"]
    node.amount = 0
    with client.websocket_connect("/ws") as socket:
        after = socket.receive_json()["map"]
    assert before["nodes"][node.id][5] > 0
    assert after["nodes"][node.id][5] == 0
    assert after["terrain"] == before["terrain"]
    assert "fog" in after and "roads" in after and "seq" in after


def test_idle_disconnected_observer_is_unsubscribed(observer):
    client, _ = observer
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "hello"
        assert len(client.app.state.hub.queues) == 1

    async def subscribers():
        for _ in range(20):
            if not client.app.state.hub.queues:
                return 0
            await asyncio.sleep(0.01)
        return len(client.app.state.hub.queues)

    assert client.portal.call(subscribers) == 0


def test_slow_observer_resyncs_instead_of_silently_losing_map_deltas():
    async def check():
        hub = Hub()
        slow = asyncio.Queue(maxsize=4)
        fast = asyncio.Queue(maxsize=4)
        hub.queues.update((slow, fast))
        for seq in range(5):
            frame = json.dumps({"type": "frame", "seq": seq})
            hub._fan_out(frame)
            assert await fast.get() == frame
        assert json.loads(await slow.get()) == {"type": "resync"}
        assert json.loads(await slow.get())["seq"] == 4
        assert slow.empty()

    asyncio.run(check())
