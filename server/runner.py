"""The live world's main loop, shared by `cli.py live` (text only) and `cli.py serve` (with the browser view).

One thread owns the world. It advances the clock to the wall clock (× speed), runs the night's
evolution at 00:30 in its own process, hands the night's genomes to the civilizations at 07:00, ticks
the v3/v4 AI schedule, and once a second builds a frame for the page. Anyone else who wants to read
the world takes `lock` first.

Version 6 (24/7): in real time the world is saved every minute. On start it continues from the last
save and replays the missing hours in fast mode (without the local AI). The doctor checks it every
10 minutes, and a frame every 2 minutes is kept for the 24-hour timelapse.

Rivalry: each kingdom may think with its own model (rules.yaml → llm.models) and plays its own lineage's
best genome (07:00, rebirth, new era); wars, battles, castles and Wonders go to the scoreboard (SQLite).
"""
from __future__ import annotations

import gzip
import copy
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from engine import persist
from engine.rules import default_rules, hhmm
from engine.store import Store
from engine.world import World

CLI = Path(__file__).resolve().parent.parent / "cli.py"


class LiveRunner:
    def __init__(self, *, db=None, seed: int = 1, speed: float | None = None, start=None, evolve: bool = True,
                 echo: bool = False, llm=None, commentator: bool = True, pictures: bool = True,
                 frames: bool = True, persistent: bool | None = None, save_dir: Path | None = None,
                 new_world: bool = False, llms: dict | None = None, profile: str = "classic",
                 models: dict[str, str] | None = None):
        from ai.commentator import Commentator
        from ai.images import ComfyPainter
        from ai.live_mind import LiveMind
        from ai.llm import client_from_rules, clients_from_rules
        from ai.rule_lab import RuleLab

        from engine.rules import Rules
        self.profile = profile
        self.rules = rules = Rules.profile(profile) if profile != "classic" else default_rules()
        simulation = rules.raw.get("simulation", {})
        if models:
            unknown = set(models) - {c["id"] for c in rules.civs}
            if unknown or any(not name.strip() for name in models.values()):
                raise ValueError(f"Invalid kingdom/model selection: {models}")
            rules.llm.setdefault("models", {}).update(models)
        if db is None and profile != "classic":
            db = CLI.parent / "data" / f"{profile}.db"
        self.store = Store(db, handoff=True)              # built here, used only by the world thread
        self.db_path = self.store.path
        self.speed = max(0.01, float(simulation.get("speed", 1.0) if speed is None else speed))
        # the real world (real time, now) is saved and resumed; a world watched faster or from another date is a test
        self.realtime = profile == "classic" and self.speed == 1.0 and start is None
        self.persistent = bool(simulation.get("persistent", self.realtime)) if persistent is None else persistent
        self.save_dir = Path(save_dir) if save_dir else (persist.SAVE_DIR / profile if profile != "classic" else persist.SAVE_DIR)
        self.genomes = self.store.morning_genomes()
        self.recovered = ""
        loaded = None
        if self.persistent and not new_world:
            loaded, which = persist.load_latest(self.save_dir)
            if loaded is not None:
                self.recovered = which
        if loaded is None and not self.genomes:            # no training yet: two different minds from day 1
            from ai.genome import Genome
            self.genomes = Genome.live_start(rules)
        self.w = loaded if loaded is not None else World(rules, seed=seed, genomes=self.genomes, echo=echo,
                                                         start=start)
        if loaded is not None and profile != "classic":
            # Existing civilization saves keep their world balance and cities,
            # but must receive today's runtime pacing and freshness settings.
            self.w.rules.raw["simulation"] = copy.deepcopy(simulation)
            for setting in ("every_minutes", "adaptive", "max_interval_hours", "directive_hours",
                            "urgent_cooldown_minutes", "max_pending", "decision_max_game_hours",
                            "decision_max_real_seconds"):
                if setting in rules.council:
                    self.w.rules.council[setting] = copy.deepcopy(rules.council[setting])
        self.w.journal.echo = echo
        self.w.journal.sink = self.store.add_event
        self.w.live = True                                # a fallen kingdom is reborn after 7 days (v5)
        self.w.military.rebirth_genome = self._best_genome
        self.book_from = RuleLab
        self._load_book()
        self.llm = llm if llm is not None else client_from_rules(rules)
        # one model per kingdom (rivalry); a test that passes one fake LLM gets it for both kings
        if llms is None:
            llms = {c["id"]: llm for c in rules.civs} if llm is not None else clients_from_rules(rules, self.llm)
        self.llms = dict(llms)
        self._wall0 = time.monotonic()
        self._game0 = self.w.clock.t
        self.caught_up = self.catch_up() if loaded is not None and self.realtime else 0.0
        self.commentator = Commentator(rules, self.llm) if commentator else None
        painter = ComfyPainter(rules.images) if pictures and rules.images.get("backend", "off") != "off" else None
        self.mind = LiveMind(self.w, self.store, self.llm, self.commentator, painter, on_comment=self._comment,
                             llms=self.llms, recover_morning=self.persistent, game_speed=self.speed)
        self.evolve = evolve and simulation.get("night_training", True)
        from .pacing import AdaptiveClock
        pace_config = simulation.get("adaptive_clock", {})
        self.pacing = (AdaptiveClock(self.w.clock.t, time.monotonic(), self.speed, pace_config)
                       if pace_config.get("enabled", False) and not self.realtime else None)
        self.lock = threading.RLock()
        self.frames = None
        self.recorder = None
        if frames:
            self._make_frames()
        self.frame_seconds = float(rules.web.get("frame_seconds", 1.0))
        self.on_frame: Callable[[dict], None] | None = None
        self.on_status: Callable[[str], None] | None = None
        self._stop = threading.Event()
        self._evolving: subprocess.Popen | None = None
        self._evolved_on = None
        # Completed training in SQLite may be newer than the recovered world.
        # Track the handoff in the save, rather than assuming it happened before a crash.
        self._applied_night = getattr(self.w, "applied_training_night",
                                      0 if loaded is not None else self.store.last_night())
        self.w.applied_training_night = self._applied_night
        self._wall0 = time.monotonic()
        self._game0 = self.w.clock.t
        from .doctor import Doctor
        self.doctor = Doctor(self)
        self._next_save = time.monotonic() + float(rules.persistence.get("save_seconds", 60))
        self._next_record = time.monotonic()
        self._next_doctor = time.monotonic() + float(rules.doctor.get("every_minutes", 10)) * 60
        self._saved_hour = None
        self._report_done = None

    # ------------------------------------------------------------------ helpers
    def _make_frames(self) -> None:
        """The page's frame maker and the timelapse recorder (again after going back to a save)."""
        from .snapshot import FrameMaker
        web = self.rules.web
        self.frames = FrameMaker(self.w, int(web.get("journal_lines", 60)), int(web.get("thoughts_lines", 30)))
        self.recorder = FrameMaker(self.w, 60, 30, own_effects=False)
        models = {c["id"]: str(getattr(self.llms.get(c["id"], self.llm), "model", "")) for c in self.rules.civs}
        self.frames.models = self.recorder.models = models

    def _load_book(self) -> None:
        book = self.book_from(self.store.db, self.rules).accepted()
        for civ in self.w.civs:
            if civ.ai is not None:
                civ.ai.book = list(book)
        self.book_size = len(book)

    def _best_genome(self, civ):
        """A reborn kingdom gets the best genome of its own lineage from last night's training."""
        return self.store.lineage_best(civ.id) or civ.genome

    def model_label(self, civ_id: str | None) -> str:
        """Who was thinking for this kingdom: its model, or "genes only" while that model is away."""
        client = self.llms.get(civ_id, self.llm)
        try:
            up = client.available()
        except Exception:
            up = False
        return str(getattr(client, "model", "?")) if up else "genes only"

    def drain_rivalry(self) -> None:
        """Move the world's scoreboard facts into SQLite, with the model and the genome that played."""
        feed = getattr(self.w, "rivalry_feed", None)
        if not feed:
            return
        rows, self.w.rivalry_feed = list(feed), []
        labels: dict = {}
        for r in rows:
            civ = self.w.civ_by_id(r["civ"])
            if r["civ"] not in labels:
                labels[r["civ"]] = self.model_label(r["civ"])
            r["model"], r["mind"] = labels[r["civ"]], civ.genome.id if civ else None
        self.store.save_rivalry(rows)

    def _comment(self, c: dict) -> None:
        if self.frames is not None:
            self.frames.comments.append(c)

    def target_t(self) -> float:
        if self.realtime:
            return time.time()                    # the real world follows the real clock, whatever happened
        if self.pacing is not None:
            return self.pacing.target(time.monotonic())
        return self._game0 + (time.monotonic() - self._wall0) * self.speed

    def tick_size(self) -> float:
        """1 s ticks in real time; bigger ticks when watching faster (at most fast mode's 10 s)."""
        speed = self.pacing.speed if self.pacing is not None else self.speed
        return min(self.rules.fast_tick, max(self.rules.real_tick, speed / 20))

    def update_pacing(self) -> None:
        if self.pacing is None:
            return
        now = time.monotonic()
        waits = [max(0.0, now - request["wall"]) for request in self.mind.pending_decisions.values()]
        crisis = bool(self.w.military.battles) or any(
            civ.alive and (any(b.on_fire for b in civ.buildings)
                           or (civ.food_rate > 0 and civ.stock["food"] < civ.food_rate * 4 * 3600))
            for civ in self.w.civs)
        info = self.pacing.update(self.w.clock.t, now, waits, crisis)
        for maker in (self.frames, self.recorder):
            if maker is not None:
                maker.pacing = info

    def set_decor(self, on: bool) -> None:
        """The doctor turns the decor off when ticks are slow: fewer frames for the page."""
        self.frame_seconds = float(self.rules.web.get("frame_seconds", 1.0)) * (1 if on else 3)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ recovery (v6)
    def catch_up(self) -> float:
        """Replay the hours the world missed (PC off, crash) in fast mode, without the local AI.
        Returns the hours replayed."""
        missing = self.target_t() - self.w.clock.t
        if missing <= 60:
            return 0.0
        hours = missing / 3600
        self.w.log(None, "recovery", f"The world was away for {hours:.1f} hours; replaying them in fast mode.", 5)
        self.w.run_until(self.target_t(), tick=float(self.rules.persistence.get("catch_up_tick", 10)))
        self.w.log(None, "recovery", f"Caught up: {hours:.1f} hours replayed.", 4)
        return hours

    def restore(self) -> str:
        """The doctor found impossible values: go back to the last good save (at most a minute old)."""
        hooks = self.w.detach_hooks()
        result = "no good save: values repaired in place"
        for name in (persist.LATEST, persist.PREVIOUS):
            path = self.save_dir / name
            if not path.exists():
                continue
            try:
                loaded, _ = persist.read_save(path)
            except Exception:
                continue
            if self.doctor.impossible_values(loaded):
                continue
            self.w.adopt(loaded)
            self._applied_night = getattr(self.w, "applied_training_night", 0)
            result = f"went back to {name}"
            break
        self.w.attach_hooks(hooks)
        if result.startswith("no good"):
            self.doctor.repair_in_place()
        if self.frames is not None:
            self._make_frames()
            if self.on_frame is not None:
                self.on_frame({"type": "reset"})               # pages reload their map
        if self.pacing is not None:
            self.pacing.rebase(self.w.clock.t, time.monotonic())
        return result

    def clients(self) -> list:
        """Every model client in use (the shared one and each kingdom's), once each."""
        out = {id(c): c for c in [self.llm, *getattr(self, "llms", {}).values()] if c is not None}
        return list(out.values())

    def flush_usage(self) -> None:
        """Save what each model was used since the last flush (calls, tokens, seconds), per real day."""
        from datetime import date
        for c in self.clients():
            usage = getattr(c, "usage", None)
            if usage is not None:
                counted = usage.take()
                if counted:
                    self.store.add_model_usage(date.today().isoformat(), counted)

    def save(self) -> None:
        if self.doctor.impossible_values():
            return                                             # never save a broken world
        hour = self.w.clock.local.strftime("%Y-%m-%d %H")
        persist.save_world(self.w, self.save_dir, hourly=hour != self._saved_hour)
        if hour != self._saved_hour and self._saved_hour is not None and hour.endswith(" 03"):
            persist.prune(self.save_dir, hourly_days=int(self.rules.persistence.get("hourly_days", 7)))
        self._saved_hour = hour

    def record(self) -> None:
        """One frame for the 24-hour timelapse."""
        if self.recorder is None:
            return
        f = self.recorder.frame()
        f["nodes_all"] = "".join(chr(48 + q) for q in self.recorder.node_q)
        blob = gzip.compress(json.dumps(f, separators=(",", ":")).encode("utf-8"), 6)
        keep = float(self.rules.timelapse.get("keep_hours", 48)) * 3600
        self.store.record_frame(self.w.clock.t, blob, keep)

    # ------------------------------------------------------------------ the loop
    def advance(self, budget: float = 0.25) -> None:
        """Catch the world up with the clock, spending at most `budget` seconds of CPU."""
        with self.lock:
            self.update_pacing()
        t_end = self.target_t()
        tick = self.tick_size()
        until = time.perf_counter() + budget
        while self.w.clock.t < t_end - 1e-6 and time.perf_counter() < until:
            with self.lock:                                   # short bursts: web requests get their turn
                burst = time.perf_counter() + 0.02
                while self.w.clock.t < t_end - 1e-6 and time.perf_counter() < burst:
                    t0 = time.perf_counter()
                    self.w.step(min(tick, t_end - self.w.clock.t))
                    self.doctor.tick_time((time.perf_counter() - t0) * 1000)
            time.sleep(0.001)
        with self.lock:
            self.drain_rivalry()
            self.schedule()
            self.mind.tick()
            self.update_pacing()

    def new_era(self) -> None:
        """A Wonder won the era some days ago: the world starts again on a new map (same object, so the page,
        the local AI and the doctor keep working); both kingdoms get the best genomes from training."""
        self.drain_rivalry()                                   # the old era's last facts (the Wonder)
        hooks = self.w.detach_hooks()
        genomes = self.store.morning_genomes() or [c.genome for c in self.w.civs]     # each kingdom's own line
        self.w.adopt(self.w.new_era_world(genomes))
        self.w.applied_training_night = self._applied_night
        self.w.attach_hooks(hooks)
        self.w.live = True
        self._load_book()
        self.doctor.seen.clear()
        self.doctor.idle_flags.clear()
        if self.frames is not None:
            self._make_frames()
            if self.on_frame is not None:
                self.on_frame({"type": "reset"})               # pages reload the new map

    def schedule(self) -> None:
        at = getattr(self.w, "new_era_at", None)
        if at is not None and self.w.clock.t >= at:
            self.new_era()
        local = self.w.clock.local
        secs = local.hour * 3600 + local.minute * 60
        ev = self.rules.evolution
        # 00:30 — the night's evolution starts in its own process; the world keeps running
        if self.evolve and self._evolving is None and self._evolved_on != local.date() \
                and hhmm(ev["start"]) <= secs < 6 * 3600:
            self._evolving = subprocess.Popen([sys.executable, str(CLI), "evolve", "--nights", "1",
                                               "--db", str(self.db_path)])
            self._evolved_on = local.date()
            self.w.log(None, "evolution", "The night's evolution has started.", 3)
        if self._evolving is not None and self._evolving.poll() is not None:
            self._evolving = None
        # 07:00 — each kingdom takes the best genome of its own lineage (rivalry)
        if self._evolving is None and secs >= hhmm(ev["morning"]) and self.store.last_night() > self._applied_night:
            best = self.store.morning_genomes()
            from ai import self_mind
            who = self_mind.mode(self.rules)
            if best and who == "llm":                           # the models keep the characters they chose
                self.w.log(None, "genome", "The kingdoms keep the characters their own minds chose.", 3)
            elif best:
                for civ, g in zip(self.w.civs, best):
                    civ.genome = g
                    self.w.log(civ, "genome", f"{civ.name} receives genome {g.id} (F = {g.meta['fitness']:.3f}).", 5)
                    if who == "mixed":
                        civ.mind_next_t = self.w.clock.t       # its model adjusts the night's best now
            self._applied_night = self.store.last_night()
            self.w.applied_training_night = self._applied_night
            self._load_book()                                   # rules accepted overnight
        # Monday 06:15 — the doctor's weekly report, with the Chronicle
        doc = self.rules.doctor
        if local.weekday() == int(doc.get("report_weekday", 0)) and self._report_done != local.date() \
                and secs >= hhmm(self.rules.council.get("chronicle", "06:15")):
            self._report_done = local.date()
            facts = self.doctor.report_facts()

            def apply(result, facts=facts):
                text, by_ai = result
                self.store.save_doctor_report(facts["date"], text, facts["bugs"], by_ai)
                self.w.log(None, "doctor_report", f"The doctor's week: {text}", 4)

            self.mind.submit("doctor_report", lambda: self.doctor.write_report(facts, self.llm), apply)

    def run(self, status_every: float = 60.0) -> None:
        """Blocks until stop() (or Ctrl+C in the caller)."""
        next_frame = time.monotonic()
        next_status = time.monotonic() + status_every
        record_every = float(self.rules.timelapse.get("record_seconds", 120))
        save_every = float(self.rules.persistence.get("save_seconds", 60))
        doctor_every = float(self.rules.doctor.get("every_minutes", 10)) * 60
        try:
            while not self._stop.is_set():
                self.advance()
                now = time.monotonic()
                if self.frames is not None and self.on_frame is not None and now >= next_frame:
                    with self.lock:
                        frame = self.frames.frame()
                    self.on_frame(frame)
                    next_frame = now + self.frame_seconds
                if now >= self._next_save:
                    with self.lock:
                        if self.persistent:
                            self.save()
                        self.flush_usage()                     # what each model cost this minute
                    self._next_save = now + save_every
                if self.recorder is not None and now >= self._next_record:
                    with self.lock:
                        self.record()
                    self._next_record = now + record_every
                if now >= self._next_doctor:
                    with self.lock:
                        if self.doctor.check() == "restore":
                            bad = self.doctor.impossible_values()
                            how = self.restore()
                            self.doctor.note("values", None, "impossible values: " + "; ".join(bad[:3]), how)
                    self._next_doctor = now + doctor_every
                if self.on_status is not None and now >= next_status:
                    self.on_status(self.status())
                    next_status = now + status_every
                ahead = self.w.clock.t - self.target_t()
                current_speed = self.pacing.speed if self.pacing is not None else self.speed
                wait = min(next_frame - time.monotonic(), ahead / current_speed if ahead > 0 else 0.0, 0.2)
                self._stop.wait(max(0.005, wait))
        finally:
            if self.persistent:
                with self.lock:
                    self.save()                                # a clean stop saves the last minute too
            self.flush_usage()
            self.mind.close()
            from ai import lookahead
            lookahead.shutdown()                               # the futures' worker process

    def status(self) -> str:
        from cli import status_line
        with self.lock:
            return status_line(self.w)

