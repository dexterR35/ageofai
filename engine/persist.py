"""Version 6: the live world on disk — a save every minute, hourly copies, and pruning.

data/saves/world.save        the latest state (written every minute, atomically)
data/saves/world.prev.save   the one before it (if the latest is damaged, this one is used)
data/saves/YYYY-MM-DD_HH.save  one an hour; after 7 days only one a day is kept; when the disk is over
                               90% full, hourly only for the last day and one a week after 30 days (the doctor)

Saves are gzip-compressed pickles of the World. They are only ever read back by this program.
"""
from __future__ import annotations

import gzip
import os
import pickle
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from .rules import DATA_DIR

if TYPE_CHECKING:
    from .world import World

SAVE_DIR = DATA_DIR / "saves"
LATEST = "world.save"
PREVIOUS = "world.prev.save"
VERSION = 1


def save_world(w: "World", folder: Path = SAVE_DIR, hourly: bool = False) -> Path:
    """Write the world atomically (tmp file, then rename). Returns the path of the latest save."""
    folder.mkdir(parents=True, exist_ok=True)
    hooks = w.detach_hooks()
    try:
        blob = gzip.compress(pickle.dumps({"version": VERSION, "saved": time.time(), "world": w},
                                          protocol=pickle.HIGHEST_PROTOCOL), 3)
    finally:
        w.attach_hooks(hooks)
    latest, prev, tmp = folder / LATEST, folder / PREVIOUS, folder / (LATEST + ".tmp")
    tmp.write_bytes(blob)
    if latest.exists():
        os.replace(latest, prev)
    os.replace(tmp, latest)
    if hourly:
        stamp = datetime.fromtimestamp(w.clock.t, w.clock.tz).strftime("%Y-%m-%d_%H")
        shutil.copyfile(latest, folder / f"{stamp}.save")
    return latest


def read_save(path: Path) -> tuple["World", float]:
    data = pickle.loads(gzip.decompress(path.read_bytes()))
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise ValueError(f"{path.name}: not a save of this version")
    return data["world"], float(data.get("saved", 0))


def load_latest(folder: Path = SAVE_DIR) -> tuple["World | None", str]:
    """The newest readable save: the latest, else the one before it. Returns (world, which file)."""
    for name in (LATEST, PREVIOUS):
        p = folder / name
        if not p.exists():
            continue
        try:
            w, _ = read_save(p)
            return w, name
        except Exception:                                  # damaged file: try the older one
            continue
    return None, ""


def hourly_saves(folder: Path = SAVE_DIR) -> list[tuple[datetime, Path]]:
    out = []
    for p in folder.glob("*.save"):
        try:
            out.append((datetime.strptime(p.stem, "%Y-%m-%d_%H"), p))
        except ValueError:
            continue
    return sorted(out)


def prune(folder: Path = SAVE_DIR, now: datetime | None = None, hourly_days: int = 7,
          disk_full: bool = False, keep_all_days: int = 30, full_hourly_days: int = 1) -> int:
    """Normally: after `hourly_days` keep one save a day.
    Disk nearly full: hourly saves only for the last `full_hourly_days`, then one a day, and after
    `keep_all_days` one a week — always less than the normal rule keeps, so it really frees space.
    Returns how many files were deleted."""
    now = now or datetime.now()
    daily_from = now - timedelta(days=min(full_hourly_days, hourly_days) if disk_full else hourly_days)
    weekly_from = now - timedelta(days=keep_all_days) if disk_full else None
    groups: dict[str, list[Path]] = {}
    for when, p in hourly_saves(folder):
        if weekly_from is not None and when < weekly_from:
            year, week, _ = when.isocalendar()
            groups.setdefault(f"week {year}-{week}", []).append(p)
        elif when < daily_from:
            groups.setdefault(when.strftime("%Y-%m-%d"), []).append(p)
    gone = 0
    for files in groups.values():
        for p in sorted(files)[1:]:                        # keep the first one of the day (or week)
            p.unlink(missing_ok=True)
            gone += 1
    return gone


def disk_used_share(folder: Path = DATA_DIR) -> float:
    u = shutil.disk_usage(folder)
    return u.used / u.total
