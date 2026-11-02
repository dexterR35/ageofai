"""Talking to the local AI (Ollama). Everything that uses it must keep working when it is down.

Ollama runs on this PC (http://127.0.0.1:11434). The design doc uses Qwen3 14B for thinking
and nomic-embed-text for memory vectors. No internet, no subscription.

    llm = OllamaClient()
    if llm.available():
        answer = llm.chat_json(system="…", user="…")   # → dict, or raises LLMError

Tests use FakeLLM, which answers from a script.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
import zlib
from typing import Any, Callable, Protocol


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    def available(self) -> bool: ...
    def chat_json(self, system: str, user: str, temperature: float | None = None) -> dict: ...
    def chat_text(self, system: str, user: str, temperature: float | None = None) -> str: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...


def parse_json(text: str) -> dict:
    """Take the first JSON object out of a model answer (handles ```json fences and <think> blocks)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    if start < 0:
        raise LLMError("no JSON object in the answer")
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    out = json.loads(text[start:i + 1])
                except json.JSONDecodeError as e:
                    raise LLMError(f"bad JSON: {e}") from e
                if not isinstance(out, dict):
                    raise LLMError("the answer is not a JSON object")
                return out
    raise LLMError("unterminated JSON object")


class Usage:
    """Calls, failures, tokens and seconds per model, so the cost of each mind can be seen (a paid model later
    costs per token). Thread-safe enough for one worker thread plus the game thread (small int additions)."""

    FIELDS = ("calls", "failures", "tokens_in", "tokens_out", "seconds")

    def __init__(self):
        self.models: dict[str, dict[str, float]] = {}

    def add(self, model: str, calls: int = 0, failures: int = 0, tokens_in: int = 0, tokens_out: int = 0,
            seconds: float = 0.0) -> None:
        row = self.models.setdefault(model, dict.fromkeys(self.FIELDS, 0))
        row["calls"] += calls
        row["failures"] += failures
        row["tokens_in"] += int(tokens_in or 0)
        row["tokens_out"] += int(tokens_out or 0)
        row["seconds"] += seconds

    def take(self) -> dict[str, dict[str, float]]:
        """What was counted since the last take (and start again from zero)."""
        out, self.models = self.models, {}
        return out


class OllamaClient:
    def __init__(self, host: str = "http://127.0.0.1:11434", model: str = "qwen3:14b",
                 embed_model: str = "nomic-embed-text", timeout: float = 60.0, temperature: float = 0.7):
        self.host = host.rstrip("/")
        self.model = model
        self.embed_model = embed_model
        self.timeout = timeout
        self.temperature = temperature
        self.failures_in_a_row = 0        # the world doctor (v6) acts after 3 in a row within 60 s
        self.fail_times: list[float] = []
        self.paused_until = 0.0           # the doctor can switch the local AI off for a while
        self.calls = 0
        self.seconds = 0.0
        self.usage = Usage()
        self._available: tuple[float, bool] | None = None

    # ------------------------------------------------------------------ http
    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(self.host + path, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        t0 = time.perf_counter()
        model = str(payload.get("model", self.model))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                out = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            self.failures_in_a_row += 1
            self.fail_times = (self.fail_times + [time.monotonic()])[-10:]
            self.usage.add(model, failures=1, seconds=time.perf_counter() - t0)
            raise LLMError(f"Ollama: {e}") from e
        spent = time.perf_counter() - t0
        self.calls += 1
        self.seconds += spent
        self.failures_in_a_row = 0
        # Ollama reports the tokens it read (prompt) and wrote (answer)
        self.usage.add(model, calls=1, tokens_in=out.get("prompt_eval_count", 0) if isinstance(out, dict) else 0,
                       tokens_out=out.get("eval_count", 0) if isinstance(out, dict) else 0, seconds=spent)
        return out

    def available(self) -> bool:
        """Is Ollama running with our model? Cached for 60 s."""
        now = time.monotonic()
        if now < self.paused_until:
            return False
        if self._available and now - self._available[0] < 60:
            return self._available[1]
        ok = False
        try:
            with urllib.request.urlopen(self.host + "/api/tags", timeout=2) as resp:
                tags = json.loads(resp.read().decode("utf-8"))
            names = {m.get("name", "") for m in tags.get("models", [])}
            ok = any(n == self.model or n.startswith(self.model + ":") or n.split(":")[0] == self.model
                     for n in names)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            ok = False
        self._available = (now, ok)
        return ok

    # ------------------------------------------------------------------ calls
    def chat_text(self, system: str, user: str, temperature: float | None = None, json_mode: bool = False) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "think": False,
            "options": {"temperature": self.temperature if temperature is None else temperature},
        }
        if json_mode:
            payload["format"] = "json"
        out = self._post("/api/chat", payload)
        content = out.get("message", {}).get("content")
        if not isinstance(content, str):
            raise LLMError("Ollama returned no message")
        return content

    def chat_json(self, system: str, user: str, temperature: float | None = None) -> dict:
        return parse_json(self.chat_text(system, user, temperature, json_mode=True))

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = self._post("/api/embed", {"model": self.embed_model, "input": texts})
        vecs = out.get("embeddings")
        if not isinstance(vecs, list) or len(vecs) != len(texts):
            raise LLMError("Ollama returned no embeddings")
        return vecs

    def pause(self, seconds: float) -> None:
        """The doctor: no calls for a while; the tactical layer plays alone."""
        self.paused_until = time.monotonic() + seconds
        self._available = None
        self.failures_in_a_row = 0

    def unload(self) -> None:
        """Free the GPU (before image generation, version 4)."""
        try:
            self._post("/api/generate", {"model": self.model, "keep_alive": 0})
        except LLMError:
            pass


def model_for(rules, civ_id: str | None = None) -> str:
    """The model a kingdom thinks with: rules.yaml → llm.models.<civ>, else the shared llm.model."""
    c = getattr(rules, "llm", {}) or {}
    per = c.get("models") or {}
    return str(per.get(civ_id) or c.get("model", "qwen3:14b")) if civ_id else str(c.get("model", "qwen3:14b"))


def client_from_rules(rules, model: str | None = None) -> OllamaClient:
    """The client configured in rules.yaml → llm (with another model if given)."""
    c = getattr(rules, "llm", {}) or {}
    return OllamaClient(host=c.get("host", "http://127.0.0.1:11434"), model=model or model_for(rules),
                        embed_model=c.get("embed_model", "nomic-embed-text"),
                        timeout=float(c.get("timeout_seconds", 120)), temperature=float(c.get("temperature", 0.7)))


def clients_from_rules(rules, shared: "LLM | None" = None) -> dict[str, "LLM"]:
    """One client per kingdom (rivalry). Kingdoms on the same model share one client; the shared client
    (the radio's, the Chronicle's) is reused when its model matches. One GPU: the calls still go one at a
    time through LiveMind's single worker thread, and Ollama swaps the models in and out."""
    by_model: dict[str, LLM] = {}
    if shared is not None and getattr(shared, "model", None):
        by_model[shared.model] = shared
    out = {}
    for spec in rules.civs:
        m = model_for(rules, spec["id"])
        if m not in by_model:
            by_model[m] = client_from_rules(rules, m)
        out[spec["id"]] = by_model[m]
    return out


class FakeLLM:
    """A scripted stand-in for tests: `responder(system, user) -> dict | str`."""

    def __init__(self, responder: Callable[[str, str], Any] | None = None, up: bool = True, dim: int = 8,
                 model: str = "fake"):
        self.responder = responder or (lambda s, u: {})
        self.model = model
        self.up = up
        self.dim = dim
        self.prompts: list[tuple[str, str]] = []
        self.failures_in_a_row = 0
        self.fail_times: list[float] = []
        self.paused_until = 0.0
        self.usage = Usage()

    def available(self) -> bool:
        return self.up

    def chat_text(self, system: str, user: str, temperature: float | None = None) -> str:
        if not self.up:
            raise LLMError("fake LLM is down")
        self.prompts.append((system, user))
        out = self.responder(system, user)
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        self.usage.add(self.model, calls=1, tokens_in=(len(system) + len(user)) // 4, tokens_out=len(text) // 4)
        return text

    def chat_json(self, system: str, user: str, temperature: float | None = None) -> dict:
        return parse_json(self.chat_text(system, user, temperature))

    def unload(self) -> None:
        self.unloaded = True

    def pause(self, seconds: float) -> None:
        self.paused_until = time.monotonic() + seconds
        self.up = False

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Deterministic bag-of-words vectors, good enough to test similarity."""
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in re.findall(r"\w+", t.lower()):
                v[zlib.crc32(w.encode("utf-8")) % self.dim] += 1.0
            out.append(v)
        return out
