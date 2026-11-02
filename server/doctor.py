"""Version 6: the world doctor. Every 10 minutes it looks for trouble and fixes it on its own.

| check                  | when                                             | what it does                                   |
| a stuck person         | walking, same spot for 30 min                    | new path; still stuck → nearest free tile     |
| an idle kingdom        | nothing built, trained or moved for 48 work hours | clear the build queue, ask the council again   |
| impossible values      | negative or NaN stock, HP over max, NaN position  | go back to the last save (≤ 1 min old)         |
| the local AI is silent | 3 failed calls in a row within 60 s              | tactical layer only for a while (+ restart)    |
| slow ticks             | over 500 ms for 5 minutes                        | switch the decor off (fewer frames)            |
| disk almost full       | over 90%                                         | hourly saves for 1 day only; after 30 days one a week |

Every repair goes into the journal and the `doctor` table. On Mondays the Chronicle carries a short
report of the week; a problem seen more than 3 times a week is marked as a bug to fix in the code.
"""
from __future__ import annotations

import json
import math
import shlex
import subprocess
import time
from collections import Counter
from typing import TYPE_CHECKING

from engine import persist
from engine.entities import State

if TYPE_CHECKING:
    from .runner import LiveRunner

REPORT_SYSTEM = """You are the doctor of a simulated medieval world that must run for months without help.
From the week's repair log, write a short report (3-5 plain English sentences): what broke, what was fixed,
what keeps happening. Use only the facts given. Answer ONLY with JSON: {"report": "..."}"""

ACTIVE_KINDS = {"build_start", "castle_start", "train", "recruit_start", "research_start", "attack", "message"}


class Doctor:
    def __init__(self, runner: "LiveRunner"):
        self.r = runner
        self.cfg = runner.rules.doctor
        self.seen: dict[int, tuple[float, float, float, bool]] = {}   # person id → (x, y, since, repathed)
        self.idle_flags: dict[str, list[float]] = {}                  # civ → times we already acted
        self.slow_since: float | None = None
        self.decor_off = False
        self.disk_noted = None                                        # the day we last wrote "disk full"
        self.last_check = time.monotonic()

    # ------------------------------------------------------------------ helpers
    @property
    def w(self):
        return self.r.w

    def note(self, check: str, civ, text: str, action: str) -> None:
        cid = civ.id if civ is not None else None
        self.w.log(civ, "doctor", f"Doctor: {text} → {action}", 3)
        self.r.store.save_doctor(self.w.clock.t, check, cid, text, action)

    # ------------------------------------------------------------------ the round (every 10 min)
    def check(self) -> str | None:
        """Runs on the world thread with the lock held. Returns "restore" when the world must go back
        to the last save."""
        if self.impossible_values():
            return "restore"
        self.stuck_people()
        self.idle_kingdoms()
        self.local_ai()
        self.disk()
        return None

    # ------------------------------------------------------------------ 1. stuck people
    def stuck_people(self) -> None:
        now = self.w.clock.t
        limit = float(self.cfg.get("stuck_minutes", 30)) * 60
        alive = set()
        for civ in self.w.civs:
            for p in civ.people:
                alive.add(p.id)
                if p.state != State.WALK:
                    self.seen.pop(p.id, None)
                    continue
                old = self.seen.get(p.id)
                if old is None or math.hypot(p.x - old[0], p.y - old[1]) > 0.25:
                    self.seen[p.id] = (p.x, p.y, now, False)
                    continue
                x, y, since, repathed = old
                if now - since < limit:
                    continue
                if not repathed:
                    p.path, p.path_i, p.path_version = (), 0, -1         # walk() recomputes the path
                    self.seen[p.id] = (x, y, now, True)
                    self.note("stuck", civ, f"{p.name} stood still while walking for {limit / 60:.0f} min",
                              "computed a new path")
                else:
                    spot = self._free_tile(int(x + 0.5), int(y + 0.5))
                    if spot:
                        p.x, p.y = float(spot[0]), float(spot[1])
                    p.path, p.path_i, p.path_version = (), 0, -1
                    p.state = State.IDLE
                    self.seen.pop(p.id, None)
                    self.note("stuck", civ, f"{p.name} was still stuck",
                              f"moved to tile {spot}" if spot else "no free tile nearby: made idle to pick a new task")
        for pid in list(self.seen):
            if pid not in alive:
                self.seen.pop(pid)

    def _free_tile(self, x: int, y: int) -> tuple[int, int] | None:
        ring = self.w.map.nearest_walkable(x, y, 11)
        return ring[0] if ring else None

    # ------------------------------------------------------------------ 2. idle kingdoms
    def idle_kingdoms(self) -> None:
        w = self.w
        now = w.clock.t
        work_h = max(1.0, w.clock.season.hours)
        limit = float(self.cfg.get("idle_work_hours", 48)) / work_h * 86400       # 48 work hours in days
        for civ in w.civs:
            if not civ.alive or now - civ.born_t < limit:
                continue
            last = max((e.t for e in w.journal.events if e.civ_id == civ.id and e.kind in ACTIVE_KINDS),
                       default=civ.born_t)
            moving = any(u.state == "walk" for u in civ.units)
            if now - last < limit or moving:
                continue
            done = [t for t in self.idle_flags.get(civ.id, []) if now - t < 7 * 86400]
            if done and now - done[-1] < limit:
                continue
            cleared = 0
            for b in list(civ.sites()):
                if b.type != "castle" and b.level == 0 and b.progress <= 0.0:
                    civ.buildings.remove(b)
                    w.map.remove_building(b.x, b.y, b.size)
                    cleared += 1
            if civ.ai is not None:
                civ.ai.targets = []
            civ.dirty = True
            if self.r.mind is not None:
                self.r.mind.next_council[civ.id] = now
            done.append(now)
            self.idle_flags[civ.id] = done
            action = f"cleared {cleared} empty sites, asked the council for a new plan"
            if len(done) >= 2:
                action += "; second time this week: written in the report"
            self.note("idle", civ, f"{civ.name} did nothing for {self.cfg.get('idle_work_hours', 48)} working hours",
                      action)

    # ------------------------------------------------------------------ 3. impossible values
    def impossible_values(self, world=None) -> list[str]:
        bad = []
        for civ in (world or self.w).civs:
            for k, v in civ.stock.items():
                if not math.isfinite(v) or v < -1e-6:
                    bad.append(f"{civ.name} {k} = {v}")
            for b in civ.buildings:
                if not math.isfinite(b.hp) or (b.max_hp and b.hp > b.max_hp + 1e-6):
                    bad.append(f"{civ.name} building {b.type} hp {b.hp}/{b.max_hp}")
            for p in civ.people:
                if not (math.isfinite(p.x) and math.isfinite(p.y)):
                    bad.append(f"{p.name} position {p.x},{p.y}")
            for u in civ.units:
                if not math.isfinite(u.hp) or u.hp > u.max_hp + 1e-6 or not math.isfinite(u.x):
                    bad.append(f"{civ.name} {u.type} hp {u.hp}/{u.max_hp}")
        return bad

    def repair_in_place(self) -> list[str]:
        """When no good save exists: clamp the bad values instead."""
        fixed = []
        for civ in self.w.civs:
            for k, v in list(civ.stock.items()):
                if not math.isfinite(v) or v < 0:
                    civ.stock[k] = 0.0
                    fixed.append(f"{civ.name} {k}")
            for b in civ.buildings:
                if not math.isfinite(b.hp) or (b.max_hp and b.hp > b.max_hp):
                    b.hp = b.max_hp
            for p in civ.people:
                if not (math.isfinite(p.x) and math.isfinite(p.y)) and civ.castle is not None:
                    p.x, p.y = civ.castle.center
                    p.path, p.state = (), State.IDLE
            for u in civ.units:
                if not math.isfinite(u.hp) or u.hp > u.max_hp:
                    u.hp = u.max_hp
        return fixed

    # ------------------------------------------------------------------ 4. the local AI
    def local_ai(self) -> None:
        """Every model in use (the shared one and each kingdom's, rivalry) is checked on its own: a kingdom
        whose model keeps failing plays on its genes for a while; the other king keeps thinking."""
        clients = [self.r.llm, *getattr(self.r, "llms", {}).values()]
        for llm in {id(c): c for c in clients}.values():
            self._check_llm(llm)

    def _check_llm(self, llm) -> None:
        n = int(self.cfg.get("llm_failures", 3))
        times = getattr(llm, "fail_times", [])
        if getattr(llm, "failures_in_a_row", 0) < n or len(times) < n:
            return
        if times[-1] - times[-n] > float(self.cfg.get("llm_window_seconds", 60)):
            return
        minutes = float(self.cfg.get("llm_pause_minutes", 10))
        llm.pause(minutes * 60)
        action = f"tactical layer only for {minutes:.0f} min"
        cmd = self.cfg.get("ollama_restart")
        if cmd:
            try:
                subprocess.Popen(shlex.split(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                action += f", restarted Ollama ({cmd})"
            except OSError as e:
                action += f", could not restart Ollama: {e}"
        model = getattr(llm, "model", None)
        self.note("ollama", None, f"the local AI{f' ({model})' if model else ''} failed {n} times in a row", action)

    # ------------------------------------------------------------------ 5. slow ticks (called by the runner)
    def tick_time(self, ms: float) -> None:
        now = time.monotonic()
        if ms > float(self.cfg.get("slow_tick_ms", 500)):
            if self.slow_since is None:
                self.slow_since = now
            elif not self.decor_off and now - self.slow_since > float(self.cfg.get("slow_minutes", 5)) * 60:
                self.decor_off = True
                self.r.set_decor(False)
                self.note("slow", None, f"ticks over {self.cfg.get('slow_tick_ms', 500)} ms for "
                                        f"{self.cfg.get('slow_minutes', 5)} minutes", "decor off (fewer frames)")
        else:
            self.slow_since = None

    # ------------------------------------------------------------------ 6. the disk
    def disk(self) -> None:
        share = persist.disk_used_share()
        if share <= float(self.cfg.get("disk_full", 0.9)):
            return
        gone = persist.prune(self.r.save_dir, disk_full=True, keep_all_days=int(self.cfg.get("keep_all_days", 30)),
                             hourly_days=int(self.r.rules.persistence.get("hourly_days", 7)))
        today = self.w.clock.date
        if not gone and self.disk_noted == today:
            return                                             # nothing new to say: once a day is enough
        self.disk_noted = today
        action = (f"deleted {gone} old saves (hourly only for the last day, one a week after "
                  f"{self.cfg.get('keep_all_days', 30)} days)" if gone else
                  "no old saves left to delete: free space by hand")
        self.note("disk", None, f"the disk is {share:.0%} full", action)

    # ------------------------------------------------------------------ the weekly report (Monday, with the Chronicle)
    def report_facts(self) -> dict:
        rows = self.r.store.doctor_log(self.w.clock.t - 7 * 86400)
        counts = Counter(r["check_name"] for r in rows)
        bugs = [f"{k} ({n}× this week)" for k, n in counts.items() if n > int(self.cfg.get("repeats_for_bug", 3))]
        return {"date": self.w.clock.date.isoformat(), "repairs": len(rows), "by_check": dict(counts),
                "log": [f"{r['wall']}: {r['text']} → {r['action']}" for r in rows[-40:]], "bugs": bugs}

    @staticmethod
    def write_report(facts: dict, llm) -> tuple[str, bool]:
        """Any thread."""
        if not facts["repairs"]:
            return "A quiet week: the doctor had nothing to fix.", False
        plain = (f"The doctor made {facts['repairs']} repairs this week: "
                 + ", ".join(f"{n}× {k}" for k, n in facts["by_check"].items()) + "."
                 + (" Marked as bugs to fix in the code: " + "; ".join(facts["bugs"]) + "." if facts["bugs"] else ""))
        if llm is None or not llm.available():
            return plain, False
        try:
            text = str(llm.chat_json(REPORT_SYSTEM, json.dumps(facts, ensure_ascii=False)).get("report", "")).strip()
        except Exception:
            return plain, False
        if not text:
            return plain, False
        if facts["bugs"]:
            text += " Marked as bugs to fix in the code: " + "; ".join(facts["bugs"]) + "."
        return text[:1200], True
