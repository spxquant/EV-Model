# Sports EV Scanner

**Pinnacle Fair Value vs Polymarket — Read-Only**

Compares vig-removed Pinnacle probabilities against live Polymarket order book
prices to surface +EV sports bets across NBA, NHL, MLB, and ATP Tennis.

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill in your keys
cp .env.example .env
# Edit .env with your actual API keys

# 3. Run
python ev_scanner.py
```

## Usage

```bash
# Full scan (all sports, all markets)
python ev_scanner.py

# Single sport
python ev_scanner.py --sport basketball_nba

# Only show +EV opportunities (EV > 2%)
python ev_scanner.py --min-ev 2.0

# Pinnacle fair values only (skip Polymarket)
python ev_scanner.py --no-poly
```

## How It Works

1. **Pinnacle Odds** — Fetched via The Odds API (`regions=eu`, `bookmakers=pinnacle`)
2. **Vig Removal** — Multiplicative method strips the juice to get true implied probabilities
3. **Polymarket Prices** — Live order book best-ask from the CLOB API
4. **EV Formula** — `EV = (P_sharp / P_poly) - 1`
5. **Kelly Sizing** — Quarter-Kelly on a configurable bankroll (default $1,000)

## Architecture

```
.env.example        ← Template for API keys
.env                ← Your actual keys (gitignored)
ev_scanner.py       ← Main scanner script
requirements.txt    ← Python dependencies
```

## Key Design Decisions

- **3-minute cache** on Odds API calls to conserve the 20K/month credit limit
- **Team alias index** handles "Knicks" ↔ "New York Knicks" matching
- **Line verification** ensures Pinnacle +4.5 only matches Poly +4.5
- **Read-only** — no `place_order` calls, manual execution only

## Notes

- Polymarket sports market availability varies; the scanner gracefully
  falls back to showing Pinnacle fair values when no Poly match exists
- Run from a Jupyter notebook by importing: `from ev_scanner import scan, render_dashboard`
