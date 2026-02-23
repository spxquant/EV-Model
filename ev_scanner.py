"""
Sports EV Scanner v3 — Full Edge Map
Pinnacle Fair Value + Player Props + Polymarket
Read-Only • No Order Execution
Author: SystemicFlows

Usage:
    python ev_scanner.py                     # Full scan
    python ev_scanner.py --discover          # List in-season sports
    python ev_scanner.py -s basketball_nba   # Single sport
    python ev_scanner.py --props-only        # Player props only
    python ev_scanner.py --no-props          # Skip props (save credits)
    python ev_scanner.py --no-poly           # Skip Polymarket
    python ev_scanner.py --min-ev 2.0        # Filter by min EV%
    python ev_scanner.py --max-prop-events 5 # Limit prop credit usage
"""

from __future__ import annotations
import argparse, os, re, sys, time, hashlib, json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

try:
    from py_clob_client.client import ClobClient
except ImportError:
    ClobClient = None

load_dotenv()

ODDS_API_KEY     = os.getenv("ODDS_API_KEY", "")
POLY_API_KEY     = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET  = os.getenv("POLY_API_SECRET", "")
POLY_PASSPHRASE  = os.getenv("POLY_PASSPHRASE", "")
POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")

BANKROLL       = float(os.getenv("BANKROLL", "1000"))
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))
CACHE_TTL      = int(os.getenv("CACHE_TTL_SECONDS", "180"))
MIN_EV_DISPLAY = float(os.getenv("MIN_EV_DISPLAY", "-5.0"))

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
POLY_HOST     = "https://clob.polymarket.com"
CHAIN_ID      = 137

DEFAULT_SPORT_GROUPS = ["basketball_nba", "basketball_ncaab", "icehockey_nhl", "baseball_mlb"]
TENNIS_PREFIX = "tennis_"
GAME_MARKETS  = ["h2h", "spreads", "totals"]

PROP_MARKETS = {
    "basketball_nba": ["player_points","player_rebounds","player_assists","player_threes",
                       "player_points_rebounds_assists","player_points_rebounds",
                       "player_points_assists","player_rebounds_assists"],
    "basketball_ncaab": ["player_points","player_rebounds","player_assists"],
    "icehockey_nhl": ["player_points","player_shots_on_goal","player_blocked_shots"],
    "baseball_mlb": ["batter_hits","batter_total_bases","batter_rbis","batter_runs_scored",
                     "batter_walks","batter_strikeouts","batter_home_runs",
                     "pitcher_strikeouts","pitcher_outs"],
}

PROP_LABELS = {
    "player_points": "Points", "player_rebounds": "Rebounds", "player_assists": "Assists",
    "player_threes": "3PT", "player_points_rebounds_assists": "PRA",
    "player_points_rebounds": "Pts+Reb", "player_points_assists": "Pts+Ast",
    "player_rebounds_assists": "Reb+Ast", "player_shots_on_goal": "SOG",
    "player_blocked_shots": "Blocks", "batter_hits": "Hits",
    "batter_total_bases": "Tot Bases", "batter_rbis": "RBIs",
    "batter_runs_scored": "Runs", "batter_walks": "Walks",
    "batter_strikeouts": "Ks(Bat)", "batter_home_runs": "HRs",
    "pitcher_strikeouts": "Ks(Pitch)", "pitcher_outs": "Outs",
}

SOFT_BOOKS = {"draftkings","fanduel","betmgm","espnbet","betrivers","fanatics",
              "williamhill_us","caesars"}

console = Console()
credits_used = 0


# ═══════════════════════════════════════════════════════════════
# CACHE
# ═══════════════════════════════════════════════════════════════

@dataclass
class Cache:
    _s: dict = field(default_factory=dict)
    def _k(self, u, p): return hashlib.md5((u+json.dumps(p,sort_keys=True)).encode()).hexdigest()
    def get(self, u, p):
        k=self._k(u,p)
        if k in self._s:
            ts,d=self._s[k]
            if time.time()-ts<CACHE_TTL: return d
            del self._s[k]
        return None
    def put(self, u, p, d): self._s[self._k(u,p)]=(time.time(),d)

cache = Cache()


# ═══════════════════════════════════════════════════════════════
# DATA
# ═══════════════════════════════════════════════════════════════

@dataclass
class EVOpp:
    sport: str; game: str; market_type: str; selection: str
    line: Optional[float]; sharp_pct: float; target_price: float
    target_source: str; ev_pct: float; kelly_stake: float
    is_prop: bool = False

@dataclass
class PropLine:
    book: str; player: str; market_key: str; side: str
    line: float; decimal_odds: float; implied_prob: float


# ═══════════════════════════════════════════════════════════════
# CREDIT TRACKING
# ═══════════════════════════════════════════════════════════════

def log_credits(resp, label):
    global credits_used
    last = resp.headers.get("x-requests-last", "1")
    rem  = resp.headers.get("x-requests-remaining", "?")
    try: credits_used += int(last)
    except: credits_used += 1
    console.log(f"[dim]{label} — cost:{last} session:{credits_used} remaining:{rem}[/dim]")


# ═══════════════════════════════════════════════════════════════
# SPORT DISCOVERY
# ═══════════════════════════════════════════════════════════════

def discover_sports():
    try:
        r = requests.get(f"{ODDS_API_BASE}/sports/", params={"apiKey":ODDS_API_KEY,"all":"false"}, timeout=10)
        r.raise_for_status()
        return [s for s in r.json() if s.get("active")]
    except Exception as e:
        console.log(f"[red]Discovery failed: {e}[/red]"); return []

def get_targets(in_season, requested=None):
    if requested:
        keys = {s["key"] for s in in_season}
        out = []
        for r in requested:
            if r in keys: out.append(next(s for s in in_season if s["key"]==r))
            else:
                for s in in_season:
                    if s["key"].startswith(r) or s.get("group","").startswith(r): out.append(s)
        return out
    tgt = set(DEFAULT_SPORT_GROUPS)
    return [s for s in in_season if s["key"] in tgt or s["key"].startswith(TENNIS_PREFIX)]


# ═══════════════════════════════════════════════════════════════
# TEAM ALIASES
# ═══════════════════════════════════════════════════════════════

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

def _norm(n): return " ".join(n.lower().strip().removeprefix("the ").removeprefix("team ").split())

_AIDX = {}
for _fn, _al in TEAM_ALIASES.items():
    _AIDX[_norm(_fn)] = _fn
    for _a in _al: _AIDX[_norm(_a)] = _fn

def team_match(q, c):
    nq,nc = _norm(q),_norm(c)
    if nq==nc: return True
    cq,cc = _AIDX.get(nq), _AIDX.get(nc)
    if cq and cc and cq==cc: return True
    if nq in nc or nc in nq: return True
    if cc:
        for a in TEAM_ALIASES.get(cc,[]):
            if _norm(a)==nq: return True
    if cq:
        for a in TEAM_ALIASES.get(cq,[]):
            if _norm(a)==nc: return True
    return False


# ═══════════════════════════════════════════════════════════════
# ODDS API CALLS
# ═══════════════════════════════════════════════════════════════

def fetch_odds(sport, market):
    url = f"{ODDS_API_BASE}/sports/{sport}/odds/"
    params = {"apiKey":ODDS_API_KEY,"regions":"eu","markets":market,
              "oddsFormat":"decimal","bookmakers":"pinnacle"}
    c = cache.get(url,params)
    if c is not None: return c
    try:
        r = requests.get(url,params=params,timeout=15); r.raise_for_status()
        data = r.json(); log_credits(r, f"Game: {sport}/{market}")
        cache.put(url,params,data); return data
    except requests.HTTPError as e:
        if e.response and e.response.status_code==404:
            console.log(f"[dim yellow]  {sport}/{market} — N/A[/dim yellow]")
        else: console.log(f"[red]Odds err ({sport}/{market}): {e}[/red]")
        return []
    except Exception as e:
        console.log(f"[red]Odds err: {e}[/red]"); return []

def fetch_events(sport):
    try:
        r = requests.get(f"{ODDS_API_BASE}/sports/{sport}/events/",
                        params={"apiKey":ODDS_API_KEY}, timeout=10)
        r.raise_for_status(); return r.json()
    except: return []

def fetch_event_props(sport, event_id, prop_mkts):
    url = f"{ODDS_API_BASE}/sports/{sport}/events/{event_id}/odds"
    params = {"apiKey":ODDS_API_KEY,"regions":"us,us2",
              "markets":",".join(prop_mkts),"oddsFormat":"decimal"}
    c = cache.get(url,params)
    if c is not None: return c
    try:
        r = requests.get(url,params=params,timeout=20); r.raise_for_status()
        data = r.json(); log_credits(r, f"Props: {sport}/{event_id[:12]}...")
        cache.put(url,params,data); return data
    except requests.HTTPError as e:
        if e.response and e.response.status_code==404:
            console.log(f"[dim yellow]  Props N/A for {event_id[:12]}[/dim yellow]")
        else: console.log(f"[red]Props err: {e}[/red]")
        return {}
    except Exception as e:
        console.log(f"[red]Props err: {e}[/red]"); return {}


# ═══════════════════════════════════════════════════════════════
# VIG REMOVAL
# ═══════════════════════════════════════════════════════════════

def dec2imp(d): return 1.0 if d<=1.0 else 1.0/d

def remove_vig(outcomes):
    if not outcomes: return {}
    imp = [{"n":o.get("name",""),"d":o.get("description",""),
            "i":dec2imp(o.get("price",0)),"l":o.get("point")} for o in outcomes]
    tot = sum(x["i"] for x in imp)
    if tot==0: return {}
    res = {}
    for x in imp:
        k = f"{x['d']}|{x['n']}" if x["d"] else x["n"]
        res[k] = {"fair":x["i"]/tot,"line":x["l"],"desc":x["d"],"name":x["n"]}
    return res


# ═══════════════════════════════════════════════════════════════
# EXTRACT PINNACLE
# ═══════════════════════════════════════════════════════════════

def extract_pinnacle(events, market):
    out = []
    for ev in events:
        h,a = ev.get("home_team",""), ev.get("away_team","")
        for bm in ev.get("bookmakers",[]):
            if bm.get("key")!="pinnacle": continue
            for m in bm.get("markets",[]):
                if m.get("key")!=market: continue
                out.append({"sport":ev.get("sport_key",""),"game":f"{a} @ {h}",
                           "home":h,"away":a,"commence":ev.get("commence_time",""),
                           "market":market,"outcomes":m.get("outcomes",[])})
    return out


# ═══════════════════════════════════════════════════════════════
# PLAYER PROPS EV
# ═══════════════════════════════════════════════════════════════

def extract_prop_lines(event_data):
    lines = []
    for bm in event_data.get("bookmakers",[]):
        bk = bm.get("key","")
        for m in bm.get("markets",[]):
            mk = m.get("key","")
            for o in m.get("outcomes",[]):
                pl = o.get("description","")
                if not pl: continue
                side = o.get("name","")
                pt = o.get("point")
                pr = o.get("price",0)
                if pr<=1.0 or pt is None: continue
                lines.append(PropLine(bk,pl,mk,side,float(pt),pr,dec2imp(pr)))
    return lines

def find_prop_ev(lines, game_label, sport_title, min_books=3):
    groups = defaultdict(list)
    for l in lines:
        groups[(l.player,l.market_key,l.side,l.line)].append(l)

    opps = []
    for (player,mk,side,line), blines in groups.items():
        if len(blines) < min_books: continue
        for i, tgt in enumerate(blines):
            if tgt.book not in SOFT_BOOKS: continue
            others = [b for j,b in enumerate(blines) if j!=i]
            if len(others)<2: continue
            consensus = sum(b.implied_prob for b in others)/len(others)
            if consensus<=0 or tgt.implied_prob<=0: continue
            ev = (consensus/tgt.implied_prob - 1.0)*100
            if ev < 1.0: continue
            stake = kelly(consensus, tgt.implied_prob)
            label = PROP_LABELS.get(mk, mk)
            opps.append(EVOpp(
                sport=sport_title, game=game_label,
                market_type=f"{label} {side[0]} {line}",
                selection=player, line=line,
                sharp_pct=consensus, target_price=tgt.implied_prob,
                target_source=tgt.book.replace("_"," ").title(),
                ev_pct=ev, kelly_stake=stake, is_prop=True,
            ))
    return opps


# ═══════════════════════════════════════════════════════════════
# POLYMARKET
# ═══════════════════════════════════════════════════════════════

def init_poly():
    if not ClobClient: console.log("[yellow]py_clob_client missing[/yellow]"); return None
    if not all([POLY_API_KEY,POLY_API_SECRET,POLY_PASSPHRASE]):
        console.log("[yellow]Poly creds not set[/yellow]"); return None
    try:
        return ClobClient(POLY_HOST,key=POLY_PRIVATE_KEY,chain_id=CHAIN_ID,
            creds={"apiKey":POLY_API_KEY,"secret":POLY_API_SECRET,"passphrase":POLY_PASSPHRASE})
    except Exception as e:
        console.log(f"[red]Poly init fail: {e}[/red]"); return None

def fetch_poly_markets(client):
    all_m, cursor = [], ""
    kw = ["nba","nhl","mlb","atp","tennis","basketball","hockey","baseball",
          "win","beat","score","points","goals","spread","total","over","under",
          "champion","playoff","series","game","all-star","mvp"]
    try:
        for _ in range(10):
            r = client.get_markets(next_cursor=cursor)
            mkts = r if isinstance(r,list) else r.get("data",[])
            cursor = "" if isinstance(r,list) else r.get("next_cursor","")
            for m in mkts:
                txt = (m.get("question","")+m.get("description","")).lower()
                if any(k in txt for k in kw) and m.get("active") and not m.get("closed",True):
                    all_m.append(m)
            if not cursor or cursor=="LQ==": break
    except Exception as e: console.log(f"[red]Poly err: {e}[/red]")
    console.log(f"[dim]Polymarket: {len(all_m)} sports markets[/dim]")
    return all_m

def poly_best_price(client, tid, side="buy"):
    try:
        book = client.get_order_book(tid)
        if side=="buy":
            asks = book.get("asks",[])
            if asks: return float(min(asks,key=lambda x:float(x.get("price",999)))["price"])
        else:
            bids = book.get("bids",[])
            if bids: return float(max(bids,key=lambda x:float(x.get("price",0)))["price"])
    except: pass
    return None

def match_poly_game(pm, game, mtype):
    txt = (pm.get("question","")+pm.get("description","")).lower()
    hm = any(_norm(a) in txt for a in [game["home"]]+TEAM_ALIASES.get(game["home"],[]))
    am = any(_norm(a) in txt for a in [game["away"]]+TEAM_ALIASES.get(game["away"],[]))
    if not(hm or am): return None
    if mtype=="h2h": return {"type":"ml","market":pm}
    if mtype=="spreads":
        f=re.findall(r"[+-]?\d+\.5",txt)
        if f: return {"type":"spread","market":pm,"lines":[float(x) for x in f]}
    if mtype=="totals":
        f=re.findall(r"(?:over|under)\s*(\d+\.?\d*)",txt)
        if f: return {"type":"total","market":pm,"lines":[float(x) for x in f]}
    return None

def poly_tokens(m):
    toks = m.get("tokens",[])
    if not toks:
        c=m.get("condition_id","")
        return [{"token_id":c,"outcome":"YES"}] if c else []
    return [{"token_id":t["token_id"],"outcome":t.get("outcome","")} for t in toks if t.get("token_id")]


# ═══════════════════════════════════════════════════════════════
# EV + KELLY
# ═══════════════════════════════════════════════════════════════

def calc_ev(sharp, target):
    if target<=0 or target>=1: return 0.0
    return (sharp/target)-1.0

def kelly(sharp, target, br=BANKROLL, frac=KELLY_FRACTION):
    if target<=0 or target>=1: return 0.0
    b = (1.0/target)-1.0
    if b<=0: return 0.0
    f = (b*sharp-(1-sharp))/b
    return round(br*frac*f, 2) if f>0 else 0.0


# ═══════════════════════════════════════════════════════════════
# SCAN
# ═══════════════════════════════════════════════════════════════

def scan(sport_keys=None, min_ev=MIN_EV_DISPLAY, skip_poly=False,
         skip_props=False, props_only=False, max_prop_events=10):

    opps = []

    with console.status("[bold cyan]Discovering sports..."):
        in_season = discover_sports()
    if not in_season:
        console.print("[red]No sports data.[/red]"); return []

    targets = get_targets(in_season, sport_keys)
    if not targets:
        console.print("[yellow]No requested sports in-season.[/yellow]")
        console.print("\n[bold]Available:[/bold]")
        for s in in_season: console.print(f"  [cyan]{s['key']}[/cyan] — {s.get('title','')}")
        return []

    console.print(f"\n[bold]Scanning {len(targets)} sport(s):[/bold]")
    for s in targets:
        hp = s["key"] in PROP_MARKETS
        tag = " [green][+props][/green]" if hp and not skip_props else ""
        console.print(f"  [cyan]{s['key']}[/cyan] — {s.get('title','')}{tag}")
    console.print()

    # Polymarket
    pcli, pmkts = None, []
    if not skip_poly and not props_only:
        pcli = init_poly()
        if pcli:
            with console.status("[bold cyan]Fetching Polymarket..."):
                pmkts = fetch_poly_markets(pcli)

    for si in targets:
        sk, st = si["key"], si.get("title",si["key"])
        console.rule(f"[bold]{st}")

        upcoming = fetch_events(sk)
        if upcoming: console.log(f"[dim]  {len(upcoming)} upcoming events[/dim]")
        else: console.log(f"[dim yellow]  No events — skip[/dim yellow]"); continue

        # GAME LINES
        if not props_only:
            for mt in GAME_MARKETS:
                with console.status(f"[cyan]  {sk}/{mt}..."):
                    evts = fetch_odds(sk, mt)
                pdata = extract_pinnacle(evts, mt)
                if not pdata:
                    console.log(f"[dim]  No Pinnacle — {mt}[/dim]"); continue
                console.log(f"[green]  {len(pdata)} Pinnacle lines — {mt}[/green]")

                for g in pdata:
                    fairs = remove_vig(g["outcomes"])
                    for oname, fi in fairs.items():
                        fp, ln = fi["fair"], fi["line"]
                        if mt=="h2h": ml,sel = "ML", oname
                        elif mt=="spreads":
                            s = "+" if ln and ln>0 else ""
                            ml,sel = f"Spread {s}{ln}" if ln else "Spread", oname
                        elif mt=="totals":
                            ml,sel = f"Total {oname} {ln}" if ln else f"Total {oname}", oname
                        else: ml,sel = mt, oname

                        pp = None
                        if pcli and pmkts:
                            for pm in pmkts:
                                m = match_poly_game(pm,g,mt)
                                if not m: continue
                                if mt in ("spreads","totals") and ln is not None:
                                    mls = m.get("lines",[])
                                    if mls and ln not in mls: continue
                                for tk in poly_tokens(pm):
                                    if mt=="h2h":
                                        if team_match(oname,pm.get("question","")):
                                            p=poly_best_price(pcli,tk["token_id"],"buy")
                                            if p: pp=p; break
                                    elif tk["outcome"].lower()=="yes":
                                        p=poly_best_price(pcli,tk["token_id"],"buy")
                                        if p: pp=p; break
                                if pp: break

                        if pp and pp>0:
                            ev=calc_ev(fp,pp)*100; stk=kelly(fp,pp); src="Polymarket"
                        else: ev=0.0; stk=0.0; pp=None; src="—"

                        if ev>=min_ev or pp is not None:
                            opps.append(EVOpp(st,g["game"],ml,sel,ln,fp,
                                             pp if pp else 0.0,src,ev,stk,False))

        # PROPS
        if not skip_props and sk in PROP_MARKETS:
            pmk = PROP_MARKETS[sk]
            to_scan = upcoming[:max_prop_events]
            console.log(f"[cyan]  Props: {len(to_scan)} events × {len(pmk)} markets...[/cyan]")

            for ev in to_scan:
                eid = ev.get("id","")
                h,a = ev.get("home_team",""), ev.get("away_team","")
                gl = f"{a} @ {h}"
                if not eid: continue

                with console.status(f"[cyan]  Props: {gl}..."):
                    edata = fetch_event_props(sk, eid, pmk)
                if not edata or "bookmakers" not in edata: continue

                plines = extract_prop_lines(edata)
                if not plines: continue
                bks = len(set(l.book for l in plines))
                console.log(f"[green]  {gl}: {len(plines)} lines / {bks} books[/green]")

                opps.extend(find_prop_ev(plines, gl, st))

    return opps


# ═══════════════════════════════════════════════════════════════
# DASHBOARD
# ═══════════════════════════════════════════════════════════════

def render(opps):
    opps.sort(key=lambda x: x.ev_pct, reverse=True)

    console.print()
    console.print(Panel(Text.from_markup(
        "[bold white]SPORTS EV SCANNER v3[/bold white]\n"
        "[dim]Pinnacle + Player Props + Polymarket · Read-Only[/dim]\n"
        f"[dim]Bankroll: ${BANKROLL:,.0f} · Kelly: {KELLY_FRACTION:.0%} · "
        f"Credits used: {credits_used}[/dim]"
    ), box=box.DOUBLE_EDGE, border_style="cyan", expand=False))

    if not opps:
        console.print(
            "\n[yellow]No opportunities found.[/yellow]"
            "\n[dim]  • Off-day / All-Star break / off-season[/dim]"
            "\n[dim]  • Lines not posted yet[/dim]"
            "\n[dim]  • Run --discover to see available sports[/dim]\n")
        return

    game_o = [o for o in opps if not o.is_prop]
    prop_o = [o for o in opps if o.is_prop]

    if game_o:
        t = Table(box=box.ROUNDED, title="Game Lines — Pinnacle vs Polymarket",
                  title_style="bold white", show_lines=True, padding=(0,1))
        t.add_column("Game", min_width=22)
        t.add_column("Bet Type", min_width=14)
        t.add_column("Selection", min_width=14)
        t.add_column("Implied Prob", justify="right", min_width=10)
        t.add_column("Model Prob", justify="right", min_width=10)
        t.add_column("+EV %", justify="right", min_width=7)
        t.add_column("Stake", justify="right", min_width=7)
        for o in game_o:
            es = "bold green" if o.ev_pct>3 else "green" if o.ev_pct>0 else "red" if o.ev_pct<0 else "white"
            rs = "" if o.ev_pct>0 else "dim"
            ip = f"{o.target_price:.1%}" if o.target_price>0 else "—"
            ed = f"{o.ev_pct:+.1f}%" if o.target_price>0 else "—"
            sd = f"${o.kelly_stake:.0f}" if o.kelly_stake>0 else "—"
            t.add_row(o.game,o.market_type,o.selection,
                     ip,f"{o.sharp_pct:.1%}",Text(ed,style=es),
                     Text(sd,style=es if o.kelly_stake>0 else "dim"),style=rs)
        console.print(t)

    if prop_o:
        t = Table(box=box.ROUNDED, title="Player Props — Multi-Book Consensus EV",
                  title_style="bold magenta", show_lines=True, padding=(0,1))
        t.add_column("Game", min_width=22)
        t.add_column("Player", min_width=16)
        t.add_column("Bet Type", min_width=14)
        t.add_column("Implied Prob", justify="right", min_width=10)
        t.add_column("Model Prob", justify="right", min_width=10)
        t.add_column("Book", min_width=12)
        t.add_column("+EV %", justify="right", min_width=7)
        t.add_column("Stake", justify="right", min_width=7)
        for o in prop_o:
            es = "bold green" if o.ev_pct>5 else "green" if o.ev_pct>2 else "yellow"
            t.add_row(o.game,o.selection,o.market_type,
                     f"{o.target_price:.1%}",f"{o.sharp_pct:.1%}",
                     o.target_source,Text(f"+{o.ev_pct:.1f}%",style=es),
                     Text(f"${o.kelly_stake:.0f}",style=es))
        console.print(t)

    console.print()
    pos = [o for o in opps if o.ev_pct>0 and (o.target_price>0 or o.is_prop)]
    if pos:
        gp = sum(1 for o in pos if not o.is_prop)
        pp = sum(1 for o in pos if o.is_prop)
        ts = sum(o.kelly_stake for o in pos)
        parts = []
        if gp: parts.append(f"{gp} game lines")
        if pp: parts.append(f"{pp} props")
        console.print(f"  [bold green]{len(pos)} +EV opps[/bold green] ({', '.join(parts)})"
                     f"  |  Total stake: [bold]${ts:.0f}[/bold]")
    else:
        console.print("  [dim]No +EV at current prices.[/dim]")

    console.print(f"\n  [dim]{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC · "
                 f"Credits: {credits_used} · READ-ONLY[/dim]\n")


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="Sports EV Scanner v3")
    p.add_argument("--sport","-s",action="append",help="Sport key(s)")
    p.add_argument("--min-ev",type=float,default=MIN_EV_DISPLAY)
    p.add_argument("--no-poly",action="store_true")
    p.add_argument("--no-props",action="store_true")
    p.add_argument("--props-only",action="store_true")
    p.add_argument("--max-prop-events",type=int,default=10)
    p.add_argument("--discover",action="store_true")
    p.add_argument("--json",action="store_true",help="Export results to ev_data.json")
    p.add_argument("--serve",action="store_true",help="Export JSON + open dashboard")
    a = p.parse_args()

    if not ODDS_API_KEY:
        console.print("[bold red]ERROR: ODDS_API_KEY not in .env[/bold red]"); sys.exit(1)

    if a.discover:
        console.print("\n[bold]In-season sports:[/bold]\n")
        ss = discover_sports()
        if not ss: console.print("[red]None.[/red]"); sys.exit(1)
        t = Table(title="Available Sports",box=box.ROUNDED,show_lines=False)
        t.add_column("Key",style="cyan"); t.add_column("Title")
        t.add_column("Group",style="dim"); t.add_column("Props?",justify="center")
        for s in sorted(ss,key=lambda x:x.get("group","")):
            t.add_row(s["key"],s.get("title",""),s.get("group",""),
                     "[green]✓[/green]" if s["key"] in PROP_MARKETS else "")
        console.print(t)
        console.print(f"\n[dim]{len(ss)} active · python ev_scanner.py -s <key>[/dim]\n")
        return

    results = scan(a.sport, a.min_ev, a.no_poly, a.no_props, a.props_only, a.max_prop_events)
    render(results)

    if a.json or a.serve:
        export = {
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "bankroll": BANKROLL,
            "kelly_fraction": KELLY_FRACTION,
            "credits_used": credits_used,
            "opportunities": [
                {
                    "sport": o.sport, "game": o.game, "market_type": o.market_type,
                    "selection": o.selection, "line": o.line,
                    "implied_prob": round(o.target_price, 4),
                    "model_prob": round(o.sharp_pct, 4),
                    "book": o.target_source, "ev_pct": round(o.ev_pct, 2),
                    "kelly_stake": o.kelly_stake, "is_prop": o.is_prop,
                }
                for o in results
            ],
        }
        with open("ev_data.json", "w") as f:
            json.dump(export, f, indent=2)
        console.print(f"\n[bold green]Exported {len(results)} rows → ev_data.json[/bold green]")

        if a.serve:
            import webbrowser, http.server, threading
            port = 8877
            handler = http.server.SimpleHTTPRequestHandler
            srv = http.server.HTTPServer(("", port), handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            console.print(f"[bold cyan]Dashboard → http://localhost:{port}/dashboard.html[/bold cyan]")
            webbrowser.open(f"http://localhost:{port}/dashboard.html")
            try:
                while True: time.sleep(1)
            except KeyboardInterrupt:
                console.print("\n[dim]Server stopped.[/dim]")

if __name__=="__main__": main()