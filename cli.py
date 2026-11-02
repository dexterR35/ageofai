"""Command-line entry point.

    python cli.py fast --days 150 --seed 1            # fast mode: simulate 150 days in seconds/minutes
    python cli.py fast --days 30 --journal            # print every event as it happens
    python cli.py live                                # real time: 1 game second = 1 real second
    python cli.py serve                               # real time + the browser view (localhost:8000)
    python cli.py doctor                              # what the world doctor repaired this week
    python cli.py evolve --nights 5                   # nights of evolution, now
    python cli.py league                              # Hall of Champions and night history
    python cli.py bench --days 5                      # measure simulation speed
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime

from engine.entities import format_event
from engine.rules import default_rules
from engine.world import World

RES_SHORT = {"food": "fo", "wood": "wo", "stone": "st", "ore": "or", "gold": "go", "coins": "co"}


def parse_start(s: str | None, tz) -> datetime:
    if not s:
        return datetime.now(tz)
    d = datetime.fromisoformat(s)
    return d.replace(tzinfo=tz) if d.tzinfo is None else d


def status_line(w: World) -> str:
    out = [f"d{w.day:>3} {w.clock.date:%d %b} {w.clock.season.name[:3]}"]
    for civ in w.civs:
        stock = " ".join(f"{RES_SHORT[r]}{v:>5.0f}" for r, v in civ.stock.items())
        out.append(f"| {civ.name:<5} L{civ.castle_level} pop{civ.pop:>2} mo{civ.morale:.2f} {stock}")
    return " ".join(out)


def milestone_report(w: World) -> list[str]:
    """Version 1 is done when a civilization survives its first winter and reaches castle 4 in fast mode."""
    lines = []
    tz = w.clock.tz
    for civ in w.civs:
        reached = {lvl: datetime.fromtimestamp(t, tz).date() for lvl, t in civ.castle_reached.items()}
        castle = ", ".join(f"L{lvl}: day {(d - w.start_date).days + 1}" for lvl, d in sorted(reached.items()))
        lines.append(f"{civ.name}: castle {civ.castle_level} ({castle}); population {civ.pop}; "
                     f"left {civ.departures + civ.fled}, died {civ.deaths}, born {civ.births}; morale {civ.morale:.2f}")
        gathered = ", ".join(f"{r} {v:.0f}" for r, v in civ.gathered.items() if r != "coins")
        lost = ", ".join(f"{r} {v:.0f}" for r, v in civ.lost.items() if v > 1)
        lines.append(f"    gathered: {gathered}" + (f" | lost (storage full): {lost}" if lost else ""))
        tools = ", ".join(f"{c}={lvl}" for c, lvl in civ.tools.items() if lvl)
        blds: dict[str, int] = {}
        for b in civ.buildings:
            if b.built:
                blds[b.type] = blds.get(b.type, 0) + 1
        lines.append(f"    buildings: {blds}" + (f" | tools: {tools}" if tools else ""))
        winters = [row for row in civ.daily if row["date"][5:7] in ("12", "01", "02")]
        through_winter = bool(winters) and any(row["date"][5:] >= "03-01" for row in civ.daily
                                               if row["date"] > winters[0]["date"])
        ok = through_winter and civ.alive and civ.departures == 0 and civ.castle_level >= 4
        lines.append(f"    → first winter survived: {'yes' if through_winter and civ.alive else 'no'}; "
                     f"castle 4: {'yes' if civ.castle_level >= 4 else 'no'}; version 1 done: {'YES' if ok else 'not yet'}")
    return lines


def cmd_fast(args) -> int:
    from engine.rules import Rules
    profile = getattr(args, "profile", "classic")
    rules = default_rules() if profile == "classic" else Rules.profile(profile)
    w = World(rules, seed=args.seed, start=parse_start(args.start, None), echo=args.journal)
    t0 = time.perf_counter()
    every = max(1, args.every)

    def on_day(world: World) -> None:
        if (world.day - 1) % every == 0 and not args.quiet:
            print(status_line(world), flush=True)

    w.run(args.days * 86400, on_day=on_day)
    dt = time.perf_counter() - t0
    print(f"\n{args.days} days simulated in {dt:.1f} s ({w.ticks} ticks, "
          f"{1000 * dt / max(1, w.ticks):.3f} ms/tick, path searches {w.paths.searches}, "
          f"from cache {w.paths.hits})")
    for line in milestone_report(w):
        print(line)
    if args.thoughts:
        for civ in w.civs:
            print(f"\nLast thoughts — {civ.name}:")
            for t, text in list(civ.ai.thoughts)[-args.thoughts:]:
                print(f"  {datetime.fromtimestamp(t, w.clock.tz):%d %b %H:%M}  {text}")
    if args.events:
        print("\nImportant events:")
        for ev in w.journal.events:
            if ev.importance >= args.events:
                print("  " + format_event(ev, w.clock.tz))
    return 0


def make_runner(args, frames: bool, echo: bool):
    from server.runner import LiveRunner

    start = parse_start(args.start, None) if getattr(args, "start", None) else None
    models = {}
    for selection in getattr(args, "model", []):
        kingdom, separator, model = selection.partition("=")
        if not separator or not kingdom or not model.strip():
            raise ValueError("Use --model kingdom=model-name")
        models[kingdom] = model
    r = LiveRunner(db=args.db, seed=args.seed, speed=getattr(args, "speed", None), start=start,
                   evolve=not getattr(args, "no_evolve", False), echo=echo,
                   pictures=not getattr(args, "no_pictures", False), frames=frames,
                   persistent=False if getattr(args, "no_save", False) else None,
                   new_world=getattr(args, "new", False), profile=getattr(args, "profile", "classic"), models=models)
    if r.recovered:
        print(f"Continuing the saved world ({r.recovered}); replayed {r.caught_up:.1f} missing hours.", flush=True)
    elif r.persistent:
        print(f"A new world is born; it is saved every minute ({r.save_dir}).", flush=True)
    if r.genomes:
        print(f"Genomes from night {r.store.last_night()}: {r.genomes[0].id}, {r.genomes[1].id}")
    print(f"Rules in force: {r.book_size}. Local AI ({r.llm.model}): "
          f"{'ready' if r.llm.available() else 'not available'}.")
    speed = "real time" if r.speed == 1 else f"{r.speed:g}× real time"
    print(f"The world runs in {speed} ({r.w.clock}).", flush=True)
    return r


def cmd_live(args) -> int:
    r = make_runner(args, frames=False, echo=True)
    r.on_status = lambda line: print(line, flush=True)
    print("Ctrl+C to stop.")
    try:
        r.run()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from server.app import create_app

    r = make_runner(args, frames=True, echo=args.journal)
    r.on_status = lambda line: print(line, flush=True)
    web = r.rules.web
    host = args.host or web.get("host", "127.0.0.1")
    port = args.port or int(web.get("port", 8000))
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"Open http://{shown}:{port} in a browser. Ctrl+C to stop.", flush=True)
    app = create_app(r)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    # A failed simulation must stop the web server too, so start_world.ps1 can
    # restart from the latest save instead of serving a frozen map forever.
    app.state.on_world_error = lambda: setattr(server, "should_exit", True)
    server.run()
    print("Stopped.")
    return 0


def streak_of_gains(values: list[float]) -> int:
    """Longest run of nights where the score went up compared to the night before."""
    best = cur = 0
    for a, b in zip(values, values[1:]):
        cur = cur + 1 if b > a else 0
        best = max(best, cur)
    return best


def print_nights(store) -> None:
    nights = store.nights()
    if not nights:
        print("No nights yet.")
        return
    print(f"{'night':>5} {'best':>8} {'F best':>7} {'F mean':>7} {'bench':>8} {'F bench':>8} "
          f"{'diversity':>9} {'games':>6} {'time':>7}")
    for n in nights:
        bench = n.get("bench_fitness")
        print(f"{n['night']:>5} {n['best_id']:>8} {n['best_fitness']:>7.4f} {n['mean_fitness']:>7.4f} "
              f"{n.get('bench_id') or '-':>8} {bench if bench is not None else float('nan'):>8.4f} "
              f"{n['diversity']:>9.3f} {n['games']:>6} {n['seconds'] / 60:>6.1f}m")
    from ai.evolution import comparable_bench
    benched = comparable_bench(nights)                 # only nights with the same benchmark settings compare
    every = [n for n in nights if n.get("bench_fitness") is not None]
    if len(benched) < len(every):
        print(f"(The benchmark's settings changed: the streak counts only the last {len(benched)} nights.)")
    streak = streak_of_gains(benched)
    print(f"Longest rise of the benchmark score: {streak} nights in a row "
          f"(version 2 is done at 5: {'YES' if streak >= 5 else 'not yet'}).")
    from ai.evolution import stall_nights
    stalled = stall_nights(nights)
    limit = int(default_rules().evolution_watch.get("stall_nights", 21))
    if stalled >= limit:
        print(f"The benchmark has not risen for {stalled} nights: the doc says it is time to consider "
              f"reinforcement learning (version 7+).")


def cmd_evolve(args) -> int:
    from ai.evolution import Evolution
    from ai.llm import client_from_rules
    from engine.store import Store

    store = Store(args.db)
    llm = None
    if not args.no_llm:
        llm = client_from_rules(default_rules())
        if not llm.available():
            print(f"Local AI ({llm.model}) not available: no new rules or AI lessons tonight.")
            llm = None
    evo = Evolution(store, seed=args.seed, processes=args.processes, population=args.population,
                    days=args.days, peer_games=args.peer_games, champion_games=args.champion_games, llm=llm)
    for _ in range(args.nights):
        night = store.last_night() + 1
        t0 = time.perf_counter()

        def progress(done: int, total: int) -> None:
            if done == total or done % max(1, total // 10) == 0:
                el = time.perf_counter() - t0
                print(f"  night {night}: {done}/{total} games ({el / 60:.1f} min)", flush=True)

        if args.rules_only:
            rep = evo.run_rule_night(progress)
            print(f"Rule night {rep.night}: champion {rep.best_id}, {rep.games} games, {rep.seconds / 60:.1f} min",
                  flush=True)
        else:
            rep = evo.run_night(progress)
            print(f"Night {rep.night}: best {rep.best_id} F={rep.best_fitness:.4f} (Elo {rep.best_elo:.0f}), "
                  f"mean F {rep.mean_fitness:.4f}, benchmark {rep.bench_id} {rep.bench_fitness:.4f}, "
                  f"diversity {rep.diversity:.3f}, {rep.games} games, {rep.seconds / 60:.1f} min", flush=True)
        for text in rep.proposed:
            print("  new rule: " + text.replace("\n", " | "))
        for text, why in rep.invalid:
            print(f"  refused ({why}): " + text.replace("\n", " | ")[:120])
        for r in rep.rule_results:
            print(f"  rule \"{r['name']}\": {r['wins']}/{r['games']} tonight, {r['total_wins']}/{r['total_games']} "
                  f"in all, fired {r['fired']}× → {r['status']}")
        for lesson in rep.lessons:
            print(f"  lesson: {lesson}")
    print()
    if not args.rules_only:
        print_nights(store)
    print_rules(store)
    return 0


def print_rules(store) -> None:
    from ai.rule_lab import RuleLab

    lab = RuleLab(store.db, default_rules())
    rows = store.db.execute("SELECT id, name, status, wins, games, activations, created_night, text, history "
                            "FROM rules ORDER BY id").fetchall()
    if not rows:
        print("The rulebook is empty.")
        return
    print("The rulebook:")
    for r in rows:
        print(f"  #{r['id']:<3} {r['status']:<10} {r['wins']:>2}/{r['games']:<2} fired {r['activations']:>5}×  "
              f"night {r['created_night']:>3}  {r['name']}")
    accepted = lab.accepted()
    print(f"Accepted: {len(accepted)} (version 3 is done at the first one: {'YES' if accepted else 'not yet'}).")


def cmd_rules(args) -> int:
    from engine.store import Store

    store = Store(args.db)
    print_rules(store)
    if args.show:
        row = store.db.execute("SELECT text, history FROM rules WHERE id=?", (args.show,)).fetchone()
        if row:
            print("\n" + row["text"] + "\n\nHistory: " + ", ".join(json.loads(row["history"] or "[]")))
    return 0


def demo_world(args) -> World:
    """A world played in fast mode for a few days, to give the AI something to talk about."""
    w = World(default_rules(), seed=args.seed, start=parse_start(args.start, None))
    w.run(args.days * 86400)
    return w


def cmd_council(args) -> int:
    from ai.council import Council
    from ai.llm import client_from_rules

    llm = client_from_rules(default_rules())
    if not llm.available():
        print(f"The local AI ({llm.model}) is not available. Start Ollama and pull the model.")
        return 1
    w = demo_world(args)
    for civ in w.civs:
        d = Council(llm).decide(w, civ)
        print(f"\n=== {civ.name}, {w.clock.local:%d %b %H:%M}, castle {civ.castle_level}, {civ.pop} people ===")
        if d is None:
            print("The council did not answer.")
            continue
        for role, p in d.proposals.items():
            print(f"  {role.title():<10} ({p.urgency:.1f}) {p.proposal} — {p.argument}")
        print(f"  KING: {d.plan.thought}")
        print(f"        priorities {d.plan.priorities}, build next: {d.plan.build_next}, "
              f"listened to: {d.plan.listened_to} ({d.seconds:.0f} s)")
        if d.dropped:
            print(f"        refused by the code: {'; '.join(d.dropped)}")
    return 0


def cmd_chronicle(args) -> int:
    from ai import chronicle as chron
    from ai.llm import client_from_rules
    from engine.store import Store

    if args.archive:
        for row in Store(args.db).chronicles(limit=args.archive, search=args.search):
            print(row["body"] + "\n")
        return 0
    w = demo_world(args)
    llm = client_from_rules(default_rules())
    ch = chron.write_chronicle(w, llm, w.clock.t - 86400)
    print(ch.text())
    print(f"\n(written by {'the local AI' if ch.by_ai else 'code, the local AI was not available'})")
    return 0


def cmd_referee(args) -> int:
    from ai import referee
    from engine import formulas as F
    from engine.store import Store

    store = Store(args.db)
    last = store.last_night()
    if not last:
        print("No nights yet: nothing to review.")
        return 0
    counts = store.build_stats(max(1, last - 6), last)
    seen = set(store.build_stats(max(1, last - 13), last))
    pop = store.population(last)
    d = F.diversity([g.genes for g in pop], default_rules().gene_ranges)
    balance = referee.load_balance()
    verdict = referee.review(counts, seen, d, balance, datetime.now().date())
    for c in verdict.changes:
        print(f"  {c['what']}: {c['old']} → {c['new']}  ({c['reason']})")
    if not verdict.changes:
        print("No changes: the game looks balanced.")
    if args.apply:
        referee.save(verdict, balance, "manual run")
        print(f"Saved to data/balance.yaml, in effect from {verdict.effective}.")
    return 0


def cmd_pictures(args) -> int:
    from ai.images import ComfyPainter, ImageError, check_workflow, portrait_prompt

    cfg = default_rules().images
    painter = ComfyPainter(cfg)
    problems = check_workflow(painter)
    if problems:
        print("The picture workflow has problems:")
        for p in problems:
            print("  - " + p)
        return 1
    print(f"ComfyUI at {painter.url} has every node, input and model the workflow needs.")
    if args.test:
        subject = {"name": "Ana Morar", "age": 31, "job": "wood", "trait": "Strong", "kingdom": "North"}
        try:
            path = painter.paint(portrait_prompt(subject, "spring", "#3b82f6", cfg.get("style", "")), 42, "test_portrait")
        except ImageError as e:
            print(f"Painting failed: {e}")
            return 1
        print(f"Test portrait: {path}")
    return 0


def cmd_doctor(args) -> int:
    from engine.store import Store

    store = Store(args.db)
    since = time.time() - args.days * 86400
    rows = store.doctor_log(since)
    if not rows:
        print(f"No repairs in the last {args.days:g} days.")
    for r in rows:
        print(f"  {r['wall']}  [{r['check_name']}] {r['text']} → {r['action']}")
    rep = store.db.execute("SELECT * FROM doctor_reports ORDER BY date DESC LIMIT 1").fetchone()
    if rep:
        print()
        print(f"Weekly report ({rep['date']}): {rep['text']}")
    return 0


def cmd_league(args) -> int:
    from engine.store import Store

    store = Store(args.db)
    hall = store.hall()
    print("Hall of Champions:")
    for i, g in enumerate(hall, 1):
        print(f"{i:>3}. {g.id:<10} Elo {g.meta['elo']:>6.0f}  born night {g.meta.get('night', 0):>3}  "
              f"games {g.meta.get('games', 0):>4}" + ("  (protected)" if i <= 3 else ""))
    print()
    print_nights(store)
    best = store.morning_genomes()
    if best:
        print("\nGenes of last night's best genome:")
        for k, v in best[0].genes.items():
            print(f"  {k:<22} {v:.3f}")
    return 0


def cmd_bench(args) -> int:
    w = World(default_rules(), seed=args.seed, start=parse_start(args.start, None))
    t0 = time.perf_counter()
    w.run(args.days * 86400)
    dt = time.perf_counter() - t0
    print(f"{args.days} days: {dt:.2f} s, {w.ticks} ticks, {1000 * dt / max(1, w.ticks):.3f} ms/tick")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="ageofai", description="Age of AI — simulation")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fast", help="simulate many days quickly")
    f.add_argument("--profile", choices=("classic", "civilization"), default="classic")
    f.add_argument("--days", type=int, default=150)
    f.add_argument("--seed", type=int, default=1)
    f.add_argument("--start", help="start date, e.g. 2026-10-30T07:00 (default: now)")
    f.add_argument("--every", type=int, default=10, help="print a status line every N days")
    f.add_argument("--quiet", action="store_true")
    f.add_argument("--journal", action="store_true", help="print every event")
    f.add_argument("--events", type=int, default=0, help="at the end, list events with importance ≥ N")
    f.add_argument("--thoughts", type=int, default=0, help="at the end, show the last N AI thoughts")
    f.set_defaults(fn=cmd_fast)

    for name, fn, helptext in (("live", cmd_live, "run in real time, text only (evolves at 00:30, genomes at 07:00)"),
                               ("serve", cmd_serve, "run in real time with the browser view at localhost:8000")):
        l = sub.add_parser(name, help=helptext)
        l.add_argument("--seed", type=int, default=1)
        l.add_argument("--db", default=None, help="SQLite file (default data/world.db)")
        l.add_argument("--profile", choices=("classic", "civilization"), default="classic", help="world rules and separate saved world")
        l.add_argument("--model", action="append", default=[], metavar="KINGDOM=MODEL", help="select an installed Ollama model per kingdom; repeat for each")
        l.add_argument("--speed", type=float, default=None, help="normal game seconds per real second (classic: 1; civilization: 12)")
        l.add_argument("--start", default=None, help="start the world at this local time, e.g. 2027-04-05T09:00")
        l.add_argument("--no-evolve", action="store_true", help="no night evolution in this run")
        l.add_argument("--no-pictures", action="store_true", help="no local pictures (ComfyUI) in this run")
        l.add_argument("--new", action="store_true", help="start a new world instead of the saved one")
        l.add_argument("--no-save", action="store_true", help="don't save this run (saving is on in real time)")
        if name == "serve":
            l.add_argument("--host", default=None, help="0.0.0.0 = reachable from other devices at home")
            l.add_argument("--port", type=int, default=None)
            l.add_argument("--journal", action="store_true", help="also print every event here")
        l.set_defaults(fn=fn)

    e = sub.add_parser("evolve", help="run nights of evolution now")
    e.add_argument("--nights", type=int, default=1)
    e.add_argument("--db", default=None)
    e.add_argument("--seed", type=int, default=1)
    e.add_argument("--processes", type=int, default=None)
    e.add_argument("--population", type=int, default=None)
    e.add_argument("--days", type=int, default=None, help="days per game (design doc: 60)")
    e.add_argument("--peer-games", type=int, default=None)
    e.add_argument("--champion-games", type=int, default=None)
    e.add_argument("--no-llm", action="store_true", help="skip the local AI (no new rules, plain lessons)")
    e.add_argument("--rules-only", action="store_true", help="only the rule cycle: propose, paired games, verdicts")
    e.set_defaults(fn=cmd_evolve)

    ru = sub.add_parser("rules", help="the rulebook: AI-written rules and their tests")
    ru.add_argument("--db", default=None)
    ru.add_argument("--show", type=int, default=None, help="print one rule and its history")
    ru.set_defaults(fn=cmd_rules)

    for name, fn, helptext in (("council", cmd_council, "watch the king's council decide (needs Ollama)"),
                               ("chronicle", cmd_chronicle, "write today's Chronicle from a fast-mode world")):
        c = sub.add_parser(name, help=helptext)
        c.add_argument("--days", type=int, default=20, help="days of fast mode before asking")
        c.add_argument("--seed", type=int, default=1)
        c.add_argument("--start", default="2027-04-01T07:00")
        c.add_argument("--db", default=None)
        if name == "chronicle":
            c.add_argument("--archive", type=int, default=0, help="print the last N stored chronicles instead")
            c.add_argument("--search", default=None)
        c.set_defaults(fn=fn)

    rf = sub.add_parser("referee", help="run the weekly balance review now")
    rf.add_argument("--db", default=None)
    rf.add_argument("--apply", action="store_true", help="save the changes (default: only show them)")
    rf.set_defaults(fn=cmd_referee)

    pc = sub.add_parser("pictures", help="check the local picture workflow (ComfyUI); --test paints one sample")
    pc.add_argument("--test", action="store_true", help="also paint one test portrait into data/images")
    pc.set_defaults(fn=cmd_pictures)

    dr = sub.add_parser("doctor", help="what the world doctor repaired, and its last weekly report")
    dr.add_argument("--db", default=None)
    dr.add_argument("--days", type=float, default=7)
    dr.set_defaults(fn=cmd_doctor)

    lg = sub.add_parser("league", help="Hall of Champions and night history")
    lg.add_argument("--db", default=None)
    lg.set_defaults(fn=cmd_league)

    b = sub.add_parser("bench", help="measure speed")
    b.add_argument("--days", type=int, default=5)
    b.add_argument("--seed", type=int, default=1)
    b.add_argument("--start", default="2026-10-30T07:00")
    b.set_defaults(fn=cmd_bench)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
