"""Enrich cards table with subtitle data from the free Lorcana API (lorcana-api.com).

The RapidAPI Cardmarket endpoint only returns the character name (e.g. "Monterey Jack")
without the subtitle (e.g. "Good-Hearted Ranger"). The free Lorcana API at
api.lorcana-api.com returns the full card name including subtitle.

This script:
1. Adds `subtitle` and `full_name` columns to the cards table (if missing).
2. Fetches all cards from https://api.lorcana-api.com/cards/all
3. Matches cards by set code + card number.
4. Updates each card with the subtitle and full name.

Usage:
    python -m src.enrich_subtitles          # fetch + update
    python -m src.enrich_subtitles --dry-run # show what would change
"""

import json
import logging
import os
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.database import get_conn, _add_column_if_missing

log = logging.getLogger("lorcana.enrich")

# Map our set codes (with numeric prefix) to LorcanaJSON Set_ID codes (without prefix).
# Our codes: 1TFC, 2ROF, 3INK, 4URS, 5SSK, 6AZS, 7ARI, 8JAF, 9FAB, 10WHI, 11WSP, 12WIL
# API codes: TFC,  ROF,  INK,  URS,  SSK,  AZS,  ARI,  ROJ,  FAB,  WHI,  WIN,  WUN
SET_CODE_MAP = {
    "1TFC": "TFC",
    "2ROF": "ROF",
    "3INK": "INK",
    "4URS": "URS",
    "5SSK": "SSK",
    "6AZS": "AZS",
    "7ARI": "ARI",
    "8JAF": "ROJ",   # Reign of Jafar
    "9FAB": "FAB",
    "10WHI": "WHI",
    "11WSP": "WIN",  # Winterspell
    "12WIL": "WUN",  # Wilds Unknown
}

LORCANA_API_URL = "https://api.lorcana-api.com/cards/all"


def fetch_lorcana_cards() -> List[Dict[str, Any]]:
    """Fetch all cards from the free Lorcana API."""
    log.info("Fetching all cards from %s ...", LORCANA_API_URL)
    req = urllib.request.Request(LORCANA_API_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    log.info("Received %d cards from Lorcana API", len(data))
    return data


def parse_name(full_name: str) -> Tuple[str, str]:
    """Split 'Character Name - Subtitle' into (name, subtitle).

    Returns ("", "") if the name has no subtitle.
    Some cards have no subtitle (e.g. items, actions).
    """
    if " - " in full_name:
        parts = full_name.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    return full_name.strip(), ""


def build_lookup(lorcana_cards: List[Dict[str, Any]]) -> Dict[Tuple[str, int], Dict[str, str]]:
    """Build a lookup dict keyed by (Set_ID, Card_Num) -> {full_name, subtitle, name}."""
    lookup = {}
    for card in lorcana_cards:
        set_id = card.get("Set_ID", "")
        card_num = card.get("Card_Num")
        full_name = card.get("Name", "")
        if not set_id or card_num is None or not full_name:
            continue
        name, subtitle = parse_name(full_name)
        lookup[(set_id, int(card_num))] = {
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

    # Step 2: Fetch Lorcana API data
    lorcana_cards = fetch_lorcana_cards()
    lookup = build_lookup(lorcana_cards)
    log.info("Lookup built: %d cards from %d sets", len(lookup), len(SET_CODE_MAP))

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
            our_api_code = SET_CODE_MAP.get(set_code)

            if not our_api_code:
                # Set not in the free API (promo sets, quests, etc.)
                no_match += 1
                continue

            # Parse card_number — it's stored as TEXT, could be "181" or "181a"
            try:
                card_num_int = int(str(card_number).strip())
            except (ValueError, TypeError):
                no_match += 1
                continue

            key = (our_api_code, card_num_int)
            api_card = lookup.get(key)

            if not api_card:
                no_match += 1
                continue

            matched += 1

            # Skip if already has the same subtitle
            if row["subtitle"] == api_card["subtitle"] and row["full_name"] == api_card["full_name"]:
                skipped_already += 1
                continue

            if dry_run:
                old_subtitle = row["subtitle"] or "(none)"
                log.info(
                    "  UPDATE card %s (#%s): '%s' -> subtitle='%s', full_name='%s'",
                    row["name"], card_number, old_subtitle,
                    api_card["subtitle"], api_card["full_name"],
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
    log.info("Matched to Lorcana API: %d", matched)
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
