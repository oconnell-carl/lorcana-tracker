"""Targeted daily snapshot: fetch prices for priority rarities (Iconic, Enchanted, Promo).

Strategy:
1. Fetch sets we already have that contain Enchanted/Iconic — only pages 10-13 (where those cards live)
2. Fetch all Promo sets (small, cheap)
3. Fetch last ~5 pages from empty large sets (to discover Enchanted/Iconic)
4. Fetch all pages from empty small sets
5. If budget remains, fetch more pages from empty large sets

API returns 20 cards per page. Enchanted cards are typically card #200+, Iconic #240+.
So Enchanted are on pages 11-12, Iconic on page 13.

Usage:
    python -m src.targeted_snapshot
    python -m src.targeted_snapshot --budget 90
    python -m src.targeted_snapshot --dry-run
"""

import argparse
import logging
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Set

from dotenv import load_dotenv

load_dotenv()

from . import api as api_mod
from . import database

log = logging.getLogger("lorcana.targeted_snapshot")

# Priority rarities — only these get price snapshots stored
PRIORITY_RARITIES = {"Iconic", "Enchanted", "Promo"}

# Free RapidAPI tier = 100 req/day
DEFAULT_BUDGET = 95  # leave 5 as safety margin

CARDS_PER_PAGE = 20


class Budget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def can_spend(self, n: int = 1) -> bool:
        return self.used + n <= self.limit

    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def spend(self, n: int = 1) -> None:
        self.used += n
        if self.used >= self.limit:
            raise RuntimeError(f"API call budget reached ({self.limit}); stopping.")


def _store_card_with_prices(card: Dict, set_id: int, today: str, stats: Dict) -> None:
    """Store card and its price snapshots (only for priority rarities)."""
    card["set_id"] = set_id
    card_id = database.upsert_card(card)
    stats["cards_stored"] += 1

    rarity = card.get("rarity", "")
    if rarity not in PRIORITY_RARITIES:
        return

    stats[f"rarity_{rarity}_cards"] = stats.get(f"rarity_{rarity}_cards", 0) + 1

    prices = card.get("prices", {})
    
    # Cardmarket price
    cm = prices.get("cardmarket")
    if cm and cm.get("lowest_near_mint") is not None:
        eu_price = cm.get("lowest_near_mint_EU_only")
        price_to_store = eu_price if eu_price is not None else cm["lowest_near_mint"]
        database.record_snapshot(
            card_id, "cardmarket", price_to_store,
            cm.get("currency", "EUR"), snapshot_date=today
        )
        stats["snapshots_stored"] += 1

    # TCGPlayer price
    tp = prices.get("tcgplayer")
    if tp and tp.get("market_price") is not None:
        database.record_snapshot(
            card_id, "tcgplayer", tp["market_price"],
            tp.get("currency", "USD"), snapshot_date=today
        )
        stats["snapshots_stored"] += 1

    # PSA 10 price
    psa = prices.get("psa10")
    if psa and psa.get("price") is not None:
        database.record_snapshot(
            card_id, "psa10", psa["price"],
            psa.get("currency", "EUR"), snapshot_date=today
        )
        stats["snapshots_stored"] += 1


def _fetch_pages(api: api_mod.CardmarketAPI, cm_id: int, pages: List[int], 
                 budget: Budget, set_id: int, today: str, stats: Dict) -> int:
    """Fetch specific pages from a set and store cards/prices."""
    cards_fetched = 0
    for page in pages:
        if not budget.can_spend(1):
            log.warning("Budget exhausted before page %d of set cm_id=%d", page, cm_id)
            break
        
        params = {"page": page} if page > 1 else None
        data = api._get(f"/lorcana/episodes/{cm_id}/cards", params=params)
        budget.spend(1)
        
        if data is None:
            break
        
        items = data.get("data", []) if isinstance(data, dict) else data
        if not items:
            break
        
        for it in items:
            if not isinstance(it, dict):
                continue
            card = api._normalise_card(it)
            _store_card_with_prices(card, set_id, today, stats)
            cards_fetched += 1
        
        time.sleep(0.3)
    
    return cards_fetched


def _get_total_pages(card_count: int) -> int:
    """Estimate total pages for a set based on card count."""
    if card_count <= 0:
        return 0
    return (card_count + CARDS_PER_PAGE - 1) // CARDS_PER_PAGE


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Targeted Lorcana price snapshot (priority rarities)")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="API call budget")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without making API calls")
    args = parser.parse_args(argv)

    database.init_db()
    api = api_mod.get_api()
    if not api.available:
        log.warning("No RAPIDAPI_KEY set; skipping.")
        return 0

    budget = Budget(args.budget)
    today = datetime.utcnow().date().isoformat()
    stats = {"cards_stored": 0, "snapshots_stored": 0}

    sets = database.get_sets()
    log.info("=== Targeted Snapshot for %s ===", today)
    log.info("Budget: %d API calls", args.budget)
    log.info("Priority rarities: %s", ", ".join(PRIORITY_RARITIES))

    # Categorize sets
    sets_with_enchanted: Set[int] = set()
    sets_with_iconic: Set[int] = set()
    sets_with_promo: Set[int] = set()
    sets_with_cards: Set[int] = set()
    sets_empty: Set[int] = set()

    for s in sets:
        sid = s["id"]
        rarities = set(database.get_rarities_in_set(sid))

        if "Enchanted" in rarities:
            sets_with_enchanted.add(sid)
        if "Iconic" in rarities:
            sets_with_iconic.add(sid)
        if "Promo" in rarities:
            sets_with_promo.add(sid)
        
        card_count = s.get("card_count", 0) or 0
        if card_count > 0 and not rarities:
            sets_empty.add(sid)
        elif card_count > 0:
            sets_with_cards.add(sid)

    log.info("Sets with Enchanted: %s", sets_with_enchanted)
    log.info("Sets with Iconic: %s", sets_with_iconic)
    log.info("Sets with Promo: %s", sets_with_promo)
    log.info("Sets with cards: %s (%d sets)", sets_with_cards, len(sets_with_cards))
    log.info("Sets empty: %s (%d sets)", sets_empty, len(sets_empty))

    # Phase 1: Fetch pages with Enchanted/Iconic from sets we already have
    log.info("\n--- Phase 1: Existing Enchanted/Iconic sets (targeted pages) ---")
    phase1_sets = sets_with_enchanted | sets_with_iconic
    for sid in sorted(phase1_sets):
        s = database.get_set(sid)
        if not s:
            continue
        cm_id = s["cardmarket_id"]
        total_pages = _get_total_pages(s.get("card_count", 0))
        # Enchanted are on pages 10-12, Iconic on page 13
        target_pages = [p for p in range(10, total_pages + 1)]
        
        log.info("Set %s (cm_id=%d): fetching pages %s", s["name"], cm_id, target_pages)
        if args.dry_run:
            budget.spend(len(target_pages))
            continue
        
        count = _fetch_pages(api, cm_id, target_pages, budget, sid, today, stats)
        log.info("  Got %d cards from %d pages", count, len(target_pages))

    # Phase 2: Fetch all pages from sets that have Promo cards (usually small sets)
    log.info("\n--- Phase 2: Promo sets (full fetch) ---")
    for sid in sorted(sets_with_promo):
        if sid in phase1_sets:
            continue  # already fetched in phase 1
        s = database.get_set(sid)
        if not s:
            continue
        cm_id = s["cardmarket_id"]
        total_pages = _get_total_pages(s.get("card_count", 0))
        target_pages = list(range(1, total_pages + 1))
        
        log.info("Set %s (cm_id=%d): fetching pages %s", s["name"], cm_id, target_pages)
        if args.dry_run:
            budget.spend(len(target_pages))
            continue
        
        count = _fetch_pages(api, cm_id, target_pages, budget, sid, today, stats)
        log.info("  Got %d cards from %d pages", count, len(target_pages))

    # Phase 3: Fetch last ~5 pages from empty large sets (discover Enchanted/Iconic)
    log.info("\n--- Phase 3: Empty large sets (last 5 pages) ---")
    for sid in sorted(sets_empty):
        s = database.get_set(sid)
        if not s:
            continue
        cm_id = s["cardmarket_id"]
        total_pages = _get_total_pages(s.get("card_count", 0))
        if total_pages <= 5:
            # Small set, fetch all
            target_pages = list(range(1, total_pages + 1))
        else:
            # Large set, fetch last 5 pages
            target_pages = list(range(max(1, total_pages - 4), total_pages + 1))
        
        log.info("Set %s (cm_id=%d): fetching pages %s", s["name"], cm_id, target_pages)
        if args.dry_run:
            budget.spend(len(target_pages))
            continue
        
        count = _fetch_pages(api, cm_id, target_pages, budget, sid, today, stats)
        log.info("  Got %d cards from %d pages", count, len(target_pages))

    # Phase 4: If budget remains, fetch remaining pages from sets with cards (to get today's prices for all)
    log.info("\n--- Phase 4: Remaining budget for full price refresh ---")
    for sid in sorted(sets_with_cards):
        if not budget.can_spend(1):
            break
        s = database.get_set(sid)
        if not s:
            continue
        cm_id = s["cardmarket_id"]
        total_pages = _get_total_pages(s.get("card_count", 0))
        # Fetch pages we haven't done yet in phase 1
        all_pages = set(range(1, total_pages + 1))
        phase1_done = set(range(10, total_pages + 1)) if sid in phase1_sets else set()
        remaining_pages = sorted(all_pages - phase1_done)
        
        if not remaining_pages:
            continue
        
        # Only fetch as many pages as budget allows
        affordable = remaining_pages[:budget.remaining()]
        if not affordable:
            log.info("  No budget remaining, skipping")
            continue
        
        log.info("Set %s (cm_id=%d): fetching %d/%d remaining pages", s["name"], cm_id, len(affordable), len(remaining_pages))
        if args.dry_run:
            budget.used += len(affordable)
            continue
        
        count = _fetch_pages(api, cm_id, affordable, budget, sid, today, stats)
        log.info("  Got %d cards from %d pages", count, len(affordable))

    # Phase 5: If budget still remains, fetch first pages from empty large sets
    log.info("\n--- Phase 5: Fill in empty large sets ---")
    for sid in sorted(sets_empty):
        if budget.used >= budget.limit:
            break
        s = database.get_set(sid)
        if not s:
            continue
        cm_id = s["cardmarket_id"]
        total_pages = _get_total_pages(s.get("card_count", 0))
        if total_pages <= 5:
            continue  # already fully fetched in phase 3
        # Fetch pages 1 through (total-5)
        first_pages = list(range(1, total_pages - 4))
        log.info("Set %s (cm_id=%d): fetching first %d pages", s["name"], cm_id, len(first_pages))
        if args.dry_run:
            affordable = first_pages[:budget.remaining()]
            budget.used += len(affordable)
            continue
        
        count = _fetch_pages(api, cm_id, first_pages, budget, sid, today, stats)
        log.info("  Got %d cards from %d pages", count, len(first_pages))

    # Summary
    log.info("\n=== Summary ===")
    log.info("API calls used: %d / %d", budget.used, budget.limit)
    log.info("Cards stored: %d", stats["cards_stored"])
    log.info("Price snapshots stored: %d", stats["snapshots_stored"])
    for rarity in PRIORITY_RARITIES:
        key = f"rarity_{rarity}_cards"
        if key in stats:
            log.info("  %s cards with prices: %d", rarity, stats[key])

    # Enrich names (free API, no budget impact)
    log.info("\nEnriching card names from lorcana-api.com...")
    from .snapshot import enrich_card_names
    enriched = enrich_card_names()
    log.info("Enriched %d card names.", enriched)

    return 0


if __name__ == "__main__":
    sys.exit(main())
