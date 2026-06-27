# Lorcana Price Tracker

A local web app for tracking Disney Lorcana card prices across Cardmarket (EU) and TCGPlayer (US), including PSA 10 graded prices.

## Features
- Search any Lorcana card by name
- Browse cards by set
- View current lowest English Near-Mint prices (Cardmarket + TCGPlayer)
- View PSA 10 prices where available
- Price trend graphs (30 day, 3 month, 6 month, 1 year, all time)
- Card images
- Daily price snapshots stored locally in SQLite

## Tech Stack
- Backend: Python FastAPI
- Database: SQLite
- Frontend: HTML/CSS/JS + Chart.js
- API: cardmarket-api.com via RapidAPI

## Setup
1. Get a RapidAPI key from https://rapidapi.com/tcggopro/api/cardmarket-api-tcg/pricing (free tier: 100 req/day)
2. Copy `.env.example` to `.env` and add your key
3. `pip install -r requirements.txt`
4. `python src/main.py`
5. Open http://localhost:8000

## Data Storage
Price snapshots are stored in SQLite (`data/prices.db`). A daily cron job fetches current prices for all tracked cards and stores them for historical trending.
