#!/usr/bin/env python3
"""Build sports.json (and the app's bundled teams.json) from ESPN's public, key-free JSON.

Public data only: team names, scores, schedules. No family data ever goes in here.
Deterministic: same ESPN data + same --now -> byte-identical output (sorted keys, sorted teams).

  python3 tools/publish_sports.py                      # writes ./sports.json
  python3 tools/publish_sports.py --teams-out PATH     # also writes the app's bundled team list
"""
import argparse, concurrent.futures as cf, datetime as dt, json, re, sys, time, urllib.request

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
WINDOW_DAYS = 21          # keep a NEXT game only if it starts within this many days
LAST_DAYS = 120           # keep the most recent completed game this far back (preseason gaps, bye weeks)
STAR_DAYS = 4             # a "star player" blurb (one extra summary request) if the last game was this recent
BLURB_MAX = 60            # blurbs are one short line in a half-width box on the printed page
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

def _state(e):
    return (e.get("competitions") or [{}])[0].get("status", {}).get("type", {})

def schedule_events(path, espn_id, now):
    """ESPN's team schedule only returns the CURRENT season type, so (a) the first regular-season /
    postseason game was missing during preseason and (b) the last completed game was missing right
    after the season type flips (NBA opening day: preseason games drop out of the default list, so
    the Lakers box had no "Last" line). Pull the other season types as needed; merge by event id.
    Returns (events sorted by date, season info from the default request)."""
    d = get(f"{SITE}/{path}/teams/{espn_id}/schedule")
    events = {e["id"]: e for e in d.get("events", []) if e.get("id")}
    def merge(q):
        try:
            for e in get(f"{SITE}/{path}/teams/{espn_id}/schedule?{q}").get("events", []):
                if e.get("id"): events.setdefault(e["id"], e)
        except Exception:
            pass
    def has_future():
        return any(e.get("date") and iso(e["date"]) > now and _state(e).get("state") == "pre" for e in events.values())
    def has_past():
        return any(e.get("date") and iso(e["date"]) <= now and _state(e).get("completed") for e in events.values())
    cur = d.get("requestedSeason") or d.get("season") or {}
    if not has_future():
        for st in (2, 3):
            merge(f"seasontype={st}")
            if has_future(): break
    if not has_past():
        for st in (3, 2, 1):            # this season's postseason, regular season, preseason
            if st != cur.get("type"): merge(f"seasontype={st}")
            if has_past(): break
        if not has_past() and cur.get("year"):   # offseason: last season's games
            for st in (3, 2):
                merge(f"season={cur['year'] - 1}&seasontype={st}")
                if has_past(): break
    team = d.get("team") or {}
    info = {"season_type": cur.get("type"), "record": team.get("recordSummary") or None,
            "standing": team.get("standingSummary") or None}
    return sorted(events.values(), key=lambda e: e.get("date", "")), info

def team_games(path, espn_id, now):
    last = nxt = None
    events, info = schedule_events(path, espn_id, now)
    season_results = []                 # (season year, W/L/T, season type) of completed regular/postseason games
    for e in events:
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
            g = {"date": when.strftime("%Y-%m-%d"), "start_utc": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "opponent": opp, "home": home, "team_score": a, "opp_score": b,
                 "result": "W" if a > b else ("L" if a < b else "T"),
                 "_event": e["id"], "_when": when, "_stype": (e.get("seasonType") or {}).get("type"),
                 "_neutral": bool(comp.get("neutralSite"))}
            if g["_stype"] == 1: g["preseason"] = True                  # additive key; old apps ignore it
            if when <= now and (last is None or g["date"] >= last["date"]): last = g
            if g["_stype"] in (2, 3) and when <= now:
                season_results.append(((e.get("season") or {}).get("year"), g["result"], g["_stype"], when))
        elif st.get("state") == "pre" and when > now and e.get("timeValid", True) is not False:
            g = {"start_utc": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "opponent": opp, "home": home,
                 "_stype": (e.get("seasonType") or {}).get("type"), "_year": (e.get("season") or {}).get("year")}
            if nxt is None or g["start_utc"] < nxt["start_utc"]: nxt = g
    # Season context for the blurb, only from this season's regular/postseason games (never preseason).
    cur_year = max((y or 0 for y, _, _, _ in season_results), default=None)
    if season_results and ((nxt and nxt.get("_year") and nxt["_year"] != cur_year) or
                           now - season_results[-1][3] > dt.timedelta(days=LAST_DAYS)):
        season_results, cur_year = [], None     # new season next / long offseason: old record is old news
    stype = 3 if any(y == cur_year and t == 3 for y, _, t, _ in season_results) else 2
    res = [r for y, r, t, _ in season_results if (y or 0) == cur_year and t == stype]
    streak = 0
    for r in reversed(res):
        if r != "W": break
        streak += 1
    info = dict(info, win_streak=streak)
    # ESPN's recordSummary can belong to a different season/type (e.g. "0-0" for next season in
    # the MLB offseason). Trust it only if it adds up to the games we counted; otherwise use W-L(-T).
    rec = info.get("record") or ""
    parts = [int(x) for x in rec.split("-")] if re.fullmatch(r"\d+(-\d+){1,2}", rec) else []
    if not res:
        info["record"] = info["standing"] = None
    elif sum(parts) != len(res) or info.get("season_type") != stype:
        w, l, t = res.count("W"), res.count("L"), res.count("T")
        info["record"] = f"{w}-{l}" + (f"-{t}" if t else "")
        info["standing"] = None
    info["season_type"] = stype if res else info.get("season_type")
    # Season opener: the next game is a regular-season game and the team hasn't played a
    # regular-season or postseason game of that season yet.
    info["opener"] = bool(nxt and nxt.get("_stype") == 2 and not any(y == nxt.get("_year") for y, _, _, _ in season_results))
    return last, nxt, info

PRO = {"nfl", "nba", "mlb", "nhl"}

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

def _the(league, n):
    t = f"the {n}" if league in PRO else n
    return t[0].upper() + t[1:]

def blurb(league, name, last, nxt, info, star, now):
    """One short, kid-friendly line (<= BLURB_MAX chars) that adds something the Last/Next lines
    don't already say. Built only from public score facts: no injuries, off-field news or odds.
    Priority: star of a game in the last few days > record (+ win streak / standing) > season
    opener coming up > nothing (the app then shows just Last/Next)."""
    team, are = _the(league, name), ("are" if league in PRO else "is")   # "The Broncos are" / "Alabama is"
    cands = [star] if star else []
    rec = (info.get("record") or "").replace("-", "–")
    streak = info.get("win_streak") or 0
    over = nxt is None and last is not None and (now - last["_when"]).days >= 7
    if rec and info.get("season_type") == 3:          # postseason: only while still playing
        if nxt:
            cands.append(f"{team} {are} {rec} in the playoffs" + (f" with {streak} straight wins!" if streak >= 3 else "!"))
        else:                                       # out of the playoffs: keep it positive
            cands.append(f"{team} made the playoffs this season!")
    elif rec and over:
        cands.append(f"{team} finished the season {rec}.")
    elif rec:
        standing = info.get("standing")
        if streak >= 3:
            cands.append(f"{team} {are} {rec} and {'have' if are == 'are' else 'has'} won {streak} in a row!")
        if standing and not standing.lower().startswith(("t-", "0")):
            cands.append(f"{team} {are} {rec}, {standing[0].lower() + standing[1:]}.")
        cands.append(f"{team} {are} {rec} this season.")
    if info.get("opener") and nxt:
        cands.append("Next up: the first game of the season!")
    return next((c for c in cands if len(c) <= BLURB_MAX), None)

BANNED = re.compile(r"\b(bets?|betting|odds|spread|parlay|injur\w*|arrest\w*|suspen\w*|gambl\w*|wager\w*)\b", re.I)

def validate(teams):
    """The shape shipped app builds (incl. build 12) decode: Games {name, last?, next?, blurb?},
    Last {date, opponent, home, team_score, opp_score, result}, Next {start_utc, opponent, home}.
    New keys only as additions (Swift Codable ignores unknown keys); schema_version stays 1."""
    bad = []
    for tid, g in teams.items():
        if not isinstance(g.get("name"), str) or not g["name"]: bad.append(f"{tid}: name")
        l, n, b = g.get("last"), g.get("next"), g.get("blurb")
        if l is not None and not (isinstance(l.get("date"), str) and len(l["date"]) == 10 and isinstance(l.get("opponent"), str)
                                  and isinstance(l.get("home"), bool) and type(l.get("team_score")) is int
                                  and type(l.get("opp_score")) is int and l.get("result") in ("W", "L", "T")
                                  and isinstance(l.get("preseason", False), bool)
                                  and isinstance(l.get("start_utc", "Z"), str) and l.get("start_utc", "Z").endswith("Z")):
            bad.append(f"{tid}: last {l}")
        if n is not None and not (isinstance(n.get("start_utc"), str) and n["start_utc"].endswith("Z")
                                  and isinstance(n.get("opponent"), str) and isinstance(n.get("home"), bool)
                                  and isinstance(n.get("opener", False), bool)):
            bad.append(f"{tid}: next {n}")
        if l is None and n is None: bad.append(f"{tid}: no games")
        if b is not None:
            if not isinstance(b, str) or not b or len(b) > BLURB_MAX or "\n" in b: bad.append(f"{tid}: blurb {b!r}")
            elif BANNED.search(b): bad.append(f"{tid}: blurb wording {b!r}")
        if "record" in g and not isinstance(g["record"], str): bad.append(f"{tid}: record")
        if any(k.startswith("_") for k in list(g) + list(l or {}) + list(n or {})): bad.append(f"{tid}: private key leaked")
    return bad

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="sports.json")
    ap.add_argument("--teams-out")
    ap.add_argument("--now", help="UTC ISO time, for reproducible runs")
    a = ap.parse_args()
    now = dt.datetime.fromisoformat(a.now.replace("Z", "+00:00")) if a.now else dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    lo, hi = (now - dt.timedelta(days=LAST_DAYS)).strftime("%Y-%m-%d"), (now + dt.timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

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
            try: last, nxt, info = f.result()
            except Exception as e:
                failures += 1; print(f"warn: {t['id']} {e}", file=sys.stderr); continue
            if last and last["date"] < lo: last = None
            if nxt and nxt["start_utc"] > hi: nxt = None
            if last or nxt:
                teams[t["id"]] = {"name": t["short"], "_info": info, **({"last": last} if last else {}), **({"next": nxt} if nxt else {})}
    # Blurbs; star lines (one more request each) only for games within STAR_DAYS.
    league_path = {k: p for k, p, _, _ in LEAGUES}
    with cf.ThreadPoolExecutor(12) as ex:
        stars = {tid: ex.submit(star_line, league_path[tid.split(":")[0]], tid.split(":")[0], tid.split(":")[1], g["last"]["_event"])
                 for tid, g in teams.items() if "last" in g and (now - g["last"]["_when"]).days < STAR_DAYS}
        for tid, g in teams.items():
            info = g.pop("_info")
            star = stars[tid].result() if tid in stars else None
            b = blurb(tid.split(":")[0], g["name"], g.get("last"), g.get("next"), info, star, now)
            if b: g["blurb"] = b
            if info.get("record"): g["record"] = info["record"]           # additive key; old apps ignore it
            if g.get("next") and info.get("opener"): g["next"]["opener"] = True
    for g in teams.values():
        for part in ("last", "next"):
            for k in [k for k in g.get(part, {}) if k.startswith("_")]: del g[part][k]
    problems = validate(teams)
    if problems:
        sys.exit("sports.json failed validation:\n  " + "\n  ".join(problems[:20]))
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
