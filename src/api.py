"""RapidAPI client for cardmarket-api-tcg (Disney Lorcana prices).

Confirmed endpoints (tested 2026-06-27):
  GET /lorcana/episodes                        -> {"data": [...sets...]}
  GET /lorcana/episodes/{id}/cards             -> {"data": [...cards with prices...]}
  GET /lorcana/episodes/{id}/cards?page=2      -> pagination
  GET /lorcana/cards/{id}                      -> single card detail (rate-limited)

Cards come with prices inline in the episode cards listing. The card detail
endpoint may provide more data but is heavily rate-limited on the free tier.
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger("lorcana.api")

BASE_URL = os.environ.get(
    "RAPIDAPI_BASE_URL", "https://cardmarket-api-tcg.p.rapidapi.com"
)
DEFAULT_HOST = os.environ.get("RAPIDAPI_HOST", "cardmarket-api-tcg.p.rapidapi.com")
TIMEOUT = 30.0


class APIError(RuntimeError):
    pass


class CardmarketAPI:
    def __init__(self) -> None:
        self.key = os.environ.get("RAPIDAPI_KEY", "").strip()
        self.host = os.environ.get("RAPIDAPI_HOST", DEFAULT_HOST).strip()
        self.client = httpx.Client(timeout=TIMEOUT)

    @property
    def available(self) -> bool:
        return bool(self.key)

    def _headers(self) -> Dict[str, str]:
        return {
            "X-RapidAPI-Key": self.key,
            "X-RapidAPI-Host": self.host,
            "Accept": "application/json",
        }

    def _get(self, path: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Make a GET request, return parsed JSON or None."""
        if not self.available:
            log.warning("No RAPIDAPI_KEY set; skipping API call.")
            return None
        url = f"{BASE_URL}{path}"
        try:
            r = self.client.get(url, headers=self._headers(), params=params)
        except httpx.HTTPError as e:
            log.warning("Request error %s: %s", path, e)
            return None

        if r.status_code == 200:
            return r.json()
        elif r.status_code == 429:
            log.warning("Rate limited by RapidAPI (429), waiting 60s...")
            time.sleep(60)
            # Retry once
            try:
                r = self.client.get(url, headers=self._headers(), params=params)
            except httpx.HTTPError as e:
                log.warning("Retry failed: %s", e)
                return None
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                raise APIError("rate limited (429) after retry")
            else:
                log.warning("API %s -> %d: %s", path, r.status_code, r.text[:200])
                return None

    # ------------------------------- Sets ---------------------------------- #
    def get_sets(self) -> List[Dict[str, Any]]:
        """Fetch all Lorcana sets/episodes."""
        data = self._get("/lorcana/episodes")
        if data is None:
            return []
        items = data.get("data", []) if isinstance(data, dict) else data
        out: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            out.append({
                "cardmarket_id": it.get("id"),
                "name": it.get("name", ""),
                "code": it.get("code") or "",
                "release_date": it.get("released_at") or it.get("release_date"),
                "card_count": it.get("cards_total") or it.get("cards_printed_total") or 0,
                "logo": it.get("logo"),
            })
        return out

    # ------------------------------ Cards ---------------------------------- #
    def get_cards_in_set(self, set_id: int) -> List[Dict[str, Any]]:
        """Fetch all cards in a set, handling pagination.

        Returns normalised card dicts with prices inline.
        """
        all_cards: List[Dict[str, Any]] = []
        page = 1
        while True:
            params = {"page": page} if page > 1 else None
            data = self._get(f"/lorcana/episodes/{set_id}/cards", params=params)
            if data is None:
                break
            items = data.get("data", []) if isinstance(data, dict) else data
            if not items:
                break
            for it in items:
                if not isinstance(it, dict):
                    continue
                card = self._normalise_card(it)
                all_cards.append(card)
            # Check if there are more pages
            meta = data.get("meta", {}) if isinstance(data, dict) else {}
            total_pages = meta.get("last_page") or meta.get("total_pages")
            if total_pages and page >= total_pages:
                break
            if len(items) < 20:  # API returns 20 per page
                break
            page += 1
            time.sleep(0.3)  # courtesy delay

        return all_cards

    def _normalise_card(self, it: Dict[str, Any]) -> Dict[str, Any]:
        """Normalise a card dict from the API response."""
        prices = it.get("prices") or {}
        cm_prices = prices.get("cardmarket") or {}

        # Extract PSA 10 from graded array
        psa10_price = None
        graded = cm_prices.get("graded", [])
        if isinstance(graded, list):
            for g in graded:
                if isinstance(g, dict):
                    grade = g.get("grade") or g.get("label") or ""
                    if "10" in str(grade) and "PSA" in str(g.get("company", "PSA")).upper():
                        psa10_price = g.get("price") or g.get("lowest")
                        break
                elif isinstance(g, dict) and g.get("psa10"):
                    psa10_price = g["psa10"]
        elif isinstance(graded, dict):
            psa = graded.get("psa", {})
            psa10_price = psa.get("psa10") or psa.get("10") or psa.get("price")

        return {
            "cardmarket_id": it.get("id"),
            "name": it.get("name", ""),
            "card_number": it.get("card_number"),
            "rarity": it.get("rarity"),
            "image_url": it.get("image") or it.get("image_url"),
            "set_name": it.get("episode", {}).get("name") if isinstance(it.get("episode"), dict) else None,
            # Prices (inline from card listing)
            "prices": {
                "cardmarket": {
                    "currency": cm_prices.get("currency", "EUR"),
                    "lowest_near_mint": cm_prices.get("lowest_near_mint"),
                    "lowest_near_mint_EU_only": cm_prices.get("lowest_near_mint_EU_only"),
                    "7d_average": cm_prices.get("7d_average"),
                    "30d_average": cm_prices.get("30d_average"),
                    "available_items": cm_prices.get("available_items"),
                },
                "tcgplayer": self._extract_tcgplayer(prices),
                "psa10": {"currency": cm_prices.get("currency", "EUR"), "price": psa10_price} if psa10_price else None,
            },
        }

    def _extract_tcgplayer(self, prices: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Extract TCGPlayer price data if available."""
        tp = prices.get("tcg_player") or prices.get("tcgplayer")
        if isinstance(tp, dict) and tp:
            return {
                "currency": tp.get("currency", "USD"),
                "market_price": tp.get("market_price") or tp.get("market") or tp.get("price"),
            }
        return None

    def get_card_detail(self, card_id: int) -> Optional[Dict[str, Any]]:
        """Fetch single card detail (rate-limited, use sparingly)."""
        data = self._get(f"/lorcana/cards/{card_id}")
        if data is None:
            return None
        return self._normalise_card(data)


# Module-level singleton
_api: Optional[CardmarketAPI] = None


def get_api() -> CardmarketAPI:
    global _api
    if _api is None:
        _api = CardmarketAPI()
    return _api
