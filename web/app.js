// Age of AI — the page. It only watches: one WebSocket brings a frame a second, a few JSON calls fill the tabs.
import { MapView, fmtLeft, siteTime } from "./map.js";
import { TrainingView } from "./training.js";
import { LiveConnection } from "./connection.js";

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const RES_LABEL = { food: "Food", wood: "Wood", stone: "Stone", ore: "Ore", gold: "Gold", coins: "Coins" };
const RES_COLOR = { food: "var(--s1)", wood: "var(--s2)", stone: "var(--s3)", ore: "var(--s4)", gold: "var(--s5)", coins: "var(--s6)" };
const JOB = { food: "farmer", hunt: "hunter", berries: "gatherer", wood: "woodcutter", stone: "stonemason", ore: "miner",
  gold: "gold miner", build: "builder", firewood: "firewood carrier", fire: "firefighter" };

const S = { map: null, frame: null, civ: 0, tab: "map", radio: false, statsCiv: null, territory: null, terrKey: "", playing: null };
let view;
let connection;
const training = new TrainingView(lineChart);
const markup = new WeakMap();
function updateMarkup(el, html) {
  if (markup.get(el) === html) return;
  el.innerHTML = html;
  markup.set(el, html);
}

async function getJSON(url) {
  const response = await fetch(url, { signal: AbortSignal.timeout(15000) });
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

function connectionState(state, delay = 0) {
  const badge = $("#connectionStatus");
  if (state === "live" && badge.dataset.state === "live") return;
  badge.dataset.state = state;
  badge.textContent = { live: "Live", connecting: "Connecting", reconnecting: "Reconnecting", error: "Unavailable" }[state];
  $("#connectionNotice").hidden = state === "live" || (state === "connecting" && !S.frame);
  $("#connectionDetail").textContent = state === "error"
    ? "The observer could not load. Check that the world server is running, then retry."
    : `Waiting for the world server. ${S.frame ? "Showing the last received state. " : ""}${delay ? `Retrying in ${delay / 1000}s.` : "Reconnecting now…"}`;
}

// ---------------------------------------------------------------- start
async function start() {
  $("#connectionRetry").onclick = () => connection ? connection.retry() : location.reload();
  const map = await getJSON("/api/map");
  S.map = map;
  view = new MapView($("#map"), openPerson, showBuildingTip);
  await view.init(map);
  view.onUnit = showUnitTip;
  view.showFogOf(S.map.civs[S.civ].id);
  view.onView = drawMinimap;
  window.ageofai = { view, state: S, openPerson };           // handy in the browser console
  buildCivSwitch();
  connect();
  setupTabs();
  setupMapControls();
  const initialTab = location.hash.slice(1);
  showTab([...document.querySelectorAll(".tabs button")].some((b) => b.dataset.tab === initialTab) ? initialTab : "map");
  setupRadio();
  setupMinimap();
  $("#timelapse").onclick = playTimelapse;
  $("#playStop").onclick = () => stopTimelapse();
  $("#chronSearch").addEventListener("submit", (e) => { e.preventDefault(); refreshPage("chronicle", () => loadChronicles(e.target.q.value.trim())); });
  window.addEventListener("resize", () => drawMinimap());
}

function connect() {
  let centered = false;
  let lastSeq = -1;
  connection = new LiveConnection(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`, {
    onState: connectionState,
    onMessage: (msg) => {
      if (msg.type === "reset") { location.reload(); return; }      // the doctor restored a save
      if (msg.type === "hello") {
        lastSeq = -1;
        if (msg.map) {
          if (msg.map.terrain !== S.map.terrain || JSON.stringify(msg.map.civs) !== JSON.stringify(S.map.civs)) {
            location.reload(); return;
          }
          S.map = msg.map;
          if (!S.playing) view.restoreMap(msg.map);
        }
        seenMsg.clear();
        $("#journal").replaceChildren(); $("#thoughts").replaceChildren(); $("#chat").replaceChildren();
        addEvents(msg.events); addThoughts(msg.thoughts); addMessages(msg.messages || []);
        if (!msg.thoughts?.length) $("#thoughts").innerHTML = `<li class="empty">The next council's decisions will appear here.</li>`;
        return;
      }
      if (msg.type !== "frame" || msg.seq <= lastSeq) return;
      lastSeq = msg.seq;
      // The greeting's map snapshot can be newer than the last published frame.
      if (msg.seq <= S.map.seq) {
        msg.nodes = []; msg.roads = []; msg.fog = {};
        msg.events = []; msg.thoughts = []; msg.messages = []; msg.comments = [];
      }
      onFrame(msg);
      if (!centered) {
        centered = true;
        const castle = msg.buildings.find((b) => b[2] === "castle" && b[1] === S.civ);
        if (castle) view.centerOn(castle[3] + 1, castle[4] + 4, 1.6);
      }
    },
  });
  connection.connect();
  window.addEventListener("online", () => connection.retry());
  window.addEventListener("pagehide", () => connection.stop());
  window.addEventListener("pageshow", (event) => { if (event.persisted) location.reload(); });
}

// ---------------------------------------------------------------- every frame
function onFrame(f) {
  S.frame = f;
  if (S.playing) {                                    // the timelapse has the map; keep the news
    addEvents(f.events); addThoughts(f.thoughts); addMessages(f.messages || []);
    return;
  }
  render(f);
}

function render(f, replay = false) {
  view.update(f);
  const c = f.clock;
  $("#clock .time").textContent = c.hhmm;
  const era = f.era || { n: 1 };
  const next = era.new_in != null ? ` · new era in ${fmtLeft(era.new_in)}` : "";
  $("#clock .date").textContent = `${c.date} · day ${c.day} · ${c.season} · ${c.phase}${next}`;
  $("#clock").title = c.pace_reason ? `${c.pace_reason} · ${c.pending_decisions} pending AI decisions` : "";
  $("#worldMode").textContent = `${f.civs.length} civilizations · ${f.civs.some((civ) => civ.development) ? "Continuous world" : `Era ${era.n}`}${replay ? " · Replay" : ""}`;
  $("#paceStatus").textContent = c.speed > 0 ? `${c.speed}× pace · ${(60 / c.speed).toFixed(1)} min / game hour${c.pending_decisions ? ` · ${c.pending_decisions} AI thinking` : ""}` : "";
  $("#paceStatus").title = c.pace_reason || "";
  drawResources(f);
  drawCivPanel(f);
  if (!replay) {
    addEvents(f.events);
    addThoughts(f.thoughts);
    for (const cm of f.comments) onComment(cm);
    addMessages(f.messages || []);
  }
  drawDiplomacyHead(f);
  drawMinimap();
}

function buildCivSwitch() {
  const nav = $("#civSwitch");
  nav.replaceChildren(...S.map.civs.map((c, i) => {
    const b = document.createElement("button");
    b.innerHTML = `<span class="swatch" style="background:${esc(c.color)}"></span>${esc(c.name)}`;
    b.setAttribute("aria-pressed", String(i === S.civ));
    b.onclick = () => selectCiv(i, true);
    return b;
  }));
}

function selectCiv(i, centre) {
  S.civ = i;
  view.showFogOf(S.map.civs[i].id);
  [...$("#civSwitch").children].forEach((b, k) => b.setAttribute("aria-pressed", String(k === i)));
  if (S.frame) { drawResources(S.frame); drawCivPanel(S.frame); drawMinimap(); }
  if (centre && S.frame) {
    const castle = S.frame.buildings.find((b) => b[2] === "castle" && b[1] === i);
    if (castle) view.centerOn(castle[3] + 1, castle[4] + 1 + (i === 0 ? 3 : -3));
  }
}

function drawResources(f) {
  const civ = f.civs[S.civ];
  const el = $("#resources");
  updateMarkup(el, Object.keys(civ.stock).map((r) => {
    const v = civ.stock[r], cap = civ.cap[r] || 1, pct = Math.min(100, 100 * v / cap);
    return `<div class="res${pct >= 97 ? " full" : ""}" title="${RES_LABEL[r]}: ${v} of ${cap}">
      <span class="k">${RES_LABEL[r] || r}</span><span class="v">${v.toLocaleString()}</span>
      <span class="bar"><i style="width:${pct}%"></i></span></div>`;
  }).join("") + `<div class="res"><span class="k">People</span><span class="v">${civ.pop}/${civ.beds}</span><span class="bar"><i style="width:${Math.min(100, 100 * civ.pop / Math.max(1, civ.beds))}%"></i></span></div>
    <div class="res"><span class="k">Age</span><span class="v">${esc(civ.age)}</span></div>`);
}

function drawCivPanel(f) {
  const el = $("#civPanel");
  const focused = el.contains(document.activeElement) ? document.activeElement.dataset.i : null;
  updateMarkup(el, `<h2>Civilizations <span class="panel-count">${f.civs.filter((c) => c.alive).length} alive</span></h2>` + f.civs.map((c, i) => {
    const research = c.research.map((r) => `<dt>Forge: ${esc(r.craft)} T${r.level}</dt><dd>${Math.round(r.progress * 100)}%</dd>
      <dd style="grid-column:1/-1"><div class="progress"><i style="width:${r.progress * 100}%"></i></div></dd>`).join("");
    const recruits = c.recruiting.length ? `<dt>Recruiting</dt><dd>${c.recruiting.length}</dd>` : "";
    return `<article class="civcard" data-selected="${i === S.civ}">
      <button class="civ-select" aria-pressed="${i === S.civ}" data-i="${i}"><span class="head"><span class="swatch" style="background:${esc(c.color)}"></span>${esc(c.name)}<span class="age">${esc(c.development?.era || c.age)}</span></span>
      <span class="civ-summary">${c.alive ? `${c.pop} people · Castle ${c.castle} · ${c.happiness} happiness` : "Kingdom fallen"}</span></button>
      ${i === S.civ ? `<dl class="kv">
        <dt>Mind</dt><dd>${esc(String(c.mind || "default").replace(/^start-/, ""))}${c.model ? ` <span class="model" title="The model this king thinks with">· ${esc(c.model)}</span>` : ""}</dd>
        <dt>Castle</dt><dd>level ${c.castle}</dd>
        <dt>People</dt><dd>${c.pop} (${c.workers} work)</dd>
        <dt>Beds</dt><dd>${c.beds}</dd>
        <dt>Happiness</dt><dd>${c.happiness}</dd>
        <dt>Morale</dt><dd>×${c.morale.toFixed(2)}</dd>
        <dt>Buildings</dt><dd>${c.buildings}</dd>
        <dt>Gathered</dt><dd>${c.gathered.toLocaleString()}</dd>
        ${recruits}${research}
      </dl>${developmentBlock(c)}${laterBlock(c)}${armyBlock(c)}${c.alive ? "" : `<p style="color:var(--bad)">This kingdom has fallen.${c.rebirth_in != null ? ` A new one is born in ${fmtLeft(c.rebirth_in)}.` : ""}</p>`}` : ""}</article>`;
  }).join(""));
  el.querySelectorAll(".civ-select").forEach((card) => {
    card.onclick = () => selectCiv(Number(card.dataset.i), true);
  });
  if (focused != null) el.querySelector(`[data-i="${focused}"]`)?.focus({ preventScroll: true });
}

const UNIT_PLURAL = { militia: "militia", spearman: "spearmen", archer: "archers", swordsman: "swordsmen", knight: "knights", ram: "rams" };

function developmentBlock(c) {
  const d = c.development;
  if (!d) return "";
  const row = (key, value) => `<div class="row"><span>${esc(key)}</span><span>${esc(value)}</span></div>`;
  const offices = Object.entries(d.offices).map(([role, p]) => row(role, p.name)).join("");
  const project = d.project ? row("Experiment", `${d.project.name} · ${Math.round(d.project.progress * 100)}%`) : row("Research", "No active experiment");
  const materials = Object.entries(d.materials).map(([key, value]) => row(key, Math.floor(value))).join("");
  const findings = d.experiments.slice(-3).reverse().map((p) => row(p.passed ? "Validated" : "Failed trial", `${p.name} · ${p.wins}/${p.trials}`)).join("");
  return `<div class="army development"><h3>Society · ${esc(d.era.replaceAll("_", " "))}</h3>
    ${row("Education", `${d.education.toFixed(1)} / 100`)}${offices}${project}
    ${row("Discoveries", d.discoveries.length)}${findings}${materials}
    ${d.culture.tradition ? row("Tradition", d.culture.tradition) : ""}
    ${d.culture.verse ? `<p>${esc(d.culture.verse)}</p>` : ""}</div>`;
}

function armyBlock(c) {
  const kinds = Object.entries(c.army_by_type || {}).map(([k, n]) => `${n} ${UNIT_PLURAL[k] || k}`).join(", ");
  const training = (c.training || []).length ? `<div class="row"><span>Training</span><span>${c.training.length}</span></div>` : "";
  const ups = c.weapons || c.armor ? `<div class="row"><span>Weapons / armor</span><span>+${c.weapons} / +${c.armor}</span></div>` : "";
  const war = c.attacking ? ` <span class="badge attack">ATTACKING</span>` : c.gathering ? ` <span class="badge attack">GATHERING</span>` : "";
  return `<div class="army"><div class="row"><b>Army</b><span>${c.army}${war}</span></div>
    ${kinds ? `<div>${esc(kinds)}</div>` : `<div style="color:var(--ink-3)">${c.castle >= 4 ? "no soldiers yet" : "barracks open at castle 4"}</div>`}
    ${c.spies ? `<div class="row"><span>Spies</span><span>${c.spies}</span></div>` : ""}${training}${ups}
    ${c.kills || c.losses ? `<div class="row"><span>Killed / lost</span><span>${c.kills} / ${c.losses}</span></div>` : ""}
    ${c.generation > 1 ? `<div class="row"><span>Generation</span><span>${c.generation}</span></div>` : ""}</div>`;
}

function laterBlock(c) {
  const techs = S.map.techs || {};
  const learned = (c.techs || []).map((k) => (techs[k] || {}).label || k);
  const job = c.tech_job ? `<div class="row"><span>Studying ${esc((techs[c.tech_job.tech] || {}).label || c.tech_job.tech)}</span><span>${Math.round(c.tech_job.progress * 100)}%</span></div>` : "";
  const road = c.road ? `<div class="row"><span>Road</span><span>${c.road.done}/${c.road.of} tiles</span></div>` : "";
  const rows = [
    learned.length ? `<div title="${esc(learned.map((l) => l).join(", "))}">Knows: ${esc(learned.join(", "))}</div>` : "",
    job, road,
    c.carts ? `<div class="row"><span>Carts</span><span>${c.carts}</span></div>` : "",
    c.known < 0.999 ? `<div class="row"><span>Land explored</span><span>${Math.round(c.known * 100)}%</span></div>` : "",
  ].join("");
  return rows ? `<div class="army">${rows}</div>` : "";
}

function showUnitTip(row, pos) {
  const [, civI, , , type, state, hp, order] = row;
  const civ = civI < 0 ? { name: "Bandits", color: "#2b2b2b" } : S.map.civs[civI];
  const name = (S.map.units || {})[type] || type;
  const doing = type === "scout" ? "exploring" : civI < 0 ? (order === "attack" ? "raiding" : "going back to the camp") : type === "spy" ? { go: "on the way to the enemy", stay: "watching the enemy castle", back: "coming home", home: "at home" }[state] || state
    : order === "muster" ? "gathering to march" : order === "attack" ? (state === "fight" ? "fighting" : "marching to attack") : order === "return" ? "coming home" : order === "help" ? "helping a friend" : state === "fight" ? "defending" : "guarding home";
  const tip = $("#tip");
  tip.innerHTML = `<b>${esc(name)}</b> <span class="who"><span class="swatch" style="background:${esc(civ.color)}"></span>${esc(civ.name)}</span><br>${esc(doing)}${type === "spy" ? "" : `<br>Health ${Math.round(hp * 100)}%`}`;
  tip.hidden = false;
  tip.style.left = `${Math.min(pos.x + 12, innerWidth - 200)}px`;
  tip.style.top = `${pos.y + 12}px`;
  clearTimeout(tipTimer);
  tipTimer = setTimeout(() => { tip.hidden = true; }, 4000);
}

// ---------------------------------------------------------------- diplomacy (version 5)
const KIND_WORD = { trade: "Trade offer", gift: "Gift", tribute: "Tribute demand", peace: "Peace offer", war: "Declaration of war", free: "Message" };
const goods = (g) => Object.entries(g || {}).map(([k, v]) => `${Math.round(v)} ${k}`).join(", ");
const seenMsg = new Map();

function addMessages(rows) {
  const ol = $("#chat");
  if (rows.length) ol.querySelector(".empty")?.remove();
  for (const m of rows) {
    const old = seenMsg.get(m.id);
    if (old) { old.querySelector(".status").textContent = m.status; old.querySelector(".status").className = `status ${m.status}`; continue; }
    const civI = S.map.civs.findIndex((c) => c.id === m.src);
    const civ = S.map.civs[civI] || { name: m.src, color: "#888" };
    const li = document.createElement("li");
    if (civI === 1) li.className = "right";
    const deal = m.give && Object.keys(m.give).length || m.ask && Object.keys(m.ask).length
      ? `<div class="deal">${m.give && Object.keys(m.give).length ? `gives ${esc(goods(m.give))}` : ""}${m.ask && Object.keys(m.ask).length ? `${m.give && Object.keys(m.give).length ? " · " : ""}asks ${esc(goods(m.ask))}` : ""}</div>` : "";
    const what = m.reply_to ? "Reply" : (KIND_WORD[m.type] || m.type) + (m.type === "peace" && m.days ? ` (${m.days} days)` : "");
    li.innerHTML = `<div class="meta"><span class="swatch" style="background:${esc(civ.color)}"></span>${esc(civ.name)} · day ${m.day} ${esc(m.hhmm || "")} · ${esc(what)}
      <span class="status ${esc(m.status)}">${esc(m.status)}</span>${m.by_ai ? `<span title="Written by the local AI">✎ AI</span>` : ""}</div>
      ${m.text ? `<div>${esc(m.text)}</div>` : ""}${deal}`;
    seenMsg.set(m.id, li);
    ol.prepend(li);
  }
  while (ol.children.length > 120) ol.lastChild.remove();
  for (const [id, li] of seenMsg) if (!li.isConnected) seenMsg.delete(id);
  if (!ol.children.length) ol.innerHTML = `<li class="empty" style="max-width:none;background:none;border:0">No messages yet: the kings write at 09:00 and 18:00.</li>`;
}

function drawDiplomacyHead(f) {
  const d = f.diplomacy;
  if (!d || S.tab !== "diplomacy") return;
  if (S.map.civs.length > 2 && d.pairs) {
    const name = (id) => S.map.civs.find((c) => c.id === id)?.name || id;
    $("#dipHead").innerHTML = `<h2>Relations between civilizations</h2>` + d.pairs.map((p) =>
      `<div class="row"><span>${esc(name(p.a))} ↔ ${esc(name(p.b))}</span>
       <span>${esc(p.state)} · relation ${p.R} · trust ${p.trust_a}/${p.trust_b}${p.war ? " · WAR" : ""}</span></div>`).join("");
    return;
  }
  const [a, b] = S.map.civs;
  const pos = (d.R + 100) / 2;
  const peace = d.peace_days_left > 0 ? `${d.peace_days_left} days of peace left (nobody may attack)` : d.treaty_days > 0 ? `treaty: ${d.treaty_days} days left` : d.war ? "at war" : "no treaty";
  $("#dipHead").innerHTML = `
    <div class="tile"><div class="k">Relation R</div><div class="v">${d.R > 0 ? "+" : ""}${d.R}</div><div class="s">${esc(d.state)}</div><div class="rbar"><i style="left:${pos}%"></i></div></div>
    <div class="tile"><div class="k">${esc(a.name)} trusts ${esc(b.name)}</div><div class="v">${d.trust[a.id]}</div><div class="s">trust T, 0–1</div></div>
    <div class="tile"><div class="k">${esc(b.name)} trusts ${esc(a.name)}</div><div class="v">${d.trust[b.id]}</div><div class="s">trust T, 0–1</div></div>
    <div class="tile"><div class="k">War and peace</div><div class="v" style="font-size:16px;margin-top:4px">${d.war ? `<span class="badge war">WAR</span>` : "Peace"}</div><div class="s">${esc(peace)}</div></div>`;
}

// ---------------------------------------------------------------- rivalry: who fares better, per kingdom and per model
const RIVAL_ROWS = [
  ["wars_started", "Wars started"], ["wars_won", "Wars won"], ["battles", "Battles"], ["battles_won", "Battles won"],
  ["castles_destroyed", "Castles destroyed"], ["fallen", "Times fallen"], ["eras_won", "Eras won (Wonders)"],
];
const RIVAL_KIND = { war_started: "started a war", war_end: "war over", battle: "battle", castle_destroyed: "destroyed a castle",
  fallen: "fell", wonder: "Wonder", castle: "castle level" };
const castleCell = (s) => s && s.best_castle ? `${s.best_castle}${s.best_castle_days != null ? ` <span class="s">(day ${Math.ceil(s.best_castle_days)})</span>` : ""}` : "–";

async function loadRivalry() {
  const el = $("#rivalry");
  if (!el) return;
  const d = await getJSON("/api/rivalry");
  const ks = d.kingdoms || [];
  const head = `<tr><th></th>${ks.map((k) => `<th><span class="swatch" style="background:${esc(k.color)}"></span>${esc(k.name)}</th>`).join("")}</tr>`;
  const mind = `<tr><td>Mind · model</td>${ks.map((k) => `<td>${esc(String(k.mind).replace(/^start-/, ""))} · ${esc(k.model || "genes")}</td>`).join("")}</tr>`;
  const rows = RIVAL_ROWS.map(([key, label]) => `<tr><td>${esc(label)}</td>${ks.map((k) => `<td>${Number(k.score[key] || 0)}</td>`).join("")}</tr>`).join("");
  const castle = `<tr><td>Best castle (fastest)</td>${ks.map((k) => `<td>${castleCell(k.score)}</td>`).join("")}</tr>`;
  const models = (d.models || []).length ? `<h3>Per model</h3><table class="rival"><tr><th>Model</th><th>Wars won</th><th>Battles won</th><th>Castles destroyed</th><th>Fallen</th><th>Eras won</th><th>Best castle</th></tr>${d.models.map((m) =>
    `<tr><td>${esc(m.model)}</td><td>${Number(m.wars_won || 0)}/${Number((m.wars_won || 0) + (m.wars_lost || 0))}</td><td>${Number(m.battles_won || 0)}/${Number(m.battles || 0)}</td><td>${Number(m.castles_destroyed || 0)}</td><td>${Number(m.fallen || 0)}</td><td>${Number(m.eras_won || 0)}</td><td>${castleCell(m)}</td></tr>`).join("")}</table>` : "";
  const name = (id) => esc(civOf(id)?.name || id || "");
  const recent = (d.recent || []).length ? `<h3>Latest</h3><ul class="list">${d.recent.map((r) =>
    `<li><span>${name(r.civ)}: ${esc(RIVAL_KIND[r.kind] || r.kind)}${r.kind === "war_end" ? (r.value === 1 ? " (won)" : r.value === 0 ? " (lost)" : " (draw)") : r.kind === "battle" ? (r.value === 1 ? " won" : r.value === 0 ? " lost" : " drawn") : ""}${r.other ? ` · ${name(r.other)}` : ""}</span><span class="s">era ${Number(r.era || 1)} · day ${Number(r.day || 0)} · ${esc(r.model || "")}</span></li>`).join("")}</ul>` : "";
  const big = (n) => Number(n || 0).toLocaleString();
  const usage = (d.usage || []).length ? `<h3>What each model used</h3><table class="rival"><tr><th>Model</th><th>Calls</th><th>Failed</th><th>Tokens in</th><th>Tokens out</th><th>Tokens today</th><th>Time thinking</th></tr>${d.usage.map((u) =>
    `<tr><td>${esc(u.model)}</td><td>${big(u.calls)}</td><td>${big(u.failures)}</td><td>${big(u.tokens_in)}</td><td>${big(u.tokens_out)}</td><td>${big(u.tokens_today)}</td><td>${esc(fmtLeft(Number(u.seconds || 0)))}</td></tr>`).join("")}</table>` : "";
  const eras = (d.eras || []).length ? `<p class="sub">Eras won: ${d.eras.map((e) => `era ${Number(e.era)} → ${esc(e.name)} (day ${Number(e.day)})`).join(", ")}. Now in era ${Number(d.era)}.</p>` : "";
  el.innerHTML = `<table class="rival">${head}${mind}${rows}${castle}</table>${eras}${models}${usage}${recent}` +
    (!d.recent?.length ? `<div class="empty">No wars or battles yet: the scoreboard fills as the kings decide.</div>` : "");
}

const civOf = (id) => S.map.civs.find((c) => c.id === id);
const whoTag = (id) => {
  const c = civOf(id);
  return c ? `<span class="who"><span class="swatch" style="background:${esc(c.color)}"></span>${esc(c.name)}</span>` : "";
};

function addEvents(rows) {
  if (!rows?.length) return;
  const ol = $("#journal");
  ol.querySelector(".empty")?.remove();
  for (const e of rows) {
    const li = document.createElement("li");
    if (e.importance >= 6) li.className = "big";
    li.innerHTML = `<time>day ${e.day} · ${esc(e.hhmm)}</time><span>${whoTag(e.civ)}${esc(e.text)}</span>`;
    ol.prepend(li);
  }
  while (ol.children.length > 80) ol.lastChild.remove();
}

function addThoughts(rows) {
  if (!rows?.length) return;
  const ol = $("#thoughts");
  ol.querySelector(".empty")?.remove();
  for (const t of rows) {
    const li = document.createElement("li");
    li.innerHTML = `<time>${esc(t.hhmm)}</time><span>${whoTag(t.civ)}${esc(t.text)}</span>`;
    ol.prepend(li);
  }
  while (ol.children.length > 40) ol.lastChild.remove();
}

// ---------------------------------------------------------------- minimap (same isometric angle as the map)
function setupMinimap() {
  const cv = $("#minimap");
  cv.width = S.map.size * 2;
  cv.height = S.map.size + 2;
  cv.addEventListener("click", (e) => {
    const r = cv.getBoundingClientRect();
    const sx = (e.clientX - r.left) * cv.width / r.width - S.map.size, sy = (e.clientY - r.top) * cv.height / r.height - 1;
    const tx = (sx + 2 * sy) / 2, ty = (2 * sy - sx) / 2;
    view.centerOn(tx, ty);
  });
}

function drawMinimap() {
  if (!S.frame || !view) return;
  const cv = $("#minimap"), ctx = cv.getContext("2d"), n = S.map.size;
  const terr = territory(S.frame);
  const img = ctx.createImageData(cv.width, cv.height);
  const put = (x, y, r, g, b) => {
    const sx = Math.round(x - y + n), sy = Math.round((x + y) / 2) + 1;
    for (const dx of [0, 1]) {
      const i = (sy * cv.width + sx + dx) * 4;
      if (i >= 0 && i < img.data.length) { img.data[i] = r; img.data[i + 1] = g; img.data[i + 2] = b; img.data[i + 3] = 255; }
    }
  };
  const winter = S.frame.clock.season === "Winter";
  for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) {
    const water = view.terrain[y * n + x] === 1;
    let c = water ? [47, 95, 143] : winter ? [205, 214, 222] : [96, 132, 62];
    const t = terr[y * n + x];
    if (t && !water && !view.hiddenAt(t - 1, x, y)) { const k = S.map.civs[t - 1].color; c = mix(c, hexRgb(k), 0.35); }
    put(x, y, ...c);
  }
  for (const n2 of view.nodes) if (n2 && n2.kind === "tree" && n2.q > 0) {
    const p = n2.s.position; const x = (p.x / 16 + p.y / 8) / 2, y = (p.y / 8 - p.x / 16) / 2; put(x, y, 40, 84, 38);
  }
  ctx.putImageData(img, 0, 0);
  for (const b of S.frame.buildings) {
    if (view.hiddenAt(b[1], b[3], b[4], b[5])) continue;     // the selected kingdom hasn't seen it
    ctx.fillStyle = S.map.civs[b[1]].color;
    const sx = b[3] - b[4] + n, sy = (b[3] + b[4]) / 2 + 1;
    ctx.fillRect(sx - 1, sy - 1, b[2] === "castle" ? 5 : 3, b[2] === "castle" ? 4 : 2);
  }
  ctx.fillStyle = "#fff";
  for (const p of S.frame.people) if (p[8] && !view.hiddenAt(p[1], p[2], p[3])) ctx.fillRect(p[2] - p[3] + n, (p[2] + p[3]) / 2 + 1, 1, 1);
  const q = view.viewTiles().map(([x, y]) => [x - y + n, (x + y) / 2 + 1]);
  ctx.strokeStyle = "rgba(255,255,255,.85)"; ctx.lineWidth = 1;
  ctx.beginPath(); q.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y))); ctx.closePath(); ctx.stroke();
}

function territory(f) {
  const key = f.buildings.filter((b) => b[6] >= 1).map((b) => b[0]).join(",");
  if (key === S.terrKey && S.territory) return S.territory;
  const n = S.map.size, t = new Uint8Array(n * n), R = 10;
  for (const b of f.buildings) {
    if (b[6] < 1) continue;
    const cx = b[3] + (b[5] - 1) / 2, cy = b[4] + (b[5] - 1) / 2;
    for (let y = Math.max(0, Math.floor(cy - R)); y <= Math.min(n - 1, cy + R); y++)
      for (let x = Math.max(0, Math.floor(cx - R)); x <= Math.min(n - 1, cx + R); x++)
        if ((x - cx) ** 2 + (y - cy) ** 2 <= R * R) t[y * n + x] = b[1] + 1;
  }
  S.territory = t; S.terrKey = key;
  return t;
}
const hexRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const mix = (a, b, k) => a.map((v, i) => Math.round(v * (1 - k) + b[i] * k));

// ---------------------------------------------------------------- building tooltip
let tipTimer;
function showBuildingTip(row, pos) {
  const [, civI, type, , , , level, target, progress, , fire, hp, left, family] = row;
  const tip = $("#tip");
  const name = S.map.labels[type] || type;
  const civ = S.map.civs[civI];
  const lines = [`<b>${esc(name)}</b> ${level ? `· level ${level}` : "· being built"}`, `<span class="who"><span class="swatch" style="background:${esc(civ.color)}"></span>${esc(civ.name)}</span>`];
  if (family) lines.push(`Home of the ${esc(family)} family`);
  if (target > level) {
    const time = siteTime(left, S.frame?.clock.phase);
    lines.push(`${Math.round(progress * 100)}% - ${left != null && S.frame?.clock.phase === "work" ? time + " left" : time}`);
  }
  if (level) lines.push(`Condition ${Math.round(hp * 100)}%`);
  if (fire) lines.push(`<span style="color:var(--bad)">On fire!</span>`);
  tip.innerHTML = lines.join("<br>");
  tip.hidden = false;
  tip.style.left = `${Math.min(pos.x + 12, innerWidth - 200)}px`;
  tip.style.top = `${pos.y + 12}px`;
  clearTimeout(tipTimer);
  tipTimer = setTimeout(() => { tip.hidden = true; }, 4000);
}

// ---------------------------------------------------------------- the person card
async function openPerson(id) {
  const dlg = $("#card"), body = $("#cardBody");
  const r = await fetch(`/api/person/${id}`);
  if (!r.ok) { body.innerHTML = `<div class="pc"><p>This person is no longer in the world.</p></div>`; dlg.showModal(); return; }
  const p = await r.json();
  const link = (q) => q ? `<button ${q.alive ? `data-pid="${q.id}"` : 'class="gone" disabled'}>${esc(q.name)}${q.alive ? "" : " †"}</button>` : "";
  const exp = Object.entries(p.experience);
  const maxExp = Math.max(1, ...exp.map(([, v]) => v));
  const hColor = p.badge === "green" ? "var(--good)" : p.badge === "yellow" ? "var(--warn)" : "var(--bad)";
  const portrait = p.portraits[0]?.url;
  body.innerHTML = `<div class="pc">
    <header>
      ${portrait ? `<img class="portrait" src="${esc(portrait)}" alt="Portrait of ${esc(p.name)}">` : avatar(p)}
      <div><h3>${esc(p.name)}</h3>
        <div class="meta"><span class="swatch" style="background:${esc(p.color)}"></span> ${esc(p.civ_name)} · ${p.age} years (${esc(p.stage)})</div>
        <div class="meta">${esc(JOB[p.job] || p.job || "no job")} · ${esc(p.state)}${p.trait ? ` · ${esc(p.trait)}` : ""}${p.away ? ` · <b style="color:var(--warn)">${esc(p.away)}</b>` : ""}</div>
      </div>
      <button class="close" aria-label="Close">×</button>
    </header>
    <section><div class="hbar"><span>Happiness</span><span class="track"><i style="width:${p.happiness}%;background:${hColor}"></i></span><b>${p.happiness}</b></div></section>
    ${p.carrying ? `<section class="meta">Carrying ${p.carrying.amount} ${esc(p.carrying.res)}</section>` : ""}
    <section><h2>Home and family</h2>
      <div class="meta">${esc(p.home || "no bed")}</div>
      <div class="links" style="margin-top:6px">${p.spouse ? `<span class="meta">Spouse</span>${link(p.spouse)}` : ""}
        ${p.parents.length ? `<span class="meta">Parents</span>${p.parents.map(link).join("")}` : ""}
        ${p.children.length ? `<span class="meta">Children</span>${p.children.map(link).join("")}` : ""}
        ${!p.spouse && !p.parents.length && !p.children.length ? `<span class="meta">No family yet</span>` : ""}</div>
    </section>
    ${p.friends.length ? `<section><h2>Friends</h2><div class="links">${p.friends.map(link).join("")}</div></section>` : ""}
    <section><h2>Experience (hours)</h2>${exp.length ? exp.map(([k, v]) => `<div class="hbar"><span>${esc(JOB[k] || k)}</span><span class="track"><i style="width:${100 * v / maxExp}%;background:var(--gold)"></i></span><b>${v}</b></div>`).join("") : `<div class="meta">None yet</div>`}</section>
    <section><h2>Life so far</h2><ul>${p.history.slice().reverse().map((h) => `<li>${esc(h)}</li>`).join("") || "<li>Nothing yet</li>"}</ul></section>
    ${p.portraits.length > 1 ? `<section><h2>Portraits</h2><div class="gallery">${p.portraits.map((x) => `<img src="${esc(x.url)}" alt="${esc(x.caption)}" title="${esc(x.date)}">`).join("")}</div></section>` : ""}
  </div>`;
  body.querySelector(".close").onclick = () => dlg.close();
  body.querySelectorAll("[data-pid]").forEach((b) => (b.onclick = () => openPerson(Number(b.dataset.pid))));
  if (!dlg.open) dlg.showModal();
}
$("#card").addEventListener("click", (e) => { if (e.target.id === "card") e.target.close(); });

function avatar(p) {
  const initials = p.name.split(" ").map((w) => w[0]).join("");
  return `<svg class="portrait" viewBox="0 0 84 84" role="img" aria-label="${esc(p.name)}"><rect width="84" height="84" fill="var(--surface-2)"/>
    <circle cx="42" cy="34" r="15" fill="#e2b48c"/><path d="M14 84c2-20 14-28 28-28s26 8 28 28z" fill="${esc(p.color)}"/>
    <text x="42" y="80" text-anchor="middle" font-size="12" fill="#fff" opacity=".8">${esc(initials)}</text></svg>`;
}

// ---------------------------------------------------------------- timelapse (version 6): the last 24 hours in 2 minutes
async function playTimelapse() {
  if (S.playing) return;
  const btn = $("#timelapse");
  btn.disabled = true;
  let data;
  try { data = await getJSON("/api/timelapse?hours=24"); }
  catch {
    $("#ticker").hidden = false;
    $("#tickerText").textContent = "Timelapse could not load. Try again when the connection returns.";
    return;
  } finally { btn.disabled = false; }
  if (!data.frames.length) {
    $("#ticker").hidden = false;
    $("#tickerText").textContent = "Nothing recorded yet: the timelapse keeps one picture every 2 minutes of the live world.";
    return;
  }
  const frames = data.frames, comments = data.comments || [];
  const every = Math.max(60, (data.play_seconds || 120) * 1000 / frames.length);
  S.playing = { i: 0, timer: null, comments, said: 0 };
  $("#playbar").hidden = false;
  view.frameMs = every;
  const step = () => {
    const p = S.playing;
    if (!p) return;
    if (p.i >= frames.length) { stopTimelapse(); return; }
    const f = frames[p.i];
    if (f.nodes_all) view.setAllNodes(f.nodes_all);
    f.nodes = [];
    render(f, true);
    $("#playTime").textContent = `${f.clock.date} ${f.clock.hhmm}`;
    $("#playPos").style.width = `${100 * (p.i + 1) / frames.length}%`;
    while (p.said < p.comments.length && p.comments[p.said].t <= f.t) onComment(p.comments[p.said++]);
    p.i += 1;
    p.timer = setTimeout(step, every);
  };
  step();
}

async function stopTimelapse() {
  if (!S.playing) return;
  clearTimeout(S.playing.timer);
  S.playing = null;
  $("#playbar").hidden = true;
  view.lastFrameAt = 0;
  if (S.frame) render(S.frame, true);
  connection.retry(); // the greeting restores resources, roads and fog together
}

// ---------------------------------------------------------------- radio
function setupRadio() {
  const btn = $("#radio");
  btn.onclick = () => {
    S.radio = !S.radio;
    btn.setAttribute("aria-pressed", String(S.radio));
    if (S.radio) {
      speak("The radio is on. You will hear the big moments of both kingdoms.");
    } else {
      window.speechSynthesis?.cancel();
    }
  };
}

const queue = [];
let speaking = false;
function onComment(c) {
  $("#ticker").hidden = false;
  $("#tickerText").textContent = c.text;
  if (S.radio) { queue.push(c); playNext(); }
}

function playNext() {
  if (speaking || !queue.length) return;
  const c = queue.shift();
  speaking = true;
  const done = () => { speaking = false; playNext(); };
  if (c.audio) {
    const a = new Audio(c.audio);
    a.onended = done; a.onerror = () => { speak(c.text, done); };
    a.play().catch(() => speak(c.text, done));
  } else {
    speak(c.text, done);
  }
}

function speak(text, done) {
  const synth = window.speechSynthesis;
  if (!synth) { done?.(); return; }
  const u = new SpeechSynthesisUtterance(text);
  const voices = synth.getVoices().filter((v) => v.lang?.startsWith("en"));
  u.voice = voices.find((v) => /GB|UK|Daniel|George|Ryan/i.test(v.name + v.lang)) || voices[0] || null;
  u.rate = 1.0; u.pitch = 0.95;
  u.onend = () => done?.(); u.onerror = () => done?.();
  synth.speak(u);
}

// ---------------------------------------------------------------- tabs
function setupMapControls() {
  const map = $("#view-map");
  $("#centerMap").onclick = () => selectCiv(S.civ, true);
  $("#zoomIn").onclick = () => view.zoomAt(view.app.screen.width / 2, view.app.screen.height / 2, 1.25);
  $("#zoomOut").onclick = () => view.zoomAt(view.app.screen.width / 2, view.app.screen.height / 2, 0.8);
  $("#focusMap").onclick = (e) => {
    const hidden = map.classList.toggle("panels-hidden");
    e.currentTarget.setAttribute("aria-pressed", String(hidden));
    e.currentTarget.textContent = hidden ? "Show panels" : "Hide panels";
  };
  $("#panelControls").querySelectorAll("button").forEach((button) => {
    button.onclick = () => {
      map.dataset.panel = map.dataset.panel === button.dataset.panel ? "" : button.dataset.panel;
      $("#panelControls").querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.panel === map.dataset.panel)));
    };
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) view.app.ticker.stop();
    else { view.app.ticker.start(); showTab(S.tab); }
  });
}

function setupTabs() {
  const tabs = [...document.querySelectorAll(".tabs button")];
  tabs.forEach((b, i) => {
    b.id = `tab-${b.dataset.tab}`;
    b.setAttribute("aria-controls", `view-${b.dataset.tab}`);
    b.tabIndex = b.dataset.tab === S.tab ? 0 : -1;
    const panel = $(`#view-${b.dataset.tab}`);
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", b.id);
    b.onclick = () => showTab(b.dataset.tab);
    b.onkeydown = (event) => {
      const next = { ArrowRight: (i + 1) % tabs.length, ArrowLeft: (i + tabs.length - 1) % tabs.length, Home: 0, End: tabs.length - 1 }[event.key];
      if (next == null) return;
      event.preventDefault();
      tabs[next].focus();
      showTab(tabs[next].dataset.tab);
    };
  });
  window.addEventListener("hashchange", () => {
    const tab = location.hash.slice(1);
    if (tabs.some((b) => b.dataset.tab === tab)) showTab(tab);
  });
}

const pageRequests = new Set();
async function refreshPage(tab, work) {
  if (pageRequests.has(tab) || document.hidden) return;
  pageRequests.add(tab);
  const panel = $(`#view-${tab} .page-inner`);
  try {
    await work();
    panel?.querySelector(".page-error")?.remove();
  } catch {
    if (!panel || panel.querySelector(".page-error")) return;
    const notice = document.createElement("div");
    notice.className = "page-error";
    notice.setAttribute("role", "status");
    notice.innerHTML = `This view could not refresh. Any previous data is still shown. <button>Try again</button>`;
    notice.querySelector("button").onclick = () => refreshPage(tab, work);
    panel.prepend(notice);
  } finally { pageRequests.delete(tab); }
}

let statsTimer;
function showTab(tab) {
  S.tab = tab;
  history.replaceState(null, "", `#${tab}`);
  document.querySelectorAll(".tabs button").forEach((b) => {
    b.setAttribute("aria-selected", String(b.dataset.tab === tab));
    b.tabIndex = b.dataset.tab === tab ? 0 : -1;
  });
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${tab}`));
  clearInterval(statsTimer);
  if (tab === "training") { refreshPage(tab, () => training.refresh()); statsTimer = setInterval(() => refreshPage(tab, () => training.refresh()), 10000); }
  if (tab === "stats") { refreshPage(tab, loadStats); statsTimer = setInterval(() => refreshPage(tab, loadStats), 30000); }
  if (tab === "evolution") refreshPage(tab, loadEvolution);
  if (tab === "chronicle") {
    const refreshChronicles = () => refreshPage(tab, () => loadChronicles($("#chronSearch input").value.trim()));
    refreshChronicles(); statsTimer = setInterval(refreshChronicles, 15000);
  }
  if (tab === "diplomacy" && S.frame) drawDiplomacyHead(S.frame);
  if (tab === "diplomacy") { refreshPage(tab, loadRivalry); statsTimer = setInterval(() => refreshPage(tab, loadRivalry), 30000); }
  if (tab === "map" && !document.hidden) view.app.ticker.start();
  else view.app.ticker.stop();
}

// ---------------------------------------------------------------- charts (line, one axis, hover crosshair, table view)
function lineChart(el, { title, note, series, x, yFmt = (v) => v.toLocaleString(), step = false, yMin }) {
  const W = 640, H = 240, m = { l: 48, r: 92, t: 10, b: 26 };
  const pts = series.flatMap((s) => s.points);
  if (!pts.length) { el.innerHTML = `<figcaption>${esc(title)}</figcaption><div class="empty">No data yet: the first point comes at the end of game day 1.</div>`; return; }
  const xs = [...new Set(pts.map((p) => p[0]))].sort((a, b) => a - b);
  const x0 = xs[0], x1 = Math.max(xs[xs.length - 1], x0 + 1);
  let y0 = yMin ?? Math.min(0, ...pts.map((p) => p[1])), y1 = Math.max(...pts.map((p) => p[1]));
  const ticks = niceTicks(y0, y1 === y0 ? y0 + 1 : y1, 4); y0 = ticks[0]; y1 = ticks[ticks.length - 1];
  const X = (v) => m.l + (v - x0) / (x1 - x0) * (W - m.l - m.r), Y = (v) => H - m.b - (v - y0) / (y1 - y0) * (H - m.t - m.b);
  const path = (p) => p.map(([a, b], i) => {
    if (!i) return `M${X(a)},${Y(b)}`;
    return step ? `H${X(a)}V${Y(b)}` : `L${X(a)},${Y(b)}`;
  }).join("");
  const xt = niceTicks(x0, x1, 6).filter((v) => v >= x0 && v <= x1 && Number.isInteger(v));
  // direct labels at the line ends (<= 4 series), nudged apart
  const ends = series.filter((s) => s.points.length).map((s) => ({ s, y: Y(s.points[s.points.length - 1][1]) })).sort((a, b) => a.y - b.y);
  for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 13) ends[i].y = ends[i - 1].y + 13;
  const svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}">
    <g class="grid">${ticks.map((t) => `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(t)}" y2="${Y(t)}"/>`).join("")}</g>
    <g class="axis">${ticks.map((t) => `<text x="${m.l - 8}" y="${Y(t) + 4}" text-anchor="end">${esc(yFmt(t))}</text>`).join("")}
      ${xt.map((t) => `<text x="${X(t)}" y="${H - 6}" text-anchor="middle">${esc(x.fmt(t))}</text>`).join("")}</g>
    ${series.filter((s) => s.connect !== false).map((s) => `<path d="${path(s.points)}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`).join("")}
    ${series.filter((s) => s.connect === false || s.points.length === 1).flatMap((s) => s.points.map((p) => `<circle cx="${X(p[0])}" cy="${Y(p[1])}" r="3" fill="${s.color}"/>`)).join("")}
    ${series.length <= 4 ? ends.map((e) => `<text class="lbl" x="${W - m.r + 8}" y="${e.y + 4}">${esc(e.s.name)}</text>`).join("") : ""}
    <line class="cross" y1="${m.t}" y2="${H - m.b}" stroke="var(--ink-3)" stroke-width="1" visibility="hidden"/>
    <g class="dots"></g>
    <rect class="hit" x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent"/>
  </svg>`;
  const legend = series.length >= 2 ? `<div class="legend">${series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>` : "";
  const table = `<details class="table"><summary>Show as a table</summary><table><tr><th>${esc(x.name)}</th>${series.map((s) => `<th>${esc(s.name)}</th>`).join("")}</tr>
    ${xs.map((v) => `<tr><td>${esc(x.fmt(v))}</td>${series.map((s) => { const p = s.points.find((q) => q[0] === v); return `<td>${p ? esc(yFmt(p[1])) : "–"}</td>`; }).join("")}</tr>`).join("")}</table></details>`;
  el.innerHTML = `<figcaption>${esc(title)}</figcaption>${note ? `<div class="note">${esc(note)}</div>` : ""}${legend}${svg}${table}`;
  const root = el.querySelector("svg"), cross = root.querySelector(".cross"), dots = root.querySelector(".dots");
  const tip = chartTip();
  root.querySelector(".hit").addEventListener("pointermove", (e) => {
    const r = root.getBoundingClientRect();
    const px = (e.clientX - r.left) * W / r.width;
    const v = xs.reduce((a, b) => (Math.abs(X(b) - px) < Math.abs(X(a) - px) ? b : a), xs[0]);
    cross.setAttribute("x1", X(v)); cross.setAttribute("x2", X(v)); cross.setAttribute("visibility", "visible");
    const rows = series.map((s) => ({ s, p: s.points.find((q) => q[0] === v) })).filter((q) => q.p);
    dots.innerHTML = rows.map(({ s, p }) => `<circle cx="${X(v)}" cy="${Y(p[1])}" r="4" fill="${s.color}" stroke="var(--surface)" stroke-width="2"/>`).join("");
    tip.innerHTML = `<div style="color:var(--ink-3);margin-bottom:3px">${esc(x.fmt(v))}</div>` +
      rows.map(({ s, p }) => `<div class="row"><span><span class="swatch" style="background:${s.color}"></span> ${esc(s.name)}</span><b>${esc(yFmt(p[1]))}</b></div>`).join("");
    tip.hidden = false;
    tip.style.left = `${Math.min(e.clientX + 14, innerWidth - 180)}px`; tip.style.top = `${e.clientY + 14}px`;
  });
  root.querySelector(".hit").addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); dots.innerHTML = ""; tip.hidden = true; });
}

function chartTip() {
  let t = $(".chart-tip");
  if (!t) { t = document.createElement("div"); t.className = "chart-tip"; t.hidden = true; document.body.append(t); }
  return t;
}

function niceTicks(lo, hi, n) {
  const span = hi - lo || 1, raw = span / n, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((k) => k * mag).find((k) => span / k <= n) || mag * 10;
  const a = Math.floor(lo / step) * step, b = Math.ceil(hi / step) * step, out = [];
  for (let v = a; v <= b + step / 2; v += step) out.push(+v.toFixed(6));
  return out;
}

// ---------------------------------------------------------------- Stats
async function loadStats() {
  const data = await getJSON("/api/stats");
  const day = { name: "Day", fmt: (v) => `day ${v}` };
  const civS = (key) => data.civs.map((c) => ({ name: c.name, color: c.color, points: c.days.map((d) => [d.day, d[key]]) }));
  lineChart($("#chartPop"), { title: "Population", note: "People in each kingdom at the end of the day", series: civS("pop"), x: day });
  lineChart($("#chartCastle"), { title: "Castle level", note: "Levels 1–10; the age changes at 3, 5 and 8", series: civS("castle"), x: day, step: true, yMin: 0 });
  lineChart($("#chartMorale"), { title: "Morale", note: "Multiplies all work: 0.5 at worst, 1.2 at best", series: civS("morale"), x: day, yFmt: (v) => v.toFixed(2) });
  lineChart($("#chartBuildings"), { title: "Buildings", note: "Finished buildings of every type", series: civS("buildings"), x: day });
  const i = S.statsCiv ?? S.civ;
  const civ = data.civs[i];
  const filt = $("#statsCiv");
  filt.innerHTML = data.civs.map((c, k) => `<button aria-pressed="${k === i}" data-i="${k}"><span class="swatch" style="background:${esc(c.color)}"></span>${esc(c.name)}</button>`).join("");
  filt.querySelectorAll("button").forEach((b) => (b.onclick = () => { S.statsCiv = Number(b.dataset.i); loadStats(); }));
  const res = Object.keys(RES_LABEL);
  lineChart($("#chartRes"), {
    title: `Resources in store — ${civ.name}`, note: "End-of-day stock (the filter above picks the kingdom)",
    series: res.map((r) => ({ name: RES_LABEL[r], color: RES_COLOR[r], points: civ.days.map((d) => [d.day, d[r] ?? 0]) })), x: day,
  });
}

// ---------------------------------------------------------------- Evolution
async function loadEvolution() {
  const d = await getJSON("/api/evolution");
  const night = { name: "Night", fmt: (v) => `night ${v}` };
  const nn = d.nights;
  lineChart($("#chartNights"), {
    title: "Score of the best genome, night by night",
    note: "Compare benchmark scores only across rounds with matching test settings. See Training for the settings and per-round reports.",
    series: [
      { name: "Benchmark", color: "var(--s1)", points: nn.filter((n) => n.bench_fitness != null).map((n) => [n.night, n.bench_fitness]) },
      { name: "Best of the night", color: "var(--s2)", points: nn.map((n) => [n.night, n.best_fitness]) },
    ], x: night, yFmt: (v) => v.toFixed(2),
  });
  const st = d.stall || { nights: 0, limit: 21 };
  $("#stall").innerHTML = st.nights >= st.limit
    ? `<div class="card" style="border-color:var(--warn)"><b>The benchmark has not risen for ${st.nights} nights.</b> The doc's rule: only now is it worth trying reinforcement learning.</div>`
    : nn.length ? `<p class="sub">Nights since the benchmark last rose: ${st.nights} (the alarm rings at ${st.limit}).</p>` : "";
  const g = $("#genes");
  if (d.best) {
    g.innerHTML = `<h2>Genes of ${esc(d.best.id)} (in the world today) <button class="linkbtn" id="treeBtn">Family tree</button></h2><div class="genes">` + Object.entries(d.best.genes).map(([k, v]) => {
      const [lo, hi] = d.ranges[k] || [0, 1];
      return `<div class="gene"><span>${esc(k.replaceAll("_", " "))}</span><span class="track" title="range ${lo}–${hi}"><i style="left:${100 * (v - lo) / ((hi - lo) || 1)}%"></i></span><b>${v.toFixed(2)}</b></div>`;
    }).join("") + `</div>`;
    $("#treeBtn").onclick = () => showTree(d.best.id);
  } else g.innerHTML = `<h2>Genes</h2><div class="empty">No night of evolution yet: both kingdoms play the default genome.</div>`;
  $("#lessons").innerHTML = `<h2>Lessons of the night</h2>` + (d.lessons.length ? `<ul class="list">${d.lessons.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : `<div class="empty">No lessons yet.</div>`);
  $("#hall").innerHTML = `<h2>Hall of Champions</h2>` + (d.hall.length ? `<ol class="list">${d.hall.map((h, i) => `<li><span>${i + 1}. ${esc(h.id)}${i < 3 ? " ★" : ""}</span><b>Elo ${h.elo}</b></li>`).join("")}</ol>` : `<div class="empty">Empty until the first night.</div>`);
  $("#rulebook").innerHTML = `<h2>The rulebook (AI-written rules)</h2>` + (d.rules.length ? `<ul class="list">${d.rules.map((r) => `<li><span>${esc(r.name)}</span><span><span class="status ${esc(r.status)}">${esc(r.status)}</span> ${r.wins}/${r.games}</span></li>`).join("")}</ul>` : `<div class="empty">No rules proposed yet.</div>`);
}

async function showTree(id) {
  const r = await fetch(`/api/genome/${encodeURIComponent(id)}/tree?depth=6`);
  const box = $("#tree");
  if (!r.ok) { box.innerHTML = `<div class="empty">No family tree for ${esc(id)}.</div>`; return; }
  const { tree } = await r.json();
  const byDepth = {};
  for (const n of tree) (byDepth[n.depth] ||= []).push(n);
  box.innerHTML = `<h2>Family tree of ${esc(id)}</h2><p class="sub">Each row is one generation further back. The genes listed changed from the parents' values (crossover takes each gene from one parent; mutation moves a few).</p>` +
    Object.keys(byDepth).sort((a, b) => a - b).map((dpt) => `<div class="gen"><div class="gen-k">${dpt === "0" ? "today" : dpt === "1" ? "parents" : `${dpt} back`}</div><div class="gen-row">${byDepth[dpt].map((n) => `
      <div class="gnode"><b>${esc(n.id)}</b><span>${n.night ? `night ${n.night}` : "start"} · F ${n.fitness != null ? n.fitness.toFixed(3) : "–"}</span>
      ${Object.entries(n.changed).slice(0, 6).map(([k, v]) => `<span class="chg">${esc(k.replaceAll("_", " "))}: ${v.from} → ${v.to}</span>`).join("")}
      ${n.parents.length ? "" : `<span class="chg">no parents (first night)</span>`}</div>`).join("")}</div></div>`).join("");
  box.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------------------------------------------------------------- Chronicle
let chroniclesKey;
async function loadChronicles(q = "") {
  const rows = await getJSON(`/api/chronicles?limit=14${q ? `&search=${encodeURIComponent(q)}` : ""}`);
  const el = $("#chronicles");
  const key = JSON.stringify([q, rows]);
  if (key === chroniclesKey) return;
  chroniclesKey = key;
  if (!rows.length) { el.innerHTML = `<div class="empty">${q ? "Nothing found." : "No Chronicle has been saved yet. Editions are normally prepared at 06:15 and published at 07:00; a missed edition is recovered when the live world restarts. This archive refreshes automatically."}</div>`; return; }
  el.innerHTML = rows.map((c) => {
    const rest = c.body.split("\n").slice(3);            // title line, blank, the headline again
    const pics = c.images.map((i) => `<figure style="margin:0"><img src="${esc(i.url)}" alt="${esc(i.caption)}"><figcaption>${esc(i.caption)}</figcaption></figure>`).join("");
    const table = c.table.length ? `<table><tr>${Object.keys(c.table[0]).map((k) => `<th>${esc(k)}</th>`).join("")}</tr>${c.table.map((r) => `<tr>${Object.values(r).map((v) => `<td>${esc(v)}</td>`).join("")}</tr>`).join("")}</table>` : "";
    const text = rest.join("\n").split(/\n\s*Numbers of the day:[\s\S]*/)[0].trim();
    const doctor = c.doctor ? `<div class="doctor"><b>The doctor's week</b>${esc(c.doctor.text)}</div>` : "";
    return `<article class="paper"><div class="date">${esc(c.date)}</div><h2>${esc(c.headline)}</h2>
      ${pics ? `<div class="pics">${pics}</div>` : ""}<p>${esc(text)}</p>${table}${doctor}
      <div class="by">${c.by_ai ? "Written by the local AI; numbers by the code." : "Written by the code (the local AI was away)."}</div></article>`;
  }).join("");
}

start().catch((e) => { connectionState("error"); console.error(e); });
