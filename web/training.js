// Saved experiments and live training status. Viewing a report never starts a game.
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const score = (n) => n == null ? "—" : Number(n).toFixed(3);
const count = (n) => n == null ? "—" : Number(n).toLocaleString();
const date = (s) => s ? new Date(s).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "Not recorded";
const duration = (s) => {
  if (s == null) return "—";
  const total = Math.round(s), h = Math.floor(total / 3600), m = Math.floor(total % 3600 / 60);
  return h ? `${h} h ${m} min` : m ? `${m} min ${total % 60} sec` : `${total} sec`;
};
const delta = (n, key) => !key ? '<span class="training-baseline" title="This older round did not save its benchmark settings, so its score change cannot be verified.">Settings not saved</span>' : n == null ? '<span class="training-baseline">Baseline</span>' : `<span class="${n > 0 ? "training-up" : n < 0 ? "training-down" : "training-baseline"}">${n > 0 ? "+" : ""}${score(n)}</span>`;
const metric = (label, value, note) => `<div class="card training-metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></div>`;

async function get(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

export class TrainingView {
  constructor(chart) {
    this.chart = chart;
    this.selected = null;
    this.detailRound = null;
    this.request = 0;
    this.loading = false;
  }

  async refresh() {
    if (this.loading) return;
    this.loading = true;
    try {
      const data = await get("/api/training");
      this.data = data;
      const rows = data.rounds, last = rows.at(-1), status = data.status;
      const state = status.running ? "Training in progress" : status.enabled ? "Waiting for the next round" : "Automatic training is off";
      const next = status.next_start ? `Next start: ${date(status.next_start)}` : "";
      $("#trainingStatus").innerHTML = `<div><span class="training-dot ${status.running ? "running" : ""}"></span><b>${esc(state)}</b><span>${esc(next)}</span></div><small>Updated ${new Date().toLocaleTimeString()} · refreshes every 10 seconds</small>`;
      $("#trainingMetrics").innerHTML = metric("Completed rounds", count(rows.length), "History is retained between restarts") +
        metric("Latest benchmark", score(last?.bench_fitness), last ? `Round ${last.night} · controlled test score` : "The first result will appear here") +
        metric("Main games completed", count(rows.reduce((n, r) => n + (r.games || 0), 0)), "Benchmark and rule tests are separate") +
        metric("Applied to the live world", status.applied_night ? `Round ${status.applied_night}` : "Initial strategies", `${count(data.next_population)} strategies saved for the next generation`);
      const historyKey = JSON.stringify(rows);
      if (historyKey !== this.historyKey) {
        this.historyKey = historyKey;
        this.drawChart(rows);
        $("#trainingHistory").innerHTML = rows.length ? `<table class="training-table"><thead><tr><th>Round</th><th>Finished</th><th>Main games</th><th>Best score</th><th>Average</th><th>Benchmark</th><th>Change</th><th>Duration</th></tr></thead><tbody>${[...rows].reverse().map((r) => `<tr data-round="${r.night}"><th><button class="training-round" data-round="${r.night}">Round ${r.night}</button></th><td>${esc(date(r.finished))}</td><td>${count(r.games)}</td><td>${score(r.best_fitness)}</td><td>${score(r.mean_fitness)}</td><td>${score(r.bench_fitness)}</td><td>${delta(r.benchmark_delta, r.benchmark_key)}</td><td>${esc(duration(r.seconds))}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">No completed training yet. This page will fill automatically when the first round finishes.</div>';
        $("#trainingHistory").querySelectorAll("button[data-round]").forEach((button) => {
          button.onclick = async () => {
            this.selected = Number(button.dataset.round);
            await this.select(this.selected);
            $("#trainingDetail").scrollIntoView({ behavior: "smooth", block: "start" });
          };
        });
      }
      const chosen = this.selected ?? last?.night;
      if (chosen != null) await this.select(chosen);
      else $("#trainingDetail").innerHTML = "";
    } catch (error) {
      $("#trainingStatus").innerHTML = '<b>Training history is temporarily unavailable.</b><span>Saved reports remain intact. Retrying automatically…</span>';
    } finally {
      this.loading = false;
    }
  }

  drawChart(rows) {
    if (!rows.length) {
      $("#chartTraining").innerHTML = '<figcaption>Learning over time</figcaption><div class="empty">The first completed round establishes the baseline.</div>';
      return;
    }
    // Keep benchmark settings separate: a larger score on a new test is not proof of improvement.
    const groups = [], unknown = [];
    for (const row of rows) {
      if (row.bench_fitness == null) continue;
      if (!row.benchmark_key) { unknown.push(row); continue; }
      const key = row.benchmark_key;
      if (!groups.length || groups.at(-1).key !== key) groups.push({ key, rows: [] });
      groups.at(-1).rows.push(row);
    }
    const colors = ["var(--s1)", "var(--s3)", "var(--s5)", "var(--s4)"];
    this.chart($("#chartTraining"), {
      title: "Learning over time",
      note: "Benchmark lines connect matching test settings only; older results without settings are separate points. Nightly scores reflect each round’s different scenarios.",
      series: [
        ...(unknown.length ? [{ name: "Benchmark · settings unknown", color: "var(--s1)", connect: false, points: unknown.map((r) => [r.night, r.bench_fitness]) }] : []),
        ...groups.map((g, i) => ({ name: `Benchmark · ${g.rows[0].config.days || "?"} days${groups.length > 1 ? ` · from round ${g.rows[0].night}` : ""}`, color: colors[(i + (unknown.length ? 1 : 0)) % colors.length], points: g.rows.map((r) => [r.night, r.bench_fitness]) })),
        { name: "Best of round", color: "var(--gold)", points: rows.filter((r) => r.best_fitness != null).map((r) => [r.night, r.best_fitness]) },
        { name: "Round average", color: "var(--ink-3)", points: rows.filter((r) => r.mean_fitness != null).map((r) => [r.night, r.mean_fitness]) },
      ], x: { name: "Round", fmt: (n) => `round ${n}` }, yFmt: score,
    });
  }

  async select(night) {
    $("#trainingHistory").querySelectorAll("tr[data-round]").forEach((row) => {
      const selected = Number(row.dataset.round) === night;
      row.classList.toggle("selected", selected);
      row.querySelector("button").setAttribute("aria-pressed", String(selected));
    });
    if (this.detailRound === night) return;
    this.detailRound = null;
    const request = ++this.request;
    $("#trainingDetail").innerHTML = '<div class="empty">Loading round report…</div>';
    try {
      const data = await get(`/api/training/${night}`);
      if (request !== this.request) return;
      this.detailRound = night;
      this.drawDetail(data);
    } catch (error) {
      if (request === this.request) $("#trainingDetail").innerHTML = '<div class="empty">Could not load this report. Select the round again to retry.</div>';
    }
  }

  drawDetail(data) {
    const r = data.round, o = data.outcomes, cfg = r.config;
    const weights = this.data.fitness_weights;
    const formula = Object.entries(weights).map(([key, value]) => `${Math.round(value * 100)}% ${key === "gathered" ? "resources gathered" : key === "pop" ? "population" : key}`).join(" · ");
    const facts = [["Best strategy", r.best_id], ["Benchmark winner", r.bench_id || "Not recorded"],
      ["Started", date(r.started)], ["Finished", date(r.finished)], ["Duration", duration(r.seconds)],
      ["Population of strategies", cfg.population ? count(cfg.population) : "Not recorded"],
      ["Normal game length", cfg.days ? `${cfg.days} simulated days` : "Not recorded"],
      ["Benchmark settings", r.benchmark_key || "Not recorded for this round"],
      ["Strategy diversity", score(r.diversity)]];
    const seasonOrder = ["Spring", "Summer", "Autumn", "Winter"];
    const seasons = [...data.seasons].sort((a, b) => seasonOrder.indexOf(a.season) - seasonOrder.indexOf(b.season));
    $("#trainingDetail").innerHTML = `<div class="training-report-head"><div><span class="training-eyebrow">Saved report</span><h1>Round ${r.night}</h1></div><span class="training-saved">Completed ${esc(date(r.finished))}</span></div>
      <div class="training-metrics">${metric("Best score", score(r.best_fitness), "Highest score in this round")}${metric("Average score", score(r.mean_fitness), "Across candidate strategies")}${metric("Benchmark", score(r.bench_fitness), "Controlled evaluation")}${metric("Strategy diversity", score(r.diversity), "How different the strategies are")}</div>
      <div class="grid2">
        <div class="card"><h2>Round summary</h2><dl class="training-facts">${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl></div>
        <div class="card"><h2>Lessons from this round</h2>${data.lessons.length ? `<ul class="training-lessons">${data.lessons.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : '<div class="empty">No lessons were recorded for this round.</div>'}<p class="training-note">Lessons summarize these experiments. They are observations, not guarantees about every world.</p></div>
        <div class="card"><h2>Match outcomes</h2><dl class="training-facts"><dt>Recorded main games</dt><dd>${count(o.games)}</dd><dt>Drawn games</dt><dd>${count(o.draws || 0)}</dd><dt>Average final castle</dt><dd>${o.average_castle == null ? "—" : o.average_castle.toFixed(2)}</dd><dt>Highest final castle</dt><dd>${count(o.highest_castle)}</dd><dt>Average final population</dt><dd>${o.average_population == null ? "—" : o.average_population.toFixed(1)}</dd></dl><p class="training-note">Final-state averages include both sides of each saved main game.</p></div>
        <div class="card"><h2>Starting season</h2>${seasons.length ? `<div class="training-table-wrap"><table class="training-table compact"><thead><tr><th>Season</th><th>Games</th><th>Avg castle</th><th>Avg people</th></tr></thead><tbody>${seasons.map((s) => `<tr><th>${esc(s.season)}</th><td>${count(s.games)}</td><td>${score(s.average_castle)}</td><td>${s.average_population.toFixed(1)}</td></tr>`).join("")}</tbody></table></div>` : '<div class="empty">No match breakdown was saved.</div>'}</div>
        <div class="card wide"><h2>Winning strategies</h2><div class="training-champions">${data.champions.map((c) => `<div><h3>${esc(c.kingdom)}</h3><b>${esc(c.id)}</b><details><summary>View strategy parameters</summary><dl class="training-facts">${Object.entries(c.genes).map(([k, v]) => `<dt>${esc(k.replaceAll("_", " "))}</dt><dd>${score(v)}</dd>`).join("")}</dl></details></div>`).join("")}</div></div>
        <div class="card"><h2>Buildings across simulated games</h2>${Object.keys(data.buildings).length ? `<dl class="training-facts">${Object.entries(data.buildings).sort((a, b) => b[1] - a[1]).map(([k, v]) => `<dt>${esc(k.replaceAll("_", " "))}</dt><dd>${count(v)}</dd>`).join("")}</dl>` : '<div class="empty">No building totals were recorded.</div>'}</div>
        <div class="card"><h2>Rule tests in this round</h2>${data.rules.length ? `<ul class="training-lessons">${data.rules.map((rule) => `<li><b>${esc(rule.name)}</b>${rule.notes.map((n) => `<span class="training-rule-note">${esc(n)}</span>`).join("")}</li>`).join("")}</ul>` : '<div class="empty">No rule test results were recorded.</div>'}</div>
      </div><p class="training-note training-objective">Current scoring weights: ${esc(formula)}. A score is not a win percentage. Benchmark changes are shown only between adjacent rounds with the same recorded settings.</p>`;
  }
}
