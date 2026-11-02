// The map: 2D isometric view drawn with PixiJS (WebGL). It only watches the world.
// Tile (x, y) is centred on integer coordinates, like the simulation (a person at x = 3.4 stands on tile 3).

const TW = 32, TH = 16;                       // one tile on screen at zoom 1
const iso = (x, y) => [(x - y) * TW / 2, (x + y) * TH / 2];

const SEASON = {
  Spring: { grass: [0x5f8f3e, 0x6a9a45, 0x57863a], tree: 0xffffff, field: 0x7fae4a, snow: 0 },
  Summer: { grass: [0x6f9440, 0x7a9c48, 0x688c3b], tree: 0xf4ffe0, field: 0xd8b84a, snow: 0 },
  Autumn: { grass: [0x8a8a45, 0x958f4a, 0x7f7f3f], tree: 0xffb070, field: 0x9a7a45, snow: 0 },
  Winter: { grass: [0xd9e2ea, 0xcfd9e3, 0xe4ebf1], tree: 0xc8d6e2, field: 0xdfe6ec, snow: 1 },
};
const WATER = [0x2f5f8f, 0x34679a, 0x2b5884];
const CARRY = { food: 0xe8c55a, wood: 0x8a5a2b, stone: 0x9aa0a6, ore: 0xa0522d, gold: 0xffd700, coins: 0xffd700 };
const BADGE = [0x3fbf5f, 0xe8c13a, 0xe0483e];
const STONE_TYPES = new Set(["well", "forge", "wall", "tower", "mint", "castle", "wonder", "library", "shrine", "town_hall", "guild_hall", "academy", "foundry", "glassworks", "university", "factory", "civic_office", "apartments", "research_lab", "skyscraper"]);
const HEIGHT = { house: 13, farm: 0, lumber_camp: 11, mill: 15, quarry: 7, granary: 16, well: 6, forge: 13,
  iron_mine: 9, wall: 10, barracks: 14, tower: 30, archery_range: 9, market: 11, gold_mine: 9, mint: 16,
  stable: 12, siege_workshop: 16, castle: 26, wonder: 34,
  school: 14, library: 18, tavern: 15, shrine: 22, town_hall: 25, miners_lodge: 12, guild_hall: 24,
  brickyard: 10, academy: 25, foundry: 20, glassworks: 22, university: 30, factory: 24,
  civic_office: 35, apartments: 45, research_lab: 32, skyscraper: 95 };

function shade(color, f) {
  const r = Math.min(255, ((color >> 16) & 255) * f), g = Math.min(255, ((color >> 8) & 255) * f),
    b = Math.min(255, (color & 255) * f);
  return (r << 16) | (g << 8) | b;
}
const hex = (s) => parseInt(String(s).replace("#", ""), 16);
const BANDIT = 0x2b2b2b;
// army plan: the uniform shows the Forge's level — leather, bronze, iron, steel, gold
const METAL = [null, 0x8a5a2b, 0xcd7f32, 0x9aa0a6, 0xdde3e8, 0xffd23a];

// Sunrise / sunset in local time for each month (Bucharest, DST included).
const SUN = [[7.8, 17.0], [7.3, 17.6], [6.7, 18.3], [6.6, 20.0], [6.0, 20.5], [5.5, 21.0], [5.7, 20.9],
  [6.3, 20.3], [6.9, 19.4], [7.5, 18.4], [7.1, 16.8], [7.7, 16.6]];

export function lightAt(hour, month) {
  const [rise, set] = SUN[(month - 1 + 12) % 12];
  // night 0 = full daylight, 1 = deep night; warm = the morning/evening glow
  const ramp = (a, b, t) => Math.min(1, Math.max(0, (t - a) / (b - a)));
  const day = Math.min(ramp(rise - 0.6, rise + 0.8, hour), 1 - ramp(set - 0.8, set + 0.6, hour));
  const warm = Math.max(0, 1 - Math.abs(hour - (rise + 0.5)) / 1.4, 1 - Math.abs(hour - (set - 0.3)) / 1.4) * 0.9;
  return { night: 1 - day, warm: day > 0 ? warm : 0 };
}

export class MapView {
  constructor(el, onPerson, onBuilding) {
    this.el = el;
    this.onPerson = onPerson;
    this.onBuilding = onBuilding;
    this.people = new Map();
    this.units = new Map();            // version 5: soldiers, rams, spies
    this.carts = new Map();
    this.fx = [];
    this.fog = {};                     // version 7: civ id → Uint8Array of known tiles
    this.fogCiv = null;
    this.buildings = new Map();
    this.nodes = [];
    this.season = null;
    this.frameMs = 1000;
    this.lastFrameAt = 0;
    this.light = { night: 0, warm: 0 };
    this.dragged = false;
  }

  async init(map) {
    this.map = map;
    this.size = map.size;
    this.civs = map.civs;
    this.labels = map.labels;
    this.terrain = Uint8Array.from(atob(map.terrain), (c) => c.charCodeAt(0));
    const app = new PIXI.Application();
    await app.init({ resizeTo: this.el, background: 0x0d1117, antialias: true, autoDensity: true,
      resolution: Math.min(2, window.devicePixelRatio || 1) });
    this.app = app;
    this.el.appendChild(app.canvas);

    this.world = new PIXI.Container();
    app.stage.addChild(this.world);
    this.terrainSprite = new PIXI.Sprite();
    this.ground = new PIXI.Container();             // fields, stumps: always below everything
    this.roadsG = new PIXI.Graphics();              // version 7: roads under everything else
    this.fogSprite = new PIXI.Sprite();             // version 7: the selected kingdom's unknown land
    this.objects = new PIXI.Container();
    this.objects.sortableChildren = true;
    this.dark = new PIXI.Graphics();
    this.lights = new PIXI.Container();
    this.overlay = new PIXI.Container();            // bars and labels: readable at night
    this.world.addChild(this.terrainSprite, this.roadsG, this.ground, this.objects, this.fogSprite, this.dark,
      this.lights, this.overlay);
    const [x0] = iso(0, this.size), [x1] = iso(this.size, 0), [, y1] = iso(this.size, this.size);
    this.bounds = { x0: x0 - TW, x1: x1 + TW, y0: -TH * 4, y1: y1 + TH * 2 };

    this.makeTextures();
    this.buildNodes(map.nodes);
    const roads = Uint8Array.from(atob(map.roads || ""), (c) => c.charCodeAt(0));
    for (let i = 0; i < roads.length; i++) if (roads[i]) this.drawRoad(i % this.size, Math.floor(i / this.size), roads[i] - 1);
    if (map.camp) this.drawCamp(map.camp[0], map.camp[1]);
    for (const [cid, b64] of Object.entries(map.fog || {})) this.fog[cid] = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    this.setupInput();
    app.ticker.add((t) => this.animate(t.deltaMS));
  }

  // ------------------------------------------------------------------ textures
  tex(draw, w = 24, h = 24) {
    const g = new PIXI.Graphics();
    draw(g);
    return this.app.renderer.generateTexture({ target: g, resolution: 3, antialias: true });
  }

  makeTextures() {
    const T = {};
    T.tree = this.tex((g) => {
      g.rect(-1.2, -3, 2.4, 5).fill(0x5a3b1e);
      g.poly([0, -22, -7, -8, 7, -8]).fill(0x2f6b2a);
      g.poly([0, -17, -8.5, -3, 8.5, -3]).fill(0x2a5f25);
      g.poly([0, -22, 0, -8, 7, -8]).fill({ color: 0x000000, alpha: 0.12 });
    });
    T.stump = this.tex((g) => { g.ellipse(0, 0, 3.2, 1.8).fill(0x6b4a2a); g.ellipse(0, -0.6, 2.4, 1.2).fill(0xb08a5a); });
    T.bush = this.tex((g) => {
      g.circle(-3, -3, 4).fill(0x3d7a35); g.circle(3, -3.5, 4).fill(0x367030); g.circle(0, -6, 4).fill(0x428a3a);
      for (const [x, y] of [[-3, -5], [2, -7], [3, -3], [-1, -2]]) g.circle(x, y, 1.1).fill(0xc22d3a);
    });
    T.bushEmpty = this.tex((g) => { g.circle(-3, -3, 3.5).fill(0x4b6b3a); g.circle(3, -3, 3.5).fill(0x46633a); g.circle(0, -5.5, 3.5).fill(0x557545); });
    T.animal = this.tex((g) => {
      g.ellipse(0, -5, 5, 2.6).fill(0x8b5a2b);
      g.rect(-4, -4, 1, 4).fill(0x6b4423); g.rect(3, -4, 1, 4).fill(0x6b4423);
      g.circle(5.5, -8, 1.8).fill(0x8b5a2b); g.rect(5.6, -11, 0.6, 2.4).fill(0x5a3b1e);
    });
    const rocks = (base, speck) => this.tex((g) => {
      g.poly([-10, 0, -6, -8, 0, -11, 7, -7, 10, 0, 0, 3]).fill(shade(base, 0.85));
      g.poly([-6, -8, 0, -11, 7, -7, 1, -4]).fill(base);
      g.poly([-3, 1, 1, -4, 7, -7, 10, 0]).fill(shade(base, 0.7));
      if (speck) for (const [x, y] of [[-4, -5], [2, -7], [5, -2], [-1, -1]]) g.circle(x, y, 1.2).fill(speck);
    });
    T.stone = rocks(0xa3a8ad, null);
    T.iron = rocks(0x6e6a68, 0xb5582e);
    T.gold = rocks(0x8f8a7a, 0xffd23a);
    T.body = this.tex((g) => { g.roundRect(-2.6, -9, 5.2, 8, 1.6).fill(0xffffff); g.rect(-2.2, -1.5, 1.6, 1.8).fill(0x3a2e24); g.rect(0.6, -1.5, 1.6, 1.8).fill(0x3a2e24); });
    T.dress = this.tex((g) => { g.poly([-2.4, -9, 2.4, -9, 3.6, 0, -3.6, 0]).fill(0xffffff); });
    T.head = this.tex((g) => { g.circle(0, 0, 2.3).fill(0xe2b48c); g.arc(0, -0.4, 2.4, Math.PI, 0).fill(0x4a3220); });
    T.shadow = this.tex((g) => { g.ellipse(0, 0, 4, 1.6).fill({ color: 0x000000, alpha: 0.28 }); });
    T.sack = this.tex((g) => { g.roundRect(-2.5, -3, 5, 5, 1.5).fill(0xffffff); g.rect(-1, -4, 2, 1.2).fill(0xffffff); });
    T.log = this.tex((g) => { g.roundRect(-4, -1.5, 8, 3, 1.2).fill(0xffffff); g.circle(4, 0, 1.4).fill(0xd8b48a); });
    T.dot = this.tex((g) => { g.circle(0, 0, 1.6).fill(0xffffff); });
    T.tool = this.tex((g) => { g.rect(-0.4, -6, 0.8, 6).fill(0x6b4a2a); g.rect(-1.6, -7, 3.2, 1.6).fill(0x9aa0a6); });
    // version 5: what soldiers carry (drawn on top of a body tinted in the kingdom's colour)
    T.club = this.tex((g) => { g.rect(-0.6, -7, 1.2, 7).fill(0x6b4a2a); g.circle(0, -7, 1.4).fill(0x6b4a2a); });
    T.spear = this.tex((g) => { g.rect(-0.4, -14, 0.8, 14).fill(0x7a5a3a); g.poly([-1.3, -14, 0, -17.5, 1.3, -14]).fill(0xc9ced3); });
    T.bow = this.tex((g) => { g.arc(0, -5, 5, -1.2, 1.2).stroke({ width: 1, color: 0x7a5a3a }); g.moveTo(1.8, -9.6).lineTo(1.8, -0.4).stroke({ width: 0.5, color: 0xe8e2d0 }); });
    T.sword = this.tex((g) => { g.rect(-0.5, -9, 1, 8).fill(0xd8dde2); g.rect(-1.8, -1.6, 3.6, 0.9).fill(0x6b4a2a); });
    T.shield = this.tex((g) => { g.roundRect(-2.4, -3.5, 4.8, 6, 1.5).fill(0xffffff); });
    T.horse = this.tex((g) => {
      g.ellipse(0, -5, 7, 3).fill(0x7b4f2c); g.rect(-6, -4, 1.4, 5).fill(0x5e3b20); g.rect(4.6, -4, 1.4, 5).fill(0x5e3b20);
      g.rect(-3, -4, 1.4, 5).fill(0x5e3b20); g.rect(2, -4, 1.4, 5).fill(0x5e3b20);
      g.poly([5, -6, 9, -11, 10.5, -9.5, 7, -4]).fill(0x7b4f2c);
    });
    T.ram = this.tex((g) => {
      g.poly([-10, -2, 10, -2, 7, -10, -7, -10]).fill(0x7a5a3a); g.poly([-7, -10, 7, -10, 0, -15]).fill(0x5e4430);
      g.rect(8, -6, 6, 2.4).fill(0x4e3a28); for (const x of [-7, 0, 7]) g.circle(x, -1, 2.4).fill(0x3a2a1c);
    });
    T.spy = this.tex((g) => { g.poly([-3, 0, 3, 0, 2, -9, -2, -9]).fill(0x24262b); g.circle(0, -10, 2.2).fill(0x2f3238); });
    T.cart = this.tex((g) => {
      g.rect(-7, -8, 14, 6).fill(0x8a6a45); g.rect(-6, -11, 5, 3).fill(0xe8c55a); g.rect(0, -10.5, 5, 2.5).fill(0xa3a8ad);
      g.circle(-4.5, -1.5, 2.4).fill(0x3a2a1c); g.circle(4.5, -1.5, 2.4).fill(0x3a2a1c);
    });
    T.flag = this.tex((g) => { g.rect(-0.3, -6, 0.6, 6).fill(0x333333); g.poly([0, -6, 4, -5, 0, -4]).fill(0xffffff); });
    T.helmet = this.tex((g) => { g.arc(0, 0, 2.6, Math.PI, 0).fill(0xffffff); g.rect(-2.8, -0.2, 5.6, 0.8).fill(0xffffff); });
    T.spark = this.tex((g) => { for (let i = 0; i < 6; i++) { const a = i * Math.PI / 3; g.moveTo(0, 0).lineTo(Math.cos(a) * 4, Math.sin(a) * 4).stroke({ width: 1.2, color: 0xffffff }); } });
    T.glow = this.tex((g) => {
      for (let i = 8; i >= 1; i--) g.circle(0, 0, i * 4).fill({ color: 0xffb347, alpha: 0.06 + (8 - i) * 0.012 });
    });
    this.T = T;
  }

  // ------------------------------------------------------------------ terrain (redrawn when the season changes)
  drawTerrain(season) {
    const s = SEASON[season] || SEASON.Spring;
    const n = this.size, W = n * TW, H = n * TH + TH;
    const c = document.createElement("canvas");
    c.width = W; c.height = H;
    const ctx = c.getContext("2d");
    const ox = W / 2, oy = TH / 2;
    const css = (v) => "#" + v.toString(16).padStart(6, "0");
    for (let y = 0; y < n; y++) {
      for (let x = 0; x < n; x++) {
        const water = this.terrain[y * n + x] === 1;
        const v = ((x * 73856093) ^ (y * 19349663)) >>> 0;
        ctx.fillStyle = css((water ? WATER : s.grass)[v % 3]);
        const [sx, sy] = iso(x, y);
        const cx = sx + ox, cy = sy + oy;
        ctx.beginPath();
        ctx.moveTo(cx, cy - TH / 2 - 0.5); ctx.lineTo(cx + TW / 2 + 0.5, cy);
        ctx.lineTo(cx, cy + TH / 2 + 0.5); ctx.lineTo(cx - TW / 2 - 0.5, cy);
        ctx.closePath(); ctx.fill();
      }
    }
    // the border between the two halves (the map is mirrored)
    ctx.strokeStyle = "rgba(255,255,255,0.10)"; ctx.setLineDash([6, 6]); ctx.lineWidth = 2;
    const [ax, ay] = iso(-0.5, n / 2 - 0.5), [bx, by] = iso(n - 0.5, n / 2 - 0.5);
    ctx.beginPath(); ctx.moveTo(ax + ox, ay + oy - TH / 2 + TH / 2); ctx.lineTo(bx + ox, by + oy); ctx.stroke();
    if (this.terrainSprite.texture && this.terrainSprite.texture !== PIXI.Texture.EMPTY) this.terrainSprite.texture.destroy(true);
    this.terrainSprite.texture = PIXI.Texture.from(c);
    this.terrainSprite.position.set(-ox, -oy);
  }

  // ------------------------------------------------------------------ resource nodes
  buildNodes(rows) {
    for (const [id, kindI, x, y, size, q] of rows) {
      const kind = this.map.kinds[kindI];
      const s = new PIXI.Sprite();
      s.anchor.set(0.5, 0.92);
      const h = (size - 1) / 2;
      const [sx, sy] = iso(x + h, y + h);
      s.position.set(sx, sy + (size > 1 ? TH / 2 : 0));
      s.zIndex = sy;
      s.scale.set(size > 1 ? 1.6 : 1);
      this.objects.addChild(s);
      this.nodes[id] = { s, kind, q, size, phase: Math.random() * 6 };
      this.styleNode(id);
    }
  }

  styleNode(id) {
    const n = this.nodes[id];
    const T = this.T, s = n.s, base = n.size > 1 ? 1.6 : 1;
    const tint = (SEASON[this.season] || SEASON.Spring).tree;
    s.visible = true;
    s.tint = 0xffffff;
    if (n.kind === "tree") {
      s.texture = n.q > 0 ? T.tree : T.stump;
      if (n.q > 0) s.tint = tint;
      s.scale.set(base);
    } else if (n.kind === "bush") {
      s.texture = n.q > 0 ? T.bush : T.bushEmpty;
    } else if (n.kind === "animal") {
      s.texture = T.animal;
      s.visible = n.q > 0;
    } else {
      s.texture = T[n.kind] || T.stone;
      s.visible = n.q > 0;
      s.scale.set(base * (0.45 + 0.55 * n.q / 20));
    }
  }

  // ------------------------------------------------------------------ version 7: roads, camp, fog
  drawRoad(x, y) {
    const g = this.roadsG;
    const t = iso(x - 0.5, y - 0.5), r = iso(x + 0.5, y - 0.5), b = iso(x + 0.5, y + 0.5), l = iso(x - 0.5, y + 0.5);
    g.poly([...t, ...r, ...b, ...l]).fill({ color: 0x9c8462, alpha: 0.85 });
    g.poly([...t, ...r, ...b, ...l]).stroke({ width: 0.6, color: 0x6e5a3e, alpha: 0.5 });
  }

  drawCamp(x, y) {
    const g = new PIXI.Graphics();
    const [cx, cy] = iso(x, y);
    for (const [dx, dy] of [[-10, 0], [8, -4], [0, 6]]) {
      g.poly([cx + dx - 8, cy + dy, cx + dx, cy + dy - 12, cx + dx + 8, cy + dy]).fill(0x4a3b2c);
      g.poly([cx + dx - 2, cy + dy, cx + dx, cy + dy - 5, cx + dx + 2, cy + dy]).fill(0x1d1712);
    }
    g.circle(cx - 1, cy + 1, 2.5).fill(0xff7a1a);
    g.zIndex = cy + 6;
    this.objects.addChild(g);
    this.camp = [x, y];
    this.campG = g;
  }

  // ------------------------------------------------------------------ what the selected kingdom knows
  // A kingdom sees its own things everywhere; the other kingdom's (and the bandits') only on land it has explored.
  known(x, y) {
    const seen = this.fog[this.fogCiv];
    if (!seen) return true;                                  // no fog in this world
    const n = this.size;
    const tx = Math.min(n - 1, Math.max(0, Math.round(x))), ty = Math.min(n - 1, Math.max(0, Math.round(y)));
    return !!seen[ty * n + tx];
  }

  hiddenAt(civI, x, y, size = 1) {
    if (civI >= 0 && this.civs[civI]?.id === this.fogCiv) return false;
    for (let dy = 0; dy < size; dy++) for (let dx = 0; dx < size; dx++) if (this.known(x + dx, y + dy)) return false;
    return true;
  }

  applyFog() {
    for (const b of this.buildings.values()) {
      const [, civI, , x, y, size] = b.row;
      const show = !this.hiddenAt(civI, x, y, size);
      b.hidden = !show;
      for (const part of [b.g, b.label, b.bar, b.flames]) if (part) part.visible = show;
    }
    for (const p of this.people.values()) {
      const [, civI, x, y, , , , , inside] = p.row;
      p.c.visible = inside === 1 && !this.hiddenAt(civI, x, y);
    }
    for (const u of this.units.values()) {
      const [, civI, x, y] = u.row;
      u.c.visible = u.show && !this.hiddenAt(civI, x, y);
    }
    for (const k of this.carts.values()) {
      const [, civI, x, y] = k.row;
      k.c.visible = !this.hiddenAt(civI, x, y);
    }
    if (this.campG && this.camp) this.campG.visible = this.known(this.camp[0], this.camp[1]);
  }

  setFog(data) {
    for (const [cid, b64] of Object.entries(data || {})) this.fog[cid] = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    if (Object.keys(data || {}).includes(this.fogCiv)) this.drawFog();
  }

  showFogOf(cid) {
    this.fogCiv = cid;
    this.drawFog();
    this.applyFog();
    if (this.light) this.drawDark();                         // night lights of hidden houses go too
  }

  drawFog() {
    const seen = this.fog[this.fogCiv];
    if (!seen) { this.fogSprite.visible = false; return; }
    const n = this.size, W = n * TW, H = n * TH + TH;
    const c = document.createElement("canvas");
    c.width = W / 2; c.height = H / 2;                       // half resolution is plenty for fog
    const ctx = c.getContext("2d");
    ctx.fillStyle = "rgba(8,10,18,0.55)";
    const ox = W / 4, oy = TH / 4;
    let any = false;
    for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) {
      if (seen[y * n + x]) continue;
      any = true;
      const sx = (x - y) * TW / 4 + ox, sy = (x + y) * TH / 4 + oy;
      ctx.beginPath();
      ctx.moveTo(sx, sy - TH / 4 - 0.5); ctx.lineTo(sx + TW / 4 + 0.5, sy);
      ctx.lineTo(sx, sy + TH / 4 + 0.5); ctx.lineTo(sx - TW / 4 - 0.5, sy);
      ctx.fill();
    }
    if (this.fogSprite.texture && this.fogSprite.texture !== PIXI.Texture.EMPTY) this.fogSprite.texture.destroy(true);
    this.fogSprite.texture = PIXI.Texture.from(c);
    this.fogSprite.scale.set(2);
    this.fogSprite.position.set(-W / 2, -TH / 2);
    this.fogSprite.visible = any;
  }

  // the whole resource picture at once (timelapse frames, and after it ends)
  setAllNodes(qs) {
    for (let id = 0; id < this.nodes.length; id++) {
      const n = this.nodes[id];
      if (!n) continue;
      const q = typeof qs === "string" ? qs.charCodeAt(id) - 48 : qs[id];
      if (q >= 0 && q !== n.q) { n.q = q; this.styleNode(id); }
    }
  }

  // ------------------------------------------------------------------ one frame from the server
  restoreMap(map) {
    this.setAllNodes(map.nodes.map((n) => n[5]));
    this.roadsG.clear();
    const roads = Uint8Array.from(atob(map.roads || ""), (c) => c.charCodeAt(0));
    for (let i = 0; i < roads.length; i++) if (roads[i]) this.drawRoad(i % this.size, Math.floor(i / this.size), roads[i] - 1);
    this.fog = {};
    this.setFog(map.fog);
    this.drawFog();
    this.applyFog();
  }

  update(f) {
    const now = performance.now();
    if (this.lastFrameAt) this.frameMs = Math.min(3000, Math.max(250, now - this.lastFrameAt));
    this.lastFrameAt = now;
    if (f.clock.season !== this.season) {
      this.season = f.clock.season;
      this.drawTerrain(this.season);
      for (let i = 0; i < this.nodes.length; i++) if (this.nodes[i]) this.styleNode(i);
      for (const b of this.buildings.values()) b.key = "";        // farms change colour
    }
    for (const [id, q] of f.nodes) {
      const n = this.nodes[id];
      if (n) { n.q = q; this.styleNode(id); }
    }
    this.light = lightAt(f.clock.hour, f.clock.month);
    this.phase = f.clock.phase;
    this.updateBuildings(f);
    this.updatePeople(f);
    this.updateUnits(f);
    this.updateCarts(f);
    this.addEffects(f.effects || []);
    for (const [x, y, owner] of f.roads || []) this.drawRoad(x, y, owner - 1);
    if (f.fog && Object.keys(f.fog).length) this.setFog(f.fog);
    this.applyFog();
    this.drawDark();
  }

  // ------------------------------------------------------------------ buildings
  updateBuildings(f) {
    const seen = new Set();
    for (const row of f.buildings) {
      const [id, civI, type, x, y, size, level, target, progress, stage, fire, hp, left, family] = row;
      seen.add(id);
      let b = this.buildings.get(id);
      if (!b) {
        b = { g: new PIXI.Graphics(), key: "", civI, type, x, y, size, flames: null, label: null, bar: null, lights: [] };
        b.g.eventMode = "static"; b.g.cursor = "pointer";
        b.g.on("pointertap", (e) => { if (!this.dragged) this.onBuilding(b.row, e.global); });
        (type === "farm" ? this.ground : this.objects).addChild(b.g);
        this.buildings.set(id, b);
      }
      b.row = row;
      const key = `${level}|${target}|${stage}|${fire}|${this.season}`;
      if (key !== b.key) {
        b.key = key;
        this.drawBuilding(b, row);
      }
      this.siteLabel(b, row);
      this.setFire(b, fire);
    }
    for (const [id, b] of this.buildings) {
      if (!seen.has(id)) {
        b.g.destroy(); b.label?.destroy(); b.bar?.destroy(); b.flames?.destroy();
        for (const l of b.lights) l.destroy();
        this.buildings.delete(id);
      }
    }
  }

  corners(x, y, s) {
    return { t: iso(x - 0.5, y - 0.5), r: iso(x + s - 0.5, y - 0.5), b: iso(x + s - 0.5, y + s - 0.5), l: iso(x - 0.5, y + s - 0.5) };
  }

  box(g, x, y, s, h, wall, roof, roofH = 0) {
    const { t, r, b, l } = this.corners(x, y, s);
    const up = (p, d) => [p[0], p[1] - d];
    g.poly([...l, ...b, ...up(b, h), ...up(l, h)]).fill(shade(wall, 0.82));
    g.poly([...b, ...r, ...up(r, h), ...up(b, h)]).fill(shade(wall, 0.66));
    if (roofH > 0) {
      const c = [(t[0] + b[0]) / 2, (t[1] + b[1]) / 2 - h - roofH];
      g.poly([...up(l, h), ...up(b, h), ...c]).fill(shade(roof, 0.95));
      g.poly([...up(b, h), ...up(r, h), ...c]).fill(shade(roof, 0.75));
      g.poly([...up(t, h), ...up(l, h), ...c]).fill(shade(roof, 1.1));
      g.poly([...up(r, h), ...up(t, h), ...c]).fill(shade(roof, 0.9));
    } else {
      g.poly([...up(t, h), ...up(r, h), ...up(b, h), ...up(l, h)]).fill(roof);
    }
  }

  drawBuilding(b, row) {
    const [, civI, type, x, y, size, level, target, progress, stage] = row;
    const g = b.g;
    g.clear();
    const civ = hex(this.civs[civI].color);
    const stone = STONE_TYPES.has(type);
    const wall = stone ? 0xa7abb0 : 0x9a7148;
    const roof = shade(civ, 0.8);
    const { t, r, bt, l } = (() => { const c = this.corners(x, y, size); return { t: c.t, r: c.r, bt: c.b, l: c.l }; })();
    g.zIndex = bt[1];
    b.base = [(t[0] + bt[0]) / 2, (t[1] + bt[1]) / 2];
    const built = level >= 1;
    const H = HEIGHT[type] ?? 12;

    if (!built) {                                     // a new site: foundation → scaffold → walls → roof
      g.poly([...t, ...r, ...bt, ...l]).fill({ color: 0x7a6248, alpha: 0.9 }).stroke({ width: 1, color: 0x4a3a2a });
      if (type === "farm") { this.field(g, x, y, size, 0x8a6a3a, progress); return; }
      if (stage >= 2) this.box(g, x, y, size, H * (stage === 2 ? 0.55 : 1), wall, roof, stage === 3 ? H * 0.5 : 0);
      if (stage >= 1) this.scaffold(g, x, y, size, H);
      return;
    }
    if (type === "farm") {
      const s = SEASON[this.season] || SEASON.Spring;
      this.field(g, x, y, size, s.field, 1);
      return;
    }
    if (type === "castle") { this.castle(g, x, y, size, level, civ); }
    else if (type === "wall") { this.box(g, x, y, size, H, 0x8f9499, 0xb5b9bd); }
    else if (type === "tower") {
      this.box(g, x + 0.15, y + 0.15, size - 0.3, H, 0x9a9ea3, shade(civ, 0.7), 6);
    } else if (type === "wonder") {                 // a stepped pyramid with a gold top, drawn back to front
      const steps = [[1.5, 34, 0xe8c13a], [1.0, 25, 0xc9b98f], [0.5, 15, 0xbfae84], [0, 6, 0xb3a37a]];
      for (const [k, h, top] of steps) this.box(g, x + k, y + k, size - 2 * k, h, 0xd8c79a, top);
      const [cx, cy] = b.base;
      g.rect(cx - 0.8, cy - 46, 1.6, 10).fill(0x6b4a2a);
      g.poly([cx + 0.8, cy - 46, cx + 9, cy - 43, cx + 0.8, cy - 40]).fill(civ);
    } else if (type === "well") {
      const [cx, cy] = b.base;
      g.ellipse(cx, cy, 9, 4.5).fill(0x8f9499); g.ellipse(cx, cy - 1, 6, 3).fill(0x2f5f8f);
      g.rect(cx - 7, cy - 14, 1.4, 13).fill(0x6b4a2a); g.rect(cx + 5.6, cy - 14, 1.4, 13).fill(0x6b4a2a);
      g.poly([cx - 9, cy - 13, cx, cy - 19, cx + 9, cy - 13]).fill(roof);
    } else if (["quarry", "iron_mine", "gold_mine"].includes(type)) {
      this.box(g, x, y, size, H, 0x7d6a55, roof, 5);
      const [cx, cy] = b.base;
      g.poly([cx - 5, cy + 4, cx, cy - 4, cx + 5, cy + 4]).fill(0x1d1a17);   // the entrance
    } else {
      this.box(g, x, y, size, H, wall, roof, Math.max(5, H * 0.55));
      if (type === "mill") this.millBlades(b);
      if (type === "forge") { const [cx, cy] = b.base; g.rect(cx + 6, cy - H - 12, 3, 10).fill(0x55585c); }
      if (type === "market") { const [cx, cy] = b.base; g.rect(cx - 10, cy + 2, 20, 2).fill(shade(civ, 1.1)); }
    }
    if (target > level) this.scaffold(g, x, y, size, H);   // an upgrade in progress
  }

  field(g, x, y, s, color, grown) {
    const { t, r, b, l } = this.corners(x, y, s);
    g.poly([...t, ...r, ...b, ...l]).fill(shade(color, 0.85)).stroke({ width: 1, color: 0x5a4a30, alpha: 0.6 });
    for (let i = 1; i < s * 3; i++) {                // furrows
      const f = i / (s * 3);
      const a = [t[0] + (l[0] - t[0]) * f, t[1] + (l[1] - t[1]) * f], c = [r[0] + (b[0] - r[0]) * f, r[1] + (b[1] - r[1]) * f];
      g.moveTo(...a).lineTo(...c).stroke({ width: 1, color: shade(color, 0.65), alpha: 0.7 * grown });
    }
  }

  scaffold(g, x, y, s, h) {
    const { r, b, l } = this.corners(x, y, s);
    const lines = 0xc8a56a;
    for (const p of [l, b, r]) g.moveTo(p[0], p[1]).lineTo(p[0], p[1] - h - 4).stroke({ width: 1.2, color: lines });
    for (const k of [0.4, 0.8]) {
      g.moveTo(l[0], l[1] - h * k).lineTo(b[0], b[1] - h * k).lineTo(r[0], r[1] - h * k).stroke({ width: 1, color: lines, alpha: 0.9 });
    }
  }

  castle(g, x, y, s, level, civ) {
    const H = HEIGHT.castle + level * 2;
    this.box(g, x + 0.3, y + 0.3, s - 0.6, H * 0.55, 0xa9adb2, 0x8f9499);       // the yard walls
    this.box(g, x + 0.8, y + 0.8, s - 1.6, H, 0xb3b7bb, shade(civ, 0.75), 10);  // the keep
    const tw = 0.7;
    for (const [dx, dy] of [[0, 0], [s - tw, 0], [0, s - tw], [s - tw, s - tw]]) {
      this.box(g, x + dx, y + dy, tw, H * 0.85, 0x9fa3a8, shade(civ, 0.85), 6);
    }
    const c = this.corners(x + 0.8, y + 0.8, s - 1.6);
    const top = [(c.t[0] + c.b[0]) / 2, (c.t[1] + c.b[1]) / 2 - H - 10];
    g.moveTo(top[0], top[1]).lineTo(top[0], top[1] - 16).stroke({ width: 1.2, color: 0x3a3a3a });
    g.poly([top[0], top[1] - 16, top[0] + 11, top[1] - 13, top[0], top[1] - 10]).fill(civ);
  }

  millBlades(b) {
    if (b.blades) return;
    const bl = new PIXI.Graphics();
    for (let i = 0; i < 4; i++) {
      const a = i * Math.PI / 2;
      bl.poly([0, 0, Math.cos(a - 0.12) * 13, Math.sin(a - 0.12) * 13, Math.cos(a + 0.12) * 13, Math.sin(a + 0.12) * 13]).fill(0xe8dcc0);
    }
    bl.circle(0, 0, 1.6).fill(0x5a3b1e);
    bl.position.set(b.base[0] - 6, b.base[1] - HEIGHT.mill - 4);
    bl.zIndex = b.g.zIndex + 1;
    this.objects.addChild(bl);
    b.blades = bl;
  }

  siteLabel(b, row) {
    const [, , type, , , , level, target, progress, , , , left] = row;
    const site = target > level;
    if (!site) {
      if (b.label) { b.label.destroy(); b.label = null; b.bar.destroy(); b.bar = null; }
      return;
    }
    if (!b.label) {
      b.label = new PIXI.Text({ text: "", style: { fontFamily: "Inter, Segoe UI, sans-serif", fontSize: 11, fill: 0xf3ead7,
        stroke: { color: 0x1a1410, width: 3 } }, resolution: 3 });
      b.label.anchor.set(0.5, 1);
      b.bar = new PIXI.Graphics();
      this.overlay.addChild(b.bar, b.label);
    }
    const name = this.labels[type] || type;
    const what = level >= 1 ? `${name} → L${target}` : name;
    const time = siteTime(left, this.phase);
    b.label.text = `${what} - ${Math.round(progress * 100)}% - ${time}`;
    const H = (HEIGHT[type] ?? 12) + 14;
    const [cx, cy] = b.base;
    b.label.position.set(cx, cy - H - 8);
    b.bar.clear();
    b.bar.roundRect(cx - 22, cy - H - 6, 44, 4, 2).fill({ color: 0x000000, alpha: 0.55 });
    b.bar.roundRect(cx - 22, cy - H - 6, Math.max(2, 44 * progress), 4, 2).fill(hex(this.civs[b.civI].color));
  }

  setFire(b, fire) {
    if (fire && !b.flames) {
      b.flames = new PIXI.Graphics();
      b.flames.blendMode = "add";
      this.overlay.addChild(b.flames);
    } else if (!fire && b.flames) {
      b.flames.destroy(); b.flames = null;
    }
  }

  // ------------------------------------------------------------------ people
  updatePeople(f) {
    const seen = new Set();
    const T = this.T;
    for (const row of f.people) {
      const [id, civI, x, y, state, carry, badge, job, visible, sex, cart] = row;
      seen.add(id);
      let p = this.people.get(id);
      if (!p) {
        const c = new PIXI.Container();
        const shadow = new PIXI.Sprite(T.shadow), body = new PIXI.Sprite(sex === "f" ? T.dress : T.body),
          head = new PIXI.Sprite(T.head), load = new PIXI.Sprite(T.sack), dot = new PIXI.Sprite(T.dot),
          tool = new PIXI.Sprite(T.tool);
        for (const s of [shadow, body, head, load, dot, tool]) s.anchor.set(0.5, 1);
        shadow.anchor.set(0.5, 0.5);
        body.tint = hex(this.civs[civI].color);
        head.position.set(0, -8.6);
        load.position.set(3.6, -7);
        dot.position.set(0, -12.6);
        tool.position.set(3, -4); tool.pivot.set(0, 0);
        c.addChild(shadow, body, tool, head, load, dot);
        c.eventMode = "static"; c.cursor = "pointer";
        c.hitArea = new PIXI.Rectangle(-9, -20, 18, 24);      // bigger than the body: people are small and moving
        c.on("pointertap", () => { if (!this.dragged) this.onPerson(id); });
        this.objects.addChild(c);
        const [sx, sy] = iso(x, y);
        p = { c, body, head, load, dot, tool, from: [sx, sy], to: [sx, sy], t0: 0, phase: Math.random() * 6 };
        this.people.set(id, p);
      }
      const [sx, sy] = iso(x, y);
      const cur = p.c.position;
      const jump = Math.hypot(sx - cur.x, sy - cur.y) > TW * 6;
      p.from = jump ? [sx, sy] : [cur.x, cur.y];
      p.to = [sx, sy];
      p.t0 = performance.now();
      p.state = state;
      p.row = row;
      p.c.visible = visible === 1;
      p.load.visible = !!carry || !!cart;
      if (cart) { p.load.texture = T.cart; p.load.tint = 0xffffff; p.load.position.set(6, -1); p.load.scale.set(0.7); }
      else if (carry) { p.load.texture = carry === "wood" ? T.log : T.sack; p.load.tint = CARRY[carry] ?? 0xffffff; p.load.position.set(3.6, -7); p.load.scale.set(1); }
      p.dot.tint = BADGE[badge];
      p.tool.visible = state === "gather" || state === "build";
      p.civI = civI;
    }
    for (const [id, p] of this.people) if (!seen.has(id)) { p.c.destroy({ children: true }); this.people.delete(id); }
  }

  // ------------------------------------------------------------------ soldiers, rams, spies (version 5)
  updateUnits(f) {
    const T = this.T, seen = new Set();
    const WEAPON = { militia: "club", spearman: "spear", archer: "bow", swordsman: "sword", knight: "spear" };
    for (const row of f.units || []) {
      const [id, civI, x, y, type, state, hp] = row;
      seen.add(id);
      let u = this.units.get(id);
      if (!u) {
        const c = new PIXI.Container();
        const parts = {};
        const sp = (tex) => { const s = new PIXI.Sprite(tex); s.anchor.set(0.5, 1); return s; };
        parts.shadow = new PIXI.Sprite(T.shadow); parts.shadow.anchor.set(0.5);
        c.addChild(parts.shadow);
        const color = civI < 0 ? BANDIT : hex(this.civs[civI].color);
        if (type === "ram") {
          parts.body = sp(T.ram); c.addChild(parts.body);
          parts.flag = sp(T.flag); parts.flag.tint = color; parts.flag.position.set(0, -14); c.addChild(parts.flag);
        } else if (type === "spy") {
          parts.body = sp(T.spy); c.addChild(parts.body);
        } else {
          if (type === "knight") { parts.horse = sp(T.horse); c.addChild(parts.horse); }
          const lift = type === "knight" ? -6 : 0;
          parts.body = sp(T.body); parts.body.tint = color; parts.body.y = lift;
          parts.head = sp(T.head); parts.head.y = -8.6 + lift;
          parts.weapon = sp(type === "scout" ? T.flag : T[WEAPON[type]] || T.club); parts.weapon.position.set(3.2, -3 + lift);
          parts.helmet = sp(T.helmet); parts.helmet.position.set(0, -10.4 + lift); parts.helmet.visible = false;
          c.addChild(parts.body, parts.head, parts.helmet, parts.weapon);
          if (type === "swordsman") { parts.shield = sp(T.shield); parts.shield.tint = color; parts.shield.position.set(-3, -2 + lift); c.addChild(parts.shield); }
        }
        parts.bar = new PIXI.Graphics(); c.addChild(parts.bar);
        c.eventMode = "static"; c.cursor = "pointer";
        c.hitArea = new PIXI.Rectangle(-9, -22, 18, 26);
        this.objects.addChild(c);
        const [sx, sy] = iso(x, y);
        u = { c, parts, from: [sx, sy], to: [sx, sy], t0: 0, phase: Math.random() * 6, type };
        c.on("pointertap", (e) => { if (!this.dragged) this.onUnit?.(u.row, e.global); });
        this.units.set(id, u);
      }
      u.row = row;
      const [sx, sy] = iso(x, y);
      const cur = u.c.position;
      const jump = Math.hypot(sx - cur.x, sy - cur.y) > TW * 6;
      u.from = jump ? [sx, sy] : [cur.x, cur.y];
      u.to = [sx, sy];
      u.t0 = performance.now();
      u.state = state;
      u.show = !(type === "spy" && (state === "stay" || state === "home"));   // spies hide
      u.c.visible = u.show;
      const civ = civI >= 0 ? f.civs[civI] : null;
      const armor = civ ? Math.min(5, civ.armor || 0) : 0, weapons = civ ? Math.min(5, civ.weapons || 0) : 0;
      if (u.parts.helmet) { u.parts.helmet.visible = armor > 0; if (armor) u.parts.helmet.tint = METAL[armor]; }
      if (u.parts.weapon && type !== "scout") u.parts.weapon.tint = weapons ? METAL[weapons] : 0xffffff;
      const bar = u.parts.bar;
      bar.clear();
      if (hp < 0.999 && type !== "spy") {
        bar.rect(-5, -21, 10, 1.8).fill({ color: 0x000000, alpha: 0.6 });
        bar.rect(-5, -21, 10 * hp, 1.8).fill(hp > 0.5 ? 0x3fbf5f : hp > 0.25 ? 0xe8c13a : 0xe0483e);
      }
    }
    for (const [id, u] of this.units) if (!seen.has(id)) { u.c.destroy({ children: true }); this.units.delete(id); }
  }

  updateCarts(f) {
    const seen = new Set();
    for (const [id, civI, x, y] of f.carts || []) {
      seen.add(id);
      let k = this.carts.get(id);
      if (!k) {
        const c = new PIXI.Container();
        const body = new PIXI.Sprite(this.T.cart); body.anchor.set(0.5, 1);
        const flag = new PIXI.Sprite(this.T.flag); flag.anchor.set(0.5, 1); flag.tint = hex(this.civs[civI].color); flag.position.set(-6, -10);
        c.addChild(body, flag);
        this.objects.addChild(c);
        const [sx, sy] = iso(x, y);
        c.position.set(sx, sy);
        k = { c, from: [sx, sy], to: [sx, sy], t0: 0 };
        this.carts.set(id, k);
      }
      const [sx, sy] = iso(x, y);
      k.row = [id, civI, x, y];
      k.from = [k.c.x, k.c.y]; k.to = [sx, sy]; k.t0 = performance.now();
    }
    for (const [id, k] of this.carts) if (!seen.has(id)) { k.c.destroy({ children: true }); this.carts.delete(id); }
  }

  addEffects(rows) {
    for (const [x, y, , kind] of rows.slice(-60)) {
      const s = new PIXI.Sprite(this.T.spark);
      s.anchor.set(0.5);
      s.tint = kind === "tower" ? 0xffd23a : kind === "ram" ? 0xff7a1a : 0xffffff;
      const [sx, sy] = iso(x, y);
      s.position.set(sx + (Math.random() - 0.5) * 8, sy - 6 + (Math.random() - 0.5) * 6);
      s.blendMode = "add";
      s.scale.set(kind === "ram" ? 1.6 : 0.9);
      s.alpha = 0;
      this.overlay.addChild(s);
      this.fx.push({ s, t0: performance.now() + Math.random() * this.frameMs * 0.8 });
    }
  }

  // ------------------------------------------------------------------ light
  drawDark() {
    const { night, warm } = this.light;
    const g = this.dark, B = this.bounds;
    g.clear();
    if (night > 0.01) g.rect(B.x0, B.y0, B.x1 - B.x0, B.y1 - B.y0).fill({ color: 0x0a1030, alpha: 0.62 * night });
    if (warm > 0.01) g.rect(B.x0, B.y0, B.x1 - B.x0, B.y1 - B.y0).fill({ color: 0xff8a3c, alpha: 0.13 * warm });
    // torches at the castle and windows of houses where people sleep
    for (const l of this.lights.removeChildren()) l.destroy();
    if (night < 0.25) return;
    for (const b of this.buildings.values()) {
      const [, , type, , , , level] = b.row;
      if (level < 1 || !b.base || b.hidden) continue;
      if (type === "castle" || type === "house" || type === "forge" || type === "tower") {
        const s = new PIXI.Sprite(this.T.glow);
        s.anchor.set(0.5); s.blendMode = "add";
        s.scale.set(type === "castle" ? 0.9 : 0.45);
        s.alpha = night * (0.7 + Math.random() * 0.15);
        s.position.set(b.base[0], b.base[1] - (HEIGHT[type] ?? 12) * 0.5);
        this.lights.addChild(s);
      }
    }
  }

  // ------------------------------------------------------------------ every animation frame
  animate(dt) {
    const now = performance.now();
    for (const p of this.people.values()) {
      const k = Math.min(1, (now - p.t0) / this.frameMs);
      const x = p.from[0] + (p.to[0] - p.from[0]) * k, y = p.from[1] + (p.to[1] - p.from[1]) * k;
      p.phase += dt * 0.012;
      const moving = k < 1 && (p.to[0] !== p.from[0] || p.to[1] !== p.from[1]);
      const bob = moving ? Math.abs(Math.sin(p.phase * 1.6)) * 1.1 : 0;
      p.c.position.set(x, y);
      p.body.y = -bob; p.head.y = -8.6 - bob; p.load.y = -7 - bob;
      p.c.zIndex = y;
      if (p.tool.visible) p.tool.rotation = p.state === "build" ? Math.sin(p.phase * 2.4) * 0.9 : Math.sin(p.phase * 1.4) * 0.6 + 0.3;
    }
    for (const u of this.units.values()) {
      const k = Math.min(1, (now - u.t0) / this.frameMs);
      const x = u.from[0] + (u.to[0] - u.from[0]) * k, y = u.from[1] + (u.to[1] - u.from[1]) * k;
      u.phase += dt * 0.012;
      u.c.position.set(x, y);
      u.c.zIndex = y;
      const p = u.parts;
      const moving = k < 1 && (u.to[0] !== u.from[0] || u.to[1] !== u.from[1]);
      const bob = moving ? Math.abs(Math.sin(u.phase * 1.8)) * (u.type === "knight" ? 1.6 : 1) : 0;
      if (p.body) p.body.pivot.y = bob;
      if (p.head) p.head.pivot.y = bob;
      if (p.helmet) p.helmet.pivot.y = bob;
      if (p.weapon) p.weapon.rotation = u.state === "fight" ? Math.sin(u.phase * 3.2) * 1.1 : 0.15;
      if (u.type === "ram" && p.body) p.body.x = u.state === "fight" ? Math.sin(u.phase * 2) * 1.5 : 0;
    }
    for (const k of this.carts.values()) {
      const t = Math.min(1, (now - k.t0) / this.frameMs);
      const x = k.from[0] + (k.to[0] - k.from[0]) * t, y = k.from[1] + (k.to[1] - k.from[1]) * t;
      k.c.position.set(x, y); k.c.zIndex = y;
    }
    this.fx = this.fx.filter((e) => {
      const age = (now - e.t0) / 450;
      if (age < 0) return true;
      if (age >= 1) { e.s.destroy(); return false; }
      e.s.alpha = 1 - age;
      return true;
    });
    for (const n of this.nodes) {
      if (n && n.kind === "animal" && n.s.visible) { n.phase += dt * 0.002; n.s.skew.x = Math.sin(n.phase) * 0.04; }
    }
    for (const b of this.buildings.values()) {
      if (b.blades) b.blades.rotation += dt * 0.0012;
      if (b.flames && b.base) {
        const g = b.flames, [cx, cy] = b.base, H = HEIGHT[b.type] ?? 12;
        g.clear();
        for (let i = 0; i < 7; i++) {
          const ox = (Math.random() - 0.5) * 18, h = 6 + Math.random() * 12;
          g.poly([cx + ox - 3, cy - H * 0.6, cx + ox, cy - H * 0.6 - h, cx + ox + 3, cy - H * 0.6]).fill({ color: i % 2 ? 0xff7a1a : 0xffd23a, alpha: 0.85 });
        }
      }
    }
  }

  // ------------------------------------------------------------------ pan, zoom, centre
  setupInput() {
    const view = this.app.canvas;
    let down = null;
    view.addEventListener("pointerdown", (e) => { down = { x: e.clientX, y: e.clientY, wx: this.world.x, wy: this.world.y }; this.dragged = false; });
    window.addEventListener("pointermove", (e) => {
      if (!down) return;
      const dx = e.clientX - down.x, dy = e.clientY - down.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) this.dragged = true;
      if (this.dragged) { this.world.position.set(down.wx + dx, down.wy + dy); this.onView?.(); }
    });
    window.addEventListener("pointerup", () => { down = null; setTimeout(() => { this.dragged = false; }, 0); });
    view.addEventListener("wheel", (e) => {
      e.preventDefault();
      const rect = view.getBoundingClientRect();
      this.zoomAt(e.clientX - rect.left, e.clientY - rect.top, Math.exp(-e.deltaY * 0.0015));
    }, { passive: false });
  }

  zoomAt(px, py, factor) {
    const w = this.world, old = w.scale.x;
    const s = Math.min(4, Math.max(0.2, old * factor));
    const wx = (px - w.x) / old, wy = (py - w.y) / old;
    w.scale.set(s);
    w.position.set(px - wx * s, py - wy * s);
    this.onView?.();
  }

  centerOn(tx, ty, scale) {
    const w = this.world;
    if (scale) w.scale.set(scale);
    const [sx, sy] = iso(tx, ty);
    w.position.set(this.app.screen.width / 2 - sx * w.scale.x, this.app.screen.height / 2 - sy * w.scale.y);
    this.onView?.();
  }

  // the visible area in tile coordinates (for the minimap)
  viewTiles() {
    const w = this.world, sc = w.scale.x, W = this.app.screen.width, H = this.app.screen.height;
    const toTile = (px, py) => {
      const sx = (px - w.x) / sc, sy = (py - w.y) / sc;
      return [(sx / (TW / 2) + sy / (TH / 2)) / 2, (sy / (TH / 2) - sx / (TW / 2)) / 2];
    };
    return [toTile(0, 0), toTile(W, 0), toTile(W, H), toTile(0, H)];
  }
}

// a construction site's time left: building stops at lunch, in the evening and at night
export function siteTime(left, phase) {
  if (phase === "lunch") return "paused: lunch break";
  if (phase === "evening" || phase === "night") return "paused until work starts";
  return left == null ? "waiting for builders" : fmtLeft(left);
}

export function fmtLeft(sec) {
  if (sec < 60) return "under a minute";
  const total = Math.round(sec / 60);                      // round once, so 1 h 59.6 min shows as 2 h, never "1 h 60 min"
  const h = Math.floor(total / 60), m = total % 60;
  if (h >= 48) return `${(total / 1440).toFixed(1)} days`;
  return h ? (m ? `${h} h ${m} min` : `${h} h`) : `${m} min`;
}

export { iso };
