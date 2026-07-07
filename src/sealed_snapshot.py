"""Daily snapshot for sealed Lorcana products.

Fetches all sealed products (Booster Boxes, Decks, TCG boxes, etc.) from the
Cardmarket API and stores both the product metadata and a daily price snapshot.

Strategy:
1. Fetch all sealed products via paginated /lorcana/products (8 pages x 20 = ~140 products)
2. Upsert each product into the DB
3. Record a price snapshot for each product (lowest, 7d_avg, 30d_avg, per-country lows)

Usage:
    python -m src.sealed_snapshot
    python -m src.sealed_snapshot --budget 10      # leave margin over 8 pages
    python -m src.sealed_snapshot --force          # re-run even if today's snapshot exists
    python -m src.sealed_snapshot --dry-run
    python -m src.sealed_snapshot --max-runtime 600
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from . import api as api_mod
from . import database

log = logging.getLogger("lorcana.sealed_snapshot")

# Each page of /lorcana/products returns 20 products; ~8 pages total (~140 products).
PRODUCTS_PER_PAGE = 20

# Free RapidAPI tier = 100 req/day; sealed products share the daily budget.
# Default to a budget that covers one full run with margin.
DEFAULT_BUDGET = 10

# Hard runtime limit (seconds) — prevents indefinite hangs
DEFAULT_MAX_RUNTIME = 600  # 10 minutes

# Per-request timeout (seconds) — lower than api.py default of 30s
REQUEST_TIMEOUT = 15.0

# Delay between requests (seconds) — courtesy to API
REQUEST_DELAY = 0.3


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


def _record_product_snapshot(product: Dict, today: str, stats: Dict) -> None:
    """Store product and its daily price snapshot (Cardmarket only)."""
    product_id = database.upsert_sealed_product(product)
    stats["products_stored"] += 1

    prices = product.get("prices", {})
    cm = prices.get("cardmarket")
    if cm is None or cm.get("lowest") is None:
        return

    database.record_sealed_snapshot(
        product_id, "cardmarket",
        price=cm.get("lowest"),
        currency=cm.get("currency", "EUR"),
        lowest_EU_only=cm.get("lowest_EU_only"),
        lowest_DE=cm.get("lowest_DE"),
        lowest_FR=cm.get("lowest_FR"),
        lowest_IT=cm.get("lowest_IT"),
        avg_7d=cm.get("7d_average"),
        avg_30d=cm.get("30d_average"),
        available_items=cm.get("available_items"),
        snapshot_date=today,
    )
    stats["snapshots_stored"] += 1


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Sealed Lorcana product price snapshot")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                        help=f"API call budget (default: {DEFAULT_BUDGET}, ~1 per page)")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without making API calls")
    parser.add_argument("--max-runtime", type=int, default=DEFAULT_MAX_RUNTIME,
                        help=f"Hard runtime limit in seconds (default: {DEFAULT_MAX_RUNTIME})")
    parser.add_argument("--force", action="store_true",
                        help="Run even if snapshots already exist for today")
    args = parser.parse_args(argv)

    database.init_db()
    api = api_mod.get_api()
    if not api.available:
        log.warning("No RAPIDAPI_KEY set; skipping live fetch. Cached data will be used.")
        return 0

    today = datetime.now(timezone.utc).date().isoformat()

    # Idempotency: skip if already ran today (unless --force)
    existing = database.sealed_snapshot_exists(today)
    if existing > 0 and not args.force:
        log.info("Today's sealed snapshot already has %d records. Use --force to re-run. Skipping.",
                 existing)
        return 0

    budget = Budget(args.budget)
    runtime = RuntimeGuard(args.max_runtime)
    stats = {"products_stored": 0, "snapshots_stored": 0}

    # Override the API client timeout for this run
    try:
        api.client = api_mod.httpx.Client(timeout=REQUEST_TIMEOUT)
    except Exception:
        pass

    log.info("=== Sealed Products Snapshot for %s ===", today)
    log.info("Budget: %d API calls | Max runtime: %ds | Request timeout: %.0fs",
             args.budget, args.max_runtime, REQUEST_TIMEOUT)

    if args.dry_run:
        # Estimate pages: we know it's ~140 products / 20 per page = 7 pages, round up to 8
        est_pages = 8
        log.info("[DRY RUN] Would fetch %d pages of /lorcana/products and store %d products.",
                 est_pages, est_pages * PRODUCTS_PER_PAGE)
        return 0

    # Fetch all pages; /lorcana/products is paginated 20 per page
    page = 1
    consecutive_failures = 0
    while True:
        if runtime.expired():
            log.warning("Runtime limit (%ds) reached, stopping. elapsed=%.1fs",
                        runtime.max_seconds, runtime.elapsed())
            break
        if not budget.can_spend(1):
            log.warning("API call budget exhausted before page %d", page)
            break

        params = {"page": page} if page > 1 else None
        log.info("Fetching /lorcana/products page %d (budget: %d left, runtime: %.0fs left)...",
                 page, budget.remaining(), runtime.remaining())

        try:
            data = api._get("/lorcana/products", params=params)
        except api_mod.APIError as e:
            log.warning("API error on page %d: %s", page, e)
            consecutive_failures += 1
            budget.spend(1)
            if consecutive_failures >= 3:
                log.error("Too many consecutive failures, aborting.")
                break
            time.sleep(REQUEST_DELAY)
            continue

        budget.spend(1)

        if data is None:
            consecutive_failures += 1
            log.warning("No data returned for page %d (consecutive failures: %d)",
                        page, consecutive_failures)
            if consecutive_failures >= 3:
                log.error("Too many consecutive failures, aborting.")
                break
            time.sleep(REQUEST_DELAY)
            continue

        items = data.get("data", []) if isinstance(data, dict) else data
        if not items:
            log.info("Page %d returned no items — end of pagination.", page)
            break

        consecutive_failures = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            product = api._normalise_sealed_product(it)
            _record_product_snapshot(product, today, stats)

        log.info("  Stored %d products from page %d (total so far: %d)",
                 len(items), page, stats["products_stored"])

        if len(items) < PRODUCTS_PER_PAGE:
            log.info("Page %d returned %d items (< %d) — last page.",
                     page, len(items), PRODUCTS_PER_PAGE)
            break

        page += 1
        time.sleep(REQUEST_DELAY)

    log.info("\n=== Sealed Products Snapshot Summary ===")
    log.info("API calls used: %d / %d", budget.used, budget.limit)
    log.info("Runtime: %.1fs / %ds", runtime.elapsed(), runtime.max_seconds)
    log.info("Products stored: %d", stats["products_stored"])
    log.info("Price snapshots stored: %d", stats["snapshots_stored"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
