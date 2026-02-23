"""
EV Scanner — Live API Server
Runs continuous scans and serves results via HTTP + Server-Sent Events.
Dashboard auto-updates when new data arrives or opportunities vanish.

Usage:
    pip install flask flask-cors
    python ev_server.py                    # Start server on :8877
    python ev_server.py --interval 120     # Scan every 2 min
    python ev_server.py --port 9000        # Custom port
    python ev_server.py --no-props         # Skip props
    python ev_server.py --no-poly          # Skip Polymarket
"""

from __future__ import annotations
import argparse, os, re, sys, time, hashlib, json, threading
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
from queue import Queue

import requests as req
from dotenv import load_dotenv

try:
    from flask import Flask, jsonify, Response, send_from_directory
    from flask_cors import CORS
except ImportError:
    print("Install flask: pip install flask flask-cors")
    sys.exit(1)

try:
    from py_clob_client.client import ClobClient
except ImportError:
    ClobClient = None

load_dotenv()

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

ODDS_API_KEY     = os.getenv("ODDS_API_KEY", "")
POLY_API_KEY     = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET  = os.getenv("POLY_API_SECRET", "")
POLY_PASSPHRASE  = os.getenv("POLY_PASSPHRASE", "")
POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")

BANKROLL       = float(os.getenv("BANKROLL", "1000"))
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))
CACHE_TTL      = int(os.getenv("CACHE_TTL_SECONDS", "180"))

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
POLY_HOST     = "https://clob.polymarket.com"
CHAIN_ID      = 137

DEFAULT_SPORTS = [
    "americanfootball_nfl",
    "americanfootball_ncaaf",
    "basketball_nba",
    "basketball_ncaab",
    "basketball_wnba",
    "icehockey_nhl",
    "baseball_mlb",
]
TENNIS_PREFIX  = "tennis_"
GAME_MARKETS   = ["h2h", "spreads", "totals"]

PROP_MARKETS = {
    "americanfootball_nfl": [
        "player_pass_yds","player_pass_tds","player_pass_completions",
        "player_rush_yds","player_rush_attempts","player_reception_yds",
        "player_receptions","player_anytime_td",
    ],
    "americanfootball_ncaaf": [
        "player_pass_yds","player_pass_tds","player_rush_yds",
        "player_reception_yds","player_receptions","player_anytime_td",
    ],
    "basketball_nba": [
        "player_points","player_rebounds","player_assists","player_threes",
        "player_points_rebounds_assists","player_points_rebounds",
        "player_points_assists","player_rebounds_assists",
    ],
    "basketball_ncaab": ["player_points","player_rebounds","player_assists"],
    "basketball_wnba": [
        "player_points","player_rebounds","player_assists","player_threes",
        "player_points_rebounds_assists",
    ],
    "icehockey_nhl": ["player_points","player_shots_on_goal","player_blocked_shots"],
    "baseball_mlb": [
        "batter_hits","batter_total_bases","batter_rbis","batter_runs_scored",
        "batter_walks","batter_strikeouts","batter_home_runs",
        "pitcher_strikeouts","pitcher_outs",
    ],
}

PROP_LABELS = {
    "player_points":"Points","player_rebounds":"Rebounds","player_assists":"Assists",
    "player_threes":"3PT","player_points_rebounds_assists":"PRA",
    "player_points_rebounds":"Pts+Reb","player_points_assists":"Pts+Ast",
    "player_rebounds_assists":"Reb+Ast","player_shots_on_goal":"SOG",
    "player_blocked_shots":"Blocks","batter_hits":"Hits","batter_total_bases":"Tot Bases",
    "batter_rbis":"RBIs","batter_runs_scored":"Runs","batter_walks":"Walks",
    "batter_strikeouts":"Ks(Bat)","batter_home_runs":"HRs",
    "pitcher_strikeouts":"Ks(Pitch)","pitcher_outs":"Outs",
    "player_pass_yds":"Pass Yds","player_pass_tds":"Pass TDs",
    "player_pass_completions":"Completions","player_rush_yds":"Rush Yds",
    "player_rush_attempts":"Rush Att","player_reception_yds":"Rec Yds",
    "player_receptions":"Receptions","player_anytime_td":"Anytime TD",
}

SOFT_BOOKS = {"draftkings","fanduel","betmgm","espnbet","betrivers","fanatics",
              "williamhill_us","caesars"}

TEAM_ALIASES = {
    "Atlanta Hawks":["Hawks","ATL"],"Boston Celtics":["Celtics","BOS"],
    "Brooklyn Nets":["Nets","BKN"],"Charlotte Hornets":["Hornets","CHA"],
    "Chicago Bulls":["Bulls","CHI"],"Cleveland Cavaliers":["Cavaliers","Cavs","CLE"],
    "Dallas Mavericks":["Mavericks","Mavs","DAL"],"Denver Nuggets":["Nuggets","DEN"],
    "Detroit Pistons":["Pistons","DET"],"Golden State Warriors":["Warriors","GSW","GS"],
    "Houston Rockets":["Rockets","HOU"],"Indiana Pacers":["Pacers","IND"],
    "Los Angeles Clippers":["Clippers","LAC"],"Los Angeles Lakers":["Lakers","LAL"],
    "Memphis Grizzlies":["Grizzlies","MEM"],"Miami Heat":["Heat","MIA"],
    "Milwaukee Bucks":["Bucks","MIL"],"Minnesota Timberwolves":["Timberwolves","Wolves","MIN"],
    "New Orleans Pelicans":["Pelicans","NOP"],"New York Knicks":["Knicks","NYK"],
    "Oklahoma City Thunder":["Thunder","OKC"],"Orlando Magic":["Magic","ORL"],
    "Philadelphia 76ers":["76ers","Sixers","PHI"],"Phoenix Suns":["Suns","PHX"],
    "Portland Trail Blazers":["Trail Blazers","Blazers","POR"],
    "Sacramento Kings":["Kings","SAC"],"San Antonio Spurs":["Spurs","SAS"],
    "Toronto Raptors":["Raptors","TOR"],"Utah Jazz":["Jazz","UTA"],
    "Washington Wizards":["Wizards","WAS"],
    "Anaheim Ducks":["Ducks","ANA"],"Boston Bruins":["Bruins"],
    "Buffalo Sabres":["Sabres","BUF"],"Calgary Flames":["Flames","CGY"],
    "Carolina Hurricanes":["Hurricanes","Canes","CAR"],
    "Chicago Blackhawks":["Blackhawks"],"Colorado Avalanche":["Avalanche","Avs","COL"],
    "Columbus Blue Jackets":["Blue Jackets","CBJ"],
    "Dallas Stars":["Stars"],"Detroit Red Wings":["Red Wings"],
    "Edmonton Oilers":["Oilers","EDM"],"Florida Panthers":["Panthers","FLA"],
    "Los Angeles Kings":["LA Kings","LAK"],"Minnesota Wild":["Wild"],
    "Montreal Canadiens":["Canadiens","Habs","MTL"],
    "Nashville Predators":["Predators","Preds","NSH"],
    "New Jersey Devils":["Devils","NJD"],"New York Islanders":["Islanders","NYI"],
    "New York Rangers":["Rangers","NYR"],"Ottawa Senators":["Senators","Sens","OTT"],
    "Philadelphia Flyers":["Flyers"],"Pittsburgh Penguins":["Penguins","Pens","PIT"],
    "San Jose Sharks":["Sharks","SJS"],"Seattle Kraken":["Kraken","SEA"],
    "St. Louis Blues":["Blues","STL"],"Tampa Bay Lightning":["Lightning","TBL","TB"],
    "Toronto Maple Leafs":["Maple Leafs","Leafs"],
    "Utah Hockey Club":["Utah HC"],"Vancouver Canucks":["Canucks","VAN"],
    "Vegas Golden Knights":["Golden Knights","VGK"],
    "Washington Capitals":["Capitals","Caps","WSH"],"Winnipeg Jets":["Jets","WPG"],
    "Arizona Diamondbacks":["Diamondbacks","D-backs"],
    "Atlanta Braves":["Braves"],"Baltimore Orioles":["Orioles","BAL"],
    "Boston Red Sox":["Red Sox"],"Chicago Cubs":["Cubs","CHC"],
    "Chicago White Sox":["White Sox","CWS"],"Cincinnati Reds":["Reds","CIN"],
    "Cleveland Guardians":["Guardians"],"Colorado Rockies":["Rockies"],
    "Detroit Tigers":["Tigers"],"Houston Astros":["Astros"],
    "Kansas City Royals":["Royals","KC"],"Los Angeles Angels":["Angels","LAA"],
    "Los Angeles Dodgers":["Dodgers","LAD"],"Miami Marlins":["Marlins"],
    "Milwaukee Brewers":["Brewers"],"Minnesota Twins":["Twins"],
    "New York Mets":["Mets","NYM"],"New York Yankees":["Yankees","NYY"],
    "Oakland Athletics":["Athletics","A's","OAK"],
    "Philadelphia Phillies":["Phillies"],"Pittsburgh Pirates":["Pirates"],
    "San Diego Padres":["Padres","SDP"],"San Francisco Giants":["Giants","SFG"],
    "Seattle Mariners":["Mariners"],"St. Louis Cardinals":["Cardinals","Cards"],
    "Tampa Bay Rays":["Rays"],"Texas Rangers":["Rangers","TEX"],
    "Toronto Blue Jays":["Blue Jays","Jays"],
    "Washington Nationals":["Nationals","Nats"],
}

# Build alias index
def _norm(n): return " ".join(n.lower().strip().removeprefix("the ").removeprefix("team ").split())
_AIDX = {}
for _fn, _al in TEAM_ALIASES.items():
    _AIDX[_norm(_fn)] = _fn
    for _a in _al: _AIDX[_norm(_a)] = _fn

def team_match(q, c):
    nq, nc = _norm(q), _norm(c)
    if nq == nc: return True
    cq, cc = _AIDX.get(nq), _AIDX.get(nc)
    if cq and cc and cq == cc: return True
    if nq in nc or nc in nq: return True
    return False

# ═══════════════════════════════════════════════════════════════
# CACHE + STATE
# ═══════════════════════════════════════════════════════════════

class Cache:
    def __init__(self):
        self._s = {}
    def _k(self, u, p): return hashlib.md5((u + json.dumps(p, sort_keys=True)).encode()).hexdigest()
    def get(self, u, p):
        k = self._k(u, p)
        if k in self._s:
            ts, d = self._s[k]
            if time.time() - ts < CACHE_TTL: return d
            del self._s[k]
        return None
    def put(self, u, p, d): self._s[self._k(u, p)] = (time.time(), d)

cache = Cache()
credits_used = 0
scan_lock = threading.Lock()
sse_clients: list[Queue] = []

# Current state
current_data = {
    "scanned_at": None,
    "scanning": False,
    "bankroll": BANKROLL,
    "kelly_fraction": KELLY_FRACTION,
    "credits_used": 0,
    "opportunities": [],
    "scan_count": 0,
}

# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def dec2imp(d): return 1.0 if d <= 1.0 else 1.0 / d

def imp2american(prob):
    """Convert implied probability (0-1) to American odds string."""
    if prob <= 0 or prob >= 1: return "—"
    if prob >= 0.5:
        return f"{int(-100 * prob / (1 - prob))}"
    else:
        return f"+{int(100 * (1 - prob) / prob)}"

def dec2american(decimal_odds):
    """Convert decimal odds to American odds string."""
    if decimal_odds <= 1.0: return "—"
    if decimal_odds >= 2.0:
        return f"+{int((decimal_odds - 1) * 100)}"
    else:
        return f"{int(-100 / (decimal_odds - 1))}"

def log_cred(resp, label):
    global credits_used
    last = resp.headers.get("x-requests-last", "1")
    try: credits_used += int(last)
    except: credits_used += 1

def remove_vig(outcomes):
    if not outcomes: return {}
    imp = [{"n": o.get("name", ""), "d": o.get("description", ""),
            "i": dec2imp(o.get("price", 0)), "l": o.get("point"),
            "price": o.get("price", 0)} for o in outcomes]
    tot = sum(x["i"] for x in imp)
    if tot == 0: return {}
    res = {}
    for x in imp:
        k = f"{x['d']}|{x['n']}" if x["d"] else x["n"]
        res[k] = {"fair": x["i"] / tot, "line": x["l"], "desc": x["d"],
                  "name": x["n"], "price": x["price"]}
    return res

def kelly(sharp, target, br=BANKROLL, frac=KELLY_FRACTION):
    if target <= 0 or target >= 1: return 0.0
    b = (1.0 / target) - 1.0
    if b <= 0: return 0.0
    f = (b * sharp - (1 - sharp)) / b
    return round(br * frac * f, 2) if f > 0 else 0.0

# ═══════════════════════════════════════════════════════════════
# ODDS API
# ═══════════════════════════════════════════════════════════════

def discover_sports():
    try:
        r = req.get(f"{ODDS_API_BASE}/sports/", params={"apiKey": ODDS_API_KEY, "all": "false"}, timeout=10)
        r.raise_for_status()
        return [s for s in r.json() if s.get("active")]
    except: return []

def get_targets(in_season):
    tgt = set(DEFAULT_SPORTS)
    return [s for s in in_season if s["key"] in tgt or s["key"].startswith(TENNIS_PREFIX)]

def fetch_odds(sport, market):
    url = f"{ODDS_API_BASE}/sports/{sport}/odds/"
    params = {"apiKey": ODDS_API_KEY, "regions": "eu", "markets": market,
              "oddsFormat": "decimal", "bookmakers": "pinnacle"}
    c = cache.get(url, params)
    if c is not None: return c
    try:
        r = req.get(url, params=params, timeout=15); r.raise_for_status()
        data = r.json(); log_cred(r, f"Game:{sport}/{market}")
        cache.put(url, params, data); return data
    except: return []

def fetch_events(sport):
    try:
        r = req.get(f"{ODDS_API_BASE}/sports/{sport}/events/",
                    params={"apiKey": ODDS_API_KEY}, timeout=10)
        r.raise_for_status(); return r.json()
    except: return []

def fetch_event_props(sport, event_id, prop_mkts):
    url = f"{ODDS_API_BASE}/sports/{sport}/events/{event_id}/odds"
    params = {"apiKey": ODDS_API_KEY, "regions": "us,us2",
              "markets": ",".join(prop_mkts), "oddsFormat": "decimal"}
    c = cache.get(url, params)
    if c is not None: return c
    try:
        r = req.get(url, params=params, timeout=20); r.raise_for_status()
        data = r.json(); log_cred(r, f"Props:{event_id[:8]}")
        cache.put(url, params, data); return data
    except: return {}

# ═══════════════════════════════════════════════════════════════
# PINNACLE EXTRACTION
# ═══════════════════════════════════════════════════════════════

def extract_pinnacle(events, market):
    out = []
    for ev in events:
        h, a = ev.get("home_team", ""), ev.get("away_team", "")
        for bm in ev.get("bookmakers", []):
            if bm.get("key") != "pinnacle": continue
            for m in bm.get("markets", []):
                if m.get("key") != market: continue
                out.append({"sport": ev.get("sport_key", ""), "game": f"{a} @ {h}",
                           "home": h, "away": a, "market": market,
                           "outcomes": m.get("outcomes", [])})
    return out

# ═══════════════════════════════════════════════════════════════
# PLAYER PROPS
# ═══════════════════════════════════════════════════════════════

def extract_prop_lines(event_data):
    lines = []
    for bm in event_data.get("bookmakers", []):
        bk = bm.get("key", "")
        for m in bm.get("markets", []):
            mk = m.get("key", "")
            for o in m.get("outcomes", []):
                pl = o.get("description", "")
                if not pl: continue
                side = o.get("name", "")
                pt = o.get("point")
                pr = o.get("price", 0)
                if pr <= 1.0 or pt is None: continue
                lines.append({"book": bk, "player": pl, "market_key": mk,
                             "side": side, "line": float(pt), "price": pr,
                             "implied": dec2imp(pr)})
    return lines

def find_prop_ev(lines, game_label, sport_title, min_books=3):
    groups = defaultdict(list)
    for l in lines:
        groups[(l["player"], l["market_key"], l["side"], l["line"])].append(l)

    opps = []
    for (player, mk, side, line), blines in groups.items():
        if len(blines) < min_books: continue
        for i, tgt in enumerate(blines):
            if tgt["book"] not in SOFT_BOOKS: continue
            others = [b for j, b in enumerate(blines) if j != i]
            if len(others) < 2: continue
            consensus = sum(b["implied"] for b in others) / len(others)
            if consensus <= 0 or tgt["implied"] <= 0: continue
            ev = (consensus / tgt["implied"] - 1.0) * 100
            if ev < 1.0: continue
            stk = kelly(consensus, tgt["implied"])
            label = PROP_LABELS.get(mk, mk)
            opps.append({
                "sport": sport_title, "game": game_label,
                "selection": player,
                "market_type": f"{label} {side[0]} {line}",
                "line": line,
                "implied_prob": round(tgt["implied"], 4),
                "model_prob": round(consensus, 4),
                "decimal_odds": round(tgt["price"], 3),
                "american_odds": dec2american(tgt["price"]),
                "book": tgt["book"].replace("_", " ").title(),
                "ev_pct": round(ev, 2),
                "kelly_stake": stk,
                "is_prop": True,
                "num_books": len(blines),
            })
    return opps

# ═══════════════════════════════════════════════════════════════
# POLYMARKET
# ═══════════════════════════════════════════════════════════════

def init_poly():
    if not ClobClient or not all([POLY_API_KEY, POLY_API_SECRET, POLY_PASSPHRASE]):
        return None
    try:
        return ClobClient(POLY_HOST, key=POLY_PRIVATE_KEY, chain_id=CHAIN_ID,
            creds={"apiKey": POLY_API_KEY, "secret": POLY_API_SECRET, "passphrase": POLY_PASSPHRASE})
    except: return None

def fetch_poly_markets(client):
    all_m, cursor = [], ""
    kw = ["nba","nhl","mlb","nfl","ncaab","ncaaf","wnba","ncaa","college basketball",
          "college football","atp","wta","tennis","football","basketball","hockey",
          "baseball","win","beat","score","points","goals","touchdown",
          "spread","total","over","under","champion","playoff","series","game","mvp",
          "super bowl","march madness","world series","stanley cup"]
    try:
        for _ in range(10):
            r = client.get_markets(next_cursor=cursor)
            mkts = r if isinstance(r, list) else r.get("data", [])
            cursor = "" if isinstance(r, list) else r.get("next_cursor", "")
            for m in mkts:
                txt = (m.get("question", "") + m.get("description", "")).lower()
                if any(k in txt for k in kw) and m.get("active") and not m.get("closed", True):
                    all_m.append(m)
            if not cursor or cursor == "LQ==": break
    except: pass
    return all_m

def poly_best_price(client, tid, side="buy"):
    try:
        book = client.get_order_book(tid)
        if side == "buy":
            asks = book.get("asks", [])
            if asks: return float(min(asks, key=lambda x: float(x.get("price", 999)))["price"])
    except: pass
    return None

def match_poly_game(pm, game, mtype):
    txt = (pm.get("question", "") + pm.get("description", "")).lower()
    hm = any(_norm(a) in txt for a in [game["home"]] + TEAM_ALIASES.get(game["home"], []))
    am = any(_norm(a) in txt for a in [game["away"]] + TEAM_ALIASES.get(game["away"], []))
    if not (hm or am): return None
    if mtype == "h2h": return {"type": "ml", "market": pm}
    if mtype == "spreads":
        f = re.findall(r"[+-]?\d+\.5", txt)
        if f: return {"type": "spread", "market": pm, "lines": [float(x) for x in f]}
    if mtype == "totals":
        f = re.findall(r"(?:over|under)\s*(\d+\.?\d*)", txt)
        if f: return {"type": "total", "market": pm, "lines": [float(x) for x in f]}
    return None

def poly_tokens(m):
    toks = m.get("tokens", [])
    if not toks:
        c = m.get("condition_id", "")
        return [{"token_id": c, "outcome": "YES"}] if c else []
    return [{"token_id": t["token_id"], "outcome": t.get("outcome", "")} for t in toks if t.get("token_id")]

# ═══════════════════════════════════════════════════════════════
# CONFIDENCE SCORING ENGINE
# ═══════════════════════════════════════════════════════════════
#
# Scores each opportunity 0-100 and assigns a grade (A+ through D).
# Factors:
#   1. EV Magnitude        (0-30 pts) — higher EV = better
#   2. Book Consensus      (0-25 pts) — more books agreeing = more reliable
#   3. Market Reliability   (0-20 pts) — some markets are historically sharper
#   4. Edge Stability      (0-15 pts) — model prob vs implied gap consistency
#   5. Kelly Confidence    (0-10 pts) — Kelly suggests meaningful stake
#
# Also flags correlated bets (multiple bets on same game).

# Historical reliability weights by market type
MARKET_RELIABILITY = {
    # Game lines — Pinnacle is extremely sharp here
    "ML": 18, "Spread": 17, "Total": 16,
    # Props — consensus model, inherently noisier
    "Points": 14, "Rebounds": 12, "Assists": 12, "3PT": 10,
    "PRA": 11, "Pts+Reb": 11, "Pts+Ast": 11, "Reb+Ast": 11,
    "SOG": 10, "Blocks": 9,
    "Hits": 12, "Tot Bases": 11, "RBIs": 10, "Runs": 10,
    "Walks": 8, "Ks(Bat)": 11, "HRs": 9,
    "Ks(Pitch)": 13, "Outs": 11,
    "Pass Yds": 13, "Pass TDs": 10, "Completions": 11,
    "Rush Yds": 12, "Rush Att": 10, "Rec Yds": 12,
    "Receptions": 11, "Anytime TD": 8,
}

def score_opportunities(opps):
    """Score and grade each opportunity, detect correlated bets."""
    if not opps:
        return opps

    # Track games for correlation detection
    game_counts = defaultdict(int)
    for o in opps:
        if o["ev_pct"] > 0:
            game_counts[o["game"]] += 1

    scored = []
    for o in opps:
        score = 0.0

        # 1. EV Magnitude (0-30)
        ev = o["ev_pct"]
        if ev <= 0:
            ev_score = 0
        elif ev <= 1:
            ev_score = ev * 5          # 0-5
        elif ev <= 3:
            ev_score = 5 + (ev - 1) * 5  # 5-15
        elif ev <= 5:
            ev_score = 15 + (ev - 3) * 5  # 15-25
        else:
            ev_score = min(30, 25 + (ev - 5) * 1)  # 25-30, caps
        score += ev_score

        # 2. Book Consensus (0-25)
        nb = o.get("num_books", 1)
        if o["is_prop"]:
            # Props: more books = more reliable consensus
            if nb >= 6: book_score = 25
            elif nb >= 5: book_score = 20
            elif nb >= 4: book_score = 15
            elif nb >= 3: book_score = 10
            else: book_score = 5
        else:
            # Game lines: Pinnacle alone is very sharp
            if o["book"] == "Polymarket" and o["implied_prob"] > 0:
                book_score = 20  # Cross-market validation
            else:
                book_score = 15  # Pinnacle fair value only
        score += book_score

        # 3. Market Reliability (0-20)
        mtype = o["market_type"]
        mkt_score = 10  # default
        for key, val in MARKET_RELIABILITY.items():
            if key.lower() in mtype.lower():
                mkt_score = val
                break
        score += mkt_score

        # 4. Edge Stability (0-15)
        # Bigger gap between model and implied = more confident edge
        if o["implied_prob"] > 0 and o["model_prob"] > 0:
            gap = o["model_prob"] - o["implied_prob"]
            if gap > 0.08: edge_score = 15
            elif gap > 0.05: edge_score = 12
            elif gap > 0.03: edge_score = 9
            elif gap > 0.01: edge_score = 6
            else: edge_score = 3
        else:
            edge_score = 0
        score += edge_score

        # 5. Kelly Confidence (0-10)
        stk = o["kelly_stake"]
        if stk >= 50: kelly_score = 10
        elif stk >= 30: kelly_score = 8
        elif stk >= 15: kelly_score = 6
        elif stk >= 5: kelly_score = 4
        elif stk > 0: kelly_score = 2
        else: kelly_score = 0
        score += kelly_score

        # Normalize to 0-100
        score = min(100, round(score))

        # Grade
        if score >= 85: grade = "A+"
        elif score >= 75: grade = "A"
        elif score >= 65: grade = "B+"
        elif score >= 55: grade = "B"
        elif score >= 45: grade = "C+"
        elif score >= 35: grade = "C"
        else: grade = "D"

        # Correlation flag
        correlated = game_counts.get(o["game"], 0) > 2

        o["confidence_score"] = score
        o["grade"] = grade
        o["correlated"] = correlated
        scored.append(o)

    # Sort by confidence score descending
    scored.sort(key=lambda x: x["confidence_score"], reverse=True)

    # Tag top picks — top 5 +EV bets with A/A+ grade and no correlation issues
    top_count = 0
    for o in scored:
        if top_count >= 5:
            o["top_pick"] = False
            continue
        if o["ev_pct"] > 0 and o["grade"] in ("A+", "A") and not o["correlated"]:
            o["top_pick"] = True
            top_count += 1
        else:
            o["top_pick"] = False

    return scored


# ═══════════════════════════════════════════════════════════════
# MAIN SCAN
# ═══════════════════════════════════════════════════════════════

def run_scan(skip_poly=False, skip_props=False, max_prop_events=10):
    global credits_used, current_data
    opps = []

    current_data["scanning"] = True
    broadcast_sse({"type": "scan_start"})
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] Scan starting...")

    in_season = discover_sports()
    if not in_season:
        current_data["scanning"] = False
        return

    targets = get_targets(in_season)
    if not targets:
        current_data["scanning"] = False
        return

    # Polymarket
    pcli, pmkts = None, []
    if not skip_poly:
        pcli = init_poly()
        if pcli: pmkts = fetch_poly_markets(pcli)

    for si in targets:
        sk, st = si["key"], si.get("title", si["key"])
        upcoming = fetch_events(sk)
        if not upcoming: continue

        # GAME LINES
        for mt in GAME_MARKETS:
            evts = fetch_odds(sk, mt)
            pdata = extract_pinnacle(evts, mt)
            if not pdata: continue

            for g in pdata:
                fairs = remove_vig(g["outcomes"])
                for oname, fi in fairs.items():
                    fp, ln, price = fi["fair"], fi["line"], fi["price"]
                    if mt == "h2h":
                        ml, sel = "ML", oname
                    elif mt == "spreads":
                        s = "+" if ln and ln > 0 else ""
                        ml, sel = f"Spread {s}{ln}" if ln else "Spread", oname
                    elif mt == "totals":
                        ml, sel = f"Total {oname} {ln}" if ln else f"Total {oname}", oname
                    else:
                        ml, sel = mt, oname

                    pp = None
                    if pcli and pmkts:
                        for pm in pmkts:
                            m = match_poly_game(pm, g, mt)
                            if not m: continue
                            if mt in ("spreads", "totals") and ln is not None:
                                mls = m.get("lines", [])
                                if mls and ln not in mls: continue
                            for tk in poly_tokens(pm):
                                if mt == "h2h":
                                    if team_match(oname, pm.get("question", "")):
                                        p = poly_best_price(pcli, tk["token_id"], "buy")
                                        if p: pp = p; break
                                elif tk["outcome"].lower() == "yes":
                                    p = poly_best_price(pcli, tk["token_id"], "buy")
                                    if p: pp = p; break
                            if pp: break

                    if pp and pp > 0:
                        ev = ((fp / pp) - 1.0) * 100
                        stk = kelly(fp, pp)
                        src = "Polymarket"
                    else:
                        ev = 0.0; stk = 0.0; pp = 0.0; src = "—"

                    opps.append({
                        "sport": st, "game": g["game"], "selection": sel,
                        "market_type": ml, "line": ln,
                        "implied_prob": round(pp, 4) if pp else 0.0,
                        "model_prob": round(fp, 4),
                        "decimal_odds": round(price, 3),
                        "american_odds": dec2american(price),
                        "book": src, "ev_pct": round(ev, 2),
                        "kelly_stake": stk, "is_prop": False,
                        "num_books": 1,  # Pinnacle is the single sharp source
                    })

        # PROPS
        if not skip_props and sk in PROP_MARKETS:
            pmk = PROP_MARKETS[sk]
            for ev in upcoming[:max_prop_events]:
                eid = ev.get("id", "")
                if not eid: continue
                h, a = ev.get("home_team", ""), ev.get("away_team", "")
                gl = f"{a} @ {h}"
                edata = fetch_event_props(sk, eid, pmk)
                if not edata or "bookmakers" not in edata: continue
                plines = extract_prop_lines(edata)
                if plines:
                    opps.extend(find_prop_ev(plines, gl, st))

    # ── CONFIDENCE SCORING ENGINE ─────────────────────────────
    opps = score_opportunities(opps)

    # Update state
    current_data.update({
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "scanning": False,
        "credits_used": credits_used,
        "opportunities": opps,
        "scan_count": current_data["scan_count"] + 1,
    })

    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] Scan complete: {len(opps)} opportunities, {credits_used} credits used")
    broadcast_sse({"type": "scan_complete", "count": len(opps)})

# ═══════════════════════════════════════════════════════════════
# SSE (Server-Sent Events)
# ═══════════════════════════════════════════════════════════════

def broadcast_sse(data):
    msg = f"data: {json.dumps(data)}\n\n"
    dead = []
    for q in sse_clients:
        try: q.put_nowait(msg)
        except: dead.append(q)
    for q in dead:
        try: sse_clients.remove(q)
        except: pass

# ═══════════════════════════════════════════════════════════════
# SCAN LOOP
# ═══════════════════════════════════════════════════════════════

def scan_loop(interval, skip_poly, skip_props, max_prop_events):
    while True:
        with scan_lock:
            try:
                run_scan(skip_poly, skip_props, max_prop_events)
            except Exception as e:
                print(f"Scan error: {e}")
                current_data["scanning"] = False
        time.sleep(interval)

# ═══════════════════════════════════════════════════════════════
# FLASK APP
# ═══════════════════════════════════════════════════════════════

app = Flask(__name__, static_folder=".")
CORS(app)

@app.route("/")
def index():
    return send_from_directory(".", "dashboard.html")

@app.route("/api/data")
def api_data():
    return jsonify(current_data)

@app.route("/api/stream")
def api_stream():
    def gen():
        q = Queue()
        sse_clients.append(q)
        try:
            # Send current state immediately
            yield f"data: {json.dumps({'type': 'connected', 'count': len(current_data['opportunities'])})}\n\n"
            while True:
                msg = q.get()
                yield msg
        except GeneratorExit:
            try: sse_clients.remove(q)
            except: pass

    return Response(gen(), mimetype="text/event-stream",
                   headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/rescan", methods=["POST"])
def api_rescan():
    if current_data["scanning"]:
        return jsonify({"status": "already_scanning"})
    # Trigger immediate scan in background
    threading.Thread(target=lambda: run_scan(
        skip_poly=False, skip_props=False, max_prop_events=10
    ), daemon=True).start()
    return jsonify({"status": "scan_triggered"})

@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(".", path)

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="EV Scanner Live Server")
    p.add_argument("--port", type=int, default=8877)
    p.add_argument("--interval", type=int, default=180, help="Scan interval in seconds (default: 180)")
    p.add_argument("--no-poly", action="store_true")
    p.add_argument("--no-props", action="store_true")
    p.add_argument("--max-prop-events", type=int, default=10)
    a = p.parse_args()

    if not ODDS_API_KEY:
        print("ERROR: ODDS_API_KEY not set in .env"); sys.exit(1)

    import webbrowser

    print(f"""
╔══════════════════════════════════════════════════════╗
║  SystemicFlows — EV Scanner Live                     ║
║  Dashboard: http://localhost:{a.port}                  ║
║  Scan interval: {a.interval}s · Props: {'OFF' if a.no_props else 'ON'}               ║
╚══════════════════════════════════════════════════════╝
""")

    # Start scan loop in background
    t = threading.Thread(target=scan_loop, args=(a.interval, a.no_poly, a.no_props, a.max_prop_events), daemon=True)
    t.start()

    # Auto-open browser after short delay
    threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{a.port}")).start()

    app.run(host="0.0.0.0", port=a.port, debug=False, threaded=True)

if __name__ == "__main__":
    main()