"""Daily Lorcana market report: biggest movers in lowest listed English NM price.

Compares the two most recent cardmarket snapshot dates (day-on-day) and the
latest snapshot vs ~7 days ago (week-on-week) for premium rarities only:
Iconic, Enchanted, Epic, Promo, Legendary.

Outputs a Telegram-friendly HTML report to stdout (or --output file).

Usage:
    python -m src.market_report
    python -m src.market_report --output /tmp/report.html
    python -m src.market_report --date 2026-08-09   # report as-of a given date
"""

import argparse
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "prices.db"

TRACKED_RARITIES = ("Iconic", "Enchanted", "Epic", "Promo", "Legendary")

# Sealed product types Carl cares about (English-language retail pieces)
SEALED_TYPES = (
    "Booster Box", "Illumineer's Trove", "Collector's Set",
    "Collection Starter Set", "Starter Deck", "2 Starter Deck Set",
    "3 Starter Deck Set", "Gift Set", "Gift Box",
)

# Noise filters
MIN_PRICE = 2.0          # ignore cards currently under €2
MAX_PRICE = 10_000.0     # above this is a placeholder/junk listing, not a market price
MIN_ABS_CHANGE = 0.50    # need at least €0.50 move
MIN_PCT_CHANGE = 3.0     # ...and at least 3% move
TOP_N_DAY = 10
TOP_N_WEEK = 5
TOP_N_LOWS = 8


def get_conn():
    return sqlite3.connect(str(DB_PATH))


def snapshot_dates(conn, source="cardmarket"):
    rows = conn.execute(
        "SELECT DISTINCT snapshot_date FROM price_snapshots WHERE source=? "
        "ORDER BY snapshot_date DESC", (source,)
    ).fetchall()
    return [r[0] for r in rows]


def prices_for_date(conn, date, source="cardmarket"):
    """Return {card_id: (price, available_items)} for tracked rarities on a date."""
    q = """
        SELECT s.card_id, s.price, s.available_items
        FROM price_snapshots s
        JOIN cards c ON c.id = s.card_id
        WHERE s.source = ? AND s.snapshot_date = ?
          AND c.rarity IN ({})
    """.format(",".join("?" * len(TRACKED_RARITIES)))
    out = {}
    for cid, price, avail in conn.execute(q, (source, date, *TRACKED_RARITIES)):
        if price is not None and price <= MAX_PRICE:
            out[cid] = (price, avail)
    return out


def avg_prices_window(conn, start_date, end_date, source="cardmarket"):
    """Return {card_id: (avg_price, n_days)} averaged across snapshots in [start, end]."""
    q = """
        SELECT s.card_id, AVG(s.price), COUNT(DISTINCT s.snapshot_date)
        FROM price_snapshots s
        JOIN cards c ON c.id = s.card_id
        WHERE s.source = ? AND s.snapshot_date BETWEEN ? AND ?
          AND s.price IS NOT NULL AND s.price > 0 AND s.price <= ?
          AND c.rarity IN ({})
        GROUP BY s.card_id
    """.format(",".join("?" * len(TRACKED_RARITIES)))
    return {cid: (avg, n) for cid, avg, n in conn.execute(
        q, (source, start_date, end_date, MAX_PRICE, *TRACKED_RARITIES))}


def window_movers(cur_avg, prev_avg, meta, min_days=2):
    """Movers based on windowed average prices (7d avg vs prior 7d avg).

    Requires >= min_days of data in each window to avoid single-day artifacts.
    """
    rows = []
    for cid, (avg, n) in cur_avg.items():
        if cid not in prev_avg or avg < MIN_PRICE or n < min_days:
            continue
        old_avg, old_n = prev_avg[cid]
        if old_n < min_days or not old_avg or old_avg <= 0:
            continue
        chg = avg - old_avg
        pct = (chg / old_avg) * 100.0
        if abs(chg) >= MIN_ABS_CHANGE and abs(pct) >= MIN_PCT_CHANGE:
            m = meta.get(cid, {})
            rows.append({"name": m.get("name", "?"), "subtitle": m.get("subtitle", ""),
                         "rarity": m.get("rarity", "?"), "set": m.get("set", ""),
                         "old": old_avg, "new": avg, "chg": chg, "pct": pct})
    gainers = sorted((r for r in rows if r["chg"] > 0), key=lambda r: -r["pct"])
    losers = sorted((r for r in rows if r["chg"] < 0), key=lambda r: r["pct"])
    return gainers, losers


def sealed_for_date(conn, date):
    """Return {product_id: price} for tracked sealed types on a date."""
    q = """
        SELECT s.product_id, s.price
        FROM sealed_price_snapshots s
        JOIN sealed_products p ON p.id = s.product_id
        WHERE s.snapshot_date = ? AND s.price IS NOT NULL AND s.price > 0
          AND p.product_type IN ({})
    """.format(",".join("?" * len(SEALED_TYPES)))
    return {pid: price for pid, price in conn.execute(q, (date, *SEALED_TYPES))
            if price <= MAX_PRICE}


def sealed_avg_window(conn, start_date, end_date):
    """Return {product_id: (avg_price, n_days)} for tracked sealed types."""
    q = """
        SELECT s.product_id, AVG(s.price), COUNT(DISTINCT s.snapshot_date)
        FROM sealed_price_snapshots s
        JOIN sealed_products p ON p.id = s.product_id
        WHERE s.snapshot_date BETWEEN ? AND ?
          AND s.price IS NOT NULL AND s.price > 0 AND s.price <= ?
          AND p.product_type IN ({})
        GROUP BY s.product_id
    """.format(",".join("?" * len(SEALED_TYPES)))
    return {pid: (avg, n) for pid, avg, n in conn.execute(
        q, (start_date, end_date, MAX_PRICE, *SEALED_TYPES))}


def sealed_meta(conn):
    q = "SELECT id, name, product_type FROM sealed_products"
    return {r[0]: {"name": r[1], "type": r[2] or ""} for r in conn.execute(q)}


def sealed_movers(cur, prev, meta, min_days=2):
    """Sealed movers from windowed averages {pid: (avg, n_days)}."""
    rows = []
    for pid, (avg, n) in cur.items():
        if pid not in prev or n < min_days:
            continue
        old_avg, old_n = prev[pid]
        if old_n < min_days or not old_avg or old_avg <= 0:
            continue
        chg = avg - old_avg
        pct = (chg / old_avg) * 100.0
        if abs(chg) >= 1.0 and abs(pct) >= MIN_PCT_CHANGE:
            m = meta.get(pid, {})
            rows.append({"name": m.get("name", "?"), "type": m.get("type", ""),
                         "old": old_avg, "new": avg, "chg": chg, "pct": pct})
    gainers = sorted((r for r in rows if r["chg"] > 0), key=lambda r: -r["pct"])
    losers = sorted((r for r in rows if r["chg"] < 0), key=lambda r: r["pct"])
    return gainers, losers


def fmt_sealed_mover(r):
    arrow = "🟢" if r["chg"] > 0 else "🔴"
    return (f"{arrow} <b>{r['name']}</b>\n"
            f"   €{r['old']:.2f} → €{r['new']:.2f}  ({r['chg']:+.2f}, {r['pct']:+.1f}%)")


def card_meta(conn):
    q = """
        SELECT c.id, c.name, c.subtitle, c.rarity, s.name AS set_name
        FROM cards c LEFT JOIN sets s ON s.id = c.set_id
    """
    return {r[0]: {"name": r[1], "subtitle": r[2] or "", "rarity": r[3],
                   "set": r[4] or ""} for r in conn.execute(q)}


def nearest_date(dates, target, max_gap_days=3):
    """Pick the available snapshot date closest to target (<= max_gap_days away)."""
    best, best_gap = None, max_gap_days + 1
    t = datetime.strptime(target, "%Y-%m-%d").date()
    for d in dates:
        gap = abs((datetime.strptime(d, "%Y-%m-%d").date() - t).days)
        if gap < best_gap:
            best, best_gap = d, gap
    return best


def movers(latest, previous, meta):
    """Return (gainers, losers) sorted by pct change."""
    rows = []
    for cid, (price, avail) in latest.items():
        if cid not in previous or price < MIN_PRICE:
            continue
        old = previous[cid][0]
        if not old or old <= 0:
            continue
        chg = price - old
        pct = (chg / old) * 100.0
        if abs(chg) >= MIN_ABS_CHANGE and abs(pct) >= MIN_PCT_CHANGE:
            m = meta.get(cid, {})
            rows.append({"name": m.get("name", "?"), "subtitle": m.get("subtitle", ""),
                         "rarity": m.get("rarity", "?"), "set": m.get("set", ""),
                         "old": old, "new": price, "chg": chg, "pct": pct})
    gainers = sorted((r for r in rows if r["chg"] > 0), key=lambda r: -r["pct"])
    losers = sorted((r for r in rows if r["chg"] < 0), key=lambda r: r["pct"])
    return gainers, losers


def all_time_lows(conn, latest_date, meta):
    """Cards at their lowest recorded cardmarket price (min 3 snapshots, price >= MIN_PRICE)."""
    q = """
        SELECT s.card_id, s.price,
               (SELECT MIN(p2.price) FROM price_snapshots p2
                WHERE p2.card_id = s.card_id AND p2.source = 'cardmarket'
                  AND p2.price IS NOT NULL AND p2.price > 0) AS min_price,
               (SELECT COUNT(*) FROM price_snapshots p3
                WHERE p3.card_id = s.card_id AND p3.source = 'cardmarket') AS snaps
        FROM price_snapshots s
        JOIN cards c ON c.id = s.card_id
        WHERE s.source = 'cardmarket' AND s.snapshot_date = ?
          AND c.rarity IN ({})
    """.format(",".join("?" * len(TRACKED_RARITIES)))
    out = []
    for cid, price, min_price, snaps in conn.execute(q, (latest_date, *TRACKED_RARITIES)):
        if price and min_price and price <= min_price and snaps >= 10 \
                and MIN_PRICE <= price <= MAX_PRICE:
            m = meta.get(cid, {})
            out.append({"name": m.get("name", "?"), "subtitle": m.get("subtitle", ""),
                        "rarity": m.get("rarity", "?"), "set": m.get("set", ""),
                        "price": price})
    return sorted(out, key=lambda r: -r["price"])


def supply_shifts(latest, previous, meta, min_drop=5):
    """Big available_items drops (potential buyouts)."""
    rows = []
    for cid, (price, avail) in latest.items():
        if cid not in previous or avail is None:
            continue
        old_avail = previous[cid][1]
        if old_avail is None or old_avail < min_drop:
            continue
        drop = old_avail - avail
        if drop >= min_drop and drop / old_avail >= 0.3:
            m = meta.get(cid, {})
            rows.append({"name": m.get("name", "?"), "subtitle": m.get("subtitle", ""),
                         "rarity": m.get("rarity", "?"), "price": price,
                         "old": old_avail, "new": avail, "drop": drop})
    return sorted(rows, key=lambda r: -r["drop"])[:5]


def fmt_card(r, show_set=True):
    label = r["name"]
    if r.get("subtitle"):
        label += f" — {r['subtitle']}"
    if show_set and r.get("set"):
        label += f" ({r['set']})"
    return label


def fmt_mover(r):
    arrow = "🟢" if r["chg"] > 0 else "🔴"
    return (f"{arrow} <b>{fmt_card(r)}</b> [{r['rarity']}]\n"
            f"   €{r['old']:.2f} → €{r['new']:.2f}  ({r['chg']:+.2f}, {r['pct']:+.1f}%)")


def market_overview(cur_avg, prev_avg, meta):
    """Per-rarity tracked value: current 7d avg total vs previous 7d avg total."""
    by_rarity = {}
    prev_by_rarity = {}
    for cid, (avg, _n) in cur_avg.items():
        r = meta.get(cid, {}).get("rarity", "?")
        by_rarity.setdefault(r, []).append(avg)
        if cid in prev_avg:
            prev_by_rarity.setdefault(r, []).append(prev_avg[cid][0])
    lines = []
    for r in TRACKED_RARITIES:
        vals = by_rarity.get(r)
        if not vals:
            continue
        total = sum(vals)
        prev_total = sum(prev_by_rarity.get(r, []))
        delta = total - prev_total if prev_total else 0.0
        pct = (delta / prev_total * 100.0) if prev_total else 0.0
        sign = "+" if delta >= 0 else ""
        lines.append(f"• {r}: {len(vals)} cards, €{total:,.0f} avg value "
                     f"({sign}€{delta:,.0f} / {sign}{pct:.1f}% w/w)")
    return lines


def build_report(as_of=None):
    conn = get_conn()
    dates = snapshot_dates(conn)
    if len(dates) < 2:
        return "Not enough snapshot data to compare (need at least 2 days)."

    latest_date = as_of if as_of in dates else dates[0]
    if as_of and as_of not in dates:
        latest_date = nearest_date(dates, as_of) or dates[0]
    li = dates.index(latest_date)
    prev_date = dates[li + 1] if li + 1 < len(dates) else None
    week_target = (datetime.strptime(latest_date, "%Y-%m-%d").date()
                   - timedelta(days=7)).strftime("%Y-%m-%d")
    week_date = nearest_date(dates[li:], week_target, max_gap_days=3)

    meta = card_meta(conn)
    latest = prices_for_date(conn, latest_date)
    previous = prices_for_date(conn, prev_date) if prev_date else {}

    # 7-day windows: current = [latest-6, latest], previous = [latest-13, latest-7]
    latest_d = datetime.strptime(latest_date, "%Y-%m-%d").date()
    cur_start = (latest_d - timedelta(days=6)).strftime("%Y-%m-%d")
    prev_start = (latest_d - timedelta(days=13)).strftime("%Y-%m-%d")
    prev_end = (latest_d - timedelta(days=7)).strftime("%Y-%m-%d")
    cur_avg = avg_prices_window(conn, cur_start, latest_date)
    prev_avg = avg_prices_window(conn, prev_start, prev_end)
    # Cardmarket snapshot history starts 2026-07-31; skip w/w if prev window empty
    have_week = bool(prev_avg)

    parts = []
    parts.append(f"📊 <b>Lorcana Market Report — {latest_date}</b>")
    parts.append(f"<i>Tracking {len(latest)} premium cards (Iconic/Enchanted/Epic/Promo/Legendary)"
                 f" · lowest listed English NM</i>")

    if prev_date:
        g, l = movers(latest, previous, meta)
        gap_note = "" if prev_date == (datetime.strptime(latest_date, "%Y-%m-%d").date()
                                       - timedelta(days=1)).strftime("%Y-%m-%d") \
            else f" (vs {prev_date})"
        parts.append(f"\n<b>Day-on-day movers{gap_note}</b>")
        if g:
            parts.append("<b>Top gainers:</b>")
            parts.extend(fmt_mover(r) for r in g[:TOP_N_DAY])
        if l:
            parts.append("<b>Top losers:</b>")
            parts.extend(fmt_mover(r) for r in l[:TOP_N_DAY])
        if not g and not l:
            parts.append("No significant moves (±3% / €0.50 threshold).")

    if have_week:
        wg, wl = window_movers(cur_avg, prev_avg, meta)
        parts.append(f"\n<b>Week-on-week (7d avg {cur_start}→{latest_date} "
                     f"vs {prev_start}→{prev_end})</b>")
        if wg:
            parts.append("<b>Gainers:</b>")
            parts.extend(fmt_mover(r) for r in wg[:TOP_N_WEEK])
        if wl:
            parts.append("<b>Losers:</b>")
            parts.extend(fmt_mover(r) for r in wl[:TOP_N_WEEK])
        if not wg and not wl:
            parts.append("No significant weekly moves.")

    lows = all_time_lows(conn, latest_date, meta)
    if lows:
        parts.append(f"\n<b>📉 At all-time tracked lows ({len(lows)} cards):</b>")
        for r in lows[:TOP_N_LOWS]:
            parts.append(f"• {fmt_card(r)} [{r['rarity']}] — €{r['price']:.2f}")

    # --- Sealed products section ---
    smeta = sealed_meta(conn)
    sealed_cur = sealed_avg_window(conn, cur_start, latest_date)
    sealed_prev = sealed_avg_window(conn, prev_start, prev_end)
    sealed_day_cur = sealed_for_date(conn, latest_date)
    sealed_day_prev = sealed_for_date(conn, prev_date) if prev_date else {}

    if sealed_cur:
        parts.append(f"\n<b>📦 Sealed Products ({len(sealed_cur)} tracked: Booster Boxes, "
                     f"Troves, Starter Decks, Gift/Collector's Sets)</b>")
        # Day-on-day sealed movers (point-in-time)
        sd_rows = []
        for pid, price in sealed_day_cur.items():
            if pid in sealed_day_prev and sealed_day_prev[pid] > 0:
                old = sealed_day_prev[pid]
                chg, pct = price - old, (price - old) / old * 100.0
                if abs(chg) >= 1.0 and abs(pct) >= MIN_PCT_CHANGE:
                    sd_rows.append({"name": smeta.get(pid, {}).get("name", "?"),
                                    "old": old, "new": price, "chg": chg, "pct": pct})
        sd_g = sorted((r for r in sd_rows if r["chg"] > 0), key=lambda r: -r["pct"])[:5]
        sd_l = sorted((r for r in sd_rows if r["chg"] < 0), key=lambda r: r["pct"])[:5]
        if sd_g or sd_l:
            parts.append("<b>Day-on-day:</b>")
            parts.extend(fmt_sealed_mover(r) for r in sd_g + sd_l)
        if sealed_prev:
            swg, swl = sealed_movers(sealed_cur, sealed_prev, smeta)
            if swg or swl:
                parts.append("<b>Week-on-week (7d avg):</b>")
                parts.extend(fmt_sealed_mover(r) for r in (swg[:5] + swl[:5]))
            if not (sd_g or sd_l or swg or swl):
                parts.append("No significant sealed moves.")
        # Booster box price ladder
        boxes = sorted(
            ((smeta[pid]["name"], avg) for pid, (avg, _n) in sealed_cur.items()
             if smeta.get(pid, {}).get("type") == "Booster Box"),
            key=lambda x: -x[1])
        if boxes:
            parts.append("<b>Booster Box prices (7d avg):</b>")
            parts.extend(f"• {name} — €{avg:,.0f}" for name, avg in boxes[:8])

    if prev_date:
        shifts = supply_shifts(latest, previous, meta)
        if shifts:
            parts.append("\n<b>📦 Supply drops (possible buyouts):</b>")
            for r in shifts:
                parts.append(f"• {fmt_card(r, show_set=False)} [{r['rarity']}] — "
                             f"{r['old']} → {r['new']} listings (€{r['price']:.2f})")
        parts.append("\n<b>Market overview (7d avg tracked value, w/w):</b>")
        if have_week:
            parts.extend(market_overview(cur_avg, prev_avg, meta))
        else:
            by_r = {}
            for cid, (avg, _n) in cur_avg.items():
                by_r.setdefault(meta.get(cid, {}).get("rarity", "?"), []).append(avg)
            for r in TRACKED_RARITIES:
                vals = by_r.get(r)
                if vals:
                    parts.append(f"• {r}: {len(vals)} cards, €{sum(vals):,.0f} avg value")

    conn.close()
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", help="Write report to file instead of stdout")
    ap.add_argument("--date", help="Report as-of date (YYYY-MM-DD)")
    args = ap.parse_args()
    report = build_report(args.date)
    if args.output:
        Path(args.output).write_text(report)
        print(f"Report written to {args.output} ({len(report)} chars)")
    else:
        print(report)


if __name__ == "__main__":
    main()
