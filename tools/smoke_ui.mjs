/* Headless smoke test for the UI.
 *
 * There is no browser in this environment, and shipping a page nobody has
 * executed is how a mistyped element id becomes "it doesn't work". This runs the
 * real app.js — unmodified — inside a Node VM with a small DOM shim, against the
 * real HTTP API of a running server, and then asserts that the things a reader
 * would look at actually got written: the stats, the table rows, the SVG paths,
 * the notes, and the caption.
 *
 *   node tools/smoke_ui.mjs                       # expects 127.0.0.1:8795
 *   node tools/smoke_ui.mjs http://127.0.0.1:8795
 *
 * It is a smoke test, not a screenshot: it cannot tell you the layout looks
 * right, only that the data reached the DOM nodes that draw it.
 */

import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const BASE = process.argv[2] || "http://127.0.0.1:8795";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.resolve(HERE, "..", "app", "static");

const results = [];
const check = (name, cond, detail = "") => {
  results.push({ name, ok: !!cond });
  console.log(`  [${cond ? "ok  " : "FAIL"}] ${name}${detail ? `  — ${detail}` : ""}`);
};

/* ------------------------------------------------------------------ DOM shim */

class ClassList {
  constructor() { this.s = new Set(); }
  add(c) { this.s.add(c); }
  remove(c) { this.s.delete(c); }
  toggle(c, on) { if (on === undefined) { this.s.has(c) ? this.s.delete(c) : this.s.add(c); } else if (on) this.s.add(c); else this.s.delete(c); }
  contains(c) { return this.s.has(c); }
  toString() { return [...this.s].join(" "); }
}

class El {
  constructor(tag = "div", id = "") {
    this.tagName = tag.toUpperCase();
    this.id = id;
    // lazy so constructing a node does not construct a tree
    this._children = null;
    this.classList = new ClassList();
    this.style = {};
    this.dataset = {};
    this.value = "";
    this.hidden = false;
    this.attributes = {};
    this._text = "";
    this._html = "";
    this.listeners = {};
    this.offsetWidth = 160;
    this.offsetHeight = 52;
    this.clientWidth = 1200;
    this.clientHeight = 340;
    this._q = new Map();
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); }
  get children() {
    if (!this._children) this._children = Array.from({ length: 8 }, () => new El("td"));
    return this._children;
  }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  removeAttribute(k) { delete this.attributes[k]; }
  appendChild(c) {
    this.children.push(c);
    // a real <select> shows its options; mimic that so the test can see them
    if (c && c.tagName === "OPTION") this._html += `<option value="${c.value}">${c.textContent}</option>`;
    return c;
  }
  dispatch(type, extra = {}) {
    for (const fn of this.listeners[type] || []) fn({ target: this, preventDefault() {}, ...extra });
  }
  click() { this.dispatch("click"); }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  getBoundingClientRect() { return { left: 0, top: 0, width: this.clientWidth, height: this.clientHeight }; }
  querySelector(sel) {
    if (!this._q.has(sel)) this._q.set(sel, new El("div", sel));
    return this._q.get(sel);
  }
  querySelectorAll() { return []; }
  focus() {}
  select() {}
}

const ids = new Set();
const html = fs.readFileSync(path.join(APP, "index.html"), "utf8");
for (const m of html.matchAll(/id="([^"]+)"/g)) ids.add(m[1]);

const els = new Map();
const getEl = (id) => {
  if (!els.has(id)) els.set(id, new El("div", id));
  return els.get(id);
};

const kindButtons = ["call", "put"].map((k) => { const e = new El("button"); e.dataset.kind = k; return e; });
const sideButtons = ["long", "short"].map((s) => { const e = new El("button"); e.dataset.side = s; return e; });

const document = {
  getElementById: (id) => (ids.has(id) ? getEl(id) : getEl(id)),   // created on demand
  createElement: (tag) => new El(tag),
  querySelectorAll: (sel) => {
    if (sel === "[data-kind]") return kindButtons;
    if (sel === "[data-side]") return sideButtons;
    return [];
  },
  body: new El("body"),
  activeElement: null,
};

const store = new Map();
const localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
};

const location = { search: "", pathname: "/" };
const history = { replaceState: (_a, _b, url) => { location.search = "?" + String(url).split("?")[1]; } };

const realFetch = globalThis.fetch;
const fetch = (url, opts) => realFetch(new URL(url, BASE).toString(), opts);

const ctx = {
  console,
  document,
  window: {
    addEventListener() {},
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    innerWidth: 1400,
  },
  localStorage,
  location,
  history,
  navigator: { clipboard: { writeText: async () => {} } },
  fetch,
  setTimeout,
  clearTimeout,
  URLSearchParams,
  URL,
  Blob: class { constructor(parts) { this.parts = parts; } },
  Math,
  Number,
  Date,
  JSON,
  isNaN,
  parseFloat,
  parseInt,
};
ctx.globalThis = ctx;
ctx.self = ctx;

/* ------------------------------------------------------------------ run it */

const src = fs.readFileSync(path.join(APP, "app.js"), "utf8");
const script = new vm.Script(src, { filename: "app.js" });
script.runInContext(vm.createContext(ctx));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* The page loads its chain over the network — a cold CBOE fetch can take several
   seconds — so wait for the ladder to exist rather than for a fixed number of
   milliseconds, or a slow first request reads as a broken page. */
const until = async (expr, ms = 30000) => {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    try { if (vm.runInContext(expr, ctx)) return true; } catch (_) { /* not defined yet */ }
    await sleep(150);
  }
  return false;
};
check("the first ladder arrives", await until("state.ladder && state.ladder.rows.length > 0"));

/* ------------------------------------------------------------------ assert */

const body = getEl("ladder-body").innerHTML;
const chart = getEl("chart").innerHTML;
const premium = getEl("s-premium").textContent;
const iv = getEl("s-iv").textContent;
const notes = getEl("notes-list").innerHTML;
const caption = getEl("chart-caption").textContent;
const expiry = getEl("f-expiry");

check("ticker defaulted", getEl("f-ticker").value.length > 0, getEl("f-ticker").value);
check("expiries loaded into the picker", (expiry.innerHTML || "").includes("<option"), `${(expiry.innerHTML.match(/<option/g) || []).length} options`);
check("expiry has a value", !!expiry.value, expiry.value);
check("strike filled", Number(getEl("f-strike").value) > 0, getEl("f-strike").value);
check("premium written to the stats", /\$[\d,]+\.\d\d/.test(premium), premium);
check("model value written", /\$/.test(getEl("s-theory").textContent), getEl("s-theory").textContent);
check("vol shown as a percentage", /%$/.test(iv), iv);
check("breakeven written", Number(getEl("s-be").textContent.replace(/,/g, "")) > 0, getEl("s-be").textContent);
check("delta written", /^[+-]?\d\.\d{3}$/.test(getEl("s-delta").textContent), getEl("s-delta").textContent);
check("greeks line written", /Θ/.test(getEl("s-greeks").textContent), getEl("s-greeks").textContent);
check("table has rows", (body.match(/<tr/g) || []).length >= 8, `${(body.match(/<tr/g) || []).length} rows`);
check("table marks spot and strike", body.includes(">spot<") && body.includes(">strike<"));
check("table shows a loss and a gain", body.includes("neg") && body.includes("pos"));
check("chart drew paths", (chart.match(/<path/g) || []).length >= 2, `${(chart.match(/<path/g) || []).length} paths`);
check("chart drew the zero line and markers", chart.includes("stroke-dasharray") && chart.includes("spot "));
check("caption explains the two lines", /arithmetic/i.test(caption), caption.slice(0, 60) + "…");
check("notes rendered", (notes.match(/<li>/g) || []).length >= 3, `${(notes.match(/<li>/g) || []).length} notes`);
check("notes mention the model convention", /early exercise|European exercise/.test(notes));
check("provenance line written", /source/.test(getEl("provenance").textContent), getEl("provenance").textContent);
check("URL was synced", /[?&]t=/.test(location.search), location.search);
check("no error box", getEl("error").hidden !== false, getEl("error").textContent);

/* ------------------------------------------------------- phase 2: interactions
 * Everything above ran the page once. These are the paths a reader will actually
 * take — flip the type, decay the clock, change the model, ask for a name that has
 * no options — each one re-entering the same code with a different request.
 */

kindButtons[1].click();                       // put
await sleep(1200);
check("switching to put refetches", getEl("s-delta").textContent.trim().startsWith("-"),
      getEl("s-delta").textContent);
check("put row set still built", (getEl("ladder-body").innerHTML.match(/<tr/g) || []).length >= 8);
check("url carries the type", /[?&]k=put/.test(location.search), location.search);

const days = getEl("f-days");
days.value = "5";
days.dispatch("input");
await sleep(900);
check("decay re-labels the premium column",
      (getEl("ladder").querySelector("thead tr").children[2].textContent || "").includes("+5d"),
      getEl("ladder").querySelector("thead tr").children[2].textContent);
check("decay is announced in the hint", /5 days/.test(getEl("table-hint").textContent),
      getEl("table-hint").textContent);
check("decay shows up in the URL", /[?&]d=5/.test(location.search), location.search);

const model = getEl("f-model");
model.value = "bs";
model.dispatch("change");
await sleep(900);
check("switching to the European model reprices", /European exercise/.test(getEl("notes-list").innerHTML));

const steps = getEl("f-steps");
steps.value = "8";
steps.dispatch("input");
await sleep(800);
check("row count follows the steps field",
      (getEl("ladder-body").innerHTML.match(/<tr/g) || []).length <= 14,
      `${(getEl("ladder-body").innerHTML.match(/<tr/g) || []).length} rows`);

/* a short is helped by a premium fall: the "vs entry" column has to change sign with
   the position, or it prints green beside a red P&L on the same row */
function spotRowCellsNow() { return spotRow(); }
function spotRow() {
  const rows = getEl("ladder-body").innerHTML.split("<tr").slice(1);
  const row = rows.find((r) => r.includes("mark-spot"));
  if (!row) return null;
  return [...row.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map((m) => m[1].replace(/<[^>]+>/g, "").trim());
}
const longCells = spotRow();
const shortBtn = sideButtons[1];
shortBtn.click();
await sleep(1200);
const shortCells = spotRow();
check("short rows exist", Boolean(shortCells), shortCells ? shortCells.length + " cells" : "none");
const numOf = (cell) => parseFloat(String(cell).replace(/[^0-9.\-]/g, "")) || 0;
if (longCells && shortCells) {
  // columns: 0 stock · 1 move · 2 Δ · 3 premium now · 4 P&L today · 5 at expiry · 6 P&L at expiry · 7 return
  check("the P&L today flips sign with the position",
        Math.sign(numOf(shortCells[4])) === -Math.sign(numOf(longCells[4])),
        `${longCells[4]} → ${shortCells[4]}`);
  check("and the P&L at expiry flips with it, not just the mark",
        Math.sign(numOf(shortCells[6])) === -Math.sign(numOf(longCells[6])),
        `${longCells[6]} → ${shortCells[6]}`);
  /* the decay slider is still at +5d from the earlier step; the spot-row invariant
     only holds at zero decay, so put the clock back before checking it */
  const daysEl = getEl("f-days");
  daysEl.value = "0";
  daysEl.dispatch("input");
  await sleep(1200);
  const spotRow = spotRowCellsNow();
  check("at today's spot the two P&L columns disagree by design",
        Math.abs(numOf(spotRow[4])) < 0.02 * Math.max(1, Math.abs(numOf(spotRow[6]))),
        `today ${spotRow[4]} vs expiry ${spotRow[6]}`);
}
check("the greeks are quoted per contract", /per contract/.test(getEl("s-greeks-note").textContent),
      getEl("s-greeks-note").textContent);

/* the model picker must be able to say "you decide" — otherwise the index/European
   auto-detect can never fire from the page */
/* the picker's markup lives in index.html, which this shim never parses, so the
   assertion reads the file the server sends */
const pageHtml = fs.readFileSync(path.join(APP, "index.html"), "utf8");
check("the model picker offers auto", /<option value="">/.test(pageHtml),
      (pageHtml.match(/<select id="f-model">[\s\S]*?<\/select>/) || [""])[0].replace(/\s+/g, " ").slice(0, 120));

/* Two requests in flight, the first slower than the second: the page must show the
   contract the reader is looking at now. This is the SOXX failure — controls said
   "150, 15 Jan", the ladder on screen was a 200-strike, 25-day contract. */
const liveFetch = ctx.fetch;
const ladderNow = vm.runInContext("state.ladder", ctx);
const payload = (strike, expiry) => ({
  generated: new Date().toISOString(),
  ladder: { ...ladderNow, strike, expiry, breakeven: strike + 0.04, rows: ladderNow.rows },
  market: null,
  asof: null,
});
ctx.fetch = async (url) => {
  if (!String(url).endsWith("/api/ladder")) return liveFetch(url);
  const slow = String(url).endsWith("/api/ladder") && !ctx.__servedSlow;
  ctx.__servedSlow = true;
  const body = slow ? payload(111.0, "2026-12-18") : payload(222.0, "2027-01-15");
  await sleep(slow ? 900 : 30);
  return { ok: true, json: async () => body };
};
const first = vm.runInContext("loadLadder()", ctx);
const second = vm.runInContext("loadLadder()", ctx);
await Promise.all([first, second]);
await sleep(50);
check("a slow, superseded ladder never overwrites the current one",
      Math.abs(vm.runInContext("state.ladder.strike", ctx) - 222) < 1e-9,
      `strike on screen ${vm.runInContext("state.ladder.strike", ctx)}`);
check("and the controls agree with what was rendered",
      getEl("f-strike").value === String(vm.runInContext("state.ladder.strike", ctx)),
      `field ${getEl("f-strike").value}`);
ctx.fetch = liveFetch;

/* The reported case, driven through the form the way a reader drives it: SOXX, then a
   strike and an expiry, and the ladder on screen has to be that contract. */
getEl("f-ticker").value = "SOXX";
getEl("f-ticker").dispatch("input");
check("the SOXX chain loads", await until('state.chain && state.chain.ticker === "SOXX"'));
const soxxExpiry = getEl("f-expiry").value;
kindButtons[0].click();                  // back to CALL: phase 2 left the page on PUT
getEl("f-expiry").value = "2027-01-15";
getEl("f-expiry").dispatch("change");
getEl("f-strike").value = "150";
getEl("f-strike").dispatch("input");
await until('state.ladder && state.ladder.strike === 150 && state.ladder.expiry === "2027-01-15" && state.ladder.kind === "call"', 15000);
const lad2 = vm.runInContext("state.ladder", ctx);
check("SOXX 15 Jan 150 prices the strike that is in the box",
      Math.abs(lad2.strike - 150) < 1e-9, `strike ${lad2.strike}`);
check("and the expiry that is in the picker",
      lad2.expiry === "2027-01-15", `expiry ${lad2.expiry} (picker started at ${soxxExpiry})`);
check("with a premium a 53-day 150 call should have",
      lad2.kind === "call" && lad2.entry > 2 && lad2.entry < 12,
      `${lad2.kind} entry ${lad2.entry.toFixed(2)}`);
check("and its break-even just above the strike",
      Math.abs(lad2.breakeven - (150 + lad2.entry)) < 1e-6, `be ${lad2.breakeven.toFixed(2)}`);
check("no stale stat block left behind",
      getEl("s-be").textContent === lad2.breakeven.toFixed(2), getEl("s-be").textContent);

/* a symbol with no listed options must fail loudly, not empty */
getEl("f-ticker").value = "ZZZZ";
getEl("f-ticker").dispatch("input");
await sleep(12000);
check("unknown ticker surfaces an error", getEl("error").hidden === false && getEl("error").textContent.length > 10,
      String(getEl("error").textContent).slice(0, 80));

const fails = results.filter((r) => !r.ok);
console.log(`\n${results.length - fails.length} passed, ${fails.length} failed`);
process.exit(fails.length ? 1 : 0);
