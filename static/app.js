/* Lorcana Price Tracker frontend */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  sets: [],
  currentSetId: null,
  currentCardId: null,
  currentRange: "30d",
  chart: null,
  sortBy: "name",
  sortDir: "asc",
  rarityFilter: "all",
};

const SOURCES = [
  { key: "cardmarket", label: "Cardmarket (EUR)", color: "#4ade80", currency: "EUR" },
  { key: "tcgplayer", label: "TCGPlayer (USD)", color: "#60a5fa", currency: "USD" },
  { key: "psa10", label: "PSA 10", color: "#fbbf24", currency: "—" },
];

const RARITY_ORDER = {
  "Iconic": 0, "Enchanted": 1, "Epic": 2, "Legendary": 3,
  "Super_rare": 4, "rare": 5, "Uncommon": 6, "Common": 7,
  "Promo": 8, "Oversized": 9,
};

const RARITY_LABELS = {
  "Iconic": "Iconic", "Enchanted": "Enchanted", "Epic": "Epic",
  "Legendary": "Legendary", "Super_rare": "Super Rare", "rare": "Rare",
  "Uncommon": "Uncommon", "Common": "Common", "Promo": "Promo", "Oversized": "Oversized",
};

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

function fmtPrice(val, cur) {
  if (val == null || val === "") return `<span class="na">—</span>`;
  const num = Number(val);
  if (!isFinite(num)) return `<span class="na">—</span>`;
  const sym = cur === "USD" ? "$" : cur === "EUR" ? "€" : "";
  return `${sym}${num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function rarityLabel(r) {
  return RARITY_LABELS[r] || r || "—";
}

// --------------------------------- Status -------------------------------- //
async function loadStatus() {
  try {
    const s = await api("/api/status");
    const pill = $("#status-pill");
    if (s.api_available) {
      pill.textContent = `● Live API · ${s.set_count} sets · ${s.card_count} cards`;
      pill.classList.add("online");
      pill.classList.remove("offline");
    } else {
      pill.textContent = `○ Cached · ${s.set_count} sets · ${s.card_count} cards`;
      pill.classList.add("offline");
      pill.classList.remove("online");
    }
  } catch {
    $("#status-pill").textContent = "○ Offline";
  }
}

// ---------------------------------- Sets --------------------------------- //
async function loadSets() {
  const list = $("#set-list");
  try {
    const data = await api("/api/sets");
    state.sets = data.sets || [];
    if (!state.sets.length) {
      list.innerHTML = `<p class="muted">No sets yet. Run the snapshot job to populate.</p>`;
      return;
    }
    list.innerHTML = "";
    state.sets.forEach((s) => {
      const el = document.createElement("div");
      el.className = "set-item";
      el.dataset.id = s.id;
      const meta = [s.code, s.release_date, `${s.card_count || 0} cards`].filter(Boolean).join(" · ");
      el.innerHTML = `<div class="s-name">${escapeHtml(s.name)}</div><div class="s-meta">${escapeHtml(meta)}</div>`;
      el.addEventListener("click", () => selectSet(s.id, el));
      list.appendChild(el);
    });
    // Also render landing page set grid
    renderSetGrid();
  } catch (e) {
    list.innerHTML = `<p class="muted">Failed to load sets: ${escapeHtml(e.message)}</p>`;
  }
}

function renderSetGrid() {
  const content = $("#content");
  if (state.currentSetId || state.currentCardId) return; // Only show on landing
  const sets = state.sets;
  if (!sets.length) return;

  let cards = "";
  for (const s of sets) {
    const logo = s.logo
      ? `<img class="set-logo" src="${escapeHtml(s.logo)}" alt="${escapeHtml(s.name)}" onerror="this.style.display='none';this.nextElementSibling.style.display='flex';" /><div class="set-logo-placeholder" style="display:none">${escapeHtml(s.name.charAt(0))}</div>`
      : `<div class="set-logo-placeholder">${escapeHtml(s.name.charAt(0))}</div>`;
    const meta = [s.code, s.release_date, `${s.card_count || 0} cards`].filter(Boolean).join(" · ");
    cards += `
      <div class="set-card" data-set-id="${s.id}">
        ${logo}
        <div class="set-card-body">
          <div class="set-card-name">${escapeHtml(s.name)}</div>
          <div class="set-card-meta">${escapeHtml(meta)}</div>
        </div>
      </div>`;
  }

  content.innerHTML = `
    <div class="landing">
      <h2 class="landing-title">Disney Lorcana Sets</h2>
      <p class="landing-sub">Select a set to browse cards and prices</p>
      <div class="set-grid">${cards}</div>
    </div>`;

  $$('#content .set-card').forEach((el) =>
    el.addEventListener("click", () => {
      const setId = el.dataset.setId;
      const sidebarEl = $(`.set-item[data-id="${setId}"]`);
      selectSet(Number(setId), sidebarEl);
    })
  );
}

async function selectSet(id, el) {
  state.currentSetId = id;
  $$(".set-item").forEach((x) => x.classList.remove("active"));
  if (el) el.classList.add("active");
  await renderSetView(id);
}

async function renderSetView(setId) {
  const content = $("#content");
  content.innerHTML = `<div class="muted">Loading cards…</div>`;
  try {
    const data = await api(`/api/sets/${setId}/cards`);
    const set = data.set;
    let cards = data.cards || [];

    // Store original prices for sorting
    cards = cards.map(c => ({
      ...c,
      _cmPrice: c.prices?.cardmarket?.price ?? null,
      _tcgPrice: c.prices?.tcgplayer?.price ?? null,
      _psaPrice: c.prices?.psa10?.price ?? null,
      _avg7d: c.prices?.cardmarket?.avg_7d ?? null,
      _avg30d: c.prices?.cardmarket?.avg_30d ?? null,
      _availItems: c.prices?.cardmarket?.available_items ?? null,
    }));

    state._setView = { set, cards };
    renderCardTable(set, cards);
  } catch (e) {
    content.innerHTML = `<p class="muted">Error: ${escapeHtml(e.message)}</p>`;
  }
}

function pctDiff(current, average) {
  if (current == null || average == null || average === 0) return null;
  return ((current - average) / average) * 100;
}

function fmtPct(val) {
  if (val == null || !isFinite(val)) return `<span class="na">—</span>`;
  const sign = val >= 0 ? "+" : "";
  const cls = val > 5 ? "pct-up" : val < -5 ? "pct-down" : "pct-flat";
  return `<span class="${cls}">${sign}${val.toFixed(1)}%</span>`;
}

function getFilteredSortedCards() {
  const { cards } = state._setView;
  let filtered = cards;
  if (state.rarityFilter !== "all") {
    filtered = filtered.filter(c => c.rarity === state.rarityFilter);
  }
  const sorted = [...filtered].sort((a, b) => {
    let cmp = 0;
    switch (state.sortBy) {
      case "name":
        cmp = a.name.localeCompare(b.name);
        break;
      case "rarity":
        cmp = (RARITY_ORDER[a.rarity] ?? 99) - (RARITY_ORDER[b.rarity] ?? 99);
        break;
      case "cardmarket":
        cmp = (a._cmPrice ?? Infinity) - (b._cmPrice ?? Infinity);
        break;
      case "tcgplayer":
        cmp = (a._tcgPrice ?? Infinity) - (b._tcgPrice ?? Infinity);
        break;
      case "psa10":
        cmp = (a._psaPrice ?? Infinity) - (b._psaPrice ?? Infinity);
        break;
      case "avg7d":
        cmp = (a._avg7d ?? Infinity) - (b._avg7d ?? Infinity);
        break;
      case "avg30d":
        cmp = (a._avg30d ?? Infinity) - (b._avg30d ?? Infinity);
        break;
      case "supply":
        cmp = (a._availItems ?? -Infinity) - (b._availItems ?? -Infinity);
        break;
    }
    return state.sortDir === "desc" ? -cmp : cmp;
  });
  return sorted;
}

function renderCardTable(set, cards) {
  const content = $("#content");
  const filtered = getFilteredSortedCards();
  const count = filtered.length;

  // Rarity filter buttons
  const rarityCounts = {};
  cards.forEach(c => { rarityCounts[c.rarity] = (rarityCounts[c.rarity] || 0) + 1; });
  const rarityButtons = ["all", "Iconic", "Enchanted", "Epic", "Legendary", "Promo", "Super_rare", "rare", "Uncommon", "Common"]
    .filter(r => r === "all" || rarityCounts[r])
    .map(r => {
      const label = r === "all" ? "All" : rarityLabel(r);
      const active = state.rarityFilter === r ? "active" : "";
      const cnt = r === "all" ? cards.length : (rarityCounts[r] || 0);
      return `<button class="rarity-btn ${active}" data-rarity="${r}">${label} <span class="rb-count">${cnt}</span></button>`;
    }).join("");

  // Sort indicators
  const sortIcon = (col) => {
    if (state.sortBy !== col) return "↕";
    return state.sortDir === "asc" ? "↑" : "↓";
  };

  let rows = "";
  if (!count) {
    rows = `<tr><td colspan="12" class="na" style="text-align:center;padding:30px">No cards match this filter</td></tr>`;
  } else {
    for (const c of filtered) {
      const cm = c.prices?.cardmarket;
      const tp = c.prices?.tcgplayer;
      const psa = c.prices?.psa10;
      const vs7d = pctDiff(cm?.price, cm?.avg_7d);
      const vs30d = pctDiff(cm?.price, cm?.avg_30d);
      const supply = cm?.available_items;
      const thumb = c.image_url
        ? `<img class="card-thumb" src="${escapeHtml(c.image_url)}" alt="" loading="lazy" onerror="this.style.display='none';this.nextElementSibling.style.display='flex';" /><span class="card-thumb-placeholder" style="display:none">◇</span>`
        : `<span class="card-thumb-placeholder">◇</span>`;
      rows += `
        <tr data-card-id="${c.id}">
          <td class="thumb-cell">${thumb}</td>
          <td><strong>${escapeHtml(c.name)}</strong></td>
          <td class="num">${escapeHtml(c.card_number || "")}</td>
          <td>${c.rarity ? `<span class="rarity rarity-${c.rarity.toLowerCase()}">${escapeHtml(rarityLabel(c.rarity))}</span>` : `<span class="na">—</span>`}</td>
          <td class="price-eur">${fmtPrice(cm?.price, cm?.currency || "EUR")}</td>
          <td class="price-avg">${fmtPrice(cm?.avg_7d, cm?.currency || "EUR")}</td>
          <td class="price-avg">${fmtPrice(cm?.avg_30d, cm?.currency || "EUR")}</td>
          <td class="pct-cell">${fmtPct(vs7d)}</td>
          <td class="pct-cell">${fmtPct(vs30d)}</td>
          <td class="price-usd">${fmtPrice(tp?.price, tp?.currency || "USD")}</td>
          <td class="price-psa">${fmtPrice(psa?.price, psa?.currency || "USD")}</td>
          <td class="num supply-cell" title="Number of listings on Cardmarket">${supply != null ? supply : `<span class="na">—</span>`}</td>
        </tr>`;
    }
  }

  content.innerHTML = `
    <div class="section-head"><h2>${escapeHtml(set.name)}</h2><span class="count">${count} cards</span></div>
    <div class="rarity-filters">${rarityButtons}</div>
    <div class="card-table-wrap">
      <table class="cards">
        <thead><tr>
          <th class="thumb-col"></th>
          <th class="sortable" data-sort="name">Name ${sortIcon("name")}</th>
          <th>#</th>
          <th class="sortable" data-sort="rarity">Rarity ${sortIcon("rarity")}</th>
          <th class="sortable" data-sort="cardmarket">CM Price ${sortIcon("cardmarket")}</th>
          <th class="sortable" data-sort="avg7d">7D Avg ${sortIcon("avg7d")}</th>
          <th class="sortable" data-sort="avg30d">30D Avg ${sortIcon("avg30d")}</th>
          <th>% vs 7D</th>
          <th>% vs 30D</th>
          <th class="sortable" data-sort="tcgplayer">TCGPlayer ${sortIcon("tcgplayer")}</th>
          <th class="sortable" data-sort="psa10">PSA 10 ${sortIcon("psa10")}</th>
          <th class="sortable" data-sort="supply">Supply ${sortIcon("supply")}</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;

  // Wire up row clicks
  $$("#content tbody tr[data-card-id]").forEach((tr) =>
    tr.addEventListener("click", () => openCardPage(tr.dataset.cardId))
  );

  // Wire up sortable headers
  $$("#content th.sortable").forEach((th) =>
    th.addEventListener("click", () => {
      const col = th.dataset.sort;
      if (state.sortBy === col) {
        state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
      } else {
        state.sortBy = col;
        state.sortDir = col === "name" ? "asc" : "desc";
      }
      renderCardTable(state._setView.set, state._setView.cards);
    })
  );

  // Wire up rarity filter buttons
  $$("#content .rarity-btn").forEach((btn) =>
    btn.addEventListener("click", () => {
      state.rarityFilter = btn.dataset.rarity;
      renderCardTable(state._setView.set, state._setView.cards);
    })
  );
}

// ------------------------------ Card detail page ------------------------------ //
async function openCardPage(cardId) {
  state.currentCardId = cardId;
  const content = $("#content");
  content.innerHTML = `<div class="muted">Loading card…</div>`;
  // Scroll to top
  window.scrollTo(0, 0);
  try {
    const data = await api(`/api/cards/${cardId}`);
    await renderCardPage(data.card);
  } catch (e) {
    content.innerHTML = `<p class="muted">Error: ${escapeHtml(e.message)}</p>`;
  }
}

async function renderCardPage(card) {
  const content = $("#content");
  const cm = card.prices?.cardmarket;
  const tp = card.prices?.tcgplayer;
  const psa = card.prices?.psa10;
  const img = card.image_url
    ? `<img class="detail-img" src="${escapeHtml(card.image_url)}" alt="${escapeHtml(card.name)}" onerror="this.style.display='none';this.nextElementSibling.style.display='flex';" /><div class="detail-img empty" style="display:none">No image available</div>`
    : `<div class="detail-img empty">No image</div>`;

  const setName = card.set_name || state.sets.find(s => s.id === card.set_id)?.name || "—";

  content.innerHTML = `
    <div class="card-detail-page">
      <button class="back-btn" id="back-btn">← Back to ${escapeHtml(setName)}</button>
      <div class="detail-head">
        ${img}
        <div class="detail-info">
          <h2>${escapeHtml(card.name)}</h2>
          <div class="sub">
            ${escapeHtml(setName)} · #${escapeHtml(card.card_number || "—")}
            ${card.rarity ? ` · <span class="rarity rarity-${card.rarity.toLowerCase()}">${escapeHtml(rarityLabel(card.rarity))}</span>` : ""}
          </div>
          <div class="price-grid">
            <div class="price-card">
              <div class="pc-label">Cardmarket</div>
              <div class="pc-value eur">${fmtPrice(cm?.price, cm?.currency || "EUR")}</div>
              <div class="pc-foot">${cm?.date ? `as of ${cm.date}` : "EUR · lowest NM"}</div>
            </div>
            <div class="price-card">
              <div class="pc-label">TCGPlayer</div>
              <div class="pc-value usd">${fmtPrice(tp?.price, tp?.currency || "USD")}</div>
              <div class="pc-foot">${tp?.date ? `as of ${tp.date}` : "USD · market"}</div>
            </div>
            <div class="price-card">
              <div class="pc-label">PSA 10</div>
              <div class="pc-value psa">${fmtPrice(psa?.price, psa?.currency || "USD")}</div>
              <div class="pc-foot">${psa?.date ? `as of ${psa.date}` : "graded"}</div>
            </div>
          </div>
        </div>
      </div>

      <h3 class="chart-title">Price Trend</h3>
      <div class="range-toggle" id="range-toggle">
        <button data-range="30d" class="active">30D</button>
        <button data-range="3m">3M</button>
        <button data-range="6m">6M</button>
        <button data-range="1y">1Y</button>
        <button data-range="all">All</button>
      </div>
      <div class="chart-wrap"><canvas id="price-chart"></canvas></div>
      <div class="legend" id="chart-legend"></div>

      <h3 class="chart-title">Price Details</h3>
      <div class="card-table-wrap">
        <table class="cards detail-prices">
          <thead><tr>
            <th>Source</th><th>Price</th><th>Currency</th><th>As of</th>
          </tr></thead>
          <tbody>
            ${cm ? `<tr><td>Cardmarket (lowest NM, English)</td><td class="price-eur">${fmtPrice(cm?.price, cm?.currency || "EUR")}</td><td>${escapeHtml(cm?.currency || "EUR")}</td><td>${escapeHtml(cm?.date || "—")}</td></tr>` : ""}
            ${tp ? `<tr><td>TCGPlayer (market)</td><td class="price-usd">${fmtPrice(tp?.price, tp?.currency || "USD")}</td><td>${escapeHtml(tp?.currency || "USD")}</td><td>${escapeHtml(tp?.date || "—")}</td></tr>` : ""}
            ${psa ? `<tr><td>PSA 10 (graded)</td><td class="price-psa">${fmtPrice(psa?.price, psa?.currency || "USD")}</td><td>${escapeHtml(psa?.currency || "—")}</td><td>${escapeHtml(psa?.date || "—")}</td></tr>` : ""}
            ${!cm && !tp && !psa ? `<tr><td colspan="4" class="na" style="text-align:center;padding:20px">No price data yet</td></tr>` : ""}
          </tbody>
        </table>
      </div>
    </div>
  `;

  // Back button
  $("#back-btn").addEventListener("click", () => {
    if (state.currentSetId) renderSetView(state.currentSetId);
  });

  // Range toggle
  $$("#range-toggle button").forEach((b) =>
    b.addEventListener("click", () => {
      $$("#range-toggle button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.currentRange = b.dataset.range;
      loadChart(card.id);
    })
  );

  await loadChart(card.id);
}

async function loadChart(cardId) {
  const canvas = $("#price-chart");
  const legendEl = $("#chart-legend");
  const wrap = canvas?.parentElement;
  if (!canvas || !wrap) return;

  try {
    const data = await api(`/api/cards/${cardId}/history?range=${state.currentRange}`);
    const series = data.series || {};
    const sources = SOURCES.filter((s) => series[s.key] && series[s.key].length);
    if (!sources.length) {
      wrap.innerHTML = `<div class="chart-empty">No historical data yet. Daily snapshots build the trend over time.</div>`;
      legendEl.innerHTML = "";
      return;
    }
    const dateSet = new Set();
    sources.forEach((s) => series[s.key].forEach((p) => dateSet.add(p.date)));
    const labels = Array.from(dateSet).sort();
    const datasets = sources.map((s) => {
      const map = Object.fromEntries(series[s.key].map((p) => [p.date, p.price]));
      return {
        label: s.label,
        data: labels.map((d) => (d in map ? map[d] : null)),
        borderColor: s.color,
        backgroundColor: s.color + "22",
        tension: 0.3,
        spanGaps: true,
        pointRadius: 2,
        borderWidth: 2,
      };
    });
    if (state.chart) state.chart.destroy();
    state.chart = new Chart(canvas, {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "#1a2030",
            borderColor: "#2e3750",
            borderWidth: 1,
            callbacks: {
              label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y ?? "—"}`,
            },
          },
        },
        scales: {
          x: { ticks: { color: "#5c6680", maxRotation: 0, autoSkip: true }, grid: { color: "rgba(255,255,255,0.04)" } },
          y: { ticks: { color: "#5c6680" }, grid: { color: "rgba(255,255,255,0.06)" } },
        },
      },
    });
    legendEl.innerHTML = sources
      .map((s) => `<span><span class="dot" style="background:${s.color}"></span>${s.label} (${series[s.key].length} pts)</span>`)
      .join("");
  } catch (e) {
    wrap.innerHTML = `<div class="chart-empty">Error loading history: ${escapeHtml(e.message)}</div>`;
  }
}

// --------------------------------- Search -------------------------------- //
let searchTimer;
function initSearch() {
  const input = $("#search");
  const results = $("#search-results");
  input.addEventListener("input", () => {
    clearTimeout(searchTimer);
    const q = input.value.trim();
    if (q.length < 2) {
      results.hidden = true;
      return;
    }
    searchTimer = setTimeout(async () => {
      try {
        const data = await api(`/api/search?q=${encodeURIComponent(q)}`);
        const items = data.results || [];
        if (!items.length) {
          results.innerHTML = `<li class="muted">No matches</li>`;
          results.hidden = false;
          return;
        }
        results.innerHTML = items
          .map(
            (c) =>
              `<li data-id="${c.id}"><span class="r-name">${escapeHtml(c.name)}</span><span class="r-set">${escapeHtml(c.set_name || "")} · #${escapeHtml(c.card_number || "")}</span></li>`
          )
          .join("");
        results.hidden = false;
        $$("#search-results li").forEach((li) =>
          li.addEventListener("click", () => {
            openCardPage(li.dataset.id);
            results.hidden = true;
            input.value = "";
          })
        );
      } catch {
        results.hidden = true;
      }
    }, 200);
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".search-wrap")) results.hidden = true;
  });
}

// ---------------------------------- Boot --------------------------------- //
window.addEventListener("DOMContentLoaded", () => {
  loadStatus();
  loadSets();
  initSearch();
});
