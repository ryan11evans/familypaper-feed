#!/usr/bin/env python3
"""Build sports.json (and the app's bundled teams.json) from ESPN's public, key-free JSON.

Public data only: team names, scores, schedules. No family data ever goes in here.
Deterministic: same ESPN data + same --now -> byte-identical output (sorted keys, sorted teams).

  python3 tools/publish_sports.py                      # writes ./sports.json
  python3 tools/publish_sports.py --teams-out PATH     # also writes the app's bundled team list
"""
import argparse, concurrent.futures as cf, datetime as dt, json, sys, time, urllib.request

SITE = "https://site.api.espn.com/apis/site/v2/sports"
STAND = "https://site.api.espn.com/apis/v2/sports"
LEAGUES = [  # key, ESPN path, label, college conferences (None = pro: all teams)
    ("nfl", "football/nfl", "NFL", None),
    ("nba", "basketball/nba", "NBA", None),
    ("mlb", "baseball/mlb", "MLB", None),
    ("nhl", "hockey/nhl", "NHL", None),
    ("cfb", "football/college-football", "College Football", {"acc", "big12", "big10", "sec", "pac12", "ind"}),
    ("cbb", "basketball/mens-college-basketball", "College Basketball", {"acc", "big12", "big10", "sec", "bige"}),
]
WINDOW_DAYS = 21
BLURB_DAYS = 7            # a team gets a recap blurb if its last game was within this many days
STAR_DAYS = 4             # ...and a "star player" line (one extra summary request) within this many          # keep a team only if it played or plays within this many days
SCHEMA_VERSION = 1

def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "familypaper-feed/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1: raise
            time.sleep(1 + i)

def teams_for(key, path, confs):
    if confs is None:
        d = get(f"{SITE}/{path}/teams?limit=1000")
        raw = [t["team"] for t in d["sports"][0]["leagues"][0]["teams"]]
    else:
        d = get(f"{STAND}/{path}/standings")
        raw = [e["team"] for c in d.get("children", []) if c.get("abbreviation") in confs
               for e in c.get("standings", {}).get("entries", [])]
    out = {}
    for t in raw:
        if t.get("isActive") is False or t.get("isAllStar"): continue
        out[str(t["id"])] = {"id": f"{key}:{t['id']}", "name": t.get("displayName", ""),
                             "short": t.get("shortDisplayName") or t.get("name") or t.get("displayName", ""),
                             "abbr": t.get("abbreviation", "")}
    return list(out.values())

def iso(s):  # ESPN "2026-10-18T20:25Z"
    return dt.datetime.strptime(s.replace("Z", ""), "%Y-%m-%dT%H:%M" if len(s) <= 17 else "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)

def score(c):
    s = c.get("score")
    if isinstance(s, dict): s = s.get("value", s.get("displayValue"))
    try: return int(float(s))
    except (TypeError, ValueError): return None

def schedule_events(path, espn_id, now):
    """ESPN's team schedule only returns the CURRENT season type (e.g. NBA preseason), so the
    first regular-season / postseason game was missing. If there's no upcoming game, also pull
    the regular season (2) and postseason (3) and merge by event id."""
    d = get(f"{SITE}/{path}/teams/{espn_id}/schedule")
    events = {e["id"]: e for e in d.get("events", []) if e.get("id")}
    def has_future(evs):
        return any(e.get("date") and iso(e["date"]) > now and
                   (e.get("competitions") or [{}])[0].get("status", {}).get("type", {}).get("state") == "pre"
                   for e in evs)
    if not has_future(events.values()):
        for st in (2, 3):
            try:
                more = get(f"{SITE}/{path}/teams/{espn_id}/schedule?seasontype={st}").get("events", [])
            except Exception:
                continue
            for e in more:
                if e.get("id"): events.setdefault(e["id"], e)
            if has_future(events.values()): break
    return sorted(events.values(), key=lambda e: e.get("date", ""))

def team_games(path, espn_id, now):
    last = nxt = None
    for e in schedule_events(path, espn_id, now):
        comp = (e.get("competitions") or [{}])[0]
        cs = comp.get("competitors", [])
        me = next((c for c in cs if str(c.get("id")) == espn_id), None)
        op = next((c for c in cs if str(c.get("id")) != espn_id), None)
        if not me or not op or not e.get("date"): continue
        when = iso(e["date"]); st = comp.get("status", {}).get("type", {})
        opp = op["team"].get("shortDisplayName") or op["team"].get("displayName", "")
        home = me.get("homeAway") == "home"
        if st.get("completed"):
            a, b = score(me), score(op)
            if a is None or b is None: continue
            g = {"date": when.strftime("%Y-%m-%d"), "opponent": opp, "home": home, "team_score": a, "opp_score": b,
                 "result": "W" if a > b else ("L" if a < b else "T"),
                 "_event": e["id"], "_when": when, "_stype": (e.get("seasonType") or {}).get("type"),
                 "_neutral": bool(comp.get("neutralSite"))}
            if when <= now and (last is None or g["date"] >= last["date"]): last = g
        elif st.get("state") == "pre" and when > now and e.get("timeValid", True) is not False:
            g = {"start_utc": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "opponent": opp, "home": home}
            if nxt is None or g["start_utc"] < nxt["start_utc"]: nxt = g
    return last, nxt

PRO = {"nfl", "nba", "mlb", "nhl"}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

def star_line(path, league, espn_id, event_id):
    """One positive stat line for the team's top performer, from ESPN's game summary leaders.
    Only numbers + a name: no injuries, no off-field news, nothing to moderate."""
    try:
        s = get(f"{SITE}/{path}/summary?event={event_id}")
    except Exception:
        return None
    mine = next((t for t in s.get("leaders", []) if str((t.get("team") or {}).get("id")) == espn_id), None)
    if not mine: return None
    cats = {c.get("name"): c["leaders"][0] for c in mine.get("leaders", []) if c.get("leaders")}
    def num(v, unit):
        import re
        m = re.search(r"(\d+)\s*" + unit, v or "")
        return int(m.group(1)) if m else None
    def nm(c): return (c.get("athlete") or {}).get("displayName")
    if league in ("nfl", "cfb"):
        p, r = cats.get("passingYards"), cats.get("rushingYards")
        py, ry = num(p and p.get("displayValue"), "YDS"), num(r and r.get("displayValue"), "YDS")
        if py and py >= 150 and nm(p): return f"{nm(p)} threw for {py} yards."
        if ry and ry >= 50 and nm(r): return f"{nm(r)} ran for {ry} yards."
        if py and nm(p): return f"{nm(p)} threw for {py} yards."
    elif league in ("nba", "cbb"):
        c = cats.get("points")
        n = num(c and c.get("displayValue"), "")
        if n and nm(c): return f"{nm(c)} led the team with {n} points."
    elif league == "nhl":
        c = cats.get("goals")
        n = num(c and c.get("displayValue"), "")
        if n and nm(c): return f"{nm(c)} scored {'a goal' if n == 1 else f'{n} goals'}" + ("!" if n >= 3 else ".")
    return None

def recap(league, name, last, star):
    """Deterministic, kid-friendly one/two sentences built only from the score facts."""
    the = lambda n: f"the {n}" if league in PRO else n
    team, opp = the(name), the(last["opponent"])
    team = team[0].upper() + team[1:]
    a, b = last["team_score"], last["opp_score"]
    where = "" if last["_neutral"] else (" at home" if last["home"] else " on the road")
    day = WEEKDAYS[last["_local_weekday"]]
    kind = {1: " in a preseason game", 3: " in the playoffs"}.get(last["_stype"], "")
    if last["result"] == "W":
        verb = f"beat {opp} {a}–{b}"
    elif last["result"] == "L":
        verb = f"lost to {opp} {b}–{a}"
    else:
        verb = f"tied {opp} {a}–{b}"
    s = f"{team} {verb}{where} on {day}{kind}."
    return f"{s} {star}" if star else s

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="sports.json")
    ap.add_argument("--teams-out")
    ap.add_argument("--now", help="UTC ISO time, for reproducible runs")
    a = ap.parse_args()
    now = dt.datetime.fromisoformat(a.now.replace("Z", "+00:00")) if a.now else dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    lo, hi = (now - dt.timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d"), (now + dt.timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    catalog, jobs = [], []
    for key, path, label, confs in LEAGUES:
        ts = sorted(teams_for(key, path, confs), key=lambda t: t["name"])
        catalog.append({"key": key, "label": label, "teams": ts})
        jobs += [(path, t) for t in ts]
    teams, failures = {}, 0
    with cf.ThreadPoolExecutor(12) as ex:
        futs = {ex.submit(team_games, p, t["id"].split(":")[1], now): t for p, t in jobs}
        for f in cf.as_completed(futs):
            t = futs[f]
            try: last, nxt = f.result()
            except Exception as e:
                failures += 1; print(f"warn: {t['id']} {e}", file=sys.stderr); continue
            if last and last["date"] < lo: last = None
            if nxt and nxt["start_utc"] > hi: nxt = None
            if last or nxt:
                teams[t["id"]] = {"name": t["short"], **({"last": last} if last else {}), **({"next": nxt} if nxt else {})}
    # Recap blurbs: only for teams that played within BLURB_DAYS; star lines (one more request
    # each) only within STAR_DAYS. Weekday in Pacific time (games are evening US time).
    pacific = dt.timezone(dt.timedelta(hours=-8))
    league_path = {k: p for k, p, _, _ in LEAGUES}
    recent = [(tid, g) for tid, g in teams.items() if "last" in g and (now - g["last"]["_when"]).days < BLURB_DAYS]
    with cf.ThreadPoolExecutor(12) as ex:
        stars = {tid: ex.submit(star_line, league_path[tid.split(":")[0]], tid.split(":")[0], tid.split(":")[1], g["last"]["_event"])
                 for tid, g in recent if (now - g["last"]["_when"]).days < STAR_DAYS}
        for tid, g in recent:
            last = g["last"]
            last["_local_weekday"] = (last["_when"] - dt.timedelta(hours=7 if 3 <= last["_when"].month <= 10 else 8)).weekday()
            star = stars[tid].result() if tid in stars else None
            g["blurb"] = recap(tid.split(":")[0], g["name"], last, star)
    for g in teams.values():
        for k in [k for k in g.get("last", {}) if k.startswith("_")]: del g["last"][k]
    if failures > len(jobs) // 4:
        sys.exit(f"too many ESPN failures ({failures}/{len(jobs)}); not writing")
    doc = {"schema_version": SCHEMA_VERSION, "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "source": "ESPN public scoreboard data", "teams": dict(sorted(teams.items()))}
    with open(a.out, "w") as f: json.dump(doc, f, sort_keys=True, separators=(",", ":"), ensure_ascii=False); f.write("\n")
    print(f"{a.out}: {len(teams)} teams with games, {failures} failures")
    if a.teams_out:
        with open(a.teams_out, "w") as f: json.dump({"leagues": catalog}, f, indent=1, ensure_ascii=False); f.write("\n")
        print(f"{a.teams_out}: {sum(len(l['teams']) for l in catalog)} teams")

if __name__ == "__main__":
    main()
