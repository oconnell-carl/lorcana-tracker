"""FastAPI app serving the Lorcana price tracker UI and JSON API."""

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import api as api_mod
from . import database
from . import snapshot as snapshot_mod
from . import targeted_snapshot as targeted_snapshot_mod

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("lorcana.main")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Lorcana Price Tracker", version="0.1.0")

RANGE_DAYS = {"30d": 30, "3m": 90, "6m": 180, "1y": 365, "all": None}


@app.on_event("startup")
def _startup() -> None:
    database.init_db()
    log.info("Database initialized at %s", database.DB_PATH)
    if not api_mod.get_api().available:
        log.warning("RAPIDAPI_KEY not set; running in cached/offline mode.")


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _attach_prices(card: Dict[str, Any]) -> Dict[str, Any]:
    prices = database.get_latest_prices(card["id"])
    card["prices"] = {
        src: {"price": snap["price"], "currency": snap["currency"], "date": snap["snapshot_date"]}
        for src, snap in prices.items()
    }
    return card


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/sets")
def list_sets() -> Dict[str, Any]:
    sets = database.get_sets()
    # If DB empty and API is available, try to populate.
    if not sets and api_mod.get_api().available:
        try:
            api_sets = api_mod.get_api().get_sets()
            for s in api_sets:
                database.upsert_set(s)
            sets = database.get_sets()
        except api_mod.APIError as e:
            log.warning("API error fetching sets: %s", e)
    return {"sets": sets, "api_available": api_mod.get_api().available}


@app.get("/api/sets/{set_id}/cards")
def set_cards(set_id: int) -> Dict[str, Any]:
    s = database.get_set(set_id)
    if not s:
        raise HTTPException(status_code=404, detail="Set not found")
    cards = database.get_cards_in_set(set_id)
    # Lazy-populate from API if empty.
    if not cards and api_mod.get_api().available and s.get("cardmarket_id"):
        try:
            api_cards = api_mod.get_api().get_cards_in_set(s["cardmarket_id"])
            for c in api_cards:
                c["set_id"] = set_id
                database.upsert_card(c)
            cards = database.get_cards_in_set(set_id)
        except api_mod.APIError as e:
            log.warning("API error fetching cards: %s", e)
    cards = [_attach_prices(c) for c in cards]
    return {"set": s, "cards": cards}


@app.get("/api/cards/{card_id}")
def card_detail(card_id: int) -> Dict[str, Any]:
    card = database.get_card(card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    card = _attach_prices(card)
    # Append set name if available.
    if card.get("set_id"):
        s = database.get_set(card["set_id"])
        card["set_name"] = s["name"] if s else None
    return {"card": card}


@app.get("/api/cards/{card_id}/history")
def card_history(
    card_id: int,
    range: str = Query("30d", pattern="^(30d|3m|6m|1y|all)$"),
    source: Optional[str] = None,
) -> Dict[str, Any]:
    card = database.get_card(card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    days = RANGE_DAYS.get(range)
    history = database.get_history(card_id, source=source, days=days)

    # If no local history, attempt to fetch trend from API and backfill.
    # NOTE: The free-tier API doesn't provide historical trend data.
    # Price history is built up locally from daily snapshots.

    # Group by source for the frontend.
    series: Dict[str, List[Dict[str, Any]]] = {}
    for h in history:
        series.setdefault(h["source"], []).append({
            "date": h["snapshot_date"], "price": h["price"], "currency": h["currency"],
        })
    return {"card_id": card_id, "range": range, "series": series}


@app.get("/api/search")
def search(q: str = Query(..., min_length=1)) -> Dict[str, Any]:
    cards = database.search_cards(q)
    cards = [_attach_prices(c) for c in cards]
    return {"query": q, "results": cards}


@app.get("/api/status")
def status() -> Dict[str, Any]:
    return {
        "api_available": api_mod.get_api().available,
        "db_path": database.DB_PATH,
        "set_count": len(database.get_sets()),
        "card_count": len(database.all_card_ids()),
    }


@app.get("/api/snapshot/targeted")
def run_targeted_snapshot(budget: int = 95) -> Dict[str, Any]:
    """Run the targeted snapshot job (priority rarities only)."""
    import io
    import contextlib
    
    log_buffer = io.StringIO()
    handler = logging.StreamHandler(log_buffer)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("lorcana.targeted_snapshot")
    logger.addHandler(handler)
    
    try:
        result = targeted_snapshot_mod.main(["--budget", str(budget)])
        logger.removeHandler(handler)
        return {"status": "ok", "return_code": result, "logs": log_buffer.getvalue()}
    except Exception as e:
        logger.removeHandler(handler)
        return {"status": "error", "error": str(e), "logs": log_buffer.getvalue()}


def main() -> None:
    import uvicorn
    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
