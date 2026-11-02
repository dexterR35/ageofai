"""The radio commentator (version 4): one or two sentences about the big moments, read aloud.

Ollama writes the line; Piper (a local program) turns it into a WAV file. Without Piper the browser
reads the line with its own voice; without Ollama the commentator reads the journal line itself.
Limits from the doc: events with I >= 6, at most 20 lines an hour, silent 22:00–07:00.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from engine.rules import DATA_DIR, hhmm

from .chronicle import importance
from .llm import LLM, LLMError

SYSTEM = """You are the radio newsreader of a medieval world where two AI kingdoms, North and South, grow by themselves.
Announce the event in 1 or 2 short spoken sentences, at most {max_chars} characters, in plain English.
Use only the facts given; never invent numbers or names. No hashtags, no emojis.
Answer ONLY with JSON: {{"line": "..."}}"""

SPORTS = """You are a lively sports commentator at a medieval battle between two AI kingdoms, North and South.
Call the moment in 1 or 2 short spoken sentences, at most {max_chars} characters, in plain English.
Use only the facts given; never invent numbers or names. Answer ONLY with JSON: {{"line": "..."}}"""

BATTLE_KINDS = {"attack", "battle", "battle_update", "battle_end", "fall", "loot", "retreat"}

AUDIO_DIR = DATA_DIR / "audio"


@dataclass
class Pending:
    t: float
    civ: str | None
    kind: str
    text: str
    importance: float
    battle: bool = False


@dataclass
class Comment:
    t: float
    civ: str | None
    text: str
    by_ai: bool
    audio: str | None = None         # file name in data/audio
    event: str = ""

    def as_dict(self) -> dict:
        return {"t": self.t, "civ": self.civ, "text": self.text, "by_ai": self.by_ai,
                "audio": f"/media/audio/{self.audio}" if self.audio else None, "event": self.event}


@dataclass
class Commentator:
    rules: object
    llm: LLM | None
    audio_dir: Path = AUDIO_DIR
    pending: list[Pending] = field(default_factory=list)
    spoken_at: list[float] = field(default_factory=list)    # game times of the last hour's lines

    def __post_init__(self):
        cfg = self.rules.commentator
        self.min_i = float(cfg.get("min_importance", 6))
        self.per_hour = int(cfg.get("per_hour", 20))
        self.quiet = (hhmm(cfg.get("quiet_from", "22:00")), hhmm(cfg.get("quiet_to", "07:00")))
        self.max_chars = int(cfg.get("max_chars", 200))
        self.keep_days = float(cfg.get("keep_audio_days", 7))
        root = DATA_DIR.parent                    # relative paths in rules.yaml start at the project folder

        def at_root(p):
            return str(root / p) if p and not Path(p).is_absolute() and (root / p).exists() else p

        self.piper = at_root(cfg.get("piper"))
        self.voice = at_root(cfg.get("piper_voice"))

    # ------------------------------------------------------------------ main thread
    def hear(self, ev) -> None:
        """Every journal event passes here; the big ones wait for the microphone."""
        i = importance(ev.kind, ev.text)
        battle = ev.kind in BATTLE_KINDS
        if i >= self.min_i or battle:
            self.pending.append(Pending(ev.t, ev.civ_id, ev.kind, ev.text, max(i, 9.0) if battle else i, battle))
            self.pending = sorted(self.pending, key=lambda p: -p.importance)[:5]

    def is_quiet(self, local) -> bool:
        s = local.hour * 3600 + local.minute * 60
        a, b = self.quiet
        return (s >= a or s < b) if a > b else (a <= s < b)

    def next_line(self, now: float, local) -> Pending | None:
        """The event to talk about now, or None (quiet hours, hourly limit, nothing big)."""
        self.pending = [p for p in self.pending if now - p.t < 600]       # old news is no news
        if not self.pending:
            return None
        battle = [p for p in self.pending if p.battle]
        if battle:                     # a battle is called every 30 s, day or night (the doc's exception)
            p = max(battle, key=lambda q: q.t)
            self.pending = [q for q in self.pending if not q.battle]
            return p
        if self.is_quiet(local):
            self.pending.clear()
            return None
        self.spoken_at = [t for t in self.spoken_at if now - t < 3600]
        if len(self.spoken_at) >= self.per_hour:
            return None
        self.spoken_at.append(now)
        return self.pending.pop(0)

    # ------------------------------------------------------------------ any thread
    def write(self, p: Pending) -> Comment:
        text, by_ai = p.text, False
        if self.llm is not None and self.llm.available():
            facts = json.dumps({"kingdom": p.civ, "event": p.text, "kind": p.kind}, ensure_ascii=False)
            try:
                style = SPORTS if p.battle else SYSTEM
                line = str(self.llm.chat_json(style.format(max_chars=self.max_chars), facts).get("line", "")).strip()
                if line:
                    text, by_ai = line, True
            except LLMError:
                pass
        text = clip(text, self.max_chars)
        c = Comment(p.t, p.civ, text, by_ai, event=p.text)
        c.audio = self.speak(text, p.t)
        return c

    def piper_ready(self) -> bool:
        return bool(self.piper and self.voice and (Path(self.piper).exists() or shutil.which(self.piper))
                    and Path(self.voice).exists())

    def speak(self, text: str, t: float) -> str | None:
        """Piper → data/audio/<time>.wav. None if Piper isn't set up (the browser speaks instead)."""
        if not self.piper_ready():
            return None
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        name = f"{int(t)}_{int(time.time() * 1000) % 100000}.wav"
        try:
            subprocess.run([self.piper, "--model", self.voice, "--output_file", str(self.audio_dir / name)],
                           input=text.encode("utf-8"), capture_output=True, timeout=60, check=True)
        except (OSError, subprocess.SubprocessError):
            return None
        return name

    def clean_audio(self, now_wall: float | None = None) -> int:
        """Audio is kept 7 days (so the timelapse can play it)."""
        now_wall = now_wall or time.time()
        gone = 0
        if self.audio_dir.exists():
            for f in self.audio_dir.glob("*.wav"):
                if now_wall - f.stat().st_mtime > self.keep_days * 86400:
                    f.unlink(missing_ok=True)
                    gone += 1
        return gone


def clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    cut = text[: n - 1]
    for stop in (". ", "! ", "? "):
        i = cut.rfind(stop)
        if i > n // 2:
            return cut[: i + 1]
    return cut.rsplit(" ", 1)[0] + "…"
