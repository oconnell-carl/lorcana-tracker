"""Daily snapshot job: fetch and store current prices for all Lorcana cards.

Usage:
    python -m src.snapshot            # full run (sets + cards + prices)
    python -m src.snapshot --sets     # only refresh the set list
    python -m src.snapshot --cards    # only refresh card lists (includes prices)
    python -m src.snapshot --prices   # re-snapshot prices for existing cards

Designed to be run via cron once per day. Respects the free-tier quota by
tracking how many API calls it makes and stopping when nearing the limit.
"""

import argparse
import logging
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from . import api as api_mod
from . import database

log = logging.getLogger("lorcana.snapshot")

# Free Lorcana API for card metadata (subtitles, etc.)
LORCANA_API_URL = "https://api.lorcana-api.com/cards/all"

# Free RapidAPI tier = 100 req/day. Each set page = 1 call.
# 20 sets × ~1-2 pages each = ~30 calls. Keep budget for re-runs.
DAILY_CALL_BUDGET = 95


class Budget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def spend(self, n: int = 1) -> None:
        self.used += n
        if self.used >= self.limit:
            raise RuntimeError(f"API call budget reached ({self.limit}); stopping.")


def refresh_sets(api: api_mod.CardmarketAPI, budget: Budget) -> int:
    log.info("Fetching Lorcana sets...")
    sets = api.get_sets()
    budget.spend(1)
    count = 0
    for s in sets:
        database.upsert_set(s)
        count += 1
    log.info("Stored %d sets.", count)
    return count


def refresh_cards_and_prices(api: api_mod.CardmarketAPI, budget: Budget) -> int:
    """Fetch all cards from all sets. Prices come inline with card listing."""
    sets = database.get_sets()
    total_cards = 0
    total_snapshots = 0
    today = datetime.now(timezone.utc).date().isoformat()

    for s in sets:
        cm_id = s["cardmarket_id"]
        log.info("Fetching cards for set %s (%s)...", s.get("name"), cm_id)
        try:
            cards = api.get_cards_in_set(cm_id)
        except api_mod.APIError as e:
            log.warning("API error for set %s: %s", cm_id, e)
            break
        budget.spend(1)  # at least 1 call per set (may be more with pagination)

        for card in cards:
            card["set_id"] = s["id"]
            card_id = database.upsert_card(card)
            total_cards += 1

            # Store price snapshots from inline prices
            prices = card.get("prices", {})
            cm = prices.get("cardmarket")
            if cm and cm.get("lowest_near_mint") is not None:
                # Use EU_only price if available (closer to English-only)
                eu_price = cm.get("lowest_near_mint_EU_only")
                price_to_store = eu_price if eu_price is not None else cm["lowest_near_mint"]
                database.record_snapshot(
                    card_id, "cardmarket", price_to_store,
                    cm.get("currency", "EUR"), snapshot_date=today,
                    avg_7d=cm.get("7d_average"),
                    avg_30d=cm.get("30d_average"),
                    available_items=cm.get("available_items"),
                    lowest_near_mint_raw=cm.get("lowest_near_mint"),
                )
                total_snapshots += 1

            tp = prices.get("tcgplayer")
            if tp and tp.get("market_price") is not None:
                database.record_snapshot(
                    card_id, "tcgplayer", tp["market_price"],
                    tp.get("currency", "USD"), snapshot_date=today
                )
                total_snapshots += 1

            psa = prices.get("psa10")
            if psa and psa.get("price") is not None:
                database.record_snapshot(
                    card_id, "psa10", psa["price"],
                    psa.get("currency", "EUR"), snapshot_date=today
                )
                total_snapshots += 1

        time.sleep(0.5)  # courtesy delay between sets

        if budget.used >= budget.limit:
            log.warning("Budget hit after set %s.", s.get("name"))
            break

    log.info("Stored %d cards, %d price snapshots.", total_cards, total_snapshots)
    return total_snapshots


def refresh_prices_only(api: api_mod.CardmarketAPI, budget: Budget) -> int:
    """Re-fetch card listings to get fresh prices (same as cards+prices)."""
    return refresh_cards_and_prices(api, budget)


def enrich_card_names() -> int:
    """Enrich card names with subtitles from the free lorcana-api.com.
    e.g. 'Buzz Lightyear' -> 'Buzz Lightyear - Space Ranger'

    Updates the `full_name` and `subtitle` columns on the cards table.
    The `name` column is left as the base character name.
    """
    from .enrich_subtitles import run_enrichment
    import io
    import contextlib

    # Capture log output from the enrichment module
    log.info("Enriching card names from lorcana-api.com...")
    try:
        run_enrichment(dry_run=False)
        # Return approximate count (the function logs details)
        import sqlite3
        conn = sqlite3.connect(database.DB_PATH)
        count = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE full_name IS NOT NULL AND full_name != ''"
        ).fetchone()[0]
        conn.close()
        log.info("Enriched %d card names.", count)
        return count
    except Exception as e:
        log.warning("Card name enrichment failed: %s", e)
        return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Lorcana price snapshot job")
    parser.add_argument("--sets", action="store_true", help="Only refresh sets")
    parser.add_argument("--cards", action="store_true", help="Refresh card lists and prices")
    parser.add_argument("--prices", action="store_true", help="Re-snapshot prices only")
    args = parser.parse_args(argv)

    database.init_db()
    api = api_mod.get_api()
    if not api.available:
        log.warning("No RAPIDAPI_KEY set; skipping live fetch. Cached data will be used.")
        return 0

    budget = Budget(DAILY_CALL_BUDGET)
    try:
        do_all = not (args.sets or args.cards or args.prices)
        if do_all or args.sets:
            refresh_sets(api, budget)
        if do_all or args.cards or args.prices:
            refresh_cards_and_prices(api, budget)
        # Always enrich names (free API, no rate limit)
        enrich_card_names()
    except RuntimeError as e:
        log.warning("Stopped early: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
