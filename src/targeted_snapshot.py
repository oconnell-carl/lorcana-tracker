"""Targeted daily snapshot: fetch prices for priority rarities (Iconic, Enchanted, Epic, Promo).

Strategy:
1. Phase 1: Fetch priority rarity pages from ALL sets — pages 10+ for sets with
   Enchanted/Iconic/Epic, all pages for small Promo-only sets. This ensures price
   data for every Iconic, Enchanted, Epic, and Promo card across all sets before
   spending API calls on lower rarities.
2. Phase 2: Full fetch remaining pages from daily priority sets (1, 5, 7-13).
3. Phase 3: If alternating day (odd day-of-year), full fetch remaining pages from
   alternating sets (2, 3, 4, 6).
4. Phase 4: Remaining pages from other sets (budget permitting).

Price storage:
- Priority rarities (Iconic, Enchanted, Epic, Promo): always stored.
- Secondary rarities (Legendary and below): only stored after Phase 1 completes
  for all sets with priority rarities.

Set tiers:
- Daily: Sets 1, 5, 7, 8, 9, 10, 11, 12, 13 — fetched every day
- Alternating: Sets 2, 3, 4, 6 — full fetch every 2nd day (odd day-of-year)
- Other: Sets 14+ — priority rarities in Phase 1, remaining pages in Phase 4

Usage:
    python -m src.targeted_snapshot
    python -m src.targeted_snapshot --budget 92
    python -m src.targeted_snapshot --dry-run
    python -m src.targeted_snapshot --max-runtime 600   # 10 min hard limit
    python -m src.targeted_snapshot --force              # re-run even if today's snapshot exists
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

# Priority rarities — always get price snapshots
PRIORITY_RARITIES = {"Iconic", "Enchanted", "Epic", "Promo"}

# Secondary rarities — only get price snapshots after all priority rarities
# across all sets are covered
SECONDARY_RARITIES = {
    "Legendary", "Super_rare", "SUPER RARE", "rare",
    "Uncommon", "Common", "Oversized", "Quest",
}

# Set tiers for fetch prioritisation
DAILY_SET_IDS = {1, 5, 7, 8, 9, 10, 11, 12, 13}
ALTERNATING_SET_IDS = {2, 3, 4, 6}

# Free RapidAPI tier = 100 req/day; sealed uses ~8, leaving 92 for cards
DEFAULT_BUDGET = 92

CARDS_PER_PAGE = 20

# Hard runtime limit (seconds) — prevents indefinite hangs
DEFAULT_MAX_RUNTIME = 600  # 10 minutes

# Per-request timeout (seconds)
REQUEST_TIMEOUT = 15.0

# Delay between requests (seconds) — courtesy to API
REQUEST_DELAY = 0.3

# Max consecutive failures before aborting a phase
MAX_CONSECUTIVE_FAILURES = 3

# Module-level flag: set to True after Phase 1 confirms all priority rarities covered
_store_secondary = False


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
    """Store card and its price snapshots.

    Always stores prices for PRIORITY_RARITIES (Iconic, Enchanted, Epic, Promo).
    Stores prices for SECONDARY_RARITIES (Legendary and below) only when
    the module-level _store_secondary flag is True (set after Phase 1 completes
    for all sets with priority rarities).
    """
    card["set_id"] = set_id
    card_id = database.upsert_card(card)
    stats["cards_stored"] += 1

    rarity = card.get("rarity", "")
    is_priority = rarity in PRIORITY_RARITIES
    is_secondary = _store_secondary and rarity in SECONDARY_RARITIES

    if not is_priority and not is_secondary:
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
        if not budget.can_spend(1):
            log.warning("Budget exhausted before page %d of set cm_id=%d", page, cm_id)
            break

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


def _get_priority_pages(s: Dict) -> List[int]:
    """Determine which pages to fetch for priority rarities in a set.

    For sets with Enchanted/Iconic/Epic: pages 10+ (where those cards live).
    For small/Promo-only sets: all pages (they're cheap).
    """
    total_pages = _get_total_pages(s.get("card_count", 0))
    if total_pages == 0:
        return []

    rars = set(database.get_rarities_in_set(s["id"]))
    has_high_priority = bool(rars & {"Enchanted", "Iconic", "Epic"})

    if has_high_priority:
        # Enchanted/Iconic/Epic are on pages 10+
        return list(range(10, total_pages + 1))
    elif "Promo" in rars:
        # Promo-only set — usually small, fetch all pages
        return list(range(1, total_pages + 1))
    else:
        # No priority rarities at all (e.g., Set 2)
        return []


def _get_remaining_pages(s: Dict, already_done: Set[int]) -> List[int]:
    """Get pages not yet fetched for a set.

    Returns pages in DESCENDING order (highest page first) so that higher-value
    cards (Super Rares, Legendaries on pages 8-9) are fetched before Commons
    and Uncommons on pages 1-7, in case budget runs out mid-phase.
    """
    total_pages = _get_total_pages(s.get("card_count", 0))
    if total_pages == 0:
        return []
    all_pages = set(range(1, total_pages + 1))
    return sorted(all_pages - already_done, reverse=True)


def main(argv=None) -> int:
    global _store_secondary

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

    # Determine alternating day (odd day-of-year = run alternating sets)
    day_of_year = datetime.now(timezone.utc).timetuple().tm_yday
    run_alternating = (day_of_year % 2 == 1)

    sets = database.get_sets()
    log.info("=== Targeted Snapshot for %s ===", today)
    log.info("Budget: %d API calls | Max runtime: %ds | Request timeout: %.0fs",
             args.budget, args.max_runtime, REQUEST_TIMEOUT)
    log.info("Priority rarities: %s", ", ".join(sorted(PRIORITY_RARITIES)))
    log.info("Secondary rarities: %s", ", ".join(sorted(SECONDARY_RARITIES)))
    log.info("Daily sets: %s", sorted(DAILY_SET_IDS))
    log.info("Alternating sets: %s (run today: %s)", sorted(ALTERNATING_SET_IDS), run_alternating)

    # Categorize sets
    # priority_target_sets: sets with card_count > 0 that have priority rarities
    # (used to determine if _store_secondary can be enabled after Phase 1)
    priority_target_sets: Set[int] = set()
    daily_sets: List[Dict] = []
    alternating_sets: List[Dict] = []
    other_sets: List[Dict] = []

    for s in sorted(sets, key=lambda x: x["id"]):
        sid = s["id"]
        rars = set(database.get_rarities_in_set(sid))
        card_count = s.get("card_count", 0) or 0

        if card_count > 0 and rars & PRIORITY_RARITIES:
            priority_target_sets.add(sid)

        if sid in DAILY_SET_IDS:
            daily_sets.append(s)
        elif sid in ALTERNATING_SET_IDS:
            alternating_sets.append(s)
        else:
            other_sets.append(s)

    log.info("Sets with priority rarities to cover: %s", sorted(priority_target_sets))
    log.info("Daily sets: %d | Alternating sets: %d | Other sets: %d",
             len(daily_sets), len(alternating_sets), len(other_sets))

    # Track pages fetched per set across phases
    pages_fetched_by_set: Dict[int, Set[int]] = {}

    # ============================================================
    # Phase 1: Priority rarity pages from ALL sets
    # ============================================================
    log.info("\n--- Phase 1: Priority rarity pages from ALL sets ---")
    phase1_sets = daily_sets + alternating_sets + other_sets
    phase1_covered: Set[int] = set()

    for s in phase1_sets:
        if runtime.expired():
            log.warning("Runtime limit reached during Phase 1")
            break

        sid = s["id"]
        target_pages = _get_priority_pages(s)
        if not target_pages:
            log.info("  Set %s (id=%d): no priority pages to fetch (card_count=%d)",
                     s["name"], sid, s.get("card_count", 0))
            # If this set has priority rarities but 0 cards, it's not in priority_target_sets
            # If it has priority rarities and cards but all on pages we can't determine, skip
            continue

        cm_id = s["cardmarket_id"]
        log.info("  Set %s (id=%d, cm_id=%d): fetching priority pages %s",
                 s["name"], sid, cm_id, target_pages)

        if args.dry_run:
            budget.spend(len(target_pages))
            pages_fetched_by_set[sid] = set(target_pages)
            phase1_covered.add(sid)
            continue

        count = _fetch_pages(api, cm_id, target_pages, budget, sid, today, stats, runtime)
        pages_fetched_by_set[sid] = set(target_pages[:count])
        if count > 0:
            phase1_covered.add(sid)
        log.info("    Got %d/%d pages from %s", count, len(target_pages), s["name"])

    log.info("Phase 1 complete. API calls used: %d/%d | Budget remaining: %d | Runtime: %.1fs",
             budget.used, budget.limit, budget.remaining(), runtime.elapsed())

    # Check if all priority target sets were covered in Phase 1
    all_priorities_covered = priority_target_sets.issubset(phase1_covered)
    if all_priorities_covered:
        log.info("✓ All priority rarities covered across all sets. Secondary rarity pricing enabled.")
        _store_secondary = True
    else:
        uncovered = priority_target_sets - phase1_covered
        log.warning("✗ Not all priority sets covered (missing: %s). Secondary pricing disabled.",
                    sorted(uncovered))
        _store_secondary = False

    # ============================================================
    # Phase 2: Remaining pages from DAILY_SETS (1, 5, 7-13)
    # ============================================================
    log.info("\n--- Phase 2: Remaining pages from daily sets ---")
    for s in daily_sets:
        if runtime.expired() or not budget.can_spend(1):
            log.warning("Budget/runtime exhausted, stopping Phase 2")
            break

        sid = s["id"]
        already_done = pages_fetched_by_set.get(sid, set())
        remaining = _get_remaining_pages(s, already_done)
        if not remaining:
            continue

        cm_id = s["cardmarket_id"]
        log.info("  Set %s (id=%d): %d remaining pages (budget: %d left, runtime: %.0fs left)",
                 s["name"], sid, len(remaining), budget.remaining(), runtime.remaining())

        if args.dry_run:
            budget.used += min(len(remaining), budget.remaining())
            continue

        count = _fetch_pages(api, cm_id, remaining, budget, sid, today, stats, runtime)
        pages_fetched_by_set[sid] = pages_fetched_by_set.get(sid, set()) | set(remaining[:count])
        log.info("    Got %d/%d pages from %s", count, len(remaining), s["name"])

    log.info("Phase 2 complete. API calls used: %d/%d | Budget remaining: %d | Runtime: %.1fs",
             budget.used, budget.limit, budget.remaining(), runtime.elapsed())

    # ============================================================
    # Phase 3: If alternating day, remaining pages from ALTERNATING_SETS (2, 3, 4, 6)
    # ============================================================
    if run_alternating:
        log.info("\n--- Phase 3: Remaining pages from alternating sets (alternating day) ---")
        for s in alternating_sets:
            if runtime.expired() or not budget.can_spend(1):
                log.warning("Budget/runtime exhausted, stopping Phase 3")
                break

            sid = s["id"]
            already_done = pages_fetched_by_set.get(sid, set())
            remaining = _get_remaining_pages(s, already_done)
            if not remaining:
                continue

            cm_id = s["cardmarket_id"]
            log.info("  Set %s (id=%d): %d remaining pages (budget: %d left, runtime: %.0fs left)",
                     s["name"], sid, len(remaining), budget.remaining(), runtime.remaining())

            if args.dry_run:
                budget.used += min(len(remaining), budget.remaining())
                continue

            count = _fetch_pages(api, cm_id, remaining, budget, sid, today, stats, runtime)
            pages_fetched_by_set[sid] = pages_fetched_by_set.get(sid, set()) | set(remaining[:count])
            log.info("    Got %d/%d pages from %s", count, len(remaining), s["name"])

        log.info("Phase 3 complete. API calls used: %d/%d | Budget remaining: %d | Runtime: %.1fs",
                 budget.used, budget.limit, budget.remaining(), runtime.elapsed())
    else:
        log.info("\n--- Phase 3: Skipped (not an alternating day) ---")

    # ============================================================
    # Phase 4: Remaining pages from other sets (budget permitting)
    # ============================================================
    log.info("\n--- Phase 4: Remaining pages from other sets ---")
    for s in other_sets:
        if runtime.expired() or not budget.can_spend(1):
            log.warning("Budget/runtime exhausted, stopping Phase 4")
            break

        sid = s["id"]
        already_done = pages_fetched_by_set.get(sid, set())
        remaining = _get_remaining_pages(s, already_done)
        if not remaining:
            continue

        cm_id = s["cardmarket_id"]
        affordable = remaining[:budget.remaining()]
        if not affordable:
            continue

        log.info("  Set %s (id=%d): %d remaining pages, fetching %d (budget: %d left, runtime: %.0fs left)",
                 s["name"], sid, len(remaining), len(affordable),
                 budget.remaining(), runtime.remaining())

        if args.dry_run:
            budget.used += len(affordable)
            continue

        count = _fetch_pages(api, cm_id, affordable, budget, sid, today, stats, runtime)
        pages_fetched_by_set[sid] = pages_fetched_by_set.get(sid, set()) | set(affordable[:count])
        log.info("    Got %d/%d pages from %s", count, len(affordable), s["name"])

    log.info("Phase 4 complete. API calls used: %d/%d | Runtime: %.1fs",
             budget.used, budget.limit, runtime.elapsed())

    # ============================================================
    # Summary
    # ============================================================
    log.info("\n=== Summary ===")
    log.info("API calls used: %d / %d", budget.used, budget.limit)
    log.info("Runtime: %.1fs / %ds", runtime.elapsed(), args.max_runtime)
    log.info("Cards stored: %d", stats["cards_stored"])
    log.info("Price snapshots stored: %d", stats["snapshots_stored"])
    log.info("Secondary rarity pricing: %s", "ENABLED" if _store_secondary else "DISABLED")
    for rarity in sorted(PRIORITY_RARITIES | SECONDARY_RARITIES):
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
