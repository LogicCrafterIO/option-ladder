/* Option ladder — front end. No dependencies, no build step.
 *
 * The page holds one contract and asks the server for it repeatedly; every
 * interaction is a debounced request, and the URL is kept in step so a ladder
 * can be bookmarked or pasted to someone else.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const API = "";
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

const state = {
  ticker: "",
  kind: "call",
  expiry: "",
  strike: null,
  lo: -25, hi: 25, steps: 15, days: 0,
  vol: null, rate: null, carry: null, entry: null, spot: null,
  model: "", volMode: "sticky_strike", side: "long",
  chain: null, ladder: null, loading: false, chainError: null,
};

/* ------------------------------------------------------------------ format */

const nf = (v, d = 2) =>
  (v === null || v === undefined || Number.isNaN(v))
    ? "—"
    : v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });

const money = (v, d = 2) => (v === null || v === undefined) ? "—"
  : (v < 0 ? "-$" : "$") + nf(Math.abs(v), d);

const signedMoney = (v) => (v === null || v === undefined) ? "—"
  : (v > 0 ? "+$" : v < 0 ? "-$" : "$") + nf(Math.abs(v), v && Math.abs(v) < 1000 ? 2 : 0);

const pct = (v, d = 1) => (v === null || v === undefined || !Number.isFinite(v)) ? "—"
  : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v * 100).toFixed(d) + "%";

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

function dateLabel(iso) {
  const d = new Date(iso + "T12:00:00");
  return `${MONTHS[d.getMonth()]} ${d.getDate()} ${d.getFullYear()}`;
}

function shortDate(iso) {
  const d = new Date(iso + "T12:00:00");
  return `${String(d.getDate()).padStart(2,"0")} ${MONTHS[d.getMonth()]}`;
}

function toneClass(v) { return v > 0 ? "pos" : v < 0 ? "neg" : ""; }

/* ------------------------------------------------------------------ network */

async function getJSON(url, opts) {
  const res = await fetch(url, opts);
  let body = null;
  try { body = await res.json(); } catch (_) { body = null; }
  if (!res.ok) {
    const msg = (body && body.detail) ? body.detail : `${res.status} ${res.statusText}`;
    throw new Error(msg);
  }
  return body;
}

let ladderTimer = null;
function scheduleLadder(delay = 250) {
  clearTimeout(ladderTimer);
  ladderTimer = setTimeout(loadLadder, delay);
}

/* ------------------------------------------------------------------ chain */

let chainSeq = 0;

async function loadChain(fresh = false) {
  const t = state.ticker.trim().toUpperCase();
  if (!t) return;
  ladderSeq++;                  // whatever the old ticker had in flight is now stale
  const ticket = ++chainSeq;
  showError(null);
  setBusy(true);
  try {
    const chain = await getJSON(`${API}/api/chain?ticker=${encodeURIComponent(t)}&fresh=${fresh ? 1 : 0}`);
    if (ticket !== chainSeq) return;         // a newer ticker has taken over
    state.chain = chain;
    state.ticker = chain.ticker;
    $("f-ticker").value = chain.ticker;
    rememberTicker(chain.ticker);

    /* expiry picker */
    const sel = $("f-expiry");
    sel.innerHTML = "";
    const keep = chain.expiries.some((e) => e.expiry === state.expiry) ? state.expiry : null;
    for (const e of chain.expiries) {
      const opt = document.createElement("option");
      opt.value = e.expiry;
      opt.textContent = `${dateLabel(e.expiry)}  ·  ${Math.round(e.dte)}d`;
      sel.appendChild(opt);
    }
    const def = keep || chain.expiries.find((e) => e.dte >= 7)?.expiry || chain.expiries[0]?.expiry;
    if (def) { sel.value = def; state.expiry = def; }

    /* strike suggestions for the chosen expiry */
    fillStrikes();

    /* header badges */
    const src = chain.source || "";
    const stale = src.includes("stale");
    $("session-badge").textContent = chain.session
      ? `${stale ? "stale snapshot" : "delayed"} · ${prettySession(chain.session)}`
      : `${src}`;
    $("session-badge").className = "badge " + (stale ? "stale" : "live");
    $("spot-badge").textContent = `spot ${nf(chain.spot)}`;

    syncDecayBound();
    if (chain.notes && chain.notes.length) {
      chain.notes.forEach((n) => console.info("[chain]", n));
    }
    state.chainError = null;
  } catch (err) {
    if (ticket !== chainSeq) return;         // a stale failure is not the reader's problem
    // No chain means no ladder: say why once and stop, rather than clearing the
    // message and firing a second request that fails for the same reason.
    state.chainError = err.message;
    state.chain = null;
    showError(err.message);
  } finally {
    setBusy(false);
  }
  if (ticket !== chainSeq || state.chainError) return;
  await loadLadder();
}

function prettySession(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const s = d.toLocaleString("en-US", {
    weekday: "short", day: "numeric", month: "short", hour: "2-digit",
    minute: "2-digit", hour12: false, timeZone: "America/New_York",
  });
  return `${s} ET`;
}

function syncDecayBound() {
  const row = currentExpiryRow();
  if (!row) return;
  const dte = Math.max(1, Math.floor(row.dte));
  $("f-days").max = String(dte);
  state.days = clamp(state.days, 0, dte);
  $("f-days").value = String(state.days);
  $("f-days-out").textContent = String(state.days);
}

function currentExpiryRow() {
  return state.chain?.expiries.find((e) => e.expiry === state.expiry) || null;
}

function fillStrikes() {
  const row = currentExpiryRow();
  const input = $("f-strike");
  let dl = document.getElementById("strike-list");
  if (!dl) {
    dl = document.createElement("datalist");
    dl.id = "strike-list";
    document.body.appendChild(dl);
    input.setAttribute("list", "strike-list");
  }
  dl.innerHTML = "";
  if (!row) return;
  for (const s of row.strikes) {
    const o = document.createElement("option");
    o.value = String(s);
    dl.appendChild(o);
  }
  const listed = state.strike !== null && state.strike !== undefined &&
    row.strikes.some((s) => Math.abs(s - state.strike) < 1e-6);
  if (!listed) {
    // only take over the field when what is in it cannot be priced on this expiry;
    // a strike the reader typed while the chain was loading must survive its arrival
    state.strike = row.atm ?? row.strikes[Math.floor(row.strikes.length / 2)];
    input.value = String(state.strike);
  }
}

/* ------------------------------------------------------------------ ladder */

/* Requests race: a slow answer for the previous strike must never land on top of the
   answer for the current one, or the page shows one contract while its controls show
   another. Every request takes a ticket, and only the newest ticket may render. */
let ladderSeq = 0;

async function loadLadder() {
  if (state.chainError || !state.ticker || !state.expiry || !state.strike) return;
  const body = {
    ticker: state.ticker,
    kind: state.kind,
    expiry: state.expiry,
    strike: Number(state.strike),
    model: state.model || null,
    vol_mode: state.volMode,
    vol: state.vol,
    rate: state.rate,
    carry: state.carry,
    days: state.days,
    lo_pct: state.lo,
    hi_pct: state.hi,
    steps: state.steps,
    entry: state.entry,
    side: state.side,
    spot: state.spot,
  };
  const ticket = ++ladderSeq;
  showError(null);
  setBusy(true);
  try {
    const out = await getJSON(`${API}/api/ladder`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (ticket !== ladderSeq) return;          // a newer request has taken over
    state.ladder = out.ladder;
    // the server is the authority on what it priced: if it snapped the expiry to a
    // listed one, the picker has to say so rather than keep naming the date that was asked for
    if (out.ladder.expiry !== state.expiry) {
      state.expiry = out.ladder.expiry;
      $("f-expiry").value = state.expiry;
      fillStrikes();
      syncDecayBound();
    }
    if (Number(out.ladder.strike) !== Number(state.strike)) {
      state.strike = Number(out.ladder.strike);
      $("f-strike").value = String(out.ladder.strike);
    }
    // the server clamps the decay to the contract's life; adopt its answer so the
    // slider, the table header and the URL never disagree with the number shown
    if (out.ladder.days_forward !== state.days) {
      state.days = out.ladder.days_forward;
      $("f-days").value = String(state.days);
      $("f-days-out").textContent = String(state.days);
    }
    render(out);
    syncURL();
  } catch (err) {
    if (ticket !== ladderSeq) return;
    showError(err.message);
  } finally {
    setBusy(false);
  }
}

/* ------------------------------------------------------------------ render */

function render(out) {
  const lad = out.ladder;
  const chain = out.market;
  if (chain) state.chain = chain;

  /* header */
  $("spot-badge").textContent = `spot ${nf(lad.spot)}`;

  /* stats */
  const mk = lad.market || {};
  const perContract = lad.entry * lad.multiplier;
  $("s-premium").textContent = money(lad.entry);
  $("s-premium-note").textContent =
    `${money(perContract, 0)} per contract · ${lad.entry_source}` +
    (mk.mid ? ` · bid ${nf(mk.bid)} / ask ${nf(mk.ask)}` : "");

  $("s-theory").textContent = money(lad.theory_now);
  const gap = mk.theory_vs_anchor;
  $("s-theory-note").textContent = gap === null || gap === undefined
    ? `${lad.model === "binomial" ? "American tree" : "European BS"} · ${lad.days_forward ? `+${lad.days_forward}d` : "today"}`
    : `vs ${mk.anchor === "mid" ? "mid" : "vendor theo"} ${nf(mk.anchor_px)} (${gap >= 0 ? "+" : ""}${nf(gap, 2)})`;

  $("s-iv").textContent = (lad.iv * 100).toFixed(1) + "%";
  $("s-iv-note").textContent = lad.iv_source;
  $("s-iv").className = "v" + (lad.iv_source.startsWith("manual") ? " flat" : "");

  const noBreakeven = !lad.breakeven || lad.breakeven <= 0;
  $("s-be").textContent = noBreakeven ? "—" : nf(lad.breakeven);
  const beMove = (lad.breakeven - lad.spot) / lad.spot;
  $("s-be-note").textContent = noBreakeven
    ? "no price breaks even — the premium exceeds the strike"
    : `${pct(beMove)} from spot · ${lad.kind === "call" ? "up" : "down"} to break even`;

  $("s-delta").textContent = (lad.greeks.delta >= 0 ? "+" : "") + lad.greeks.delta.toFixed(3);
  $("s-delta-note").textContent =
    `Γ ${(lad.greeks.gamma * lad.multiplier).toFixed(2)} /$1 per contract`;

  /* greeks are quoted per share; the ladder's money is per contract, so scale them —
     otherwise "$" sits beside a number that is 100x smaller than the P&L next to it */
  const mult = lad.multiplier;
  $("s-greeks").textContent =
    `Θ ${signedMoney(lad.greeks.theta * mult)}/d   ν ${signedMoney(lad.greeks.vega * mult)}/pt   P₍ITM₎ ` +
    (lad.itm_prob_risk_neutral === null || !Number.isFinite(lad.itm_prob_risk_neutral)
      ? "—" : (lad.itm_prob_risk_neutral * 100).toFixed(0) + "%");
  $("s-greeks-note").textContent =
    `per contract (×${mult}) · ${lad.greeks.delta_model} · risk-neutral P(ITM) · ${lad.dte.toFixed(1)}d left`;

  renderTable(lad);
  drawChart(lad);
  renderNotes(lad, chain);
}

function markTags(marks) {
  return (marks || []).map((m) => {
    const label = m === "spot" ? "spot" : m === "strike" ? "strike" : "break-even";
    return `<span class="tag">${label}</span>`;
  }).join("");
}

function renderTable(lad) {
  const tbody = $("ladder-body");
  const decayed = lad.days_forward > 0;
  const premiumOf = (r) => decayed ? r.premium_after_days : r.premium_now;
  const pnlOf = (r) => decayed ? r.pnl_later : r.pnl_now;
  const retOf = (r) => r.return_expiry;

  /* header tweak for the decay mode */
  const th = $("ladder").querySelector("thead tr");
  const premiumTh = th.children[2];
  premiumTh.textContent = decayed ? `premium +${lad.days_forward}d` : "premium now";

  if (!lad.rows.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty">no rows — widen the range</td></tr>`;
    return;
  }

  const maxAbs = Math.max(...lad.rows.map((r) => Math.abs(r.pnl_expiry)), 1);

  tbody.innerHTML = lad.rows.map((r) => {
    const pm = premiumOf(r);
    const pnl = pnlOf(r);
    const ret = retOf(r);
    const marks = r.marks || [];
    const cls = marks.includes("spot") ? "mark-spot"
      : marks.includes("strike") ? "mark-strike"
      : marks.includes("breakeven") ? "mark-breakeven" : "";
    const barW = clamp((Math.abs(pnl) / maxAbs) * 46, 0, 46);
    const bar = `<span class="bar" style="width:${barW.toFixed(1)}px;background:${pnl >= 0 ? "var(--good)" : "var(--bad)"}"></span>`;
    return `<tr class="${cls}">
      <td class="stock">${nf(r.underlying)}${markTags(marks)}</td>
      <td class="${r.move_pct >= 0 ? "pos" : "neg"}">${pct(r.move_pct)}</td>
      <td>${(r.delta >= 0 ? "+" : "") + r.delta.toFixed(3)}</td>
      <td>${nf(pm)}</td>
      <td class="${toneClass(pnl)}">${signedMoney(pnl)}${bar}</td>
      <td>${nf(r.at_expiry)}</td>
      <td class="${toneClass(r.pnl_expiry)}">${signedMoney(r.pnl_expiry)}</td>
      <td class="${toneClass(r.return_expiry)}">${pct(r.return_expiry)}</td>
    </tr>`;
  }).join("");

  $("table-hint").textContent = decayed
    ? `stock at that price, ${lad.days_forward} days from now · premium = what the contract is worth then`
    : "rows are the stock price; premium now reprices the same contract, expiry value is arithmetic";
}

function renderNotes(lad, chain) {
  const box = $("notes");
  const list = $("notes-list");
  const items = [];

  const prem = lad.entry * lad.multiplier;
  const volStory = lad.vol_mode === "sticky_delta"
    ? `IV starts at ${(lad.iv * 100).toFixed(1)}% but is re-read off this expiry's smile at each price, so the strike keeps its place in the curve rather than its vol — on the equity skew that means a fixed strike loses vol as the stock falls and gains it as the stock rallies`
    : `IV is held at ${(lad.iv * 100).toFixed(1)}% across every row — the strike keeps its vol, not its place in the curve`;
  items.push(
    `Every "premium" here is one contract at that stock price, with ${lad.model === "binomial"
      ? "early exercise allowed (the American tree every US single-name option actually is)"
      : "European exercise assumed (correct for cash-settled index options, slightly low for an American-style put)"}, ` +
    `${volStory}; r ${(lad.rate * 100).toFixed(2)}%, q ${(lad.carry * 100).toFixed(2)}%.`
  );
  items.push(
    `Both vol conventions hold the surface's <em>level</em> where it is. A real selloff bids up vol across the whole curve as well as re-shaping it, ` +
    `so a long put's crash value is usually above every number here and a long call's melt-up value usually below. ` +
    `That lift is not modelled: it would be a guess wearing a parameter's clothes. Read the ladder as the surface-held-still answer, and the smile toggle as the range either side of it.`
  );
  items.push(
    `"At expiry" is not a model: it is max(stock − ${lad.strike}, 0) for a call and max(${lad.strike} − stock, 0) for a put. ` +
    `P&L is that against ${money(lad.entry)} of premium (${money(prem, 0)} per contract), ${lad.side} ${lad.multiplier === 100 ? "one contract" : `${lad.multiplier} shares`}.`
  );
  items.push(
    `The premium columns do not include slippage: buying this costs the ask and selling it gets the bid` +
    (mk_spreadText(lad) ? `, and this contract's spread is ${mk_spreadText(lad)}` : "") + "."
  );
  for (const n of lad.notes || []) items.push(n);
  if (chain && chain.spot_field === "close" && chain.after_hours) {
    items.push(
      `Spot is the session close (${nf(chain.spot)}), not the ${nf(chain.after_hours)} last print: the quotes beside it belong to that close, ` +
      `because pricing Friday's chain against an after-hours tape describes a contract nobody quoted.`);
  }

  list.innerHTML = items.map((t) => `<li>${t}</li>`).join("");
  box.hidden = false;

  const src = chain?.source || "cboe";
  $("provenance").textContent =
    `${lad.ticker} ${lad.kind.toUpperCase()} ${dateLabel(lad.expiry)} ${nf(lad.strike)} · ` +
    `quotes ${chain?.session ? prettySession(chain.session) : "unknown session"} · source ${src}` +
    `${chain?.age_seconds ? ` · ${Math.round(chain.age_seconds)}s old` : ""} · generated ${new Date().toLocaleTimeString()}`;
}

function mk_spreadText(lad) {
  const mk = lad.market || {};
  if (!mk.mid || !mk.spread) return "";
  const p = mk.spread_pct_of_mid;
  return `${nf(mk.spread)} wide (${p === null ? "—" : (p * 100).toFixed(1) + "% of mid"})`;
}

/* ------------------------------------------------------------------ chart */

function drawChart(lad) {
  const svg = $("chart");
  const wrap = $("chart-wrap");
  const W = Math.max(420, wrap.clientWidth - 16);
  const H = 340;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("width", W);
  svg.setAttribute("height", H);
  svg.removeAttribute("preserveAspectRatio");

  const pad = { l: 78, r: 18, t: 14, b: 34 };
  const x0 = pad.l, x1 = W - pad.r, y0 = pad.t, y1 = H - pad.b;

  const rows = lad.rows;
  if (!rows.length) { svg.innerHTML = ""; return; }
  const decayed = lad.days_forward > 0;
  const pnlExp = rows.map((r) => r.pnl_expiry);
  const pnlNow = rows.map((r) => decayed ? r.pnl_later : r.pnl_now);
  const xs = rows.map((r) => r.underlying);
  if (xs.length < 2) { svg.innerHTML = ""; return; }

  let ymin = Math.min(0, ...pnlExp, ...pnlNow);
  let ymax = Math.max(0, ...pnlExp, ...pnlNow);
  if (ymax - ymin < 1e-6) { ymax += 1; ymin -= 1; }
  const padY = (ymax - ymin) * 0.08;
  ymin -= padY; ymax += padY;

  const sx = (x) => x0 + ((x - xs[0]) / (xs[xs.length - 1] - xs[0] || 1)) * (x1 - x0);
  const sy = (y) => y1 - ((y - ymin) / (ymax - ymin)) * (y1 - y0);

  const parts = [];
  parts.push(`<defs>
    <linearGradient id="gExp" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#3ecf8e" stop-opacity=".30"/>
      <stop offset="100%" stop-color="#3ecf8e" stop-opacity="0"/>
    </linearGradient>
  </defs>`);

  /* y grid + labels */
  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const v = ymin + ((ymax - ymin) * i) / ticks;
    const y = sy(v);
    parts.push(`<line x1="${x0}" y1="${y}" x2="${x1}" y2="${y}" stroke="#1e2836" stroke-width="1"/>`);
    parts.push(`<text x="${x0 - 8}" y="${y + 3.5}" fill="#62707f" font-size="10.5" font-family="var(--mono)" text-anchor="end">${signedMoney(v)}</text>`);
  }

  /* x labels */
  const xTicks = Math.min(7, xs.length);
  for (let i = 0; i < xTicks; i++) {
    const idx = Math.round((i * (xs.length - 1)) / (xTicks - 1));
    const x = sx(xs[idx]);
    parts.push(`<text x="${x}" y="${y1 + 18}" fill="#62707f" font-size="10.5" font-family="var(--mono)" text-anchor="middle">${nf(xs[idx])}</text>`);
  }

  /* zero line */
  if (ymin < 0 && ymax > 0) {
    parts.push(`<line x1="${x0}" y1="${sy(0)}" x2="${x1}" y2="${sy(0)}" stroke="#46566a" stroke-width="1" stroke-dasharray="3 3"/>`);
  }

  /* vertical markers */
  const vline = (price, color, dash, label, row) => {
    if (price < xs[0] || price > xs[xs.length - 1]) return;
    const x = sx(price);
    parts.push(`<line x1="${x}" y1="${y0}" x2="${x}" y2="${y1}" stroke="${color}" stroke-width="1" stroke-dasharray="${dash}" opacity=".85"/>`);
    parts.push(`<text x="${x + 5}" y="${y0 + 12 + row * 13}" fill="${color}" font-size="10.5" font-family="var(--mono)">${label}</text>`);
  };
  vline(lad.spot, "#4da3ff", "5 4", `spot ${nf(lad.spot)}`, 0);
  vline(lad.strike, "#e3b341", "2 3", `K ${nf(lad.strike)}`, 1);

  /* area under the expiry P&L */
  const line = (vals) => vals.map((v, i) => `${i ? "L" : "M"}${sx(xs[i]).toFixed(2)},${sy(v).toFixed(2)}`).join(" ");
  parts.push(`<path d="${line(pnlExp)} L${sx(xs[xs.length - 1]).toFixed(2)},${sy(0).toFixed(2)} L${sx(xs[0]).toFixed(2)},${sy(0).toFixed(2)} Z" fill="url(#gExp)"/>`);

  /* premium-now curve, then expiry P&L on top */
  parts.push(`<path d="${line(pnlNow)}" fill="none" stroke="#4da3ff" stroke-width="2" stroke-linejoin="round" opacity=".95"/>`);
  parts.push(`<path d="${line(pnlExp)}" fill="none" stroke="#3ecf8e" stroke-width="2.6" stroke-linejoin="round"/>`);

  /* breakeven dot on the expiry curve */
  const be = lad.breakeven;
  if (be >= xs[0] && be <= xs[xs.length - 1]) {
    parts.push(`<circle cx="${sx(be)}" cy="${sy(0)}" r="4" fill="#0d1117" stroke="#c3b0e8" stroke-width="2"/>`);
    parts.push(`<text x="${sx(be) + 7}" y="${sy(0) - 7}" fill="#c3b0e8" font-size="10.5" font-family="var(--mono)">be ${nf(be)}</text>`);
  }

  /* interactive layer */
  parts.push(`<g id="hoverLayer" style="display:none">
     <line id="hoverLine" x1="0" y1="${y0}" x2="0" y2="${y1}" stroke="#8a97a6" stroke-width="1" stroke-dasharray="2 3" opacity=".7"/>
     <circle id="hoverExp" r="4" fill="#3ecf8e" stroke="#0d1117" stroke-width="2"/>
     <circle id="hoverNow" r="4" fill="#4da3ff" stroke="#0d1117" stroke-width="2"/>
   </g>`);
  parts.push(`<rect id="hoverZone" x="${x0}" y="${y0}" width="${x1 - x0}" height="${y1 - y0}" fill="transparent"/>`);

  svg.innerHTML = parts.join("");

  /* caption */
  const per = lad.multiplier;
  $("chart-caption").textContent =
    `${lad.side} ${lad.kind} · P&L per ${per === 100 ? "contract" : `${per} shares`} after paying ${money(lad.entry)} ` +
    `(${money(per * lad.entry, 0)}). The green line is expiry — arithmetic, and the "P&L at expiry" column. The blue ` +
    `line is the same contract sold ${decayed ? `${lad.days_forward} days from now` : "today"} with the stock there — ` +
    "the \"P&L today\" column, which is where the model lives.";

  attachHover({ svg, rows, xs, sx, sy, pnlExp, pnlNow, lad, decayed, x0, x1, y0, y1 });
}

function attachHover({ svg, rows, xs, sx, sy, pnlExp, pnlNow, lad, decayed, x0, x1, y0, y1 }) {
  const zone = svg.querySelector("#hoverZone");
  const layer = svg.querySelector("#hoverLayer");
  const line = svg.querySelector("#hoverLine");
  const dotE = svg.querySelector("#hoverExp");
  const dotN = svg.querySelector("#hoverNow");
  const tip = $("tooltip");
  const wrap = $("chart-wrap");

  const move = (ev) => {
    const rect = svg.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    if (px < x0 || px > x1) { leave(); return; }
    const frac = (px - x0) / (x1 - x0);
    const target = xs[0] + frac * (xs[xs.length - 1] - xs[0]);
    let best = 0, bestD = Infinity;
    for (let i = 0; i < xs.length; i++) {
      const d = Math.abs(xs[i] - target);
      if (d < bestD) { bestD = d; best = i; }
    }
    const x = sx(xs[best]);
    layer.style.display = "";
    line.setAttribute("x1", x); line.setAttribute("x2", x);
    dotE.setAttribute("cx", x); dotE.setAttribute("cy", sy(pnlExp[best]));
    dotN.setAttribute("cx", x); dotN.setAttribute("cy", sy(pnlNow[best]));

    const r = rows[best];
    tip.hidden = false;
    tip.textContent =
      `stock ${nf(r.underlying)}  ${pct(r.move_pct)}\n` +
      `at expiry ${signedMoney(r.pnl_expiry)}  (${pct(r.return_expiry)})\n` +
      `premium ${decayed ? `+${lad.days_forward}d` : "now"} ${nf(decayed ? r.premium_after_days : r.premium_now)}  ` +
      `(${pct((decayed ? r.premium_after_days : r.premium_now) / lad.entry - 1)})`;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let left = x + 14;
    if (left + tw > wrap.clientWidth - 6) left = x - tw - 14;
    tip.style.left = `${Math.max(4, left)}px`;
    tip.style.top = `${clamp(sy(pnlExp[best]) - th - 10, 4, wrap.clientHeight - th - 4)}px`;
  };
  const leave = () => { layer.style.display = "none"; tip.hidden = true; };
  zone.addEventListener("mousemove", move);
  zone.addEventListener("mouseleave", leave);
  svg.addEventListener("mouseleave", leave);
}

/* ------------------------------------------------------------------ csv / url */

function toCSV() {
  const lad = state.ladder;
  if (!lad) return "";
  const decayed = lad.days_forward > 0;
  const num = (v, d) => (v === null || v === undefined || !Number.isFinite(v))
    ? "" : v.toFixed(d);
  const head = ["ticker", "kind", "expiry", "strike", "spot", "iv", "model", "vol_mode",
    "position", "entry_premium", "entry_source", "stock_price", "move_pct",
    "premium_now", "at_expiry",
    "pnl_if_stock_there_now_per_contract", "return_if_stock_there_now_pct",
    "pnl_at_expiry_per_contract", "return_at_expiry_pct",
    decayed ? `premium_plus_${lad.days_forward}d` : "premium_plus_0d",
    decayed ? `pnl_plus_${lad.days_forward}d_per_contract` : "pnl_plus_0d_per_contract",
    decayed ? `return_plus_${lad.days_forward}d_pct` : "return_plus_0d_pct"];
  const lines = [head.join(",")];
  for (const r of lad.rows) {
    lines.push([
      lad.ticker, lad.kind, lad.expiry, lad.strike, lad.spot, lad.iv.toFixed(6), lad.model,
      lad.vol_mode, lad.side, lad.entry.toFixed(4), `"${lad.entry_source}"`,
      r.underlying.toFixed(4), (r.move_pct * 100).toFixed(3),
      r.premium_now.toFixed(4), r.at_expiry.toFixed(4),
      r.pnl_now.toFixed(2), num(r.return_now === null ? null : r.return_now * 100, 2),
      r.pnl_expiry.toFixed(2), num(r.return_expiry === null ? null : r.return_expiry * 100, 2),
      r.premium_after_days.toFixed(4),
      r.pnl_later.toFixed(2),
      num(r.return_later === null ? null : r.return_later * 100, 2),
    ].join(","));
  }
  return lines.join("\n");
}

function downloadCSV() {
  const csv = toCSV();
  if (!csv) return;
  const lad = state.ladder;
  const name = `${lad.ticker}-${lad.kind}-${lad.expiry}-${lad.strike}-ladder.csv`
    .replace(/\s+/g, "");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

function syncURL() {
  const p = new URLSearchParams();
  if (state.ticker) p.set("t", state.ticker);
  p.set("k", state.kind);
  if (state.expiry) p.set("e", state.expiry);
  if (state.strike) p.set("s", state.strike);
  if (state.lo !== -25) p.set("lo", state.lo);
  if (state.hi !== 25) p.set("hi", state.hi);
  if (state.steps !== 15) p.set("n", state.steps);
  if (state.days) p.set("d", state.days);
  if (state.vol) p.set("v", state.vol);
  if (state.rate !== null) p.set("r", state.rate);
  if (state.carry !== null) p.set("q", state.carry);
  if (state.entry) p.set("ent", state.entry);
  if (state.spot) p.set("px", state.spot);
  if (state.model) p.set("m", state.model);
  if (state.volMode !== "sticky_strike") p.set("vm", state.volMode);
  if (state.side !== "long") p.set("side", state.side);
  history.replaceState(null, "", `${location.pathname}?${p.toString()}`);
}

function readURL() {
  const p = new URLSearchParams(location.search);
  const num = (k) => (p.has(k) && p.get(k) !== "" ? Number(p.get(k)) : null);
  state.ticker = (p.get("t") || localStorage.getItem("ol.ticker") || "").toUpperCase();
  state.kind = p.get("k") === "put" ? "put" : "call";
  state.expiry = p.get("e") || "";
  state.strike = num("s");
  if (num("lo") !== null) state.lo = num("lo");
  if (num("hi") !== null) state.hi = num("hi");
  if (num("n") !== null) state.steps = num("n");
  if (num("d") !== null) state.days = num("d");
  state.vol = num("v");
  state.rate = num("r");
  state.carry = num("q");
  state.entry = num("ent");
  state.spot = num("px");
  const mv = p.get("m");
  state.model = mv === "bs" || mv === "binomial" ? mv : "";   /* "" = let the server choose */
  state.volMode = p.get("vm") === "sticky_delta" ? "sticky_delta" : "sticky_strike";
  state.side = p.get("side") === "short" ? "short" : "long";
}

function rememberTicker(t) {
  try {
    const list = JSON.parse(localStorage.getItem("ol.history") || "[]");
    const next = [t, ...list.filter((x) => x !== t)].slice(0, 12);
    localStorage.setItem("ol.history", JSON.stringify(next));
    localStorage.setItem("ol.ticker", t);
    $("ticker-history").innerHTML = next.map((x) => `<option value="${x}">`).join("");
  } catch (_) { /* private mode: history is optional */ }
}

/* ------------------------------------------------------------------ ui glue */

function showError(msg) {
  const box = $("error");
  if (!msg) { box.hidden = true; box.textContent = ""; return; }
  box.hidden = false;
  box.textContent = msg;
}

function setBusy(on) {
  state.loading = on;
  const s = $("btn-refresh").querySelector(".spin");
  if (s) s.hidden = !on;
}

function reflectForm() {
  $("f-ticker").value = state.ticker;
  document.querySelectorAll("[data-kind]").forEach((b) =>
    b.classList.toggle("on", b.dataset.kind === state.kind));
  document.querySelectorAll("[data-side]").forEach((b) =>
    b.classList.toggle("on", b.dataset.side === state.side));
  $("f-expiry").value = state.expiry || "";
  if (state.strike) $("f-strike").value = String(state.strike);
  $("f-lo").value = String(state.lo);
  $("f-hi").value = String(state.hi);
  $("f-steps").value = String(state.steps);
  $("f-days").value = String(state.days);
  $("f-days-out").textContent = String(state.days);
  $("f-vol").value = state.vol === null ? "" : String(+(state.vol * 100).toFixed(2));
  $("f-rate").value = state.rate === null ? "" : String(+(state.rate * 100).toFixed(3));
  $("f-carry").value = state.carry === null ? "" : String(+(state.carry * 100).toFixed(3));
  $("f-entry").value = state.entry === null ? "" : String(state.entry);
  $("f-spot").value = state.spot === null ? "" : String(state.spot);
  $("f-model").value = state.model;
  $("f-volmode").value = state.volMode;
}

function wire() {
  /* ticker: load the chain on Enter or blur, debounced on typing */
  let tickerTimer = null;
  $("f-ticker").addEventListener("input", (e) => {
    const v = e.target.value.toUpperCase().trim();
    state.ticker = v;
    clearTimeout(tickerTimer);
    tickerTimer = setTimeout(() => { if (v) loadChain(false); }, 600);
  });
  $("f-ticker").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); state.expiry = ""; state.strike = null; loadChain(false); }
  });

  document.querySelectorAll("[data-kind]").forEach((b) =>
    b.addEventListener("click", () => { state.kind = b.dataset.kind; reflectForm(); scheduleLadder(0); }));
  document.querySelectorAll("[data-side]").forEach((b) =>
    b.addEventListener("click", () => { state.side = b.dataset.side; reflectForm(); scheduleLadder(0); }));

  $("f-expiry").addEventListener("change", (e) => {
    state.expiry = e.target.value;
    state.strike = null;
    state.days = 0;
    fillStrikes();
    syncDecayBound();
    scheduleLadder(0);
  });

  $("f-strike").addEventListener("input", (e) => {
    const v = Number(e.target.value);
    if (Number.isFinite(v) && v > 0) { state.strike = v; scheduleLadder(280); }
  });

  for (const id of ["f-lo", "f-hi", "f-steps"]) {
    $(id).addEventListener("input", (e) => {
      const v = Number(e.target.value);
      if (!Number.isFinite(v)) return;
      if (id === "f-lo") state.lo = v;
      if (id === "f-hi") state.hi = v;
      if (id === "f-steps") state.steps = clamp(Math.round(v), 4, 60);
      scheduleLadder(280);
    });
  }

  $("f-days").addEventListener("input", (e) => {
    state.days = Number(e.target.value) || 0;
    $("f-days-out").textContent = String(state.days);
    scheduleLadder(120);
  });

  const advDebounce = () => window.setTimeout(() => {
    const read = (id, scale = 1) => {
      const raw = $(id).value.trim();
      if (raw === "") return null;
      const v = Number(raw);
      return Number.isFinite(v) ? v * scale : null;
    };
    state.vol = read("f-vol", 0.01);
    state.rate = read("f-rate", 0.01);
    state.carry = read("f-carry", 0.01);
    state.entry = read("f-entry");
    state.spot = read("f-spot");
    scheduleLadder(0);
  }, 220);
  for (const id of ["f-vol", "f-rate", "f-carry", "f-entry", "f-spot"]) {
    $(id).addEventListener("input", advDebounce);
  }
  $("f-model").addEventListener("change", (e) => { state.model = e.target.value || ""; scheduleLadder(0); });
  $("f-volmode").addEventListener("change", (e) => { state.volMode = e.target.value; scheduleLadder(0); });

  $("btn-refresh").addEventListener("click", () => loadChain(true));
  $("btn-csv").addEventListener("click", downloadCSV);
  $("btn-link").addEventListener("click", async () => {
    syncURL();
    const btn = $("btn-link");
    try {
      await navigator.clipboard.writeText(location.href);
      btn.textContent = "copied";
    } catch (_) {
      btn.textContent = "url in the address bar";
    }
    setTimeout(() => { btn.textContent = "copy link"; }, 1600);
  });

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (state.ladder) drawChart(state.ladder); }, 120);
  });

  /* keyboard: "/" focuses the ticker */
  window.addEventListener("keydown", (e) => {
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    if (e.key === "/" && tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") {
      e.preventDefault();
      $("f-ticker").focus();
      $("f-ticker").select();
    }
  });
}

async function init() {
  readURL();
  reflectForm();
  try {
    const hist = JSON.parse(localStorage.getItem("ol.history") || "[]");
    $("ticker-history").innerHTML = hist.map((x) => `<option value="${x}">`).join("");
  } catch (_) { /* optional */ }
  if (!state.ticker) {
    state.ticker = "AAPL";
    state.expiry = "";
    state.strike = null;
  }
  reflectForm();
  wire();
  await loadChain(false);
}

init();
