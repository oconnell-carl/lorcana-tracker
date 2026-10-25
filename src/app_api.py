"""View-model API for the rebuilt frontend (``static/app.js``).

The rebuilt UI was written against a flat, display-oriented contract
(``cm_trend``, ``cm_low``, ``usd``, ``/market`` ...) rather than the nested
``prices.cardmarket`` shape served by the original ``/api/*`` endpoints.

Everything here is computed from the existing SQLite schema and mounted at
``/api/app/*`` so the original API keeps working unchanged.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query

from . import database
from .market_report import (
    MAX_PRICE,
    MIN_ABS_CHANGE,
    MIN_PCT_CHANGE,
    MIN_PRICE,
    SEALED_TYPES,
    TRACKED_RARITIES,
)

router = APIRouter(prefix="/api/app", tags=["app"])

RANGE_DAYS = {"30d": 30, "3m": 90, "6m": 180, "1y": 365, "all": None}

# The API feed stores a few rarities with inconsistent casing.
_RARITY_FIX = {"rare": "Rare", "SUPER RARE": "Super_rare"}


# --------------------------------------------------------------------------- #
# Connection / small helpers
# --------------------------------------------------------------------------- #
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(database.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _rarity(value: Optional[str]) -> Optional[str]:
    return _RARITY_FIX.get(value, value) if value else value


def _num_sort(card_number: Optional[str]) -> float:
    """Sort key for card numbers like '12/204' or 'P1'."""
    raw = (card_number or "").split("/")[0].strip()
    try:
        return float(raw)
    except ValueError:
        return 999_999.0


def _latest_date(conn: sqlite3.Connection, source: str = "cardmarket") -> Optional[str]:
    row = conn.execute(
        "SELECT MAX(snapshot_date) FROM price_snapshots WHERE source=?", (source,)
    ).fetchone()
    return row[0] if row else None


def _latest_prices(
    conn: sqlite3.Connection, card_ids: Optional[List[int]] = None
) -> Dict[int, Dict[str, Dict[str, Any]]]:
    """Latest snapshot per card/source: ``{card_id: {source: row}}``."""
    where, params = "", []
    if card_ids is not None:
        if not card_ids:
            return {}
        where = "WHERE p.card_id IN (%s)" % ",".join("?" * len(card_ids))
        params = list(card_ids)
    sql = f"""
        SELECT p.* FROM price_snapshots p
        JOIN (
            SELECT card_id, source, MAX(snapshot_date) AS maxd
            FROM price_snapshots GROUP BY card_id, source
        ) m ON p.card_id = m.card_id AND p.source = m.source
           AND p.snapshot_date = m.maxd
        {where}
    """
    out: Dict[int, Dict[str, Dict[str, Any]]] = {}
    for row in conn.execute(sql, params):
        out.setdefault(row["card_id"], {})[row["source"]] = dict(row)
    return out


def _latest_sealed_prices(
    conn: sqlite3.Connection, product_ids: Optional[List[int]] = None
) -> Dict[int, Dict[str, Dict[str, Any]]]:
    where, params = "", []
    if product_ids is not None:
        if not product_ids:
            return {}
        where = "WHERE p.product_id IN (%s)" % ",".join("?" * len(product_ids))
        params = list(product_ids)
    sql = f"""
        SELECT p.* FROM sealed_price_snapshots p
        JOIN (
            SELECT product_id, source, MAX(snapshot_date) AS maxd
            FROM sealed_price_snapshots GROUP BY product_id, source
        ) m ON p.product_id = m.product_id AND p.source = m.source
           AND p.snapshot_date = m.maxd
        {where}
    """
    out: Dict[int, Dict[str, Dict[str, Any]]] = {}
    for row in conn.execute(sql, params):
        out.setdefault(row["product_id"], {})[row["source"]] = dict(row)
    return out


def _card_rows(conn: sqlite3.Connection, sql: str, params: Tuple = ()) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT c.*, s.name AS set_name, s.code AS set_code "
        "FROM cards c LEFT JOIN sets s ON s.id = c.set_id " + sql,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def _flat_card(card: Dict[str, Any], prices: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Shape a card row the way the rebuilt frontend reads it."""
    cm = prices.get("cardmarket") or {}
    tcg = prices.get("tcgplayer") or {}
    subtitle = card.get("subtitle") or ""
    name = card.get("name") or ""
    full_name = card.get("full_name") or (f"{name} — {subtitle}" if subtitle else name)
    return {
        "id": card["id"],
        "cardmarket_id": card.get("cardmarket_id"),
        "name": name,
        "full_name": full_name,
        "version": subtitle,
        "card_number": card.get("card_number"),
        "num_sort": _num_sort(card.get("card_number")),
        "rarity": _rarity(card.get("rarity")),
        "set_id": card.get("set_id"),
        "set_name": card.get("set_name"),
        "set_code": card.get("set_code") or "",
        "image_small": card.get("image_url"),
        "image_large": card.get("image_url"),
        # Not stored locally: the API feed has no ink/type/printing metadata.
        "ink": None,
        "type": None,
        "printings": [],
        # Cardmarket "price" is the lowest English NM listing; avg_* are the
        # rolling averages, lowest_near_mint_raw is the lowest listing in any
        # language/condition.
        "cm_trend": cm.get("price"),
        "cm_low": cm.get("lowest_near_mint_raw") or cm.get("price"),
        "cm_avg7": cm.get("avg_7d"),
        "cm_avg30": cm.get("avg_30d"),
        "cm_avg1": None,
        # The stored TCGPlayer snapshots are in EUR (the upstream API converts).
        "usd": tcg.get("price"),
        "usd_currency": tcg.get("currency") or "EUR",
        "usd_foil": None,
        "cm_url": None,
        "tcgplayer_url": None,
        "date_added": (card.get("created_at") or "")[:10] or None,
    }


def _flat_sealed(
    product: Dict[str, Any], prices: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    cm = prices.get("cardmarket") or {}
    price = cm.get("price")
    return {
        "id": product["id"],
        "name": product.get("name"),
        "set_name": product.get("set_name"),
        "product_type": product.get("product_type"),
        "image_url": product.get("image_url"),
        "cm_url": product.get("cardmarket_url"),
        "tcggo_url": product.get("tcggo_url"),
        # "trend" in the UI == the current lowest listing.
        "trend": price,
        "low": cm.get("lowest_EU_only") or price,
        "avg": cm.get("avg_30d") or cm.get("avg_7d") or price,
        "available_items": cm.get("available_items"),
        "date_added": product.get("created_at"),
        "snapshot_date": cm.get("snapshot_date"),
    }


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
@router.get("/status")
def app_status() -> Dict[str, Any]:
    conn = _conn()
    try:
        dates = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT snapshot_date FROM price_snapshots "
                "WHERE source='cardmarket' ORDER BY snapshot_date DESC"
            )
        ]
        refreshed = conn.execute("SELECT MAX(updated_at) FROM cards").fetchone()[0]
        counts = conn.execute(
            "SELECT (SELECT COUNT(*) FROM cards), (SELECT COUNT(*) FROM sets), "
            "(SELECT COUNT(*) FROM sealed_products)"
        ).fetchone()
        return {
            "api_available": True,
            "card_count": counts[0],
            "set_count": counts[1],
            "sealed_count": counts[2],
            "snapshot_days": len(dates),
            "last_snapshot": dates[0] if dates else None,
            "refreshed_at": refreshed,
            "refreshing": False,
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Sets and cards
# --------------------------------------------------------------------------- #
@router.get("/sets")
def app_sets() -> Dict[str, Any]:
    conn = _conn()
    try:
        sets = [dict(r) for r in conn.execute("SELECT * FROM sets ORDER BY release_date DESC, name")]
        counts = {
            r[0]: r[1] for r in conn.execute("SELECT set_id, COUNT(*) FROM cards GROUP BY set_id")
        }
        latest = _latest_date(conn)
        values: Dict[int, float] = {}
        if latest:
            values = {
                r[0]: r[1]
                for r in conn.execute(
                    "SELECT c.set_id, SUM(p.price) FROM cards c "
                    "JOIN price_snapshots p ON p.card_id = c.id "
                    "WHERE p.source='cardmarket' AND p.snapshot_date=? "
                    "AND p.price IS NOT NULL AND p.price > 0 GROUP BY c.set_id",
                    (latest,),
                )
            }
        out = []
        for s in sets:
            out.append(
                {
                    "id": s["id"],
                    "code": s.get("code") or "",
                    "name": s.get("name"),
                    "card_count": counts.get(s["id"], s.get("card_count") or 0),
                    "release_date": s.get("release_date"),
                    "cover": s.get("logo"),
                    "set_value": round(values.get(s["id"], 0.0), 2) or None,
                    "cardmarket_id": s.get("cardmarket_id"),
                }
            )
        return {"sets": out}
    finally:
        conn.close()


@router.get("/sets/{set_ref}/cards")
def app_set_cards(set_ref: str) -> Dict[str, Any]:
    conn = _conn()
    try:
        if str(set_ref).isdigit():
            set_row = conn.execute("SELECT * FROM sets WHERE id=?", (int(set_ref),)).fetchone()
        else:
            set_row = conn.execute(
                "SELECT * FROM sets WHERE code=? COLLATE NOCASE", (set_ref,)
            ).fetchone()
        if not set_row:
            raise HTTPException(status_code=404, detail="Set not found")
        set_row = dict(set_row)
        rows = _card_rows(conn, "WHERE c.set_id = ?", (set_row["id"],))
        prices = _latest_prices(conn, [r["id"] for r in rows])
        cards = [_flat_card(r, prices.get(r["id"], {})) for r in rows]
        cards.sort(key=lambda c: (c["num_sort"], c["full_name"] or ""))
        return {
            "set": {
                "id": set_row["id"],
                "code": set_row.get("code") or "",
                "name": set_row.get("name"),
                "release_date": set_row.get("release_date"),
                "card_count": len(cards),
                "cover": set_row.get("logo"),
            },
            "cards": cards,
        }
    finally:
        conn.close()


@router.get("/cards/{card_id}")
def app_card(card_id: int) -> Dict[str, Any]:
    conn = _conn()
    try:
        rows = _card_rows(conn, "WHERE c.id = ?", (card_id,))
        if not rows:
            raise HTTPException(status_code=404, detail="Card not found")
        card = rows[0]
        prices = _latest_prices(conn, [card_id])
        flat = _flat_card(card, prices.get(card_id, {}))

        # "Other printings" = other cards sharing this name.
        name = (card.get("name") or "").strip()
        printing_rows = []
        if name:
            printing_rows = _card_rows(
                conn,
                "WHERE lower(c.name) = lower(?) AND c.id != ? "
                "ORDER BY s.release_date DESC, c.card_number LIMIT 12",
                (name, card_id),
            )
        pprices = _latest_prices(conn, [r["id"] for r in printing_rows])
        flat["printings"] = [
            _flat_card(r, pprices.get(r["id"], {})) for r in printing_rows
        ]
        return {"card": flat}
    finally:
        conn.close()


def _range_cutoff(latest: str, range_key: str) -> Optional[str]:
    days = RANGE_DAYS.get(range_key)
    if days is None:
        return None
    return (datetime.strptime(latest, "%Y-%m-%d").date() - timedelta(days=days)).isoformat()


@router.get("/cards/{card_id}/history")
def app_card_history(
    card_id: int, range: str = Query("all", pattern="^(30d|3m|6m|1y|all)$")
) -> Dict[str, Any]:
    conn = _conn()
    try:
        latest = _latest_date(conn)
        if not latest:
            return {"points": []}
        cutoff = _range_cutoff(latest, range)
        sql = (
            "SELECT source, snapshot_date, price, lowest_near_mint_raw "
            "FROM price_snapshots WHERE card_id=?"
        )
        params: List[Any] = [card_id]
        if cutoff:
            sql += " AND snapshot_date >= ?"
            params.append(cutoff)
        by_date: Dict[str, Dict[str, Any]] = {}
        for source, date, price, low in conn.execute(sql, params):
            point = by_date.setdefault(date, {"date": date, "usd_foil": None})
            if source == "cardmarket":
                point["cm_trend"] = price
                point["cm_low"] = low if low is not None else price
            elif source == "tcgplayer":
                point["usd"] = price
        points = [by_date[d] for d in sorted(by_date)]
        return {"points": points}
    finally:
        conn.close()


@router.get("/search")
def app_search(q: str = Query(..., min_length=1)) -> Dict[str, Any]:
    conn = _conn()
    try:
        rows = _card_rows(
            conn,
            "WHERE c.name LIKE ? COLLATE NOCASE ORDER BY c.name LIMIT 20",
            (f"%{q}%",),
        )
        prices = _latest_prices(conn, [r["id"] for r in rows])
        return {"results": [_flat_card(r, prices.get(r["id"], {})) for r in rows]}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Market report
# --------------------------------------------------------------------------- #
def _premium_prices_for_date(
    conn: sqlite3.Connection, date: str
) -> Dict[int, Tuple[float, Optional[int]]]:
    q = (
        "SELECT s.card_id, s.price, s.available_items FROM price_snapshots s "
        "JOIN cards c ON c.id = s.card_id "
        "WHERE s.source='cardmarket' AND s.snapshot_date=? "
        "AND s.price IS NOT NULL AND s.price > 0 AND s.price <= ? "
        "AND c.rarity IN (%s)" % ",".join("?" * len(TRACKED_RARITIES))
    )
    out = {}
    for cid, price, avail in conn.execute(q, (date, MAX_PRICE, *TRACKED_RARITIES)):
        if price >= MIN_PRICE:
            out[cid] = (price, avail)
    return out


def _avg_window(
    conn: sqlite3.Connection, start: str, end: str
) -> Dict[int, Tuple[float, int]]:
    q = (
        "SELECT s.card_id, AVG(s.price), COUNT(DISTINCT s.snapshot_date) "
        "FROM price_snapshots s JOIN cards c ON c.id = s.card_id "
        "WHERE s.source='cardmarket' AND s.snapshot_date BETWEEN ? AND ? "
        "AND s.price IS NOT NULL AND s.price > 0 AND s.price <= ? "
        "AND c.rarity IN (%s) GROUP BY s.card_id" % ",".join("?" * len(TRACKED_RARITIES))
    )
    return {
        cid: (avg, n)
        for cid, avg, n in conn.execute(
            q, (start, end, MAX_PRICE, *TRACKED_RARITIES)
        )
    }


def _card_meta(conn: sqlite3.Connection) -> Dict[int, Dict[str, Any]]:
    return {
        r["id"]: {
            "name": r["name"],
            "full_name": r["full_name"] or r["name"],
            "rarity": _rarity(r["rarity"]),
            "set_name": r["set_name"] or "",
            "image_small": r["image_url"],
        }
        for r in conn.execute(
            "SELECT c.id, c.name, c.full_name, c.rarity, c.image_url, s.name AS set_name "
            "FROM cards c LEFT JOIN sets s ON s.id = c.set_id"
        )
    }


def _mover(cid: int, old: float, new: float, pct: float, meta: Dict[int, Dict[str, Any]]):
    m = meta.get(cid, {})
    return {
        "id": cid,
        "full_name": m.get("full_name") or m.get("name") or "?",
        "set_name": m.get("set_name") or "",
        "rarity": m.get("rarity"),
        "image_small": m.get("image_small"),
        "now": round(new, 2),
        "then": round(old, 2),
        "pct": round(pct, 1),
    }


def _movers(
    latest: Dict[int, Tuple[float, Optional[int]]],
    previous: Dict[int, Tuple[float, Optional[int]]],
    meta: Dict[int, Dict[str, Any]],
):
    rows = []
    for cid, (price, _avail) in latest.items():
        if cid not in previous or price < MIN_PRICE:
            continue
        old = previous[cid][0]
        if not old or old <= 0:
            continue
        chg, pct = price - old, (price - old) / old * 100.0
        if abs(chg) >= MIN_ABS_CHANGE and abs(pct) >= MIN_PCT_CHANGE:
            rows.append(_mover(cid, old, price, pct, meta))
    up = sorted((r for r in rows if r["now"] > r["then"]), key=lambda r: -r["pct"])
    down = sorted((r for r in rows if r["now"] < r["then"]), key=lambda r: r["pct"])
    return up, down


def _window_movers(
    cur: Dict[int, Tuple[float, int]],
    prev: Dict[int, Tuple[float, int]],
    meta: Dict[int, Dict[str, Any]],
    min_days: int = 2,
):
    rows = []
    for cid, (avg, n) in cur.items():
        if cid not in prev or n < min_days:
            continue
        old_avg, old_n = prev[cid]
        if old_n < min_days or not old_avg or old_avg <= 0:
            continue
        chg, pct = avg - old_avg, (avg - old_avg) / old_avg * 100.0
        if abs(chg) >= MIN_ABS_CHANGE and abs(pct) >= MIN_PCT_CHANGE:
            rows.append(_mover(cid, old_avg, avg, pct, meta))
    up = sorted((r for r in rows if r["now"] > r["then"]), key=lambda r: -r["pct"])
    down = sorted((r for r in rows if r["now"] < r["then"]), key=lambda r: r["pct"])
    return up, down


def _all_time_lows(
    conn: sqlite3.Connection, latest: str, meta: Dict[int, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    q = (
        "SELECT s.card_id, s.price, "
        "(SELECT MIN(p2.price) FROM price_snapshots p2 WHERE p2.card_id=s.card_id "
        " AND p2.source='cardmarket' AND p2.price IS NOT NULL AND p2.price > 0), "
        "(SELECT COUNT(*) FROM price_snapshots p3 WHERE p3.card_id=s.card_id "
        " AND p3.source='cardmarket'), "
        "(SELECT AVG(p4.price) FROM price_snapshots p4 WHERE p4.card_id=s.card_id "
        " AND p4.source='cardmarket' AND p4.snapshot_date >= date(?, '-30 day')) "
        "FROM price_snapshots s JOIN cards c ON c.id=s.card_id "
        "WHERE s.source='cardmarket' AND s.snapshot_date=? "
        "AND c.rarity IN (%s)" % ",".join("?" * len(TRACKED_RARITIES))
    )
    out = []
    for cid, price, min_price, snaps, avg30 in conn.execute(
        q, (latest, latest, *TRACKED_RARITIES)
    ):
        if (
            price
            and min_price
            and price <= min_price
            and snaps >= 10
            and MIN_PRICE <= price <= MAX_PRICE
        ):
            m = meta.get(cid, {})
            out.append(
                {
                    "id": cid,
                    "full_name": m.get("full_name") or m.get("name") or "?",
                    "set_name": m.get("set_name") or "",
                    "rarity": m.get("rarity"),
                    "image_small": m.get("image_small"),
                    "cm_trend": round(price, 2),
                    "cm_avg30": round(avg30, 2) if avg30 else None,
                }
            )
    return sorted(out, key=lambda r: -r["cm_trend"])


def _sealed_avg_window(
    conn: sqlite3.Connection, start: str, end: str
) -> Dict[int, Tuple[float, int]]:
    q = (
        "SELECT s.product_id, AVG(s.price), COUNT(DISTINCT s.snapshot_date) "
        "FROM sealed_price_snapshots s JOIN sealed_products p ON p.id=s.product_id "
        "WHERE s.snapshot_date BETWEEN ? AND ? AND s.price IS NOT NULL "
        "AND s.price > 0 AND s.price <= ? AND p.product_type IN (%s) "
        "GROUP BY s.product_id" % ",".join("?" * len(SEALED_TYPES))
    )
    return {
        pid: (avg, n)
        for pid, avg, n in conn.execute(q, (start, end, MAX_PRICE, *SEALED_TYPES))
    }


def _sealed_movers(
    cur: Dict[int, Tuple[float, int]],
    prev: Dict[int, Tuple[float, int]],
    products: Dict[int, Dict[str, Any]],
    min_days: int = 2,
):
    rows = []
    for pid, (avg, n) in cur.items():
        if pid not in prev or n < min_days:
            continue
        old_avg, old_n = prev[pid]
        if old_n < min_days or not old_avg or old_avg <= 0:
            continue
        chg, pct = avg - old_avg, (avg - old_avg) / old_avg * 100.0
        if abs(chg) >= 1.0 and abs(pct) >= MIN_PCT_CHANGE:
            p = products.get(pid, {})
            rows.append(
                {
                    "id": pid,
                    "full_name": p.get("name") or "?",
                    "product_type": p.get("product_type") or "",
                    "image_small": p.get("image_url"),
                    "now": round(avg, 2),
                    "then": round(old_avg, 2),
                    "pct": round(pct, 1),
                }
            )
    up = sorted((r for r in rows if r["now"] > r["then"]), key=lambda r: -r["pct"])
    down = sorted((r for r in rows if r["now"] < r["then"]), key=lambda r: r["pct"])
    return up, down


@router.get("/market")
def app_market() -> Dict[str, Any]:
    conn = _conn()
    try:
        dates = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT snapshot_date FROM price_snapshots "
                "WHERE source='cardmarket' ORDER BY snapshot_date DESC"
            )
        ]
        if not dates:
            raise HTTPException(status_code=503, detail="No snapshots recorded yet")
        latest = dates[0]
        prev = dates[1] if len(dates) > 1 else None
        meta = _card_meta(conn)

        latest_prices = _premium_prices_for_date(conn, latest)
        prev_prices = _premium_prices_for_date(conn, prev) if prev else {}
        day_up, day_down = _movers(latest_prices, prev_prices, meta)

        latest_d = datetime.strptime(latest, "%Y-%m-%d").date()
        cur_start = (latest_d - timedelta(days=6)).isoformat()
        prev_start = (latest_d - timedelta(days=13)).isoformat()
        prev_end = (latest_d - timedelta(days=7)).isoformat()
        cur_avg = _avg_window(conn, cur_start, latest)
        prev_avg = _avg_window(conn, prev_start, prev_end)
        week_up, week_down = _window_movers(cur_avg, prev_avg, meta)

        lows = _all_time_lows(conn, latest, meta)

        top = []
        for cid, (price, _avail) in sorted(
            latest_prices.items(), key=lambda kv: -kv[1][0]
        )[:12]:
            m = meta.get(cid, {})
            top.append(
                {
                    "id": cid,
                    "full_name": m.get("full_name") or m.get("name") or "?",
                    "set_name": m.get("set_name") or "",
                    "rarity": m.get("rarity"),
                    "image_small": m.get("image_small"),
                    "cm_trend": round(price, 2),
                }
            )

        products = {
            r["id"]: dict(r) for r in conn.execute("SELECT * FROM sealed_products")
        }
        sealed_prices = _latest_sealed_prices(conn, list(products))
        booster = []
        for pid, p in products.items():
            if p.get("product_type") != "Booster Box":
                continue
            snap = (sealed_prices.get(pid) or {}).get("cardmarket") or {}
            if snap.get("price"):
                booster.append(
                    {
                        "id": pid,
                        "name": p.get("name"),
                        "set_name": p.get("set_name"),
                        "trend": round(snap["price"], 2),
                    }
                )
        booster.sort(key=lambda b: -b["trend"])

        sealed_cur = _sealed_avg_window(conn, cur_start, latest)
        sealed_prev = _sealed_avg_window(conn, prev_start, prev_end)
        s_up, s_down = _sealed_movers(sealed_cur, sealed_prev, products)

        return {
            "snapshot_days": len(dates),
            "latest": latest,
            "previous": prev,
            "day": {
                "basis": f"{prev} → {latest}" if prev else f"latest snapshot {latest}",
                "up": day_up,
                "down": day_down,
            },
            "week": {
                "basis": f"7d avg {cur_start}→{latest} vs {prev_start}→{prev_end}",
                "up": week_up,
                "down": week_down,
            },
            "lows": lows,
            "top": top,
            "booster_ladder": booster,
            "sealed_movers": {
                "basis": f"7d avg {cur_start}→{latest}",
                "up": s_up,
                "down": s_down,
            },
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Sealed products
# --------------------------------------------------------------------------- #
@router.get("/sealed")
def app_sealed() -> Dict[str, Any]:
    conn = _conn()
    try:
        products = [dict(r) for r in conn.execute("SELECT * FROM sealed_products")]
        prices = _latest_sealed_prices(conn, [p["id"] for p in products])
        flat = [_flat_sealed(p, prices.get(p["id"], {})) for p in products]
        flat.sort(key=lambda p: (p.get("product_type") or "", p.get("name") or ""))
        type_counts: Dict[str, int] = {}
        for p in flat:
            key = p.get("product_type") or "Other"
            type_counts[key] = type_counts.get(key, 0) + 1
        types = [
            {"product_type": t, "n": n}
            for t, n in sorted(type_counts.items(), key=lambda kv: -kv[1])
        ]
        return {"products": flat, "types": types, "count": len(flat)}
    finally:
        conn.close()


@router.get("/sealed/{product_id}")
def app_sealed_detail(product_id: int) -> Dict[str, Any]:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM sealed_products WHERE id=?", (product_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Sealed product not found")
        prices = _latest_sealed_prices(conn, [product_id])
        return {"product": _flat_sealed(dict(row), prices.get(product_id, {}))}
    finally:
        conn.close()


@router.get("/sealed/{product_id}/history")
def app_sealed_history(
    product_id: int, range: str = Query("all", pattern="^(30d|3m|6m|1y|all)$")
) -> Dict[str, Any]:
    conn = _conn()
    try:
        latest_row = conn.execute(
            "SELECT MAX(snapshot_date) FROM sealed_price_snapshots"
        ).fetchone()
        latest = latest_row[0] if latest_row else None
        if not latest:
            return {"points": []}
        cutoff = _range_cutoff(latest, range)
        sql = (
            "SELECT snapshot_date, price, lowest_EU_only "
            "FROM sealed_price_snapshots WHERE product_id=?"
        )
        params: List[Any] = [product_id]
        if cutoff:
            sql += " AND snapshot_date >= ?"
            params.append(cutoff)
        points = [
            {
                "date": date,
                "trend": price,
                "low": low if low is not None else price,
            }
            for date, price, low in conn.execute(sql, params)
        ]
        points.sort(key=lambda p: p["date"])
        return {"points": points}
    finally:
        conn.close()
