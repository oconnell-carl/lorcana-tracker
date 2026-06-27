/* Lorcana Price Tracker frontend */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  sets: [],
  currentSetId: null,
  currentRange: "30d",
  chart: null,
};

const SOURCES = [
  { key: "cardmarket", label: "Cardmarket (EUR)", color: "#4ade80", currency: "EUR" },
  { key: "tcgplayer", label: "TCGPlayer (USD)", color: "#60a5fa", currency: "USD" },
  { key: "psa10", label: "PSA 10", color: "#fbbf24", currency: "—" },
];

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
  } catch (e) {
    list.innerHTML = `<p class="muted">Failed to load sets: ${escapeHtml(e.message)}</p>`;
  }
}

async function selectSet(id, el) {
  state.currentSetId = id;
  $$(".set-item").forEach((x) => x.classList.remove("active"));
  if (el) el.classList.add("active");
  const content = $("#content");
  content.innerHTML = `<div class="muted">Loading cards…</div>`;
  try {
    const data = await api(`/api/sets/${id}/cards`);
    renderCardTable(data.set, data.cards);
  } catch (e) {
    content.innerHTML = `<p class="muted">Error: ${escapeHtml(e.message)}</p>`;
  }
}

function renderCardTable(set, cards) {
  const content = $("#content");
  const count = cards.length;
  let rows = "";
  if (!count) {
    content.innerHTML = `
      <div class="section-head"><h2>${escapeHtml(set.name)}</h2><span class="count">0 cards</span></div>
      <p class="muted">No cards cached for this set. Run <code>python -m src.snapshot --cards</code> to populate.</p>`;
    return;
  }
  for (const c of cards) {
    const cm = c.prices?.cardmarket;
    const tp = c.prices?.tcgplayer;
    const psa = c.prices?.psa10;
    rows += `
      <tr data-card-id="${c.id}">
        <td><strong>${escapeHtml(c.name)}</strong></td>
        <td class="num">${escapeHtml(c.card_number || "")}</td>
        <td>${c.rarity ? `<span class="rarity">${escapeHtml(c.rarity)}</span>` : `<span class="na">—</span>`}</td>
        <td class="price-eur">${fmtPrice(cm?.price, cm?.currency || "EUR")}</td>
        <td class="price-usd">${fmtPrice(tp?.price, tp?.currency || "USD")}</td>
        <td class="price-psa">${fmtPrice(psa?.price, psa?.currency || "USD")}</td>
      </tr>`;
  }
  content.innerHTML = `
    <div class="section-head"><h2>${escapeHtml(set.name)}</h2><span class="count">${count} cards</span></div>
    <div class="card-table-wrap">
      <table class="cards">
        <thead><tr>
          <th>Name</th><th>#</th><th>Rarity</th>
          <th>Cardmarket (EUR)</th><th>TCGPlayer (USD)</th><th>PSA 10</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  $$("#content tbody tr").forEach((tr) =>
    tr.addEventListener("click", () => openCard(tr.dataset.cardId))
  );
}

// ------------------------------ Card detail ------------------------------ //
async function openCard(cardId) {
  const modal = $("#card-modal");
  const body = $("#modal-body");
  modal.hidden = false;
  body.innerHTML = `<div class="muted">Loading…</div>`;
  try {
    const data = await api(`/api/cards/${cardId}`);
    await renderCardDetail(data.card, body);
  } catch (e) {
    body.innerHTML = `<p class="muted">Error: ${escapeHtml(e.message)}</p>`;
  }
}

async function renderCardDetail(card, body) {
  const cm = card.prices?.cardmarket;
  const tp = card.prices?.tcgplayer;
  const psa = card.prices?.psa10;
  const img = card.image_url
    ? `<img class="detail-img" src="${escapeHtml(card.image_url)}" alt="${escapeHtml(card.name)}" onerror="this.classList.add('empty');this.alt='No image';" />`
    : `<div class="detail-img empty">No image</div>`;

  body.innerHTML = `
    <div class="detail-head">
      ${img}
      <div class="detail-info">
        <h2>${escapeHtml(card.name)}</h2>
        <div class="sub">
          ${escapeHtml(card.set_name || "—")} · #${escapeHtml(card.card_number || "—")}
          ${card.rarity ? ` · <span class="rarity">${escapeHtml(card.rarity)}</span>` : ""}
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
    <div class="range-toggle" id="range-toggle">
      <button data-range="30d" class="active">30D</button>
      <button data-range="3m">3M</button>
      <button data-range="6m">6M</button>
      <button data-range="1y">1Y</button>
      <button data-range="all">All</button>
    </div>
    <div class="chart-wrap"><canvas id="price-chart"></canvas></div>
    <div class="legend" id="chart-legend"></div>
  `;

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
  const wrap = canvas.parentElement;
  try {
    const data = await api(`/api/cards/${cardId}/history?range=${state.currentRange}`);
    const series = data.series || {};
    const sources = SOURCES.filter((s) => series[s.key] && series[s.key].length);
    if (!sources.length) {
      wrap.innerHTML = `<div class="chart-empty">No historical data yet. Daily snapshots build the trend over time.</div>`;
      legendEl.innerHTML = "";
      return;
    }
    // Use the union of dates.
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
            openCard(li.dataset.id);
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

// --------------------------------- Modal --------------------------------- //
function initModal() {
  $("#modal-close").addEventListener("click", () => {
    $("#card-modal").hidden = true;
    if (state.chart) { state.chart.destroy(); state.chart = null; }
  });
  $("#card-modal").addEventListener("click", (e) => {
    if (e.target.id === "card-modal") {
      $("#card-modal").hidden = true;
      if (state.chart) { state.chart.destroy(); state.chart = null; }
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") $("#card-modal").hidden = true;
  });
}

// ---------------------------------- Boot --------------------------------- //
window.addEventListener("DOMContentLoaded", () => {
  loadStatus();
  loadSets();
  initSearch();
  initModal();
});
