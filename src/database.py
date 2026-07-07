"""SQLite database layer for storing Lorcana card data and price snapshots."""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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


def _add_column_if_missing(conn, table: str, column: str, definition: str) -> None:
    """Add a column to a table if it doesn't already exist (ALTER TABLE migration)."""
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    """Create all tables if they don't exist, then run migrations."""
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
                logo          TEXT,
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

            CREATE TABLE IF NOT EXISTS sealed_products (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                cardmarket_id INTEGER UNIQUE,
                name          TEXT NOT NULL,
                slug          TEXT,
                product_type  TEXT,
                set_name      TEXT,
                image_url     TEXT,
                tcggo_url     TEXT,
                cardmarket_url TEXT,
                created_at    TEXT DEFAULT (datetime('now')),
                updated_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sealed_type ON sealed_products(product_type);
            CREATE INDEX IF NOT EXISTS idx_sealed_set ON sealed_products(set_name);
            CREATE INDEX IF NOT EXISTS idx_sealed_name ON sealed_products(name);

            CREATE TABLE IF NOT EXISTS sealed_price_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id      INTEGER NOT NULL REFERENCES sealed_products(id) ON DELETE CASCADE,
                source          TEXT NOT NULL,           -- 'cardmarket'
                price           REAL,
                currency        TEXT,
                lowest_EU_only  REAL,
                lowest_DE       REAL,
                lowest_FR       REAL,
                lowest_IT       REAL,
                avg_7d          REAL,
                avg_30d         REAL,
                available_items INTEGER,
                snapshot_date   TEXT NOT NULL,           -- YYYY-MM-DD
                created_at      TEXT DEFAULT (datetime('now')),
                UNIQUE(product_id, source, snapshot_date)
            );
            CREATE INDEX IF NOT EXISTS idx_sealed_snap_product ON sealed_price_snapshots(product_id);
            CREATE INDEX IF NOT EXISTS idx_sealed_snap_date ON sealed_price_snapshots(snapshot_date);
            """
        )
        # Migrations: add enriched price columns to price_snapshots
        _add_column_if_missing(conn, "price_snapshots", "avg_7d", "REAL")
        _add_column_if_missing(conn, "price_snapshots", "avg_30d", "REAL")
        _add_column_if_missing(conn, "price_snapshots", "available_items", "INTEGER")
        _add_column_if_missing(conn, "price_snapshots", "lowest_near_mint_raw", "REAL")


# --------------------------------------------------------------------------- #
# Sets
# --------------------------------------------------------------------------- #
def upsert_set(s: Dict[str, Any]) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO sets (cardmarket_id, name, code, release_date, card_count, logo, updated_at)
            VALUES (:cardmarket_id, :name, :code, :release_date, :card_count, :logo, :updated_at)
            ON CONFLICT(cardmarket_id) DO UPDATE SET
                name=excluded.name, code=excluded.code,
                release_date=excluded.release_date, card_count=excluded.card_count,
                logo=excluded.logo, updated_at=excluded.updated_at
            """,
            {
                "cardmarket_id": s.get("cardmarket_id") or s.get("id"),
                "name": s.get("name", ""),
                "code": s.get("code"),
                "release_date": s.get("release_date"),
                "card_count": s.get("card_count", 0),
                "logo": s.get("logo"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
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
                "updated_at": datetime.now(timezone.utc).isoformat(),
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
                    snapshot_date: Optional[str] = None,
                    avg_7d: Optional[float] = None,
                    avg_30d: Optional[float] = None,
                    available_items: Optional[int] = None,
                    lowest_near_mint_raw: Optional[float] = None) -> None:
    if price is None:
        return
    snapshot_date = snapshot_date or datetime.now(timezone.utc).date().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO price_snapshots (card_id, source, price, currency, snapshot_date,
                                         avg_7d, avg_30d, available_items, lowest_near_mint_raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(card_id, source, snapshot_date) DO UPDATE SET
                price=excluded.price, currency=excluded.currency,
                avg_7d=excluded.avg_7d, avg_30d=excluded.avg_30d,
                available_items=excluded.available_items,
                lowest_near_mint_raw=excluded.lowest_near_mint_raw
            """,
            (card_id, source, price, currency, snapshot_date,
             avg_7d, avg_30d, available_items, lowest_near_mint_raw),
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
    sql = ("SELECT source, price, currency, snapshot_date, avg_7d, avg_30d, "
           "available_items, lowest_near_mint_raw "
           "FROM price_snapshots WHERE card_id=?")
    params: List[Any] = [card_id]
    if source:
        sql += " AND source=?"
        params.append(source)
    if days:
        sql += " AND snapshot_date >= ?"
        params.append((datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat())
    sql += " ORDER BY snapshot_date ASC"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def all_card_ids() -> List[int]:
    with get_conn() as conn:
        rows = conn.execute("SELECT id FROM cards ORDER BY id").fetchall()
        return [r["id"] for r in rows]


def get_rarities_in_set(set_id: int) -> List[str]:
    """Return list of distinct rarities present in a set."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT rarity FROM cards WHERE set_id=?", (set_id,)
        ).fetchall()
        return [r["rarity"] for r in rows if r["rarity"]]


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


# --------------------------------------------------------------------------- #
# Sealed products
# --------------------------------------------------------------------------- #
def upsert_sealed_product(p: Dict[str, Any]) -> int:
    with get_conn() as conn:
        cm_id = p.get("cardmarket_id") or p.get("id")
        cur = conn.execute(
            """
            INSERT INTO sealed_products
                (cardmarket_id, name, slug, product_type, set_name, image_url,
                 tcggo_url, cardmarket_url, updated_at)
            VALUES (:cardmarket_id, :name, :slug, :product_type, :set_name, :image_url,
                    :tcggo_url, :cardmarket_url, :updated_at)
            ON CONFLICT(cardmarket_id) DO UPDATE SET
                name=excluded.name, slug=excluded.slug,
                product_type=excluded.product_type, set_name=excluded.set_name,
                image_url=excluded.image_url, tcggo_url=excluded.tcggo_url,
                cardmarket_url=excluded.cardmarket_url, updated_at=excluded.updated_at
            """,
            {
                "cardmarket_id": cm_id,
                "name": p.get("name", ""),
                "slug": p.get("slug"),
                "product_type": p.get("product_type"),
                "set_name": p.get("set_name"),
                "image_url": p.get("image_url"),
                "tcggo_url": p.get("tcggo_url"),
                "cardmarket_url": p.get("cardmarket_url"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        if cur.lastrowid and cur.rowcount == 1:
            return cur.lastrowid
        row = conn.execute(
            "SELECT id FROM sealed_products WHERE cardmarket_id=?", (cm_id,)
        ).fetchone()
        return row["id"]


def get_sealed_products(
    product_type: Optional[str] = None,
    query: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return sealed products, optionally filtered by type / search."""
    sql = "SELECT * FROM sealed_products WHERE 1=1"
    params: List[Any] = []
    if product_type and product_type != "all":
        sql += " AND product_type=?"
        params.append(product_type)
    if query:
        sql += " AND (name LIKE ? COLLATE NOCASE OR set_name LIKE ? COLLATE NOCASE)"
        q = f"%{query}%"
        params.extend([q, q])
    sql += " ORDER BY product_type, name"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_sealed_product(product_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sealed_products WHERE id=?", (product_id,)
        ).fetchone()
        return dict(row) if row else None


def search_sealed_products(query: str, limit: int = 20) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sealed_products WHERE name LIKE ? COLLATE NOCASE "
            "ORDER BY name LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]


def record_sealed_snapshot(
    product_id: int,
    source: str,
    price: Optional[float],
    currency: Optional[str] = None,
    lowest_EU_only: Optional[float] = None,
    lowest_DE: Optional[float] = None,
    lowest_FR: Optional[float] = None,
    lowest_IT: Optional[float] = None,
    avg_7d: Optional[float] = None,
    avg_30d: Optional[float] = None,
    available_items: Optional[int] = None,
    snapshot_date: Optional[str] = None,
) -> None:
    if price is None:
        return
    snapshot_date = snapshot_date or datetime.now(timezone.utc).date().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sealed_price_snapshots
                (product_id, source, price, currency, lowest_EU_only, lowest_DE,
                 lowest_FR, lowest_IT, avg_7d, avg_30d, available_items, snapshot_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(product_id, source, snapshot_date) DO UPDATE SET
                price=excluded.price, currency=excluded.currency,
                lowest_EU_only=excluded.lowest_EU_only, lowest_DE=excluded.lowest_DE,
                lowest_FR=excluded.lowest_FR, lowest_IT=excluded.lowest_IT,
                avg_7d=excluded.avg_7d, avg_30d=excluded.avg_30d,
                available_items=excluded.available_items
            """,
            (product_id, source, price, currency, lowest_EU_only,
             lowest_DE, lowest_FR, lowest_IT, avg_7d, avg_30d,
             available_items, snapshot_date),
        )


def get_latest_sealed_prices(product_id: int) -> Dict[str, Dict[str, Any]]:
    """Return latest snapshot per source, e.g. {'cardmarket': {...}}."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.* FROM sealed_price_snapshots p
            JOIN (
                SELECT source, MAX(snapshot_date) AS maxd
                FROM sealed_price_snapshots WHERE product_id=? GROUP BY source
            ) m ON p.source=m.source AND p.snapshot_date=m.maxd
            WHERE p.product_id=?
            """,
            (product_id, product_id),
        ).fetchall()
        return {r["source"]: dict(r) for r in rows}


def get_sealed_history(
    product_id: int,
    source: Optional[str] = None,
    days: Optional[int] = None,
) -> List[Dict[str, Any]]:
    sql = ("SELECT source, price, currency, snapshot_date, lowest_EU_only, lowest_DE, "
           "lowest_FR, lowest_IT, avg_7d, avg_30d, available_items "
           "FROM sealed_price_snapshots WHERE product_id=?")
    params: List[Any] = [product_id]
    if source:
        sql += " AND source=?"
        params.append(source)
    if days:
        sql += " AND snapshot_date >= ?"
        params.append((datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat())
    sql += " ORDER BY snapshot_date ASC"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def sealed_snapshot_exists(date_str: str) -> int:
    """How many sealed snapshots exist for a given date (used to skip duplicate runs)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sealed_price_snapshots WHERE snapshot_date=?",
            (date_str,),
        ).fetchone()
        return row["n"]


def get_sealed_product_types() -> List[str]:
    """Return distinct product types in the DB (for filter UI)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT product_type FROM sealed_products "
            "WHERE product_type IS NOT NULL ORDER BY product_type"
        ).fetchall()
        return [r["product_type"] for r in rows]
