"""The browser view (version 4): http://localhost:8000.

FastAPI serves the page, a few JSON endpoints and one WebSocket that pushes a frame every second.
The page only watches: there is no endpoint that changes the game.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager, closing
from pathlib import Path

import gzip

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from engine.rules import DATA_DIR
from engine.store import Store

from ai.evolution import stall_nights

from .runner import LiveRunner
from .snapshot import map_payload, person_card, stats_payload

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class Hub:
    """Fans each frame out to every open page. Frames come from the world thread."""

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queues: set[asyncio.Queue] = set()
        self.latest: str | None = None

    def publish(self, frame: dict) -> None:                 # world thread
        text = json.dumps(frame, separators=(",", ":"))
        self.latest = text
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self._fan_out, text)

    def _fan_out(self, text: str) -> None:                  # event loop thread
        for q in list(self.queues):
            if q.full():
                # Frames contain map deltas. Silently dropping one leaves a
                # slow observer with permanently stale roads and resources.
                while not q.empty():
                    q.get_nowait()
                q.put_nowait('{"type":"resync"}')
            q.put_nowait(text)


def create_app(runner: LiveRunner, run_world: bool = True) -> FastAPI:
    hub = Hub()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.loop = asyncio.get_running_loop()
        runner.on_frame = hub.publish
        thread = None
        if run_world:
            def run():
                try:
                    runner.run()
                except Exception as error:
                    app.state.world_error = str(error)
                    logging.getLogger(__name__).exception("The world stopped; requesting a server restart")
                    callback = getattr(app.state, "on_world_error", None)
                    if callback is not None:
                        callback()

            thread = threading.Thread(target=run, name="world", daemon=True)
            app.state.world_thread = thread
            thread.start()
        yield
        runner.stop()
        if thread is not None:
            thread.join(timeout=5)

    app = FastAPI(title="Age of AI", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.world_thread = None
    app.state.world_error = None
    app.add_middleware(GZipMiddleware, minimum_size=2000)

    @app.middleware("http")
    async def fresh_page_code(request, call_next):
        """The page's scripts and styles are re-checked on every load (a cheap 304 when unchanged),
        so an updated map.js is never hidden behind the browser's cache."""
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response
    app.state.runner = runner
    app.state.hub = hub
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    for sub in ("images", "audio"):
        (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
        app.mount(f"/media/{sub}", StaticFiles(directory=DATA_DIR / sub), name=f"media-{sub}")

    def reader() -> Store:
        return Store.reader(runner.db_path)

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/map")
    def api_map():
        with runner.lock:
            data = map_payload(runner.w)
            data["seq"] = runner.frames.seq if runner.frames else 0
        return JSONResponse(data)

    @app.get("/api/person/{pid}")
    def api_person(pid: int):
        with runner.lock:
            card = person_card(runner.w, pid)
        if card is None:
            raise HTTPException(404, "No such person (they may have left or died).")
        with closing(reader()) as s:
            card["portraits"] = [{"url": f"/media/images/{r['file']}", "date": r["date"], "caption": r["caption"]}
                                 for r in s.images(person_id=pid, person_name=card["name"], limit=6)]
        return card

    @app.get("/api/stats")
    def api_stats():
        with runner.lock:
            return stats_payload(runner.w)

    @app.get("/api/evolution")
    def api_evolution():
        with runner.lock:                                    # the genomes playing now (they change at 07:00 and at a rebirth)
            in_use = [c.genome.id or "default" for c in runner.w.civs]
        with closing(reader()) as s:
            nights = s.nights()
            best = s.morning_genomes()
            last = s.last_night()
            hall = s.hall()[:8]
            rules = [dict(r) for r in s.db.execute(
                "SELECT id, name, status, wins, games, activations FROM rules ORDER BY id DESC LIMIT 20").fetchall()] \
                if _has(s, "rules") else []
            return {
                "nights": [{k: n.get(k) for k in ("night", "best_id", "best_fitness", "mean_fitness", "bench_id",
                                                  "bench_fitness", "diversity", "games", "seconds")} for n in nights],
                "best": {"id": best[0].id, "genes": best[0].genes, "fitness": best[0].meta.get("fitness")}
                if best else None,
                "ranges": {k: list(v) for k, v in runner.rules.gene_ranges.items()},
                "lessons": s.lessons(last) if last else [],
                "hall": [{"id": g.id, "elo": round(g.meta["elo"])} for g in hall],
                "rules": rules,
                "in_use": in_use,
                "stall": {"nights": stall_nights(nights),
                          "limit": int(runner.rules.evolution_watch.get("stall_nights", 21))},
            }

    @app.get("/api/training")
    def api_training():
        from .training import history, live_status

        status = live_status(runner)
        with closing(reader()) as s:
            rounds = history(s, runner.rules.timezone)
            last = rounds[-1]["night"] if rounds else 0
            next_population = s.db.execute("SELECT COUNT(*) FROM population WHERE night=?", (last + 1,)).fetchone()[0]
        return {"rounds": rounds, "status": status, "next_population": next_population,
                "fitness_weights": runner.rules.evolution["fitness"]}

    @app.get("/api/training/{night}")
    def api_training_round(night: int):
        from .training import detail

        with closing(reader()) as s:
            report = detail(s, night, runner.rules.timezone)
        if report is None:
            raise HTTPException(404, "Training round not found")
        return report

    @app.get("/api/rivalry")
    def api_rivalry():
        """The rivalry scoreboard: per kingdom and per model, plus the latest facts and the eras."""
        with runner.lock:
            kingdoms = [{"id": c.id, "name": c.name, "color": c.color, "mind": c.genome.id or "default",
                         "model": str(getattr(runner.llms.get(c.id, runner.llm), "model", "")),
                         "castle": c.castle_level, "alive": c.alive} for c in runner.w.civs]
            eras = list(getattr(runner.w, "eras", []))
            era = getattr(runner.w, "era", 1)
        with closing(reader()) as s:
            has = _has(s, "rivalry")
            by_civ = s.rivalry("civ") if has else {}
            by_model = s.rivalry("model") if has else {}
            recent = s.rivalry_recent(20) if has else []
            from datetime import date
            usage = s.model_usage(date.today().isoformat()) if _has(s, "model_usage") else []
        for k in kingdoms:
            k["score"] = by_civ.get(k["id"], {})
        return {"kingdoms": kingdoms, "models": [{"model": m, **v} for m, v in sorted(by_model.items(),
                                                                                       key=lambda kv: str(kv[0]))],
                "recent": recent, "eras": eras, "era": era, "usage": usage}

    @app.get("/api/genome/{gid}/tree")
    def api_genome_tree(gid: str, depth: int = 5):
        with closing(reader()) as s:
            tree = s.family_tree(gid, max(1, min(depth, 10)))
        if not tree:
            raise HTTPException(404, "No such genome.")
        return {"tree": tree, "ranges": {k: list(v) for k, v in runner.rules.gene_ranges.items()}}

    @app.get("/api/chronicles")
    def api_chronicles(limit: int = Query(14, ge=1, le=366), search: str | None = Query(None, max_length=100)):
        with closing(reader()) as s:
            out = []
            for row in s.chronicles(limit=limit, search=search):
                row["images"] = [{"url": f"/media/images/{r['file']}", "kind": r["kind"], "caption": r["caption"]}
                                 for r in s.images(date=row["date"])]
                row["table"] = json.loads(row.pop("table_json") or "[]")
                row["doctor"] = s.doctor_report(row["date"]) if _has(s, "doctor_reports") else None
                out.append(row)
            return out

    @app.get("/api/timelapse")
    def api_timelapse(hours: float = Query(24, gt=0, le=48), frames: int = Query(720, ge=1, le=1440)):
        """The last 24 real hours as frames (one every 2 minutes), plus the radio's lines of that time."""
        with closing(reader()) as s:
            if not _has(s, "timeline"):
                return {"frames": [], "comments": []}
            since = runner.w.clock.t - hours * 3600
            rows = s.frames_since(since)
            step = max(1, len(rows) // max(1, frames))
            out = [json.loads(gzip.decompress(blob)) for _, blob in rows[::step]]
            comments = [{**dict(r), "audio": f"/media/audio/{r['audio']}" if r["audio"] else None}
                        for r in s.db.execute("SELECT * FROM comments WHERE t >= ? ORDER BY t", (since,)).fetchall()]
        return {"frames": out, "comments": comments, "play_seconds": runner.rules.timelapse.get("play_seconds", 120)}

    @app.get("/api/doctor")
    def api_doctor(days: float = Query(7, gt=0, le=90)):
        with closing(reader()) as s:
            if not _has(s, "doctor"):
                return {"log": []}
            return {"log": s.doctor_log(runner.w.clock.t - days * 86400)[-200:]}

    @app.get("/api/comments")
    def api_comments(limit: int = Query(20, ge=1, le=200)):
        with closing(reader()) as s:
            rows = s.db.execute("SELECT * FROM comments ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [{**dict(r), "audio": f"/media/audio/{r['audio']}" if r["audio"] else None} for r in rows]

    @app.get("/api/health")
    def api_health():
        thread = app.state.world_thread
        alive = not run_world or bool(thread and thread.is_alive() and app.state.world_error is None)
        health = {"ok": alive, "world_alive": alive,
                "world_error": app.state.world_error,
                "clock": runner.w.clock.local.isoformat(timespec="seconds"),
                "speed": runner.speed, "rules_in_force": runner.book_size,
                "saved": runner.persistent, "recovered_from": runner.recovered, "caught_up_hours": runner.caught_up,
                "llm": runner.llm.available(), "piper": bool(runner.commentator and runner.commentator.piper_ready()),
                "pictures": bool(runner.mind.painter and runner.mind.painter.available())}
        return health if alive else JSONResponse(health, status_code=503)

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=4)
        hub.queues.add(q)
        tasks = []
        try:
            hello = await asyncio.to_thread(_hello, runner)
            await socket.send_text(json.dumps(hello, separators=(",", ":")))
            if hub.latest:
                await socket.send_text(hub.latest)
            async def send_frames():
                while True:
                    await socket.send_text(await q.get())

            async def watch_disconnect():
                while True:
                    if (await socket.receive())["type"] == "websocket.disconnect":
                        return

            # Observe disconnects even when the simulation isn't publishing.
            tasks = [asyncio.create_task(send_frames()), asyncio.create_task(watch_disconnect())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, ConnectionError, RuntimeError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            hub.queues.discard(q)

    return app


def _has(store: Store, table: str) -> bool:
    return store.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()         is not None


def _hello(runner: LiveRunner) -> dict:
    with runner.lock:
        data = runner.frames.hello() if runner.frames else {"type": "hello", "events": [], "thoughts": []}
        data["map"] = map_payload(runner.w)
        data["map"]["seq"] = runner.frames.seq if runner.frames else 0
    data["speed"] = runner.speed
    return data
