"""RapidAPI client for cardmarket-api-tcg (Disney Lorcana prices).

The exact RapidAPI endpoint paths vary across documentation revisions, so this
client is intentionally flexible: it tries several known path variants for each
operation and caches the first one that works. Responses are cached in SQLite so
we minimise calls against the free-tier daily quota (100 req/day).
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from . import database

log = logging.getLogger("lorcana.api")

BASE_URL = os.environ.get("RAPIDAPI_BASE_URL", "https://cardmarket-api-tcg.p.rapidapi.com")
DEFAULT_HOST = os.environ.get("RAPIDAPI_HOST", "cardmarket-api-tcg.p.rapidapi.com")
TIMEOUT = 30.0

# Lorcana's game id on cardmarket-api is 6 (Disney Lorcana). We also try None
# for endpoints that don't require a game filter.
LORCANA_GAME_ID = os.environ.get("LORCANA_GAME_ID", "6")

# Path candidates tried in order for each operation. The first that returns
# HTTP 200 with parseable JSON is cached as the working path.
PATH_CANDIDATES = {
    "sets": [
        "/lorcana/expansions",
        "/expansions",
        "/expansions?game=lorcana",
        f"/games/{LORCANA_GAME_ID}/expansions",
        "/expansion",
        "/sets",
        "/lorcana/sets",
    ],
    "set_cards": [
        "/expansion/{set_id}/cards",
        "/expansions/{set_id}/cards",
        "/expansion/{set_id}",
        "/set/{set_id}/cards",
        "/sets/{set_id}/cards",
        "/cards?expansion={set_id}",
        "/cards?set={set_id}",
        "/lorcana/expansion/{set_id}/cards",
    ],
    "card": [
        "/card/{card_id}",
        "/cards/{card_id}",
        "/card/{card_id}/prices",
        "/cards/{card_id}/prices",
        "/lorcana/card/{card_id}",
        "/price/{card_id}",
        "/prices/{card_id}",
    ],
    "card_trend": [
        "/card/{card_id}/trend",
        "/cards/{card_id}/trend",
        "/card/{card_id}/history",
        "/cards/{card_id}/history",
        "/card/{card_id}/prices/trend",
    ],
}


class APIError(RuntimeError):
    pass


class CardmarketAPI:
    def __init__(self) -> None:
        self.key = os.environ.get("RAPIDAPI_KEY", "").strip()
        self.host = os.environ.get("RAPIDAPI_HOST", DEFAULT_HOST).strip()
        self.client = httpx.Client(timeout=TIMEOUT)
        self._working_paths: Dict[str, str] = {}
        self._cache_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data",
            "api_path_cache.json",
        )
        self._load_path_cache()

    @property
    def available(self) -> bool:
        return bool(self.key)

    def _headers(self) -> Dict[str, str]:
        return {
            "X-RapidAPI-Key": self.key,
            "X-RapidAPI-Host": self.host,
            "Accept": "application/json",
        }

    # ----------------------------- path discovery ---------------------------- #
    def _load_path_cache(self) -> None:
        try:
            with open(self._cache_file, "r") as f:
                self._working_paths = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._working_paths = {}

    def _save_path_cache(self) -> None:
        os.makedirs(os.path.dirname(self._cache_file), exist_ok=True)
        with open(self._cache_file, "w") as f:
            json.dump(self._working_paths, f, indent=2)

    def _try_paths(self, op: str, set_id: Optional[int] = None,
                   card_id: Optional[int] = None) -> Optional[Any]:
        """Try each candidate path for an operation; return parsed JSON on success."""
        if not self.available:
            return None
        candidates = [p.format(set_id=set_id, card_id=card_id) for p in PATH_CANDIDATES[op]]
        # Try cached working path first.
        cached = self._working_paths.get(op)
        if cached:
            ordered = [cached.format(set_id=set_id, card_id=card_id)]
            ordered += [c for c in candidates if c not in ordered]
        else:
            ordered = candidates
        for path in ordered:
            url = f"{BASE_URL}{path}"
            try:
                r = self.client.get(url, headers=self._headers())
            except httpx.HTTPError as e:
                log.warning("request error %s: %s", path, e)
                continue
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    continue
                # Heuristic: a usable response is either a list or a dict.
                if isinstance(data, (list, dict)):
                    if path != cached:
                        log.info("discovered working path for %s: %s", op, path)
                        self._working_paths[op] = PATH_CANDIDATES[op][
                            [p.format(set_id=set_id, card_id=card_id) for p in PATH_CANDIDATES[op]].index(path)
                        ] if path in candidates else self._working_paths.get(op)
                        # store the template (with placeholders)
                        self._working_paths[op] = self._path_template(op, path)
                        self._save_path_cache()
                    return data
            elif r.status_code == 429:
                log.warning("rate limited by RapidAPI")
                raise APIError("rate limited (429)")
            else:
                log.debug("path %s -> %s", path, r.status_code)
        return None

    def _path_template(self, op: str, used_path: str) -> str:
        for tmpl in PATH_CANDIDATES[op]:
            if used_path == tmpl:
                return tmpl
        return self._working_paths.get(op, used_path)

    # ------------------------------- public API ------------------------------ #
    def get_sets(self) -> List[Dict[str, Any]]:
        data = self._try_paths("sets")
        if data is None:
            return []
        items = data if isinstance(data, list) else data.get("expansions", data.get("sets", data.get("data", [])))
        # Filter to Lorcana sets where a game/expansion identifier lets us.
        out: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            out.append({
                "cardmarket_id": it.get("id") or it.get("expansion_id"),
                "name": it.get("name", ""),
                "code": it.get("code") or it.get("abbreviation"),
                "release_date": it.get("release_date") or it.get("dateReleased"),
                "card_count": it.get("card_count") or it.get("cards") or it.get("total_cards") or 0,
            })
        return out

    def get_cards_in_set(self, set_id: int) -> List[Dict[str, Any]]:
        data = self._try_paths("set_cards", set_id=set_id)
        if data is None:
            return []
        items = data if isinstance(data, list) else data.get("cards", data.get("data", []))
        out: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            out.append({
                "cardmarket_id": it.get("id") or it.get("card_id"),
                "name": it.get("name", ""),
                "card_number": it.get("number") or it.get("card_number"),
                "rarity": it.get("rarity"),
                "image_url": it.get("image") or it.get("image_url") or it.get("imageUrl"),
            })
        return out

    def get_card_prices(self, card_id: int) -> Dict[str, Any]:
        """Return normalised price dict for a card.

        Output shape:
            {
              "cardmarket": {"currency": "EUR", "lowest_near_mint": x, "7d_average": y, "30d_average": z},
              "tcgplayer":  {"currency": "USD", "market_price": x},
              "psa10":      {"currency": "USD", "price": x},
              "trend":      [{"date": "...", "price": x}, ...]   # optional
            }
        """
        data = self._try_paths("card", card_id=card_id)
        trend = self._try_paths("card_trend", card_id=card_id)
        if data is None:
            return {}
        prices = data.get("prices", data) if isinstance(data, dict) else {}
        result: Dict[str, Any] = {}

        cm = prices.get("cardmarket") if isinstance(prices, dict) else None
        if isinstance(cm, dict):
            result["cardmarket"] = {
                "currency": cm.get("currency", "EUR"),
                "lowest_near_mint": cm.get("lowest_near_mint") or cm.get("price") or cm.get("lowest"),
                "7d_average": cm.get("7d_average"),
                "30d_average": cm.get("30d_average"),
            }
            graded = cm.get("graded", {})
            psa = graded.get("psa", {})
            psa10 = psa.get("psa10") or psa.get("10") or psa.get("price")
            if psa10 is not None:
                result["psa10"] = {"currency": cm.get("currency", "EUR"), "price": psa10}

        tp = prices.get("tcg_player") or prices.get("tcgplayer") if isinstance(prices, dict) else None
        if isinstance(tp, dict):
            result["tcgplayer"] = {
                "currency": tp.get("currency", "USD"),
                "market_price": tp.get("market_price") or tp.get("market") or tp.get("price"),
            }

        if trend:
            if isinstance(trend, list):
                result["trend"] = [
                    {"date": t.get("date") or t.get("snapshot_date"), "price": t.get("price")}
                    for t in trend if isinstance(t, dict)
                ]
            elif isinstance(trend, dict):
                series = trend.get("trend") or trend.get("history") or trend.get("prices") or []
                result["trend"] = [
                    {"date": t.get("date"), "price": t.get("price")}
                    for t in series if isinstance(t, dict)
                ]
        return result


# Module-level singleton
_api: Optional[CardmarketAPI] = None


def get_api() -> CardmarketAPI:
    global _api
    if _api is None:
        _api = CardmarketAPI()
    return _api
