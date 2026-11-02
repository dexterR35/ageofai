"""Shared test setup."""
import pytest


@pytest.fixture(autouse=True)
def no_local_piper(monkeypatch):
    """Tests never run the Piper installed on this PC (it would write real files into data/audio):
    the radio behaves as if Piper were not set up, so the browser's voice is used."""
    from ai.commentator import Commentator

    monkeypatch.setattr(Commentator, "piper_ready", lambda self: False)
