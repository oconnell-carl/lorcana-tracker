"""Enrich cards table with subtitle data from the official Ravensburger Lorcana API.

The RapidAPI Cardmarket endpoint only returns the character name (e.g. "Monterey Jack")
without the subtitle (e.g. "Good-Hearted Ranger"). The official Ravensburger API at
api.lorcana.ravensburger.com returns the subtitle as a separate field, and includes
ALL cards (including Epic/Enchanted/Iconic variants and promos).

This script:
1. Adds `subtitle` and `full_name` columns to the cards table (if missing).
2. Fetches all cards from the Ravensburger API (free, no key needed).
3. Matches cards by set number + card number (parsed from card_identifier).
4. Updates each card with the subtitle and full name.

Usage:
    python -m src.enrich_subtitles          # fetch + update
    python -m src.enrich_subtitles --dry-run # show what would change
"""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from src.database import get_conn, _add_column_if_missing

log = logging.getLogger("lorcana.enrich")

# Ravensburger API credentials (from the official Lorcana app, as used by LorcanaJSON)
RAVENSBURGER_TOKEN_URL = "https://sso.ravensburger.de/token"
RAVENSBURGER_CATALOG_URL = "https://api.lorcana.ravensburger.com/v3/catalog/en"
RAVENSBURGER_AUTH = "Basic bG9yY2FuYS1hcGktcmVhZDpFdkJrMzJkQWtkMzludWt5QVNIMHc2X2FJcVZEcHpJenVrS0lxcDlBNXRlb2c5R3JkQ1JHMUFBaDVSendMdERkYlRpc2k3THJYWDl2Y0FkSTI4S096dw=="
UNITY_VERSION = "2022.3.51f1"

# Map our set codes to Ravensburger set numbers (from card_identifier's last component)
# Our DB: 1TFC, 2ROF, 3INK, 4URS, 5SSK, 6AZS, 7ARI, 8JAF, 9FAB, 10WHI, 11WSP, 12WIL
# Also promos: D23, DIS, PR1, PR2, PR3, CH1, CH2, CC1, QU1, QU2
# Ravensburger card_identifier format: "NN/total EN set_num" or "NN/P3 EN set_num"
SET_CODE_TO_NUM = {
    "1TFC": 1, "2ROF": 2, "3INK": 3, "4URS": 4, "5SSK": 5,
    "6AZS": 6, "7ARI": 7, "8JAF": 8, "9FAB": 9, "10WHI": 10,
    "11WSP": 11, "12WIL": 12,
    # Quest sets use 'Q1'/'Q2' as set identifiers in Ravensburger API
    "QU1": "Q1", "QU2": "Q2",
}


def _get_ravensburger_token() -> str:
    """Get an OAuth token from the Ravensburger SSO."""
    resp = httpx.post(
        RAVENSBURGER_TOKEN_URL,
        headers={
            "authorization": RAVENSBURGER_AUTH,
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": f"UnityPlayer/{UNITY_VERSION} (UnityWebRequest/1.0, libcurl/8.10.1-DEV)",
            "x-unity-version": UNITY_VERSION,
        },
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_ravensburger_cards() -> List[Dict[str, Any]]:
    """Fetch all cards from the official Ravensburger Lorcana API."""
    log.info("Fetching token from Ravensburger SSO...")
    token = _get_ravensburger_token()

    log.info("Fetching card catalog from Ravensburger API...")
    resp = httpx.get(
        RAVENSBURGER_CATALOG_URL,
        headers={
            "authorization": f"Bearer {token}",
            "user-agent": f"UnityPlayer/{UNITY_VERSION} (UnityWebRequest/1.0, libcurl/8.10.1-DEV)",
            "x-unity-version": UNITY_VERSION,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    # Cards are grouped by category: actions, characters, items, locations
    cards_dict = data.get("cards", {})
    all_cards: List[Dict[str, Any]] = []
    for category, cards in cards_dict.items():
        all_cards.extend(cards)

    log.info("Received %d cards from Ravensburger API", len(all_cards))
    return all_cards


def parse_card_identifier(identifier: str) -> Tuple[Optional[int], Optional[Any]]:
    """Parse '205/204 EN 12' -> (card_num=205, set_num=12).
    Also handles '53/P3 EN 12' -> (53, 12).
    Also handles '18/35 EN Q2' -> (18, 'Q2').
    Returns (None, None) if unparseable.
    """
    if not identifier:
        return None, None
    parts = identifier.strip().split()
    if len(parts) < 3:
        return None, None
    # Card number is the first part before the slash
    try:
        card_num = int(parts[0].split("/")[0])
    except (ValueError, IndexError):
        return None, None
    # Set number is the last part (could be int or 'Q1'/'Q2')
    set_part = parts[-1]
    if set_part.isdigit():
        return card_num, int(set_part)
    # Quest sets like 'Q1', 'Q2'
    if set_part.startswith("Q") and set_part[1:].isdigit():
        return card_num, set_part
    return None, None


def build_lookup(ravensburger_cards: List[Dict[str, Any]]) -> Dict[Tuple[Any, int], Dict[str, str]]:
    """Build a lookup dict keyed by (set_num, card_num) -> {full_name, subtitle, name}."""
    lookup = {}
    for card in ravensburger_cards:
        identifier = card.get("card_identifier", "")
        card_num, set_num = parse_card_identifier(identifier)
        if card_num is None or set_num is None:
            continue

        name = card.get("name", "")
        subtitle = card.get("subtitle", "") or ""
        full_name = f"{name} - {subtitle}" if subtitle else name

        key = (set_num, card_num)
        # Only keep first occurrence (base card, not promo variant)
        if key not in lookup:
            lookup[key] = {
                "full_name": full_name,
                "name": name,
                "subtitle": subtitle,
            }

    return lookup


def run_enrichment(dry_run: bool = False) -> None:
    """Add subtitle columns and enrich all matching cards."""
    # Step 1: Migration — add columns if missing
    with get_conn() as conn:
        _add_column_if_missing(conn, "cards", "subtitle", "TEXT")
        _add_column_if_missing(conn, "cards", "full_name", "TEXT")
        conn.commit()
    log.info("Migration: subtitle + full_name columns ensured on cards table")

    # Step 2: Fetch Ravensburger API data
    ravensburger_cards = fetch_ravensburger_cards()
    lookup = build_lookup(ravensburger_cards)
    log.info("Lookup built: %d entries", len(lookup))

    # Step 3: Get our cards with set codes
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.name, c.card_number, c.subtitle, c.full_name,
                   s.code AS set_code, s.name AS set_name
            FROM cards c
            JOIN sets s ON c.set_id = s.id
            WHERE s.code IS NOT NULL
            """
        ).fetchall()
        log.info("Database has %d cards with set codes", len(rows))

        matched = 0
        updated = 0
        skipped_already = 0
        no_match = 0

        for row in rows:
            set_code = row["set_code"]
            card_number = row["card_number"]
            set_num = SET_CODE_TO_NUM.get(set_code)

            if not set_num:
                # Set not in the Ravensburger API mapping (promo/quest sets)
                # Try to infer from card_number patterns — skip for now
                no_match += 1
                continue

            # Parse card_number — stored as TEXT, could be "181" or "181a"
            try:
                card_num_int = int(str(card_number).strip())
            except (ValueError, TypeError):
                no_match += 1
                continue

            key = (set_num, card_num_int)
            api_card = lookup.get(key)

            if not api_card:
                no_match += 1
                continue

            matched += 1

            # Skip if already has the same subtitle and full_name
            if row["subtitle"] == api_card["subtitle"] and row["full_name"] == api_card["full_name"]:
                skipped_already += 1
                continue

            if dry_run:
                old_subtitle = row["subtitle"] or "(none)"
                old_full = row["full_name"] or "(none)"
                log.info(
                    "  UPDATE card %s (#%s): subtitle '%s'->'%s', full_name '%s'->'%s'",
                    row["name"], card_number, old_subtitle,
                    api_card["subtitle"], old_full, api_card["full_name"],
                )
                updated += 1
                continue

            conn.execute(
                """
                UPDATE cards
                SET subtitle = ?, full_name = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    api_card["subtitle"],
                    api_card["full_name"],
                    datetime.now(timezone.utc).isoformat(),
                    row["id"],
                ),
            )
            updated += 1

        if not dry_run:
            conn.commit()

    log.info("=== Enrichment Summary ===")
    log.info("Total cards in DB: %d", len(rows))
    log.info("Matched to Ravensburger API: %d", matched)
    log.info("Updated: %d", updated)
    log.info("Already enriched (skipped): %d", skipped_already)
    log.info("No match (promo/quest/parse): %d", no_match)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    dry_run = "--dry-run" in sys.argv
    run_enrichment(dry_run=dry_run)


if __name__ == "__main__":
    main()
