#!/usr/bin/env python3
"""DramaKorean funnel reader.

Pulls dk_events via the read-only Supabase RPC, applies the test-traffic
exclusion rules, and prints the funnel plus the diagnostic ratios the loop
uses to decide what to change next.

Usage:
    python3 tools/dk_read.py --since 2026-07-29T00:00:00Z
    python3 tools/dk_read.py --since ... --json rows.json   # read local rows instead of network
    python3 tools/dk_read.py --spend                        # TikTok spend for today only

Read-only. Never writes to Supabase, never mutates ad objects.
"""
import argparse, json, sys, urllib.request, urllib.parse, urllib.error
from collections import defaultdict

SUPABASE_URL = "https://pjvjweurelmosugwptdl.supabase.co"
SUPABASE_KEY = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBqdmp3"
                "ZXVyZWxtb3N1Z3dwdGRsIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjQzNDQxODMsImV4cCI6MjA3"
                "OTkyMDE4M30.561J0c1dA0FeQ7Fc568xeVVPDvdUW_bW1BNb78gzIKk")

TIKTOK_TOKEN = "0d43be1b7b7bace4072ca3cf479a53c3cc34c053"
ADVERTISER_ID = "7667402007149576213"
DAILY_CAP_USD = 30.0

# --- exclusion rules (from the loop brief) ---
EXCLUDED_SESSION_IDS = {"mrw64tp7v313fh"}
TEST_PID_PREFIXES = ("PREVIEW", "ANCHTEST")

FUNNEL = ["page_view", "scene_play", "scene_complete", "quiz_done",
          "teaser_view", "offer_view", "checkout_click"]


def fetch_rows(since):
    req = urllib.request.Request(
        SUPABASE_URL + "/rest/v1/rpc/dk_events_read",
        data=json.dumps({"since": since}).encode(),
        headers={"Content-Type": "application/json",
                 "apikey": SUPABASE_KEY,
                 "Authorization": "Bearer " + SUPABASE_KEY},
        method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def clean(rows):
    """Drop test traffic. Returns (kept_rows, reasons_by_session)."""
    by_sid = defaultdict(list)
    for r in rows:
        by_sid[r.get("session_id")].append(r)

    dropped = {}
    for sid, evs in by_sid.items():
        if sid in EXCLUDED_SESSION_IDS:
            dropped[sid] = "explicit test session"
            continue
        pids = {e.get("person_id") for e in evs if e.get("person_id")}
        if any(str(p).startswith(TEST_PID_PREFIXES) for p in pids):
            dropped[sid] = "PREVIEW/ANCHTEST person_id present"
            continue
        # 2+ distinct non-anon person_ids mixed into one session -> not a real visitor
        named = {p for p in pids if p and p != "anon"}
        if len(named) >= 2:
            dropped[sid] = f"{len(named)} non-anon person_ids mixed"
            continue

    kept = [r for r in rows if r.get("session_id") not in dropped]
    return kept, dropped


def latest_leaves(rows):
    """last-beacon-wins: per session keep only the leave row with max meta.seq."""
    best = {}
    for r in rows:
        if r.get("event") != "leave":
            continue
        m = r.get("meta") or {}
        seq = m.get("seq", 0) or 0
        sid = r.get("session_id")
        if sid not in best or seq > (best[sid].get("meta") or {}).get("seq", 0):
            best[sid] = r
    return list(best.values())


def pct(a, b):
    return f"{(100.0*a/b):.1f}%" if b else "n/a"


def report(rows):
    kept, dropped = clean(rows)
    print(f"rows: {len(rows)} raw -> {len(kept)} after exclusions "
          f"({len(dropped)} sessions dropped)")
    for sid, why in list(dropped.items())[:10]:
        print(f"   drop {sid}: {why}")

    sess = defaultdict(set)
    for r in kept:
        sess[r["session_id"]].add(r.get("event"))
    total = len(sess)
    print(f"\nreal sessions: {total}")

    print("\n--- FUNNEL (sessions reaching each step) ---")
    prev = None
    counts = {}
    for ev in FUNNEL:
        n = sum(1 for s in sess.values() if ev in s)
        counts[ev] = n
        step = f"  {pct(n, prev)} of prev" if prev is not None else ""
        print(f"  {ev:<16} {n:>5}   {pct(n, total):>7} of all{step}")
        prev = n

    # --- diagnostics that drive the next decision ---
    print("\n--- DIAGNOSTICS ---")
    pv = counts["page_view"]

    store0 = sum(1 for r in kept if r.get("event") == "page_view"
                 and (r.get("meta") or {}).get("store") == 0)
    print(f"  storage-blocked webviews: {store0}/{pv} ({pct(store0, pv)})")

    play = counts["scene_play"]
    audio_ok = sum(1 for s in sess.values() if "scene_audio_ok" in s)
    print(f"  scene_play {play} -> scene_audio_ok {audio_ok} ({pct(audio_ok, play)})")
    if play and audio_ok < 0.5 * play:
        print("    !! in-app webview audio is being blocked -> consider silent/subtitle-first mode")

    perr = sum(1 for s in sess.values() if "play_error" in s)
    print(f"  play_error sessions: {perr}")

    lv = latest_leaves(kept)
    secs = sorted((r.get("meta") or {}).get("sec", 0) or 0 for r in lv)
    if secs:
        n = len(secs)
        med = secs[n // 2]
        under3 = sum(1 for s in secs if s < 3)
        print(f"  dwell (sec, visible-time only) n={n} median={med}s "
              f"p90={secs[int(n*0.9)-1] if n else 0}s")
        print(f"  sessions under 3s: {under3}/{n} ({pct(under3, n)})")
        if under3 > 0.6 * n:
            print("    !! load/expectation mismatch -> first-screen hook is the bottleneck")
        walls = [(r.get("meta") or {}).get("wall", 0) or 0 for r in lv]
        if walls and sum(walls) > 1.4 * sum(secs):
            print("    (note: wall >> sec = frequent app-switching, NOT abandonment)")

    ov, cc = counts["offer_view"], counts["checkout_click"]
    print(f"  offer_view {ov} -> checkout_click {cc} ({pct(cc, ov)})")
    if cc:
        prices = defaultdict(int)
        for r in kept:
            if r.get("event") == "checkout_click":
                prices[(r.get("meta") or {}).get("price")] += 1
        print(f"  *** CHECKOUT CLICK *** by price: {dict(prices)}")
    elif ov >= 20:
        print("    !! offer reached but zero clicks -> price test ($29) is justified")

    src = defaultdict(int)
    for r in kept:
        if r.get("event") == "page_view":
            src[(r.get("utm_source"), r.get("utm_campaign"))] += 1
    if src:
        print("\n  traffic by utm_source/campaign:")
        for k, v in sorted(src.items(), key=lambda x: -x[1])[:8]:
            print(f"    {k}: {v}")
    return counts


def spend_today():
    """Read-only TikTok spend check for the DramaKorean campaign."""
    import datetime
    # account timezone is UTC-5
    today = (datetime.datetime.utcnow() - datetime.timedelta(hours=5)).strftime("%Y-%m-%d")
    params = {
        "advertiser_id": ADVERTISER_ID, "report_type": "BASIC",
        "data_level": "AUCTION_CAMPAIGN", "dimensions": json.dumps(["campaign_id"]),
        "metrics": json.dumps(["spend", "clicks", "impressions", "ctr", "cpc"]),
        "start_date": today, "end_date": today, "page_size": "100",
    }
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"https://business-api.tiktok.com/open_api/v1.3/report/integrated/get/?{q}",
        headers={"Access-Token": TIKTOK_TOKEN})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    print(json.dumps(data, indent=2)[:3000])
    total = 0.0
    for row in (data.get("data") or {}).get("list", []):
        if row.get("dimensions", {}).get("campaign_id") == "1871949785120465":
            total += float(row.get("metrics", {}).get("spend", 0))
    print(f"\nDramaKorean spend today ({today}, acct tz UTC-5): ${total:.2f} / cap ${DAILY_CAP_USD}")
    if total > DAILY_CAP_USD:
        print("!!! OVER CAP -- pause the ad group now")
    return total


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-07-29T00:00:00Z")
    ap.add_argument("--json", help="read rows from a local JSON file instead of the network")
    ap.add_argument("--spend", action="store_true", help="TikTok spend check only")
    a = ap.parse_args()
    if a.spend:
        spend_today(); sys.exit(0)
    if a.json:
        rows = json.load(open(a.json))
    else:
        try:
            rows = fetch_rows(a.since)
        except Exception as e:
            print(f"fetch failed: {e}", file=sys.stderr); sys.exit(2)
    report(rows)
