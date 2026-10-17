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
WINDOW_DAYS = 21          # keep a team only if it played or plays within this many days
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

def team_games(path, espn_id, now):
    d = get(f"{SITE}/{path}/teams/{espn_id}/schedule")
    last = nxt = None
    for e in d.get("events", []):
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
                 "result": "W" if a > b else ("L" if a < b else "T")}
            if when <= now and (last is None or g["date"] >= last["date"]): last = g
        elif st.get("state") == "pre" and when > now and e.get("timeValid", True) is not False:
            g = {"start_utc": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "opponent": opp, "home": home}
            if nxt is None or g["start_utc"] < nxt["start_utc"]: nxt = g
    return last, nxt

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
