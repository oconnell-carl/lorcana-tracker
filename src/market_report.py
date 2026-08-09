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

# Noise filters
MIN_PRICE = 2.0          # ignore cards currently under €2
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
        if price is not None:
            out[cid] = (price, avail)
    return out


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
        if price and min_price and price <= min_price and snaps >= 10 and price >= MIN_PRICE:
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


def market_overview(latest, previous, meta):
    by_rarity = {}
    prev_by_rarity = {}
    for cid, (price, _avail) in latest.items():
        r = meta.get(cid, {}).get("rarity", "?")
        by_rarity.setdefault(r, []).append(price)
        if cid in previous:
            prev_by_rarity.setdefault(r, []).append(previous[cid][0])
    lines = []
    for r in TRACKED_RARITIES:
        vals = by_rarity.get(r)
        if not vals:
            continue
        total = sum(vals)
        prev_total = sum(prev_by_rarity.get(r, []))
        delta = total - prev_total if prev_total else 0.0
        sign = "+" if delta >= 0 else ""
        lines.append(f"• {r}: {len(vals)} cards, €{total:,.0f} total "
                     f"({sign}€{delta:,.0f} d/d)")
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

    if week_date and week_date != latest_date and week_date != prev_date:
        week_prev = prices_for_date(conn, week_date)
        wg, wl = movers(latest, week_prev, meta)
        parts.append(f"\n<b>Week-on-week (vs {week_date})</b>")
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

    if prev_date:
        shifts = supply_shifts(latest, previous, meta)
        if shifts:
            parts.append("\n<b>📦 Supply drops (possible buyouts):</b>")
            for r in shifts:
                parts.append(f"• {fmt_card(r, show_set=False)} [{r['rarity']}] — "
                             f"{r['old']} → {r['new']} listings (€{r['price']:.2f})")
        parts.append("\n<b>Market overview (tracked value):</b>")
        parts.extend(market_overview(latest, previous, meta))

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
