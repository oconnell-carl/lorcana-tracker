/* Lorcana Price Tracker frontend (rebuilt from oconnell-carl/lorcana-tracker static/app.js) */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const content = $("#content");

const state = {
  sets: [],
  setView: null, // { set, cards }
  sortBy: "num", sortDir: "asc", rarity: "all", ink: "all", filter: "",
  sealed: null, sealedType: "all", sealedSort: "trend", sealedDir: "desc", sealedFilter: "",
  range: "all", chart: null, status: null,
};

const RARITY_ORDER = { Iconic: 0, Enchanted: 1, Epic: 2, Legendary: 3, Super_rare: 4, Rare: 5, Uncommon: 6, Common: 7, Promo: 8 };
const RARITY_LABEL = { Super_rare: "Super Rare" };
const INKS = ["Amber", "Amethyst", "Emerald", "Ruby", "Sapphire", "Steel"];
const rl = (r) => RARITY_LABEL[r] || r || "—";

async function api(path) {
  const r = await fetch(`${window.API_BASE}/api/app${path}`);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function money(v, cur = "EUR") {
  if (v == null || !isFinite(v)) return `<span class="na">—</span>`;
  const sym = cur === "USD" ? "$" : "€";
  const d = v >= 1000 ? 0 : 2;
  return `${sym}${Number(v).toLocaleString("en-IE", { minimumFractionDigits: d, maximumFractionDigits: d })}`;
}
function pctVal(a, b) { return a == null || b == null || !b ? null : ((a - b) / b) * 100; }
function pct(v) {
  if (v == null || !isFinite(v)) return `<span class="na">—</span>`;
  const cls = v > 3 ? "up" : v < -3 ? "down" : "flat";
  return `<span class="pct ${cls}">${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(1)}%</span>`;
}
const rarityChip = (r) => r ? `<span class="rarity r-${esc(r.toLowerCase())}">${esc(rl(r))}</span>` : "";
const inkDot = (ink) => ink ? `<span class="ink ink-${esc(ink.toLowerCase())}" title="${esc(ink)}"></span>` : "";
const usdBest = (c) => c.usd ?? c.usd_foil;

function thumb(c, cls = "thumb") {
  return c.image_small
    ? `<img class="${cls}" src="${esc(c.image_small)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'${cls} ph'}))" />`
    : `<span class="${cls} ph"></span>`;
}

function skeleton(rows = 8) {
  return `<div class="skel-head"></div>` + Array.from({ length: rows }, () => `<div class="skel-row"></div>`).join("");
}
function errorBox(e) {
  return `<div class="empty"><h2>Couldn't load data</h2><p>${esc(e.message)}. The data service may be waking up — try again in a few seconds.</p><button class="btn" onclick="route()">Retry</button></div>`;
}

// ------------------------------------------------------------------ status
async function loadStatus() {
  const pill = $("#status-pill");
  try {
    const s = await api("/status");
    state.status = s;
    const when = s.cardmarket_created_at ? new Date(s.cardmarket_created_at.replace(/([+-]\d\d)(\d\d)$/, "$1:$2")) : null;
    const d = when ? when.toLocaleDateString("en-IE", { day: "numeric", month: "short" }) : s.last_snapshot;
    pill.innerHTML = `<span class="dot ${s.refreshing ? "busy" : "ok"}"></span>${s.refreshing ? "Refreshing…" : `Prices ${esc(d)}`} · ${s.snapshot_days} day${s.snapshot_days === 1 ? "" : "s"} tracked`;
    pill.title = `${s.card_count} cards · ${s.set_count} sets · ${s.sealed_count} sealed products. Last refresh ${s.refreshed_at || "—"}`;
  } catch {
    pill.innerHTML = `<span class="dot off"></span>Offline`;
  }
}

// ------------------------------------------------------------------ router
function setNav(which) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.nav === which));
}
async function route() {
  if (state.chart) { state.chart.destroy(); state.chart = null; }
  const h = location.hash.replace(/^#\/?/, "");
  const [view, id] = h.split("/").map(decodeURIComponent);
  window.scrollTo(0, 0);
  if (view === "set" && id) { setNav("cards"); return renderSet(id); }
  if (view === "card" && id) { setNav("cards"); return renderCard(id); }
  if (view === "market") { setNav("market"); return renderMarket(); }
  if (view === "sealed" && id) { setNav("sealed"); return renderSealedDetail(id); }
  if (view === "sealed") { setNav("sealed"); return renderSealed(); }
  setNav("cards");
  return renderSets();
}
window.addEventListener("hashchange", route);

// ------------------------------------------------------------------ sets landing
async function renderSets() {
  content.innerHTML = `<div class="page">${skeleton(6)}</div>`;
  try {
    if (!state.sets.length) state.sets = (await api("/sets")).sets;
    const byDate = (a, b) => String(b.release_date || "").localeCompare(String(a.release_date || ""));
    const main = state.sets.filter((s) => s.card_count > 0).sort(byDate);
    const other = state.sets.filter((s) => !(s.card_count > 0)).sort(byDate);
    const tile = (s) => `
      <a class="set-tile" href="#/set/${s.id}">
        <div class="set-art">${s.cover ? `<img src="${esc(s.cover)}" alt="" loading="lazy" />` : ""}${s.code ? `<span class="set-code">${esc(s.code)}</span>` : ""}</div>
        <div class="set-body">
          <div class="set-name">${esc(s.name)}</div>
          <div class="set-meta"><span>${s.card_count} cards</span><span>${s.release_date ? new Date(s.release_date).getFullYear() : ""}</span></div>
          <div class="set-value"><span class="lbl">Set value (trend)</span><span class="tab-num">${money(s.set_value)}</span></div>
        </div>
      </a>`;
    content.innerHTML = `
      <div class="page">
        <div class="page-head">
          <div><h1>Sets</h1><p class="sub">Pick a set to see every card with Cardmarket and TCGPlayer prices.</p></div>
        </div>
        ${main.length ? `<h2 class="section-label">Main sets</h2>
        <div class="set-grid">${main.map(tile).join("")}</div>` : ""}
        ${other.length ? `<h2 class="section-label">Promos & special releases</h2>
        <div class="set-grid compact">${other.map(tile).join("")}</div>` : ""}
      </div>`;
  } catch (e) { content.innerHTML = errorBox(e); }
}

// ------------------------------------------------------------------ set table
async function renderSet(code) {
  content.innerHTML = `<div class="page">${skeleton()}</div>`;
  try {
    if (!state.setView || String(state.setView.set.id) !== String(code)) {
      state.setView = await api(`/sets/${encodeURIComponent(code)}/cards`);
      state.rarity = "all"; state.filter = ""; state.sortBy = "num"; state.sortDir = "asc";
    }
    drawSet();
  } catch (e) { content.innerHTML = errorBox(e); }
}

function sortedCards() {
  const { cards } = state.setView;
  const f = state.filter.toLowerCase();
  let list = cards.filter((c) =>
    (state.rarity === "all" || c.rarity === state.rarity) &&
    (!f || (c.full_name || "").toLowerCase().includes(f)));
  const key = {
    num: (c) => c.num_sort, name: (c) => c.full_name, rarity: (c) => RARITY_ORDER[c.rarity] ?? 99,
    trend: (c) => c.cm_trend, low: (c) => c.cm_low, avg7: (c) => c.cm_avg7, avg30: (c) => c.cm_avg30,
    vs30: (c) => pctVal(c.cm_trend, c.cm_avg30), usd: usdBest, foil: (c) => c.usd_foil,
  }[state.sortBy];
  list = [...list].sort((a, b) => {
    const x = key(a), y = key(b);
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    const cmp = typeof x === "string" ? x.localeCompare(y) : x - y;
    return state.sortDir === "asc" ? cmp : -cmp;
  });
  return list;
}

function drawSet() {
  const { set, cards } = state.setView;
  const list = sortedCards();
  const counts = {};
  cards.forEach((c) => (counts[c.rarity] = (counts[c.rarity] || 0) + 1));
  const rarities = ["all", ...Object.keys(RARITY_ORDER).filter((r) => counts[r])];
  const total = cards.reduce((s, c) => s + (c.cm_trend || 0), 0);
  const top = [...cards].sort((a, b) => (b.cm_trend || 0) - (a.cm_trend || 0))[0];
  const priced = cards.filter((c) => c.cm_trend != null).length;
  const arrow = (k) => state.sortBy === k ? (state.sortDir === "asc" ? "↑" : "↓") : "";
  const th = (k, label, cls = "") => `<th class="sortable ${cls}" data-sort="${k}" aria-sort="${state.sortBy === k ? (state.sortDir === "asc" ? "ascending" : "descending") : "none"}">${label}<span class="arrow">${arrow(k)}</span></th>`;

  const rows = list.map((c) => `
    <tr data-id="${c.id}" tabindex="0">
      <td class="c-thumb">${thumb(c)}</td>
      <td class="c-num tab-num">${esc(c.card_number)}</td>
      <td class="c-name"><div class="nm"><span>${esc(c.name)}</span></div>${c.version ? `<div class="ver">${esc(c.version)}</div>` : ""}</td>
      <td class="hide-sm">${rarityChip(c.rarity)}</td>
      <td class="num eur strong">${money(c.cm_trend)}</td>
      <td class="num hide-md">${money(c.cm_low)}</td>
      <td class="num hide-md">${money(c.cm_avg7)}</td>
      <td class="num hide-sm">${money(c.cm_avg30)}</td>
      <td class="num hide-sm">${pct(pctVal(c.cm_trend, c.cm_avg30))}</td>
      <td class="num usd">${money(c.usd, c.usd_currency)}</td>
      <td class="num usd hide-md">${money(c.usd_foil, c.usd_currency)}</td>
    </tr>`).join("") || `<tr><td colspan="11" class="empty-row">No cards match these filters.</td></tr>`;

  content.innerHTML = `
    <div class="page">
      <a class="back" href="#/">← All sets</a>
      <div class="page-head">
        <div><h1>${esc(set.name)}</h1><p class="sub">Set ${esc(set.code)} · ${set.release_date ? new Date(set.release_date).toLocaleDateString("en-IE", { day: "numeric", month: "long", year: "numeric" }) : ""}</p></div>
      </div>
      <div class="kpis">
        <div class="kpi"><div class="k-label">Cards</div><div class="k-val tab-num">${cards.length}</div><div class="k-foot">${priced} with Cardmarket price</div></div>
        <div class="kpi"><div class="k-label">Full set value</div><div class="k-val tab-num eur">${money(total)}</div><div class="k-foot">sum of lowest NM prices</div></div>
        <div class="kpi"><div class="k-label">Top card</div><div class="k-val tab-num eur">${top ? money(top.cm_trend) : "—"}</div><div class="k-foot">${top ? esc(top.full_name) : ""}</div></div>
      </div>
      <div class="toolbar">
        <div class="chips" role="group" aria-label="Rarity">${rarities.map((r) => `<button class="chip ${state.rarity === r ? "active" : ""}" data-rarity="${r}">${r === "all" ? "All" : esc(rl(r))}<span class="cnt">${r === "all" ? cards.length : counts[r]}</span></button>`).join("")}</div>
        <div class="toolbar-right">
          <input class="filter" id="set-filter" type="search" placeholder="Filter this set" value="${esc(state.filter)}" aria-label="Filter cards in this set" />
        </div>
      </div>
      <div class="table-wrap">
        <table class="grid">
          <thead><tr>
            <th class="c-thumb"></th>${th("num", "#")}${th("name", "Card")}${th("rarity", "Rarity", "hide-sm")}
            ${th("trend", "CM price", "num")}${th("low", "CM from", "num hide-md")}${th("avg7", "7D avg", "num hide-md")}${th("avg30", "30D avg", "num hide-sm")}${th("vs30", "vs 30D", "num hide-sm")}
            ${th("usd", "TCGP", "num")}${th("foil", "TCGP foil", "num hide-md")}
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <p class="note">CM price is the lowest English near-mint listing; “CM from” is the lowest current listing in any language or condition.</p>
    </div>`;

  $$("th.sortable").forEach((t) => t.addEventListener("click", () => {
    const k = t.dataset.sort;
    if (state.sortBy === k) state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
    else { state.sortBy = k; state.sortDir = ["num", "name", "rarity"].includes(k) ? "asc" : "desc"; }
    drawSet();
  }));
  $$(".chip[data-rarity]").forEach((b) => b.addEventListener("click", () => { state.rarity = b.dataset.rarity; drawSet(); }));
  const fi = $("#set-filter");
  fi.addEventListener("input", () => {
    state.filter = fi.value;
    const pos = fi.selectionStart;
    drawSet();
    const n = $("#set-filter"); n.focus(); n.setSelectionRange(pos, pos);
  });
  $$("tbody tr[data-id]").forEach((tr) => {
    const go = () => (location.hash = `#/card/${tr.dataset.id}`);
    tr.addEventListener("click", go);
    tr.addEventListener("keydown", (e) => e.key === "Enter" && go());
  });
}

// ------------------------------------------------------------------ card detail
async function renderCard(id) {
  content.innerHTML = `<div class="page">${skeleton(4)}</div>`;
  try {
    const { card: c } = await api(`/cards/${encodeURIComponent(id)}`);
    const vs30 = pctVal(c.cm_trend, c.cm_avg30);
    content.innerHTML = `
      <div class="page">
        <a class="back" href="#/set/${c.set_id}">← ${esc(c.set_name)}</a>
        <div class="detail">
          <div class="detail-art">${c.image_large ? `<img src="${esc(c.image_large)}" alt="${esc(c.full_name)}" />` : `<div class="art-ph">No image</div>`}</div>
          <div class="detail-main">
            <h1>${esc(c.name)}</h1>
            ${c.version ? `<div class="version">${esc(c.version)}</div>` : ""}
            <div class="meta-row">${esc(c.set_name)} · #${esc(c.card_number)} ${rarityChip(c.rarity)}</div>
            <div class="price-cards">
              <div class="pcard"><div class="p-label">Cardmarket lowest NM</div><div class="p-val eur">${money(c.cm_trend)}</div><div class="p-foot">${pct(vs30)} vs 30-day avg</div></div>
              <div class="pcard"><div class="p-label">Cardmarket from</div><div class="p-val">${money(c.cm_low)}</div><div class="p-foot">lowest listing, any condition</div></div>
              <div class="pcard"><div class="p-label">TCGPlayer market</div><div class="p-val usd">${money(usdBest(c), c.usd_currency)}</div><div class="p-foot">converted to EUR</div></div>
            </div>
            <div class="links">
              ${c.cm_url ? `<a class="btn" href="${esc(c.cm_url)}" target="_blank" rel="noopener">View on Cardmarket ↗</a>` : ""}
              ${c.tcgplayer_url ? `<a class="btn ghost" href="${esc(c.tcgplayer_url)}" target="_blank" rel="noopener">View on TCGPlayer ↗</a>` : ""}
            </div>
          </div>
        </div>

        <section class="panel">
          <div class="panel-head">
            <h2>Price trend</h2>
            <div class="seg" id="range">${["30d", "3m", "6m", "1y", "all"].map((r) => `<button data-range="${r}" class="${state.range === r ? "active" : ""}">${r.toUpperCase()}</button>`).join("")}</div>
          </div>
          <div class="chart-box"><canvas id="chart"></canvas></div>
          <div class="chart-note" id="chart-note"></div>
        </section>

        <section class="panel">
          <div class="panel-head"><h2>Cardmarket averages</h2></div>
          <div class="avg-bars">${[["30-day avg", c.cm_avg30], ["7-day avg", c.cm_avg7], ["1-day avg", c.cm_avg1], ["Lowest NM", c.cm_trend], ["From", c.cm_low]].map(([l, v]) => {
            const max = Math.max(c.cm_avg30 || 0, c.cm_avg7 || 0, c.cm_avg1 || 0, c.cm_trend || 0, c.cm_low || 0) || 1;
            return `<div class="bar-row"><span class="b-lbl">${l}</span><span class="b-track"><span class="b-fill" style="width:${v ? Math.max(2, (v / max) * 100) : 0}%"></span></span><span class="b-val tab-num">${money(v)}</span></div>`;
          }).join("")}</div>
        </section>

        ${c.printings.length ? `<section class="panel">
          <div class="panel-head"><h2>Other printings</h2></div>
          <div class="printings">${c.printings.map((p) => `
            <a class="printing" href="#/card/${p.id}">${thumb(p, "pthumb")}<div><div class="pr-set">${esc(p.set_name)} · #${esc(p.card_number)}</div>${rarityChip(p.rarity)}<div class="pr-price">${money(p.cm_trend)} <span class="usd">${money(usdBest(p), p.usd_currency)}</span></div></div></a>`).join("")}</div>
        </section>` : ""}
      </div>`;
    const fb = [["30-day avg", c.cm_avg30], ["7-day avg", c.cm_avg7], ["1-day avg", c.cm_avg1], ["Trend now", c.cm_trend]];
    $$("#range button").forEach((b) => b.addEventListener("click", () => {
      state.range = b.dataset.range;
      $$("#range button").forEach((x) => x.classList.toggle("active", x === b));
      drawHistory(`/cards/${id}/history`, [["cm_trend", "Cardmarket price (EUR)", "#d9ae5c"], ["cm_low", "Cardmarket from (EUR)", "#8a7a55"], ["usd", "TCGPlayer (EUR)", "#7aa7e0"]], fb);
    }));
    drawHistory(`/cards/${id}/history`, [["cm_trend", "Cardmarket price (EUR)", "#d9ae5c"], ["cm_low", "Cardmarket from (EUR)", "#8a7a55"], ["usd", "TCGPlayer (EUR)", "#7aa7e0"]], fb);
  } catch (e) { content.innerHTML = errorBox(e); }
}

async function drawHistory(path, series, fallback) {
  const note = $("#chart-note");
  try {
    let { points } = await api(`${path}?range=${state.range}`);
    const realCount = points.length;
    if (realCount < 2 && fallback) {
      // Not enough daily snapshots yet: plot Cardmarket's own rolling averages instead.
      series = [["v", "Cardmarket (EUR)", "#d9ae5c"]];
      points = fallback.filter(([, v]) => v != null).map(([date, v]) => ({ date, v }));
    }
    const active = series.filter(([k]) => points.some((p) => p[k] != null));
    const canvas = $("#chart");
    if (!canvas) return;
    if (state.chart) state.chart.destroy();
    await waitForChart();
    state.chart = new Chart(canvas, {
      type: "line",
      data: {
        labels: points.map((p) => p.date),
        datasets: active.map(([k, label, color]) => ({
          label, data: points.map((p) => p[k]), borderColor: color, backgroundColor: color,
          tension: 0.25, spanGaps: true, borderWidth: 2, pointRadius: points.length < 15 ? 4 : 0, pointHoverRadius: 5,
        })),
      },
      options: {
        responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "bottom", labels: { color: "#a6abc2", boxWidth: 10, boxHeight: 10, usePointStyle: true, font: { family: "General Sans", size: 12 } } },
          tooltip: { backgroundColor: "#161a2e", borderColor: "#2b3150", borderWidth: 1, titleFont: { family: "General Sans" }, bodyFont: { family: "General Sans" },
            callbacks: { label: (ctx) => `${ctx.dataset.label}: ${ctx.dataset.label.includes("USD") ? "$" : "€"}${ctx.parsed.y?.toFixed(2) ?? "—"}` } },
        },
        scales: {
          x: { ticks: { color: "#6f7591", maxRotation: 0, autoSkip: true, font: { family: "General Sans" } }, grid: { color: "rgba(255,255,255,0.04)" } },
          y: { ticks: { color: "#6f7591", font: { family: "General Sans" } }, grid: { color: "rgba(255,255,255,0.06)" }, beginAtZero: false },
        },
      },
    });
    note.textContent = realCount < 2 && fallback
      ? "Not enough daily snapshots yet, so this shows Cardmarket's current reference prices. A true day-by-day history builds from here, one snapshot per day."
      : `${realCount} daily snapshot${realCount === 1 ? "" : "s"} in range.`;
  } catch (e) { note.textContent = `Couldn't load history: ${e.message}`; }
}
function waitForChart() {
  return new Promise((res) => { const t = () => (window.Chart ? res() : setTimeout(t, 50)); t(); });
}

// ------------------------------------------------------------------ market report
function moverList(items, { sealed = false } = {}) {
  if (!items.length) return `<p class="muted small">Nothing moved enough to report.</p>`;
  return `<ol class="movers">${items.map((m) => `
    <li><a href="${sealed ? `#/sealed/${m.id}` : `#/card/${m.id}`}">
      ${sealed ? `<span class="mthumb ph sealed-ph"></span>` : thumb(m, "mthumb")}
      <span class="m-main"><span class="m-name">${esc(m.full_name)}</span><span class="m-sub">${sealed ? esc(m.product_type) : `${esc(m.set_name)} · ${esc(rl(m.rarity))}`}</span></span>
      <span class="m-nums"><span class="m-now tab-num">${money(m.now)}</span><span class="m-was tab-num">from ${money(m.then)}</span></span>
      ${pct(m.pct)}
    </a></li>`).join("")}</ol>`;
}

async function renderMarket() {
  content.innerHTML = `<div class="page">${skeleton(6)}</div>`;
  try {
    const m = await api("/market");
    content.innerHTML = `
      <div class="page">
        <div class="page-head"><div><h1>Market report</h1><p class="sub">Premium rarities (Iconic, Enchanted, Epic, Legendary, Promo) between €2 and €10,000 on Cardmarket. Moves under €0.50 or 3% are ignored.</p></div></div>
        <div class="report-grid">
          <section class="panel"><div class="panel-head"><h2>Rising ${m.snapshot_days >= 2 ? "today" : "now"}</h2><span class="basis">${esc(m.day.basis)}</span></div>${moverList(m.day.up)}</section>
          <section class="panel"><div class="panel-head"><h2>Falling ${m.snapshot_days >= 2 ? "today" : "now"}</h2><span class="basis">${esc(m.day.basis)}</span></div>${moverList(m.day.down)}</section>
          <section class="panel"><div class="panel-head"><h2>Week risers</h2><span class="basis">${esc(m.week.basis)}</span></div>${moverList(m.week.up)}</section>
          <section class="panel"><div class="panel-head"><h2>Week fallers</h2><span class="basis">${esc(m.week.basis)}</span></div>${moverList(m.week.down)}</section>
        </div>
        ${m.lows.length ? `<section class="panel"><div class="panel-head"><h2>At lowest tracked price</h2></div>${moverList(m.lows.map((c) => ({ ...c, now: c.cm_trend, then: c.cm_avg30, pct: pctVal(c.cm_trend, c.cm_avg30) })))}</section>` : ""}
        <section class="panel">
          <div class="panel-head"><h2>Most valuable cards</h2><span class="basis">Cardmarket lowest NM</span></div>
          <div class="top-grid">${m.top.map((c, i) => `<a class="top-card" href="#/card/${c.id}"><span class="rank">${i + 1}</span>${thumb(c, "tthumb")}<span class="t-name">${esc(c.full_name)}</span><span class="t-sub">${esc(c.set_name)}</span><span class="t-price eur">${money(c.cm_trend)}</span></a>`).join("")}</div>
        </section>
        <div class="report-grid">
          <section class="panel"><div class="panel-head"><h2>Booster box ladder</h2><span class="basis">English booster boxes, lowest listing</span></div>
            <div class="ladder">${(() => { const max = Math.max(...m.booster_ladder.map((b) => b.trend || 0)) || 1; return m.booster_ladder.map((b) => `<a class="bar-row" href="#/sealed/${b.id}"><span class="b-lbl">${esc(b.set_name || b.name.replace(/ Booster Box$/, ""))}</span><span class="b-track"><span class="b-fill" style="width:${Math.max(2, ((b.trend || 0) / max) * 100)}%"></span></span><span class="b-val tab-num">${money(b.trend)}</span></a>`).join(""); })()}</div>
          </section>
          <section class="panel"><div class="panel-head"><h2>Sealed movers</h2><span class="basis">${esc(m.sealed_movers.basis)} · boxes, troves, gift & collector sets</span></div>
            <h3 class="mini">Up</h3>${moverList(m.sealed_movers.up, { sealed: true })}
            <h3 class="mini">Down</h3>${moverList(m.sealed_movers.down, { sealed: true })}
          </section>
        </div>
      </div>`;
  } catch (e) { content.innerHTML = errorBox(e); }
}

// ------------------------------------------------------------------ sealed
async function renderSealed() {
  content.innerHTML = `<div class="page">${skeleton()}</div>`;
  try {
    if (!state.sealed) state.sealed = await api("/sealed");
    drawSealed();
  } catch (e) { content.innerHTML = errorBox(e); }
}

function drawSealed() {
  const { products, types } = state.sealed;
  const f = state.sealedFilter.toLowerCase();
  const key = { name: (p) => p.name, trend: (p) => p.trend, low: (p) => p.low, avg: (p) => p.avg, vsavg: (p) => pctVal(p.trend, p.avg), added: (p) => p.date_added }[state.sealedSort];
  const list = products
    .filter((p) => (state.sealedType === "all" || p.product_type === state.sealedType) && (!f || p.name.toLowerCase().includes(f)))
    .sort((a, b) => {
      const x = key(a), y = key(b);
      if (x == null) return 1; if (y == null) return -1;
      const c = typeof x === "string" ? x.localeCompare(y) : x - y;
      return state.sealedDir === "asc" ? c : -c;
    });
  const arrow = (k) => state.sealedSort === k ? (state.sealedDir === "asc" ? "↑" : "↓") : "";
  const th = (k, l, cls = "") => `<th class="sortable ${cls}" data-sort="${k}">${l}<span class="arrow">${arrow(k)}</span></th>`;
  const shown = types.filter((t) => t.n >= 2);
  content.innerHTML = `
    <div class="page">
      <div class="page-head"><div><h1>Sealed products</h1><p class="sub">${products.length} Lorcana boxes, packs, decks and sets on Cardmarket.</p></div></div>
      <div class="toolbar">
        <div class="chips">${[{ product_type: "all", n: products.length }, ...shown].map((t) => `<button class="chip ${state.sealedType === t.product_type ? "active" : ""}" data-type="${esc(t.product_type)}">${t.product_type === "all" ? "All" : esc(t.product_type)}<span class="cnt">${t.n}</span></button>`).join("")}</div>
        <div class="toolbar-right"><input class="filter" id="sealed-filter" type="search" placeholder="Filter products" value="${esc(state.sealedFilter)}" aria-label="Filter sealed products" /></div>
      </div>
      <div class="table-wrap">
        <table class="grid">
          <thead><tr>${th("name", "Product")}<th class="hide-sm">Type</th>${th("trend", "Lowest", "num")}${th("low", "From", "num")}${th("avg", "Avg sell", "num hide-sm")}${th("vsavg", "Lowest vs avg", "num hide-sm")}${th("added", "Listed", "num hide-md")}</tr></thead>
          <tbody>${list.map((p) => `
            <tr data-id="${p.id}" tabindex="0">
              <td class="c-name no-ink"><div class="nm"><span>${esc(p.name)}</span></div>${p.set_name ? `<div class="ver">${esc(p.set_name)}</div>` : ""}</td>
              <td class="hide-sm"><span class="type-chip">${esc(p.product_type)}</span></td>
              <td class="num eur strong">${money(p.trend)}</td>
              <td class="num">${money(p.low)}</td>
              <td class="num hide-sm">${money(p.avg)}</td>
              <td class="num hide-sm">${pct(pctVal(p.trend, p.avg))}</td>
              <td class="num hide-md muted">${p.date_added ? p.date_added.slice(0, 7) : ""}</td>
            </tr>`).join("") || `<tr><td colspan="7" class="empty-row">No products match.</td></tr>`}</tbody>
        </table>
      </div>
    </div>`;
  $$("th.sortable").forEach((t) => t.addEventListener("click", () => {
    const k = t.dataset.sort;
    if (state.sealedSort === k) state.sealedDir = state.sealedDir === "asc" ? "desc" : "asc";
    else { state.sealedSort = k; state.sealedDir = k === "name" ? "asc" : "desc"; }
    drawSealed();
  }));
  $$(".chip[data-type]").forEach((b) => b.addEventListener("click", () => { state.sealedType = b.dataset.type; drawSealed(); }));
  const fi = $("#sealed-filter");
  fi.addEventListener("input", () => { state.sealedFilter = fi.value; const pos = fi.selectionStart; drawSealed(); const n = $("#sealed-filter"); n.focus(); n.setSelectionRange(pos, pos); });
  $$("tbody tr[data-id]").forEach((tr) => {
    const go = () => (location.hash = `#/sealed/${tr.dataset.id}`);
    tr.addEventListener("click", go);
    tr.addEventListener("keydown", (e) => e.key === "Enter" && go());
  });
}

async function renderSealedDetail(id) {
  content.innerHTML = `<div class="page">${skeleton(4)}</div>`;
  try {
    const { product: p } = await api(`/sealed/${encodeURIComponent(id)}`);
    content.innerHTML = `
      <div class="page">
        <a class="back" href="#/sealed">← Sealed products</a>
        <div class="page-head"><div><div class="eyebrow">${esc(p.product_type)}${p.set_name ? ` · ${esc(p.set_name)}` : ""}</div><h1>${esc(p.name)}</h1></div></div>
        <div class="price-cards wide">
          <div class="pcard"><div class="p-label">Lowest listing</div><div class="p-val eur">${money(p.trend)}</div><div class="p-foot">${pct(pctVal(p.trend, p.avg))} vs avg sell</div></div>
          <div class="pcard"><div class="p-label">From</div><div class="p-val">${money(p.low)}</div><div class="p-foot">lowest EU listing</div></div>
          <div class="pcard"><div class="p-label">Avg sell price</div><div class="p-val">${money(p.avg)}</div><div class="p-foot">Cardmarket average</div></div>
        </div>
        <div class="links"><a class="btn" href="${esc(p.cm_url)}" target="_blank" rel="noopener">View on Cardmarket ↗</a></div>
        <section class="panel">
          <div class="panel-head"><h2>Price trend</h2>
            <div class="seg" id="range">${["30d", "3m", "6m", "1y", "all"].map((r) => `<button data-range="${r}" class="${state.range === r ? "active" : ""}">${r.toUpperCase()}</button>`).join("")}</div></div>
          <div class="chart-box"><canvas id="chart"></canvas></div>
          <div class="chart-note" id="chart-note"></div>
        </section>
      </div>`;
    const ser = [["trend", "Lowest (EUR)", "#d9ae5c"], ["low", "From (EUR)", "#8a7a55"]];
    $$("#range button").forEach((b) => b.addEventListener("click", () => {
      state.range = b.dataset.range;
      $$("#range button").forEach((x) => x.classList.toggle("active", x === b));
      drawHistory(`/sealed/${id}/history`, ser, [["Lowest listing", p.low], ["Avg sell price", p.avg], ["Trend now", p.trend]]);
    }));
    drawHistory(`/sealed/${id}/history`, ser, [["Lowest listing", p.low], ["Avg sell price", p.avg], ["Trend now", p.trend]]);
  } catch (e) { content.innerHTML = errorBox(e); }
}

// ------------------------------------------------------------------ global search
let timer;
function initSearch() {
  const input = $("#search"), box = $("#search-results");
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const v = input.value.trim();
    if (v.length < 2) { box.hidden = true; return; }
    timer = setTimeout(async () => {
      try {
        const { results } = await api(`/search?q=${encodeURIComponent(v)}`);
        box.innerHTML = results.length ? results.map((c) => `
          <li><a href="#/card/${c.id}">${thumb(c, "sthumb")}<span class="s-main"><span class="s-name">${esc(c.full_name)}</span><span class="s-sub">${esc(c.set_name)} · #${esc(c.card_number)} · ${esc(rl(c.rarity))}</span></span><span class="s-price">${money(c.cm_trend)}</span></a></li>`).join("")
          : `<li class="none">No cards found</li>`;
        box.hidden = false;
      } catch { box.hidden = true; }
    }, 180);
  });
  box.addEventListener("click", (e) => { if (e.target.closest("a")) { box.hidden = true; input.value = ""; } });
  document.addEventListener("click", (e) => { if (!e.target.closest(".search-wrap")) box.hidden = true; });
  input.addEventListener("keydown", (e) => { if (e.key === "Escape") { box.hidden = true; input.blur(); } });
}

initSearch();
loadStatus();
setInterval(loadStatus, 120000);
route();
