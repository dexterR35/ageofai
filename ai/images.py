"""Locally generated pictures (version 4): the portrait of the day and up to 3 big scenes.

The doc names SDXL through diffusers; this PC has no SDXL, and torch has no build for our Python, so the
pictures come from the local ComfyUI server (Flux 3 Klein) over HTTP. Prompts are made from data
(name, age, job, trait, season, kingdom color) plus one fixed style, and every person has a fixed
seed, so the same person looks alike in every portrait. Ollama is unloaded first: one GPU.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path

from engine.rules import DATA_DIR

IMAGE_DIR = DATA_DIR / "images"
COLOR_NAMES = {"#3b82f6": "blue", "#ef4444": "red"}


class ImageError(Exception):
    pass


def person_seed(civ_id: str, name: str) -> int:
    """Fixed per person: the same face in every portrait."""
    return zlib.crc32(f"{civ_id}:{name}".encode("utf-8")) & 0x7FFFFFFF


def portrait_prompt(subject: dict, season: str, color: str, style: str) -> str:
    job = subject.get("job") or "villager"
    job = {"wood": "woodcutter", "food": "farmer", "stone": "stonemason", "ore": "miner", "gold": "gold miner",
           "build": "builder", "firewood": "woodcutter", "resting": "villager"}.get(job, job)
    trait = (subject.get("trait") or "").lower()
    trait = "" if trait.startswith("no ") else trait
    return (f"Portrait of {subject['name']}, a {subject.get('age', 30)} year old medieval {job}"
            f"{', ' + trait if trait else ''}, from the {subject.get('kingdom', '')} kingdom, "
            f"wearing simple clothes with {COLOR_NAMES.get(color, 'colored')} accents, {season.lower()} light, "
            f"head and shoulders, {style}")


def scene_prompt(event_text: str, kingdom: str | None, season: str, color: str, style: str) -> str:
    where = f"in the {kingdom} kingdom" if kingdom else "in a medieval valley"
    return (f"A scene {where}: {event_text.rstrip('.')}. Small medieval village, people at work, "
            f"{COLOR_NAMES.get(color, '')} banners, {season.lower()}, wide view, {style}")


def comfy_workflow(cfg: dict, prompt: str, seed: int, prefix: str) -> dict:
    """Text to image in ComfyUI's API format."""
    m = cfg["models"]
    size = int(cfg.get("size", 1024))
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": m["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": m["clip"], "type": m.get("clip_type", "flux3"),
                                                     "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": m["vae"]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "6": {"class_type": m.get("latent", "EmptyFlux2LatentImage"),
              "inputs": {"width": size, "height": size, "batch_size": 1}},
        "7": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "seed": seed, "steps": int(cfg.get("steps", 20)),
                                                   "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
                                                   "denoise": 1.0, "positive": ["4", 0], "negative": ["5", 0],
                                                   "latent_image": ["6", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": prefix}},
    }


@dataclass
class ComfyPainter:
    cfg: dict
    out_dir: Path = IMAGE_DIR

    @property
    def url(self) -> str:
        return str(self.cfg.get("url", "http://127.0.0.1:8188")).rstrip("/")

    def _get(self, path: str, timeout: float = 10) -> bytes:
        with urllib.request.urlopen(self.url + path, timeout=timeout) as r:
            return r.read()

    def available(self) -> bool:
        if self.cfg.get("backend", "comfyui") != "comfyui":
            return False
        try:
            self._get("/system_stats", timeout=3)
            return True
        except OSError:
            return False

    def paint(self, prompt: str, seed: int, name: str) -> Path:
        """Queue one picture, wait for it, copy it to data/images/<name>.png."""
        wf = comfy_workflow(self.cfg, prompt, seed, f"ageofai/{name}")
        body = json.dumps({"prompt": wf, "client_id": str(uuid.uuid4())}).encode("utf-8")
        req = urllib.request.Request(self.url + "/prompt", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                pid = json.loads(r.read())["prompt_id"]
        except (OSError, KeyError, ValueError) as e:
            raise ImageError(f"ComfyUI refused the job: {e}") from e
        deadline = time.monotonic() + float(self.cfg.get("timeout_seconds", 300))
        while time.monotonic() < deadline:
            time.sleep(1.0)
            try:
                hist = json.loads(self._get(f"/history/{pid}"))
            except (OSError, ValueError):
                continue
            job = hist.get(pid)
            if not job:
                continue
            status = job.get("status", {})
            if status.get("status_str") == "error":
                raise ImageError(f"ComfyUI failed: {status.get('messages', '')}"[:300])
            for out in job.get("outputs", {}).values():
                for img in out.get("images", []):
                    q = urllib.parse.urlencode({"filename": img["filename"], "subfolder": img.get("subfolder", ""),
                                                "type": img.get("type", "output")})
                    data = self._get(f"/view?{q}", timeout=60)
                    self.out_dir.mkdir(parents=True, exist_ok=True)
                    path = self.out_dir / f"{name}.png"
                    path.write_bytes(data)
                    return path
        raise ImageError("ComfyUI took too long")


def check_workflow(painter: "ComfyPainter") -> list[str]:
    """Improvements §4: does ComfyUI have every node, input and model file our workflow uses?
    Reads /object_info only (nothing is painted). Returns the problems found (empty = all good)."""
    try:
        info = json.loads(painter._get("/object_info", timeout=30))
    except (OSError, ValueError) as e:
        return [f"ComfyUI is not reachable at {painter.url}: {e}"]
    problems = []
    wf = comfy_workflow(painter.cfg, "test", 1, "ageofai/check")
    for nid, node in wf.items():
        cls = node["class_type"]
        spec = info.get(cls)
        if spec is None:
            problems.append(f"node {nid}: ComfyUI has no node called {cls}")
            continue
        required = spec.get("input", {}).get("required", {})
        for name in required:
            if name not in node["inputs"]:
                problems.append(f"node {nid} ({cls}): missing input '{name}'")
        for name, value in node["inputs"].items():
            if isinstance(value, list):                     # a link to another node
                continue
            opts = required.get(name) or spec.get("input", {}).get("optional", {}).get(name)
            if opts and isinstance(opts[0], list) and value not in opts[0]:
                problems.append(f"node {nid} ({cls}): '{value}' is not one of ComfyUI's choices for '{name}'")
    return problems


def pick_scenes(events, since: float, limit: int, min_importance: float) -> list:
    """The day's biggest moments: castle levels, big events (battles come in version 5)."""
    from .chronicle import importance

    big = [e for e in events if e.t >= since and importance(e.kind, e.text) >= min_importance]
    big.sort(key=lambda e: -importance(e.kind, e.text))
    out, seen = [], set()
    for e in big:
        key = (e.civ_id, e.kind)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
        if len(out) >= limit:
            break
    return out


def daily_pictures(painter: ComfyPainter, llm, date: str, subject: dict | None, scenes: list,
                   season: str, colors: dict[str, str], names: dict[str, str]) -> list[dict]:
    """Any thread. The portrait of the day + scenes; returns rows for the store (empty if ComfyUI is away).
    The caller saves them on the main thread (SQLite connections stay on their thread)."""
    if not painter.available():
        return []
    if llm is not None:
        llm.unload()                                   # free the GPU for the picture model
    style = painter.cfg.get("style", "medieval watercolor illustration")
    rows = []
    jobs = []
    if subject:
        civ = subject.get("civ_id", "")
        jobs.append(("portrait", subject, portrait_prompt(subject, season, colors.get(civ, ""), style),
                     person_seed(civ, subject["name"]), f"{subject['name']}, {subject.get('age')} years old"))
    for e in scenes:
        jobs.append(("scene", None, scene_prompt(e.text, names.get(e.civ_id), season, colors.get(e.civ_id, ""), style),
                     zlib.crc32(e.text.encode()) & 0x7FFFFFFF, e.text))
    for i, (kind, subj, prompt, seed, caption) in enumerate(jobs):
        name = f"{date}_{kind}_{i}"
        try:
            path = painter.paint(prompt, seed, name)
        except ImageError:
            continue
        rows.append({"date": date, "kind": kind, "person_id": subj.get("id") if subj else None,
                     "person_name": subj["name"] if subj else None, "civ": subj.get("civ_id") if subj else None,
                     "caption": caption, "prompt": prompt, "seed": seed, "file": path.name})
    return rows
