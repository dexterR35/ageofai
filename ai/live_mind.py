"""Version 3 in the live world: council, memory, reflection, chronicle and referee on their schedule.

The world never waits for the GPU: game state is read on the main thread, the AI calls run on one
background thread (one GPU = one call at a time), and results are applied back on the main thread.
If Ollama is down, everything here quietly does nothing and the tactical layer keeps playing.

Rivalry: each kingdom may think with its own model (rules.yaml → llm.models). Its council, its diplomat
and its evening reflection use that kingdom's client; the radio, the Chronicle and the referee use the
shared one. The calls still go one at a time (Ollama swaps the models); a kingdom whose model is slow or
missing simply plays on its genes until it answers again.
"""
from __future__ import annotations

import json
import logging
import random
import time
from concurrent.futures import Future, ThreadPoolExecutor

from typing import Callable

from engine.rules import hhmm

from . import chronicle as chron
from . import referee
from .council import Council
from .llm import LLM, LLMError
from . import self_mind
from .memory import MemoryBank

REFLECT_SYSTEM = """You are the king of a small medieval kingdom in a simulation. It is evening.
In 1 to 3 sentences, write what you learned today that you want to remember. Use only the facts given.
Answer ONLY with JSON: {"reflection": "..."}"""

DIPLOMAT_SYSTEM = """You speak for the king of {kingdom}, a medieval kingdom in a simulation; your neighbour is {other}.
Write at most ONE message to them, or none. Types: "trade" (give and ask resources), "gift" (give), "tribute" (ask,
a demand backed by your army), "peace" (a treaty for "days" 3-30), "war" (a declaration), "free" (words only), "none".
Resources: food, wood, stone, ore, gold, coins. You may bluff in the text, but the goods and treaties you promise are
tracked, and breaking a promise costs trust. At most 300 characters of text, plain English.
Answer ONLY with JSON: {{"type": "...", "give": {{}}, "ask": {{}}, "days": 0, "text": "..."}}"""

ANSWER_SYSTEM = """You speak for the king of {kingdom}, a medieval kingdom in a simulation. {other} sent you the message
below. Decide whether to accept. Accepting a trade means you must pay what they ask when their cart arrives (not paying
breaks the promise: their trust in you falls); accepting a tribute means you pay now; accepting peace starts a treaty
for those days (attacking during it is possible but costs trust and relation).
Answer ONLY with JSON: {{"accept": true or false, "text": "<your reply, at most 200 characters>"}}"""

REFEREE_SYSTEM = """You are the referee of a medieval simulation. Explain this week's balance changes to the players
in 2-4 plain English sentences. Use only the facts given. Answer ONLY with JSON: {"explanation": "..."}"""


class LiveMind:
    def __init__(self, world, store, llm: LLM, commentator=None, painter=None,
                 on_comment: Callable[[dict], None] | None = None, llms: dict[str, LLM] | None = None,
                 recover_morning: bool = False, game_speed: float = 1.0):
        self.w = world
        self.store = store
        self.llm = llm
        self.llms = dict(llms or {})              # civ id → that kingdom's model (rivalry); default: the shared one
        self.commentator = commentator          # version 4: the radio
        self.painter = painter                  # version 4: pictures of the day
        self.on_comment = on_comment
        self.portrait_subject = None            # the Chronicle's person of the day, for the painter
        self.rules = world.rules
        self.game_speed = game_speed
        from .notify import Notifier
        self.notifier = Notifier(self.rules.notify)   # version 7: phone notifications (off by default)
        cfg = self.rules.council
        self.urgent_delay = float(cfg.get("urgent_cooldown_minutes", 1)) * 60
        self.every = cfg.get("every_minutes", 30) * 60
        self.directive_seconds = cfg.get("directive_hours", 1) * 3600
        self.urgent = set(cfg.get("urgent_kinds", []))
        self.urgent_all = set(cfg.get("urgent_diplomacy", []))   # both kings reconsider (attacks, treaties…)
        self.memory = MemoryBank(store.db, llm)
        self.councils = {civ.id: Council(self.llm_of(civ.id), self.memory) for civ in world.civs}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ollama")
        self.jobs: list[tuple[Future, Callable]] = []          # (future, apply on main thread)
        self.job_keys: dict[Future, str] = {}
        self.busy: set[str] = set()
        self.pending_decisions: dict[str, dict] = {}
        self.situation_version = {civ.id: 0 for civ in world.civs}
        self.urgent_councils: set[str] = set()
        self.next_council = {civ.id: world.clock.t for civ in world.civs}
        self.done_today: dict[str, object] = {}
        # a world that starts at 09:00 does not run the 06:15 Chronicle or the 06:25 pictures right away
        local = world.clock.local
        now_s = local.hour * 3600 + local.minute * 60
        for key, at in (("reflection", cfg.get("reflection", "22:00")), ("chronicle", cfg.get("chronicle", "06:15")),
                        ("publish", cfg.get("publish", "07:00")), ("referee", cfg.get("referee_time", "06:05")),
                        ("balance", cfg.get("publish", "07:00")), ("audio_clean", "04:00"),
                        ("pictures", self.rules.images.get("at", "06:25"))):
            if now_s >= hhmm(at):
                self.done_today[key] = local.date()
        self.chronicle_ready = None
        if recover_morning and now_s >= hhmm(cfg.get("chronicle", "06:15")):
            today = local.date().isoformat()
            saved = self.store.db.execute("SELECT * FROM chronicles WHERE date=?", (today,)).fetchone()
            if saved is None:
                # A morning crash or an offline PC must not skip the whole day's edition.
                self.done_today.pop("chronicle", None)
                self.done_today.pop("publish", None)
            elif getattr(world, "chronicle_published_on", None) != today:
                # The draft survived in SQLite, even if the process stopped before publication.
                self.chronicle_ready = chron.Chronicle(today, saved["headline"], [], "",
                                                       json.loads(saved["table_json"]), bool(saved["by_ai"]), 0)
                self.done_today.pop("publish", None)
        previous = world.journal.sink

        self.memory_queue: list = []                  # events waiting for their vectors (see tick)

        def sink(ev):
            if previous:
                previous(ev)
            if self.memory.wants(ev):                 # never call the local AI on the game thread
                self.memory_queue.append(ev)
            civ = self.w.civ_by_id(ev.civ_id)
            self.notifier.event(ev, self.w.clock.date.isoformat(), civ.name if civ else None)
            if self.commentator is not None:
                self.commentator.hear(ev)
            if ev.civ_id and ev.kind in self.urgent:
                self.next_council[ev.civ_id] = min(self.next_council.get(ev.civ_id, 0), self.w.clock.t + self.urgent_delay)
            if ev.kind in self.urgent_all:
                for c in self.w.civs:
                    self.next_council[c.id] = min(self.next_council.get(c.id, 0), self.w.clock.t + self.urgent_delay)
            if ev.kind in self.urgent or ev.kind in self.urgent_all:
                affected = [c.id for c in self.w.civs] if ev.kind in self.urgent_all else [ev.civ_id]
                for cid in affected:
                    if cid in self.situation_version:
                        self.situation_version[cid] += 1
                        self.urgent_councils.add(cid)

        world.journal.sink = sink
        if world.diplomat is not None:                # version 5: the local AI speaks for the kings
            world.diplomat.writer = self.write_message
            world.diplomat.answerer = self.answer_message

    # ------------------------------------------------------------------ helpers
    def by_network(self, civ) -> bool:
        """This kingdom is played by a trained network (rules.yaml → rl.civs): no LLM council or mind for it."""
        from . import rl
        return rl.policy_for(self.rules, civ.id) is not None

    def llm_of(self, civ_id: str | None) -> LLM:
        """The model this kingdom thinks with."""
        return self.llms.get(civ_id, self.llm) if civ_id else self.llm

    def model_name(self, civ_id: str | None) -> str:
        return str(getattr(self.llm_of(civ_id), "model", "?"))

    def council_interval(self, thinking_seconds: float) -> float:
        cfg = self.rules.council
        if not cfg.get("adaptive", False):
            return self.every
        kingdoms = sum(c.alive for c in self.w.civs)
        measured = max(0.0, thinking_seconds) * self.game_speed * max(1, kingdoms) * 1.25
        return max(self.every, min(float(cfg.get("max_interval_hours", 24)) * 3600, measured))

    def submit(self, key: str, work: Callable[[], object], apply: Callable[[object], None]) -> None:
        self.busy.add(key)
        tracked = key.startswith(("council:", "dip:", "answer:", "mind:"))
        request = {"wall": time.monotonic(), "game": self.w.clock.t}
        if tracked:
            self.pending_decisions[key] = request

        def perform():
            maximum = self.rules.council.get("decision_max_real_seconds")
            if tracked and maximum is not None and time.monotonic() - request["wall"] > float(maximum):
                return None
            return work()

        fut = self.pool.submit(perform)
        self.job_keys[fut] = key

        def finish(result, key=key):
            self.busy.discard(key)
            self.pending_decisions.pop(key, None)
            if tracked:
                cfg = self.rules.council
                game_limit = cfg.get("decision_max_game_hours")
                wall_limit = cfg.get("decision_max_real_seconds")
                expired = ((game_limit is not None and self.w.clock.t - request["game"] > float(game_limit) * 3600)
                           or (wall_limit is not None and time.monotonic() - request["wall"] > float(wall_limit)))
                if expired:
                    self.w.log(None, "stale_decision", "An outdated AI answer was discarded; current tactical logic continues.", 3)
                    if key.startswith("council:"):
                        self.next_council[key.split(":", 1)[1]] = self.w.clock.t
                        self.urgent_councils.add(key.split(":", 1)[1])
                    elif key.startswith("mind:"):
                        civ = self.w.civ_by_id(key.split(":", 1)[1])
                        if civ is not None and civ.alive:
                            civ.mind_next_t = self.w.clock.t + self.urgent_delay
                    elif key.startswith("answer:"):
                        # Resolve an old offer using today's state rather than
                        # leave it pending after discarding the model's answer.
                        apply(None)
                    return
            apply(result)

        self.jobs.append((fut, finish))

    def once_a_day(self, key: str, at: str) -> bool:
        """True the first time we pass `at` (HH:MM) today."""
        local = self.w.clock.local
        if local.hour * 3600 + local.minute * 60 < hhmm(at):
            return False
        if self.done_today.get(key) == local.date():
            return False
        self.done_today[key] = local.date()
        return True

    # ------------------------------------------------------------------ main-thread tick
    def tick(self) -> None:
        for fut, apply in list(self.jobs):
            if fut.done():
                self.jobs.remove((fut, apply))
                key = self.job_keys.pop(fut, None)
                if key is not None:
                    self.busy.discard(key)
                    self.pending_decisions.pop(key, None)
                if fut.cancelled():
                    continue
                try:
                    apply(fut.result())
                except Exception as e:                       # an AI hiccup must never stop the world
                    if key is not None and key.startswith("council:"):
                        self.next_council[key.split(":", 1)[1]] = self.w.clock.t + self.urgent_delay
                    self.store.db.rollback()
                    try:
                        self.w.log(None, "ai_error", f"Local AI step failed: {e}", 1)
                    except Exception:
                        self.store.db.rollback()
                        logging.getLogger(__name__).exception("Could not record local AI failure: %s", e)
        now = self.w.clock.t
        if self.memory_queue and "memory" not in self.busy:
            self.start_memories()
        # version 4 — these work without Ollama too (plain lines, ComfyUI pictures)
        if self.commentator is not None and "comment" not in self.busy:
            line = self.commentator.next_line(now, self.w.clock.local)
            if line is not None:
                self.start_comment(line)
        if self.commentator is not None and self.once_a_day("audio_clean", "04:00"):
            self.commentator.clean_audio()
        if self.painter is not None and self.once_a_day("pictures", self.rules.images.get("at", "06:25")):
            self.start_pictures()
        cfg = self.rules.council
        # Recover the daily edition before queuing the kingdoms' longer councils.
        if self.once_a_day("chronicle", cfg.get("chronicle", "06:15")):
            self.start_chronicle()
        if self.chronicle_ready is not None and self.once_a_day("publish", cfg.get("publish", "07:00")):
            ch = self.chronicle_ready
            self.chronicle_ready = None
            self.w.log(None, "chronicle", f"The Chronicle is out: {ch.headline}", 7)
            self.notifier.chronicle(ch.headline, self.w.clock.date.isoformat())
            self.w.chronicle_published_on = ch.date
        up = {civ.id: self.llm_of(civ.id).available() for civ in self.w.civs if civ.alive}
        for civ in sorted(self.w.civs, key=lambda c: (c.id not in self.urgent_councils,
                                                     self.next_council.get(c.id, now))):
            if sum(key.startswith("council:") for key in self.busy) >= int(cfg.get("max_pending", len(self.w.civs))):
                break
            if civ.alive and up.get(civ.id) and now >= self.next_council.get(civ.id, now) \
                    and f"council:{civ.id}" not in self.busy and not self.by_network(civ):
                self.start_council(civ)
        if self_mind.mode(self.rules) != "genes":    # each kingdom's model chooses its own character
            for civ in self.w.civs:
                if civ.alive and up.get(civ.id) and now >= (getattr(civ, "mind_next_t", None) or 0.0) \
                        and f"mind:{civ.id}" not in self.busy and not self.by_network(civ):
                    self.start_mind(civ)
        if any(up.values()) and self.once_a_day("reflection", cfg.get("reflection", "22:00")):
            for civ in self.w.civs:
                if civ.alive and up.get(civ.id):
                    self.start_reflection(civ)
        if not self.llm.available():
            return
        if self.w.clock.local.weekday() == cfg.get("referee_weekday", 6) and \
                self.once_a_day("referee", cfg.get("referee_time", "06:05")):
            self.start_referee()
        if self.w.clock.local.weekday() == 0 and self.once_a_day("balance", cfg.get("publish", "07:00")):
            if self.rules.apply_balance(referee.load_balance()):
                self.w.log(None, "referee", "The referee's new costs are in effect.", 5)

    # ------------------------------------------------------------------ memories (events with I ≥ 5)
    def start_memories(self, batch: int = 32) -> None:
        """Vectors for the waiting events on the AI thread, one call for many; saved on the main thread."""
        events, self.memory_queue = self.memory_queue[:batch], self.memory_queue[batch:]
        texts = [self.memory.event_text(ev) for ev in events]

        def apply(vecs):
            for ev, text, vec in zip(events, texts, vecs):
                self.memory.add(ev.civ_id, ev.t, ev.day, ev.kind, text, ev.importance, vec=vec, embed=False)

        self.submit("memory", lambda: self.memory.embed_many(texts), apply)

    # ------------------------------------------------------------------ the kingdom's own character
    def start_mind(self, civ) -> None:
        """At birth and about once a week: the kingdom's model reads its situation and its genes and changes
        the ones it wants (code clamps them). No advice is given; it decides from what happened."""
        now = self.w.clock.t
        civ.mind_next_t = now + float(self.rules.llm.get("mind_every_days", 7)) * 86400
        try:
            score = self.store.rivalry("civ").get(civ.id, {})
        except Exception:                                   # no scoreboard yet
            score = {}
        ctx = self_mind.prepare(self.w, civ, score)
        llm = self.llm_of(civ.id)
        model = str(getattr(llm, "model", "model"))

        def apply(raw):
            if civ not in self.w.civs or not civ.alive:     # a new era or a fall in the meantime
                return
            if raw is None:                                  # the model did not answer: ask again in an hour
                civ.mind_next_t = self.w.clock.t + 3600
                return
            g, changes, reason = self_mind.apply(raw, civ, self.rules, model, ctx)
            if g is None:
                self.w.log(civ, "mind", f"{civ.name}'s mind ({model}) keeps its character." +
                           (f" {reason}" if reason else ""), 3)
                return
            civ.genome = g
            civ.dirty = True
            self.w.log(civ, "mind", f"{civ.name}'s mind ({model}) changes its character: {', '.join(changes)}."
                       + (f" {reason}" if reason else ""), 6)

        self.submit(f"mind:{civ.id}", lambda: self_mind.ask(llm, ctx), apply)

    # ------------------------------------------------------------------ council
    def start_council(self, civ) -> None:
        council = self.councils.get(civ.id)
        if council is None or council.llm is not self.llm_of(civ.id):
            council = self.councils[civ.id] = Council(self.llm_of(civ.id), self.memory,
                                                      council.weights if council else None)
        self.next_council[civ.id] = self.w.clock.t + self.every
        self.urgent_councils.discard(civ.id)
        ctx = council.prepare(self.w, civ, self.store.lessons())
        situation_version = self.situation_version[civ.id]

        def apply(result):
            if not result:
                self.next_council[civ.id] = self.w.clock.t + self.urgent_delay
                return
            if civ not in self.w.civs or not civ.alive:
                return
            if (self.rules.council.get("decision_max_game_hours") is not None
                    and self.situation_version[civ.id] != situation_version):
                self.next_council[civ.id] = self.w.clock.t
                self.urgent_councils.add(civ.id)
                self.w.log(civ, "stale_decision", "The situation changed while the council was thinking; a fresh council is needed.", 3)
                return
            d = council.finish(ctx, result, civ)
            interval = self.council_interval(d.seconds)
            self.next_council[civ.id] = max(ctx["t"] + interval, self.w.clock.t + self.urgent_delay)
            civ.ai.set_directive(d.plan, self.w.clock.t + max(self.directive_seconds, interval))
            civ.ai.thoughts.append((self.w.clock.t, f"The king: {d.plan.thought}"))
            self.store.save_council(d)
            who = f" (listened to the {d.plan.listened_to})" if d.plan.listened_to else ""
            self.w.log(civ, "council", f"The king decided: {d.plan.thought}{who}", 3)
            self.war_intent(civ, d.plan)
            if d.plan.culture is not None:
                from engine import development
                development.record_culture(self.w, civ, d.plan.culture)
            if d.plan.appointments is not None:
                from engine import development
                development.appoint(self.w, civ, d.plan.appointments)
            if d.plan.invention is not None:
                from engine import development
                ok, reason = development.propose(self.w, civ, d.plan.invention, self.model_name(civ.id))
                if not ok:
                    self.w.log(civ, "research_deferred", f"Research proposal deferred: {reason}.", 2)

        self.submit(f"council:{civ.id}", lambda: council.run(ctx), apply)

    def war_intent(self, civ, plan) -> None:
        """Say the king's war decision in the journal; "seek_peace" sends a peace offer right away
        (the other king's model, or its genes, answers). "attack" is carried out by the army planner."""
        target = self.w.civ_by_id(plan.target) if plan.target else None
        why = f" {plan.war_reason}" if plan.war_reason else ""
        if plan.intent == "attack" and target is not None:
            self.w.log(civ, "war_intent", f"The king of {civ.name} wants war on {target.name}.{why}", 6)
        elif plan.intent == "seek_peace" and target is not None:
            self.w.log(civ, "war_intent", f"The king of {civ.name} seeks peace with {target.name}.{why}", 5)
            if self.w.diplomat is not None:
                self.w.diplomat.offer_peace(civ, target, plan.war_reason)
        elif plan.intent == "defend":
            self.w.log(civ, "war_intent", f"The king of {civ.name} keeps the army home.{why}", 3)

    # ------------------------------------------------------------------ reflection (22:00 → memory, I = 8)
    def start_reflection(self, civ) -> None:
        since = self.w.clock.t - 86400
        facts = [e.text for e in self.w.journal.events if e.civ_id == civ.id and e.t >= since and e.importance >= 4][-25:]
        if not facts:
            return
        payload = json.dumps({"kingdom": civ.name, "today": facts}, ensure_ascii=False)

        day = self.w.day
        llm = self.llm_of(civ.id)

        def work():
            try:
                text = llm.chat_json(REFLECT_SYSTEM, payload).get("reflection")
            except LLMError:
                return None
            if not text:
                return None
            text = f"Day {day}: {str(text)[:400]}"
            return text, self.memory.embed_many([text])[0]

        def apply(result):
            if result:
                text, vec = result
                self.memory.add(civ.id, self.w.clock.t, day, "reflection", text, 8, vec=vec, embed=False)

        self.submit(f"reflect:{civ.id}", work, apply)

    # ------------------------------------------------------------------ chronicle (06:15, out at 07:00)
    def start_chronicle(self) -> None:
        facts = chron.gather_facts(self.w, self.w.clock.t - 86400)
        self.portrait_subject = facts["subject"]

        def apply(ch):
            try:
                self.store.save_chronicle(ch)
            except Exception:
                self.done_today.pop("chronicle", None)
                raise
            self.chronicle_ready = ch

        self.submit("chronicle", lambda: chron.compose(facts, self.llm), apply)

    # ------------------------------------------------------------------ referee (Sunday 06:05)
    def start_referee(self) -> None:
        from engine import formulas as F

        last = self.store.last_night()
        counts = self.store.build_stats(max(1, last - 6), last)
        seen = set(self.store.build_stats(max(1, last - 13), last))
        pop = self.store.population(last)
        d = F.diversity([g.genes for g in pop], self.rules.gene_ranges) if len(pop) > 1 else 1.0
        balance = referee.load_balance()
        verdict = referee.review(counts, seen, d, balance, self.w.clock.local.date())
        payload = json.dumps({"changes": verdict.changes, "diversity": round(d, 3)}, ensure_ascii=False)

        def work():
            if not verdict.changes:
                return "No changes this week: the game looks balanced."
            try:
                return self.llm.chat_json(REFEREE_SYSTEM, payload).get("explanation", "")
            except LLMError:
                return "; ".join(f"{c['what']}: {c['reason']}" for c in verdict.changes)

        def apply(text):
            referee.save(verdict, balance, str(text or ""))
            self.w.log(None, "referee", f"The referee: {text}", 5)

        self.submit("referee", work, apply)

    # ------------------------------------------------------------------ radio (version 4)
    def start_comment(self, line) -> None:
        def apply(c):
            if c is None:
                return
            self.store.save_comment(c.t, c.civ, c.text, c.audio, c.by_ai)
            if self.on_comment is not None:
                self.on_comment(c.as_dict())

        self.submit("comment", lambda: self.commentator.write(line), apply)

    # ------------------------------------------------------------------ pictures (06:25, version 4)
    def start_pictures(self) -> None:
        from .images import daily_pictures, pick_scenes

        cfg = self.rules.images
        now = self.w.clock.t
        scenes = pick_scenes(self.w.journal.events, now - 86400, int(cfg.get("scenes_max", 3)),
                             float(cfg.get("scene_min_importance", 7)))
        subject = self.portrait_subject or chron.portrait_subject(self.w, random.Random(int(now)))
        date = self.w.clock.date.isoformat()
        season = self.w.clock.season.name
        colors = {c.id: c.color for c in self.w.civs}
        names = {c.id: c.name for c in self.w.civs}

        def apply(rows):
            for r in rows or []:
                self.store.save_image(r)
            if rows:
                self.w.log(None, "pictures", f"The painter finished {len(rows)} picture(s) for {date}.", 2)

        others = [c for c in {id(c): c for c in self.llms.values()}.values() if c is not self.llm]

        def work():
            for c in others:                              # every kingdom's model leaves the GPU, not just one
                if hasattr(c, "unload"):
                    c.unload()
            return daily_pictures(self.painter, self.llm, date, subject, scenes, season, colors, names)

        self.submit("pictures", work, apply)

    # ------------------------------------------------------------------ diplomacy (version 5)
    def _dip_facts(self, civ, other) -> dict:
        dip = self.w.diplomacy
        d = self.w.diplomat
        return {"our_stock": {k: round(v) for k, v in civ.stock.items()},
                "we_need": d.needs(civ)[:3], "we_have_spare": [k for k, _ in d.surplus(civ)][:3],
                "relation_R": round(dip.rel(civ.id, other.id)), "state": dip.state(civ.id, other.id),
                "our_trust_in_them": round(dip.trust(civ.id, other.id), 2), "treaty": dip.treaty(civ.id, other.id),
                "at_war": dip.at_war(civ.id, other.id), "peace_period": dip.peace_period(),
                "our_soldiers": len(civ.soldiers),
                "their_soldiers_seen": civ.intel.get(other.id, {}).get("soldiers"),
                "our_strength_vs_theirs": round(civ.ai.army.ratio(other), 2) if civ.ai else None,
                "recent_messages": [f"{m.src} → {m.dst} ({m.type}, {m.status}): {m.text}"
                                    for m in dip.recent(civ.id, other.id, 6)]}

    def write_message(self, civ, other) -> bool:
        llm = self.llm_of(civ.id)
        if not llm.available() or f"dip:{civ.id}" in self.busy:
            return False
        system = DIPLOMAT_SYSTEM.format(kingdom=civ.name, other=other.name)
        payload = json.dumps(self._dip_facts(civ, other), ensure_ascii=False)

        def work():
            try:
                return llm.chat_json(system, payload)
            except LLMError:
                return None

        def apply(raw):
            if not civ.alive or not other.alive:
                return
            if not raw or str(raw.get("type", "none")).lower() == "none":
                return
            sent = self.w.diplomat.send(civ, other, {**raw, "by_ai": True})
            if sent is None:                            # the code refused it: the genes speak instead
                msg = self.w.diplomat.compose(civ, other)
                if msg:
                    self.w.diplomat.send(civ, other, msg)

        self.submit(f"dip:{civ.id}", work, apply)
        return True

    def answer_message(self, m) -> bool:
        llm = self.llm_of(m.dst)
        if not llm.available():
            return False
        me, them = self.w.civ_by_id(m.dst), self.w.civ_by_id(m.src)
        system = ANSWER_SYSTEM.format(kingdom=me.name, other=them.name)
        payload = json.dumps({"their_message": m.as_dict(), **self._dip_facts(me, them)}, ensure_ascii=False)

        def work():
            try:
                return llm.chat_json(system, payload)
            except LLMError:
                return None

        def apply(raw):
            if m.status != "sent" or not me.alive or not them.alive:
                return
            if raw is None or "accept" not in raw:
                accept, text = self.w.diplomat.decide(m)
                self.w.diplomacy.answer(m, accept, text)
                return
            accept = raw.get("accept") is True or str(raw.get("accept")).lower() == "true"
            if accept and m.type in ("trade", "tribute") and not me.can_afford(m.ask):
                accept = False
            self.w.diplomacy.answer(m, accept, str(raw.get("text", ""))[:200], by_ai=True)

        self.submit(f"answer:{m.id}", work, apply)
        return True

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.notifier.close()
