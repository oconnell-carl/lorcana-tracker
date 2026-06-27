"""Daily snapshot job: fetch and store current prices for all Lorcana cards.

Usage:
    python -m src.snapshot            # full run (sets + cards + prices)
    python -m src.snapshot --sets     # only refresh the set list
    python -m src.snapshot --prices   # only refresh prices for known cards

Designed to be run via cron once per day. Respects the free-tier quota by
tracking how many API calls it makes and stopping when nearing the limit.
"""

import argparse
import logging
import sys
import time
from datetime import datetime

from . import api as api_mod
from . import database

log = logging.getLogger("lorcana.snapshot")

# Conservative daily call budget (free RapidAPI tier = 100 req/day).
DAILY_CALL_BUDGET = 90


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


def refresh_cards(api: api_mod.CardmarketAPI, budget: Budget) -> int:
    sets = database.get_sets()
    total = 0
    for s in sets:
        log.info("Fetching cards for set %s (%s)...", s.get("name"), s["cardmarket_id"])
        cards = api.get_cards_in_set(s["cardmarket_id"])
        budget.spend(1)
        for c in cards:
            c["set_id"] = s["id"]
            database.upsert_card(c)
            total += 1
        # brief courtesy delay between set fetches
        time.sleep(0.5)
        if budget.used >= budget.limit:
            break
    log.info("Stored %d cards.", total)
    return total


def refresh_prices(api: api_mod.CardmarketAPI, budget: Budget) -> int:
    today = datetime.utcnow().date().isoformat()
    if database.snapshot_exists(today) > 0:
        log.info("Snapshots for %s already exist; continuing (will update).", today)

    card_ids = database.all_card_ids()
    log.info("Fetching prices for %d cards...", len(card_ids))
    stored = 0
    for card_id in card_ids:
        card = database.get_card(card_id)
        if not card or not card.get("cardmarket_id"):
            continue
        try:
            prices = api.get_card_prices(card["cardmarket_id"])
        except api_mod.APIError as e:
            log.warning("API error for card %s: %s", card_id, e)
            break
        budget.spend(2)  # card + trend calls (approx)
        cm = prices.get("cardmarket")
        if cm:
            database.record_snapshot(card_id, "cardmarket", cm.get("lowest_near_mint"), cm.get("currency", "EUR"))
            stored += 1
        tp = prices.get("tcgplayer")
        if tp:
            database.record_snapshot(card_id, "tcgplayer", tp.get("market_price"), tp.get("currency", "USD"))
            stored += 1
        psa = prices.get("psa10")
        if psa:
            database.record_snapshot(card_id, "psa10", psa.get("price"), psa.get("currency", "USD"))
            stored += 1

        # If the API returned trend series and we have no local history yet,
        # backfill so the graph isn't empty on first run.
        trend = prices.get("trend")
        if trend and database.get_history(card_id, days=40000).__len__() == 0:
            for point in trend:
                if point.get("date") and point.get("price") is not None:
                    database.record_snapshot(
                        card_id, "cardmarket", point["price"], cm.get("currency", "EUR") if cm else "EUR",
                        snapshot_date=point["date"][:10],
                    )

        time.sleep(0.3)
        if budget.used >= budget.limit:
            log.warning("Budget hit after %d cards.", stored)
            break
    log.info("Stored %d price snapshots.", stored)
    return stored


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Lorcana price snapshot job")
    parser.add_argument("--sets", action="store_true", help="Only refresh sets")
    parser.add_argument("--cards", action="store_true", help="Only refresh card lists")
    parser.add_argument("--prices", action="store_true", help="Only refresh prices")
    args = parser.parse_args(argv)

    database.init_db()
    api = api_mod.get_api()
    if not api.available:
        log.warning("No RAPIDAPI_KEY set; skipping live fetch. Cached data will be used.")
        return 0

    budget = Budget(DAILY_CALL_BUDGET)
    try:
        if args.sets or not (args.sets or args.cards or args.prices) and not args.prices and not args.cards:
            pass
        do_all = not (args.sets or args.cards or args.prices)
        if do_all or args.sets:
            refresh_sets(api, budget)
        if do_all or args.cards:
            refresh_cards(api, budget)
        if do_all or args.prices:
            refresh_prices(api, budget)
    except RuntimeError as e:
        log.warning("Stopped early: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
