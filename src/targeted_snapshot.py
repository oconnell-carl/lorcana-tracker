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
    python -m src.targeted_snapshot --max-runtime 600   # 10 min hard limit
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
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

# Hard runtime limit (seconds) — prevents indefinite hangs
DEFAULT_MAX_RUNTIME = 600  # 10 minutes

# Per-request timeout (seconds) — lower than api.py default of 30s
REQUEST_TIMEOUT = 15.0

# Delay between requests (seconds) — courtesy to API
REQUEST_DELAY = 0.3

# Max consecutive failures before aborting a phase
MAX_CONSECUTIVE_FAILURES = 3


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
        # Don't raise — just log and let caller check can_spend()
        if self.used >= self.limit:
            log.warning("API call budget reached (%d/%d)", self.used, self.limit)


class RuntimeGuard:
    """Tracks elapsed time and signals when the runtime budget is exhausted."""

    def __init__(self, max_seconds: int) -> None:
        self.max_seconds = max_seconds
        self.start_time = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.start_time

    def expired(self) -> bool:
        return self.elapsed() >= self.max_seconds

    def remaining(self) -> float:
        return max(0.0, self.max_seconds - self.elapsed())


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
            cm.get("currency", "EUR"), snapshot_date=today,
            avg_7d=cm.get("7d_average"),
            avg_30d=cm.get("30d_average"),
            available_items=cm.get("available_items"),
            lowest_near_mint_raw=cm.get("lowest_near_mint"),
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


def _fetch_single_page(api: api_mod.CardmarketAPI, cm_id: int, page: int,
                       set_id: int, today: str, stats: Dict) -> bool:
    """Fetch a single page, store cards/prices. Returns True if data received, False on failure."""
    params = {"page": page} if page > 1 else None

    try:
        data = api._get(f"/lorcana/episodes/{cm_id}/cards", params=params)
    except api_mod.APIError as e:
        log.warning("API error on set cm_id=%d page %d: %s", cm_id, page, e)
        return False
    except Exception as e:
        log.warning("Unexpected error on set cm_id=%d page %d: %s", cm_id, page, e)
        return False

    if data is None:
        return False

    items = data.get("data", []) if isinstance(data, dict) else data
    if not items:
        return False

    for it in items:
        if not isinstance(it, dict):
            continue
        card = api._normalise_card(it)
        _store_card_with_prices(card, set_id, today, stats)

    return True


def _fetch_pages(api: api_mod.CardmarketAPI, cm_id: int, pages: List[int],
                 budget: Budget, set_id: int, today: str, stats: Dict,
                 runtime: RuntimeGuard) -> int:
    """Fetch specific pages from a set and store cards/prices.

    Returns the number of pages successfully fetched.
    """
    pages_fetched = 0
    consecutive_failures = 0

    for page in pages:
        # Check budget
        if not budget.can_spend(1):
            log.warning("Budget exhausted before page %d of set cm_id=%d", page, cm_id)
            break

        # Check runtime
        if runtime.expired():
            log.warning("Runtime limit (%ds) reached, stopping. elapsed=%.1fs",
                        runtime.max_seconds, runtime.elapsed())
            break

        success = _fetch_single_page(api, cm_id, page, set_id, today, stats)
        budget.spend(1)

        if success:
            pages_fetched += 1
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            log.warning("Failed to fetch page %d for set cm_id=%d (consecutive failures: %d)",
                        page, cm_id, consecutive_failures)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log.error("Too many consecutive failures (%d), aborting set cm_id=%d",
                          consecutive_failures, cm_id)
                break

        time.sleep(REQUEST_DELAY)

    return pages_fetched


def _get_total_pages(card_count: int) -> int:
    """Estimate total pages for a set based on card count."""
    if card_count <= 0:
        return 0
    return (card_count + CARDS_PER_PAGE - 1) // CARDS_PER_PAGE


def _already_snapshotted_today(today: str) -> int:
    """Check if today's snapshot already ran. Returns count of existing snapshots."""
    return database.snapshot_exists(today)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Targeted Lorcana price snapshot (priority rarities)")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="API call budget")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without making API calls")
    parser.add_argument("--max-runtime", type=int, default=DEFAULT_MAX_RUNTIME,
                        help=f"Hard runtime limit in seconds (default: {DEFAULT_MAX_RUNTIME})")
    parser.add_argument("--force", action="store_true",
                        help="Run even if snapshots already exist for today")
    args = parser.parse_args(argv)

    database.init_db()
    api = api_mod.get_api()
    if not api.available:
        log.warning("No RAPIDAPI_KEY set; skipping.")
        return 0

    # Use timezone-aware UTC instead of deprecated utcnow()
    today = datetime.now(timezone.utc).date().isoformat()

    # Idempotency: skip if already ran today (unless --force)
    existing = _already_snapshotted_today(today)
    if existing > 0 and not args.force:
        log.info("Today's snapshot already has %d records. Use --force to re-run. Skipping.", existing)
        return 0

    budget = Budget(args.budget)
    runtime = RuntimeGuard(args.max_runtime)
    stats = {"cards_stored": 0, "snapshots_stored": 0}

    # Override the API client timeout for this run
    try:
        api.client = api_mod.httpx.Client(timeout=REQUEST_TIMEOUT)
    except Exception:
        pass  # fall back to default timeout

    sets = database.get_sets()
    log.info("=== Targeted Snapshot for %s ===", today)
    log.info("Budget: %d API calls | Max runtime: %ds | Request timeout: %.0fs",
             args.budget, args.max_runtime, REQUEST_TIMEOUT)
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

    # Track what was fetched across phases for Phase 4
    pages_fetched_by_set: Dict[int, Set[int]] = {}

    # Phase 1: Fetch pages with Enchanted/Iconic from sets we already have
    log.info("\n--- Phase 1: Existing Enchanted/Iconic sets (targeted pages) ---")
    phase1_sets = sets_with_enchanted | sets_with_iconic
    for sid in sorted(phase1_sets):
        if runtime.expired():
            log.warning("Runtime limit reached before Phase 1 set %d", sid)
            break

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

        count = _fetch_pages(api, cm_id, target_pages, budget, sid, today, stats, runtime)
        pages_fetched_by_set[sid] = set(target_pages[:count])
        log.info("  Got %d pages from %s", count, s["name"])

    # Phase 2: Fetch all pages from sets that have Promo cards (usually small sets)
    log.info("\n--- Phase 2: Promo sets (full fetch) ---")
    for sid in sorted(sets_with_promo):
        if runtime.expired():
            log.warning("Runtime limit reached before Phase 2 set %d", sid)
            break
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

        count = _fetch_pages(api, cm_id, target_pages, budget, sid, today, stats, runtime)
        pages_fetched_by_set[sid] = set(target_pages[:count])
        log.info("  Got %d pages from %s", count, s["name"])

    # Phase 3: Fetch last ~5 pages from empty large sets (discover Enchanted/Iconic)
    log.info("\n--- Phase 3: Empty large sets (last 5 pages) ---")
    for sid in sorted(sets_empty):
        if runtime.expired():
            log.warning("Runtime limit reached before Phase 3 set %d", sid)
            break

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

        count = _fetch_pages(api, cm_id, target_pages, budget, sid, today, stats, runtime)
        pages_fetched_by_set[sid] = set(target_pages[:count])
        log.info("  Got %d pages from %s", count, s["name"])

    # Phase 4: If budget remains, fetch remaining pages from sets with cards
    log.info("\n--- Phase 4: Remaining budget for full price refresh ---")
    for sid in sorted(sets_with_cards):
        if runtime.expired() or not budget.can_spend(1):
            break

        s = database.get_set(sid)
        if not s:
            continue
        cm_id = s["cardmarket_id"]
        total_pages = _get_total_pages(s.get("card_count", 0))
        # Fetch pages we haven't done yet in phase 1
        all_pages = set(range(1, total_pages + 1))
        already_done = pages_fetched_by_set.get(sid, set())
        if sid in phase1_sets:
            already_done |= set(range(10, total_pages + 1))
        remaining_pages = sorted(all_pages - already_done)

        if not remaining_pages:
            continue

        # Only fetch as many pages as budget allows
        affordable = remaining_pages[:budget.remaining()]
        if not affordable:
            log.info("  No budget remaining, skipping %s", s["name"])
            continue

        log.info("Set %s (cm_id=%d): fetching %d/%d remaining pages (budget: %d left, runtime: %.0fs left)",
                 s["name"], cm_id, len(affordable), len(remaining_pages),
                 budget.remaining(), runtime.remaining())
        if args.dry_run:
            budget.used += len(affordable)
            continue

        count = _fetch_pages(api, cm_id, affordable, budget, sid, today, stats, runtime)
        log.info("  Got %d pages from %s", count, s["name"])

    # Phase 5: If budget still remains, fetch first pages from empty large sets
    log.info("\n--- Phase 5: Fill in empty large sets ---")
    for sid in sorted(sets_empty):
        if runtime.expired() or budget.used >= budget.limit:
            break

        s = database.get_set(sid)
        if not s:
            continue
        cm_id = s["cardmarket_id"]
        total_pages = _get_total_pages(s.get("card_count", 0))
        if total_pages <= 5:
            continue  # already fully fetched in phase 3

        # Fetch pages 1 through (total-5), minus any already done in phase 3
        already_done = pages_fetched_by_set.get(sid, set())
        first_pages = [p for p in range(1, total_pages - 4) if p not in already_done]
        if not first_pages:
            continue

        affordable = first_pages[:budget.remaining()]
        if not affordable:
            continue

        log.info("Set %s (cm_id=%d): fetching %d first pages (budget: %d left, runtime: %.0fs left)",
                 s["name"], cm_id, len(affordable), budget.remaining(), runtime.remaining())
        if args.dry_run:
            budget.used += len(affordable)
            continue

        count = _fetch_pages(api, cm_id, affordable, budget, sid, today, stats, runtime)
        log.info("  Got %d pages from %s", count, s["name"])

    # Summary
    log.info("\n=== Summary ===")
    log.info("API calls used: %d / %d", budget.used, budget.limit)
    log.info("Runtime: %.1fs / %ds", runtime.elapsed(), runtime.max_seconds)
    log.info("Cards stored: %d", stats["cards_stored"])
    log.info("Price snapshots stored: %d", stats["snapshots_stored"])
    for rarity in PRIORITY_RARITIES:
        key = f"rarity_{rarity}_cards"
        if key in stats:
            log.info("  %s cards with prices: %d", rarity, stats[key])

    # Enrich names (free API, no budget impact)
    if not runtime.expired():
        log.info("\nEnriching card names from lorcana-api.com...")
        try:
            from .snapshot import enrich_card_names
            enriched = enrich_card_names()
            log.info("Enriched %d card names.", enriched)
        except Exception as e:
            log.warning("Card name enrichment failed: %s", e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
