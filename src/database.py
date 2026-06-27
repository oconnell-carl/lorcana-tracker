"""SQLite database layer for storing Lorcana card data and price snapshots."""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

DB_PATH = os.environ.get(
    "LORCANA_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "prices.db"),
)


def _ensure_dir() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


@contextmanager
def get_conn() -> Iterable[sqlite3.Connection]:
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create all tables if they don't exist."""
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sets (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                cardmarket_id INTEGER UNIQUE,
                name          TEXT NOT NULL,
                code          TEXT,
                release_date  TEXT,
                card_count    INTEGER DEFAULT 0,
                updated_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS cards (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                cardmarket_id INTEGER UNIQUE,
                name          TEXT NOT NULL,
                card_number   TEXT,
                rarity        TEXT,
                set_id        INTEGER REFERENCES sets(id) ON DELETE CASCADE,
                image_url     TEXT,
                created_at    TEXT DEFAULT (datetime('now')),
                updated_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_cards_set_id ON cards(set_id);
            CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name);

            CREATE TABLE IF NOT EXISTS price_snapshots (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id        INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                source         TEXT NOT NULL,   -- 'cardmarket' | 'tcgplayer' | 'psa10'
                price          REAL,
                currency       TEXT,
                snapshot_date  TEXT NOT NULL,   -- YYYY-MM-DD
                created_at     TEXT DEFAULT (datetime('now')),
                UNIQUE(card_id, source, snapshot_date)
            );
            CREATE INDEX IF NOT EXISTS idx_snap_card ON price_snapshots(card_id);
            CREATE INDEX IF NOT EXISTS idx_snap_date ON price_snapshots(snapshot_date);
            """
        )


# --------------------------------------------------------------------------- #
# Sets
# --------------------------------------------------------------------------- #
def upsert_set(s: Dict[str, Any]) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO sets (cardmarket_id, name, code, release_date, card_count, updated_at)
            VALUES (:cardmarket_id, :name, :code, :release_date, :card_count, :updated_at)
            ON CONFLICT(cardmarket_id) DO UPDATE SET
                name=excluded.name, code=excluded.code,
                release_date=excluded.release_date, card_count=excluded.card_count,
                updated_at=excluded.updated_at
            """,
            {
                "cardmarket_id": s.get("cardmarket_id") or s.get("id"),
                "name": s.get("name", ""),
                "code": s.get("code"),
                "release_date": s.get("release_date"),
                "card_count": s.get("card_count", 0),
                "updated_at": datetime.utcnow().isoformat(),
            },
        )
        if cur.lastrowid and cur.rowcount == 1:
            return cur.lastrowid
        row = conn.execute(
            "SELECT id FROM sets WHERE cardmarket_id=?", (s.get("cardmarket_id") or s.get("id"),)
        ).fetchone()
        return row["id"]


def get_sets() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM sets ORDER BY release_date DESC, name").fetchall()
        return [dict(r) for r in rows]


def get_set(set_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sets WHERE id=?", (set_id,)).fetchone()
        return dict(row) if row else None


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #
def upsert_card(c: Dict[str, Any]) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO cards (cardmarket_id, name, card_number, rarity, set_id, image_url, updated_at)
            VALUES (:cardmarket_id, :name, :card_number, :rarity, :set_id, :image_url, :updated_at)
            ON CONFLICT(cardmarket_id) DO UPDATE SET
                name=excluded.name, card_number=excluded.card_number,
                rarity=excluded.rarity, set_id=excluded.set_id,
                image_url=excluded.image_url, updated_at=excluded.updated_at
            """,
            {
                "cardmarket_id": c.get("cardmarket_id") or c.get("id"),
                "name": c.get("name", ""),
                "card_number": c.get("card_number") or c.get("number"),
                "rarity": c.get("rarity"),
                "set_id": c.get("set_id"),
                "image_url": c.get("image_url"),
                "updated_at": datetime.utcnow().isoformat(),
            },
        )
        if cur.lastrowid and cur.rowcount == 1:
            return cur.lastrowid
        row = conn.execute(
            "SELECT id FROM cards WHERE cardmarket_id=?",
            (c.get("cardmarket_id") or c.get("id"),),
        ).fetchone()
        return row["id"]


def get_cards_in_set(set_id: int) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM cards WHERE set_id=? ORDER BY card_number", (set_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_card(card_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        return dict(row) if row else None


def search_cards(query: str, limit: int = 20) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT c.*, s.name AS set_name FROM cards c "
            "LEFT JOIN sets s ON c.set_id=s.id "
            "WHERE c.name LIKE ? COLLATE NOCASE ORDER BY c.name LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Price snapshots
# --------------------------------------------------------------------------- #
def record_snapshot(card_id: int, source: str, price: Optional[float], currency: str,
                    snapshot_date: Optional[str] = None) -> None:
    if price is None:
        return
    snapshot_date = snapshot_date or datetime.utcnow().date().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO price_snapshots (card_id, source, price, currency, snapshot_date)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(card_id, source, snapshot_date) DO UPDATE SET
                price=excluded.price, currency=excluded.currency
            """,
            (card_id, source, price, currency, snapshot_date),
        )


def get_latest_prices(card_id: int) -> Dict[str, Dict[str, Any]]:
    """Return latest snapshot per source, e.g. {'cardmarket': {...}, 'tcgplayer': {...}}."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.* FROM price_snapshots p
            JOIN (
                SELECT source, MAX(snapshot_date) AS maxd
                FROM price_snapshots WHERE card_id=? GROUP BY source
            ) m ON p.source=m.source AND p.snapshot_date=m.maxd
            WHERE p.card_id=?
            """,
            (card_id, card_id),
        ).fetchall()
        return {r["source"]: dict(r) for r in rows}


def get_history(card_id: int, source: Optional[str] = None, days: Optional[int] = None) -> List[Dict[str, Any]]:
    sql = "SELECT source, price, currency, snapshot_date FROM price_snapshots WHERE card_id=?"
    params: List[Any] = [card_id]
    if source:
        sql += " AND source=?"
        params.append(source)
    if days:
        sql += " AND snapshot_date >= ?"
        params.append((datetime.utcnow() - timedelta(days=days)).date().isoformat())
    sql += " ORDER BY snapshot_date ASC"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def all_card_ids() -> List[int]:
    with get_conn() as conn:
        rows = conn.execute("SELECT id FROM cards ORDER BY id").fetchall()
        return [r["id"] for r in rows]


def snapshot_exists(date_str: str) -> int:
    """How many snapshots exist for a given date (used to skip duplicate runs)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM price_snapshots WHERE snapshot_date=?", (date_str,)
        ).fetchone()
        return row["n"]


if __name__ == "__main__":
    init_db()
    print(f"Initialized database at {DB_PATH}")
