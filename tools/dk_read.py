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
import argparse, json, re, sys, urllib.request, urllib.parse, urllib.error
from collections import defaultdict

SUPABASE_URL = "https://pjvjweurelmosugwptdl.supabase.co"
SUPABASE_KEY = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBqdmp3"
                "ZXVyZWxtb3N1Z3dwdGRsIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjQzNDQxODMsImV4cCI6MjA3"
                "OTkyMDE4M30.561J0c1dA0FeQ7Fc568xeVVPDvdUW_bW1BNb78gzIKk")

TIKTOK_TOKEN = "0d43be1b7b7bace4072ca3cf479a53c3cc34c053"
ADVERTISER_ID = "7667402007149576213"
DAILY_CAP_USD = 30.0

# --- exclusion rules (from the loop brief) ---
EXCLUDED_SESSION_IDS = {
    "mrw64tp7v313fh",
    # Loop 6's own headless verification loads of the round-2 landing URL (2026-07-29
    # 09:44 UTC). That URL existed only inside an ad object created minutes earlier that
    # had never delivered an impression, so no human could have reached it. They predate
    # the DK_QA person_id tag, hence the hardcode; loads after that deploy self-exclude.
    "ms5wesk52a0cjlox",
    "ms5wevy75zyi9sd9",
}
TEST_PID_PREFIXES = ("PREVIEW", "ANCHTEST")

FUNNEL = ["page_view", "scene_play", "scene_complete", "quiz_done",
          "teaser_view", "offer_view", "checkout_click"]


RPC_ROW_CAP = 1000  # the RPC silently truncates at 1000 rows


def _fetch_page(since):
    req = urllib.request.Request(
        SUPABASE_URL + "/rest/v1/rpc/dk_events_read",
        data=json.dumps({"since": since}).encode(),
        headers={"Content-Type": "application/json",
                 "apikey": SUPABASE_KEY,
                 "Authorization": "Bearer " + SUPABASE_KEY},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def fetch_rows(since):
    """Page past the RPC's 1000-row cap by walking `since` forward.

    The cap is silent: a full page means there is almost certainly more data
    behind it. Loop 6 read a truncated window and drew the wrong conclusion
    from it, so never trust a single call again.
    """
    seen, out, cur = set(), [], since
    for _ in range(60):
        page = _fetch_page(cur)
        if not page:
            break
        for r in page:
            k = (r.get("created_at"), r.get("session_id"), r.get("event"),
                 json.dumps(r.get("meta"), sort_keys=True))
            if k not in seen:
                seen.add(k)
                out.append(r)
        if len(page) < RPC_ROW_CAP:
            break
        nxt = max(r["created_at"] for r in page)
        if nxt == cur:  # >1000 rows share one timestamp; cannot advance
            print(f"!! cannot page past {cur} — results may be truncated")
            break
        cur = nxt
    return out


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


def client_audit(kept, sess):
    """Report which sessions look non-human, and NEVER drop one.

    We buy US mobile placement only, so a real visitor is an America/* timezone with a
    touchscreen. Datacenter automation is typically UTC (or an Asia zone) with
    maxTouchPoints 0. On 07-29 four loads inside 98s of a deploy carried the exact r2 ad URL
    on a day TikTok reported 3 clicks total -- almost certainly an ad-review crawler -- yet
    they were indistinguishable from real bouncers, which is the same illusion that burned
    loops 1-5. Hence: label loudly, subtract nothing. A wrong 'bot' call silently deletes a
    paying customer, so the verdict stays here where a human can overrule it, not in the
    page's person_id where it would erase the row.
    """
    pvs = [r for r in kept if r.get("event") == "page_view"]
    tagged = [r for r in pvs if (r.get("meta") or {}).get("c")]
    if not tagged:
        if pvs:
            print(f"  client fingerprint: 0/{len(pvs)} page_views tagged "
                  f"(pre-l13 build — cannot tell crawler from customer)")
        return

    susp = []
    for r in tagged:
        c = (r.get("meta") or {}).get("c") or {}
        tz, tp, wd = c.get("tz") or "", c.get("tp"), c.get("wd")
        why = []
        if wd == 1:
            why.append("webdriver")
        if tz and not tz.startswith("America/"):
            why.append(f"tz={tz}")
        if tp == 0:
            why.append("no-touch")
        if why:
            susp.append((r.get("session_id"), ",".join(why)))

    tz_tally = defaultdict(int)
    for r in tagged:
        tz_tally[((r.get("meta") or {}).get("c") or {}).get("tz") or "?"] += 1
    print(f"  client fingerprint: {len(tagged)}/{len(pvs)} page_views tagged")
    print("    timezones: " + ", ".join(
        f"{t}={n}" for t, n in sorted(tz_tally.items(), key=lambda x: -x[1])[:6]))
    print(f"    suspected non-human: {len(susp)}/{len(tagged)} "
          f"(NOT excluded — judge, then decide)")
    for sid, why in susp[:8]:
        print(f"      {sid}  {why}")
    if susp and len(susp) >= 0.5 * len(tagged):
        print("    !! half or more of this window is non-human — the funnel below is NOT a")
        print("       verdict on the product. Re-read it against human sessions only.")


def arrival_audit(kept, sess):
    """Separate loads a person caused from loads the webview caused on its own.

    A prefetched or prerendered document parses like a real arrival -- page_view, perf, no
    taps -- so it counts as a bounce it never was. On 07-29 eight fresh US devices landed
    with transferSize 0 AND responseStart 0 while TikTok reported fewer clicks than we
    recorded landings, which is only possible if some documents were fetched without a tap.
    Same rule as client_audit: label loudly, subtract nothing. Pre-l16 sessions have none of
    these fields, so they are reported as unknown rather than quietly assumed human.
    """
    pvs = [r for r in kept if r.get("event") == "page_view"]
    if not pvs:
        return
    perf = {r.get("session_id"): (r.get("meta") or {})
            for r in kept if r.get("event") == "perf"}
    saw_visible = {r.get("session_id") for r in kept if r.get("event") == "visible"}

    instrumented, never_seen, preloaded, zero_wire, real = 0, [], [], [], 0
    for r in pvs:
        sid, m = r.get("session_id"), (r.get("meta") or {})
        p = perf.get(sid) or {}
        has_fields = ("vs" in m) or ("dt" in p)
        dt, act = p.get("dt"), p.get("as")
        if dt == "navigational-prefetch" or (act or 0) > 0 or m.get("pr") == 1:
            preloaded.append((sid, dt or (f"activationStart={act}" if act else "prerendering")))
        if has_fields:
            instrumented += 1
            if sid not in saw_visible:
                never_seen.append((sid, f"vs={m.get('vs')}"))
            else:
                real += 1
        elif p.get("kb") == 0 and p.get("ttfb") == 0:
            # pre-l16 fallback: a document that crossed no network on a first-ever visit
            zero_wire.append(sid)

    print(f"  arrival provenance: {instrumented}/{len(pvs)} page_views instrumented (l16+)")
    if preloaded:
        print(f"    preloaded by the webview: {len(preloaded)} (NOT excluded — judge first)")
        for sid, why in preloaded[:8]:
            print(f"      {sid}  {why}")
    if never_seen:
        print(f"    never became visible: {len(never_seen)} — these are NOT bounces")
        for sid, why in never_seen[:8]:
            print(f"      {sid}  {why}")
    if instrumented:
        print(f"    confirmed seen by a human: {real}/{instrumented}")
    if zero_wire:
        print(f"    pre-l16, zero-transfer + ttfb 0: {len(zero_wire)}/{len(pvs)} "
              f"— provenance UNKNOWN, do not score as bounces")
        print("      " + ", ".join(zero_wire[:8]))


def loop_no(build):
    """Loop number out of a build stamp like '2026-07-30-l20-s2'; 0 when it has none.

    Deliberately not a substring test for 'l20': the loader ships in every build from l20
    on, so 'is this build patched' has to keep answering correctly at l21, l30, l100.
    """
    m = re.search(r"-l(\d+)", str(build or ""))
    return int(m.group(1)) if m else 0


BOOT_SINCE = 20   # first build that carries the boot.js loader


def boot_audit(kept, build_of):
    """Can a frozen snapshot still be reached at run time?

    Half of paid traffic runs a document the webview froze hours ago, so the fixes each
    loop ships never reach it. l20 makes the page fetch boot.js at run time, cache-busted,
    and boot.js reports itself. Two numbers matter here and nothing else:

      reach   -- boot fired on a build OLDER than the current one. That is the whole point:
                 a document we can no longer redeploy pulled today's code anyway.
      repairs -- boot actually rebound something (meta.fix). Empty on a current build is
                 healthy; non-empty on a stale one means a snapshot was rescued.

    Silence is not failure yet: pre-l20 documents have no loader, so they can never report.
    Judge only sessions whose page_view build is l20 or later.
    """
    boots = {}
    for r in kept:
        if r.get("event") == "boot":
            boots[r.get("session_id")] = r.get("meta") or {}
    eligible = [s for s, b in build_of.items() if loop_no(b) >= BOOT_SINCE]
    if not boots and not eligible:
        print("  boot channel: no l20+ landings yet — nothing to judge")
        return
    hosts = defaultdict(int)
    for m in boots.values():
        hosts[str(m.get("b") or "?")] += 1
    repaired = {s: m.get("fix") for s, m in boots.items() if m.get("fix")}
    stale = [s for s, m in boots.items() if loop_no(m.get("b")) < BOOT_SINCE]
    print(f"  boot channel: fired on {len(boots)} sessions"
          + (f" ({len(eligible)} l20+ landings)" if eligible else ""))
    for b, n in sorted(hosts.items(), key=lambda kv: -kv[1]):
        print(f"    host build {b}: {n}")
    if stale:
        print(f"    !! REACHED A FROZEN SNAPSHOT: {len(stale)} — {', '.join(stale[:6])}")
        print("       a document we can no longer redeploy ran today's code. The channel works.")
    if repaired:
        print(f"    repairs applied: {len(repaired)} — "
              + ", ".join(f"{s}:{f}" for s, f in list(repaired.items())[:6]))
    if eligible and not boots:
        print(f"    !! {len(eligible)} l20+ landings and ZERO boot events — the loader is "
              "not running. Check boot.js is served (200) before trusting any patch in it.")


def poster_audit(kept, leaves):
    """Did the poster art reach the screen before the visitor left?

    Loop 17 fixed the dead play path; the next cohort tapped nothing and left in 2-3s.
    'The hook failed' and 'the hook was never on screen' look identical in the funnel and
    call for opposite fixes, so l18 records when cover.jpg finished (meta.art, ms from
    nav) alongside the visible dwell already in meta.sec. This only reports; it never
    drops a session -- same rule as client_audit and arrival_audit.

    l18's `art` field read 0 for 5 of 5 real visitors -- one of them after its own `load`
    event fired -- because iOS WebKit leaves the Resource Timing size fields at 0 and
    `art` required a body size. So l19 measures the image's own onload (meta.artl) plus
    onerror (meta.arte), and `art` survives only as the cross-check below. Sessions are
    scored on artl when they have it; an l18-only row gets NO verdict, because a field
    that cannot see is not evidence of an unseen poster.
    """
    artl, art, arte = {}, {}, set()
    perf = {r.get("session_id"): (r.get("meta") or {})
            for r in kept if r.get("event") == "perf"}
    for r in kept:
        if r.get("event") not in ("perf", "leave"):
            continue
        m = r.get("meta") or {}
        sid = r.get("session_id")
        if "art" in m:
            v = m.get("art") or 0
            if v > art.get(sid, -1):
                art[sid] = v
        if "artl" in m:                  # pre-l19 row: no opinion, not a zero
            v = m.get("artl") or 0
            if v > artl.get(sid, -1):
                artl[sid] = v
            if m.get("arte"):
                arte.add(sid)
    if not artl:
        print(f"  poster paint: 0 sessions instrumented (pre-l19; {len(art)} on l18's "
              "blind `art` field, which is NOT scored) — cannot tell a failed hook "
              "from an unseen one")
        return
    secs = {r.get("session_id"): ((r.get("meta") or {}).get("sec") or 0) for r in leaves}
    failed = sorted(arte)
    never = [s for s, v in artl.items() if not v and s not in arte]
    late = [s for s, v in artl.items() if v and s in secs and v > secs[s] * 1000]

    # A pooled median here is a lie, and loop 24 caught it about to be believed. The
    # window held two populations: documents that crossed no network (ttfb 0 AND kb 0 --
    # the frozen snapshot a paid TikTok visitor actually opens) painted cover.jpg at
    # 240-1341ms, while documents that were fetched over the wire painted at 1190-2954ms
    # and tracked ttfb almost exactly. Every wire session in that window was an audit
    # crawler. Pooled, the median read 1436ms and pointed at image weight; the visitors'
    # own median was ~530ms and pointed nowhere. Report the two cohorts apart, always,
    # and headline the frozen one -- it is the only one with a customer in it. (Note the
    # wire cohort is NOT crawlers by definition: a first-ever cold-cache human lands
    # there too. It is 'we paid for the bytes', not 'not a person'.)
    frozen, wire = {}, {}
    for sid, v in artl.items():
        p = perf.get(sid) or {}
        (frozen if (p.get("ttfb") == 0 and p.get("kb") == 0) else wire)[sid] = v

    def _med(d):
        vs = sorted(v for v in d.values() if v)
        return vs[len(vs) // 2] if vs else 0

    print(f"  poster paint: {len(artl)} sessions instrumented (l19+), "
          f"cover.jpg median {_med(artl)}ms POOLED — read the split, not this number")
    print(f"    frozen snapshot (ttfb 0, kb 0 — what a paid visitor opens): "
          f"n={len(frozen)} median {_med(frozen)}ms")
    print(f"    fetched over the wire (cold cache OR audit crawler): "
          f"n={len(wire)} median {_med(wire)}ms")
    if failed:
        print(f"    !! cover.jpg FAILED to load: {len(failed)} — {', '.join(failed[:6])}")
        print("       that is our bug, not a fast exit — fix the image before reading dwell")
    if never:
        print(f"    never painted: {len(never)} — {', '.join(never[:6])}")
    if late:
        print(f"    left BEFORE the art painted: {len(late)} — {', '.join(late[:6])}")
    # The cross-check that retires (or restores) l18's field. If artl says the poster
    # arrived while art says it never did, `art` is measuring the browser, not the visitor.
    both = [s for s in artl if s in art]
    dis = [s for s in both if artl[s] and not art[s]]
    if both:
        print(f"    cross-check vs l18 `art`: {len(dis)}/{len(both)} sessions painted "
              f"per artl but read 0 per art"
              + ("  -> `art` is blind on this browser; ignore it" if dis else ""))
    # Score the verdict on the frozen cohort only. A wire session that painted slowly is
    # usually a crawler on a cold miss, and letting it vote here is what would have sent
    # the next loop off to shrink an image the customer never waited for.
    unseen = [s for s in (never + late) if s in frozen]
    if len(unseen) > 0.4 * len(frozen) and len(frozen) >= 10:
        print("    !! a large share of FROZEN loads never saw the hook -> the fix is "
              "image weight, NOT the copy")
    elif unseen:
        print(f"    ({len(unseen)}/{len(frozen)} frozen loads never saw the hook — "
              "needs n>=10 frozen for a verdict)")


def pct(a, b):
    return f"{(100.0*a/b):.1f}%" if b else "n/a"


def report(rows, path=None):
    kept, dropped = clean(rows)
    print(f"rows: {len(rows)} raw -> {len(kept)} after exclusions "
          f"({len(dropped)} sessions dropped)")
    for sid, why in list(dropped.items())[:10]:
        print(f"   drop {sid}: {why}")
    if kept:
        ts = sorted(r["created_at"] for r in kept)
        print(f"   window: {ts[0]} -> {ts[-1]}")

    # The root landing page and episode1 are DIFFERENT experiences with different
    # event vocabularies. Pooling them makes episode1's funnel unreadable, so the
    # funnel is always scoped to one page.
    seen_paths = defaultdict(int)
    for r in kept:
        if r.get("event") == "page_view":
            seen_paths[r.get("path")] += 1
    print("\n  page_views by path: " + ", ".join(
        f"{p}={n}" for p, n in sorted(seen_paths.items(), key=lambda x: -x[1])))

    # Prefix, not equality: loop 12 moved the paid landing to a fresh sub-path
    # (/episode1/s2/) to escape a path-keyed cache that was still serving round-1
    # HTML. Both paths are the same funnel, so exact matching would have silently
    # dropped every new paid session from the count.
    if path:
        sids = {r["session_id"] for r in kept if (r.get("path") or "").startswith(path)}
        kept = [r for r in kept if r["session_id"] in sids]
        print(f"  -> funnel scoped to {path}* (prefix)")

    sess = defaultdict(set)
    for r in kept:
        sess[r["session_id"]].add(r.get("event"))
    total = len(sess)
    print(f"\nreal sessions: {total}")

    # A build older than the loop-1 instrumentation emits 14-char session ids and
    # a null page_view meta. Those visitors never had the fixes, so their zeros say
    # nothing about the current page — surface them instead of scoring them.
    pvs = [r for r in kept if r.get("event") == "page_view"]
    stale = sum(1 for r in pvs if r.get("meta") is None)
    if pvs and stale:
        print(f"  !! {stale}/{len(pvs)} page_views are from a PRE-FIX build "
              f"(null meta) — exclude them before judging the funnel")

    # Which build did each session actually see? Loops 6/10/11 each had to answer this by
    # hand, with a different fragile proxy each time. Builds from loop 11 on stamp
    # page_view.meta.b, so they self-identify. Older ones get a best-effort label from
    # perf.kb -- and a cache hit reports kb 0, which is precisely the case a human eye
    # misreads as "new build" right after a deploy. Never let two builds share a funnel:
    # that is the loop 1-5 error (a broken build's zeros read as a hook problem).
    build_of = {}
    kb_of = {}
    for r in kept:
        if r.get("event") == "perf":
            kb_of[r["session_id"]] = (r.get("meta") or {}).get("kb")
    for r in pvs:
        m = r.get("meta") or {}
        sid = r["session_id"]
        if m.get("b"):
            build_of[sid] = m["b"]
        else:
            kb = kb_of.get(sid)
            if m == {} or r.get("meta") is None:
                build_of[sid] = "pre-fix(null meta)"
            elif kb is None:
                build_of[sid] = "untagged(unknown)"
            elif kb >= 200:
                build_of[sid] = "untagged(~700KB monolith)"
            elif kb > 0:
                build_of[sid] = "untagged(asset-split)"
            else:
                build_of[sid] = "untagged(kb0 cache hit — build UNKNOWN)"

    if build_of:
        tally = defaultdict(int)
        for b in build_of.values():
            tally[b] += 1
        print("\n  builds seen: " + ", ".join(
            f"{b}={n}" for b, n in sorted(tally.items(), key=lambda x: -x[1])))
        if len(tally) > 1:
            print("    !! MORE THAN ONE BUILD IN THIS WINDOW — do not judge the funnel on the")
            print("       pooled number. Per-build funnels below; score only the current build.")

    print("\n--- FUNNEL (sessions reaching each step) ---")
    prev = None
    counts = {}
    for ev in FUNNEL:
        n = sum(1 for s in sess.values() if ev in s)
        counts[ev] = n
        step = f"  {pct(n, prev)} of prev" if prev is not None else ""
        print(f"  {ev:<16} {n:>5}   {pct(n, total):>7} of all{step}")
        prev = n

    # Per-build funnels. The pooled funnel above is only meaningful when one build is live;
    # right after a deploy it is a blend, and the blend hides exactly the signal we deployed
    # to measure.
    tally = defaultdict(int)
    for b in build_of.values():
        tally[b] += 1
    if len(tally) > 1:
        for b, _n in sorted(tally.items(), key=lambda x: -x[1]):
            bs = {s: evs for s, evs in sess.items() if build_of.get(s) == b}
            bt = len(bs)
            print(f"\n  -- build {b} ({bt} sessions) --")
            bprev = None
            for ev in FUNNEL:
                n = sum(1 for evs in bs.values() if ev in evs)
                step = f"  {pct(n, bprev)} of prev" if bprev is not None else ""
                print(f"    {ev:<16} {n:>5}   {pct(n, bt):>7} of build{step}")
                bprev = n

    # --- diagnostics that drive the next decision ---
    print("\n--- DIAGNOSTICS ---")
    pv = counts["page_view"]

    client_audit(kept, sess)
    arrival_audit(kept, sess)

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
        # Only sessions that emitted a leave beacon land in `secs`. A session that is still
        # open, or whose webview was killed before pagehide, contributes nothing -- so this
        # bounce rate is computed on a subset, not on the funnel. Saying "the hook is the
        # bottleneck" off 2 of 3 beacons out of 6 sessions is how a 3-sample artifact turns
        # into a hook rewrite. Demand real coverage before the verdict is allowed to print.
        cover = pct(n, total)
        print(f"  dwell coverage: {n}/{total} sessions emitted a leave beacon ({cover})")
        if under3 > 0.6 * n and n >= 20 and n >= 0.5 * total:
            print("    !! load/expectation mismatch -> first-screen hook is the bottleneck")
        elif under3 > 0.6 * n:
            print(f"    (short-dwell heavy, but n={n} of {total} — NOT a verdict; "
                  f"needs n>=20 and >=50% coverage)")
        walls = [(r.get("meta") or {}).get("wall", 0) or 0 for r in lv]
        if walls and sum(walls) > 1.4 * sum(secs):
            print("    (note: wall >> sec = frequent app-switching, NOT abandonment)")

    poster_audit(kept, lv)
    boot_audit(kept, build_of)

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


def _acct_now():
    """Account-timezone clock (UTC-5)."""
    import datetime
    return datetime.datetime.utcnow() - datetime.timedelta(hours=5)


def delivery_watermark(today):
    """Latest hour TikTok has actually reported, across every ad group.

    TikTok's reporting runs hours behind real time. A freshly-approved ad group
    therefore reads as '0 impressions, $0.00' no matter how well it is
    delivering, simply because its whole life so far sits inside the unreported
    window. Loop 8 hit this: approved 05:39 acct, watermark 04:00 acct.
    Print the watermark next to every spend check so nobody ever again reads a
    structural blind spot as evidence that an ad group is not delivering.

    Returns the watermark as 'YYYY-MM-DD HH:MM:SS' acct time, or None.
    """
    params = {
        "advertiser_id": ADVERTISER_ID, "report_type": "BASIC",
        "data_level": "AUCTION_ADGROUP",
        "dimensions": json.dumps(["adgroup_id", "stat_time_hour"]),
        "metrics": json.dumps(["spend", "impressions"]),
        "start_date": today, "end_date": today, "page_size": "1000",
    }
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"https://business-api.tiktok.com/open_api/v1.3/report/integrated/get/?{q}",
        headers={"Access-Token": TIKTOK_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"  (watermark probe failed: {e})")
        return None
    hours = [row["dimensions"]["stat_time_hour"]
             for row in (data.get("data") or {}).get("list", [])
             if float(row.get("metrics", {}).get("impressions", 0) or 0) > 0]
    if not hours:
        print("  report watermark: no delivery reported yet today (any campaign) "
              "-- freshness unknown, treat today's zeros as UNREADABLE")
        return None
    mark = max(hours)
    now = _acct_now()
    import datetime
    lag_h = (now - datetime.datetime.strptime(mark, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600.0
    print(f"  report watermark: {mark} acct  (acct now {now:%Y-%m-%d %H:%M})  "
          f"-> ~{lag_h:.1f}h behind")
    if lag_h >= 1.5:
        print(f"    !! anything after {mark} is NOT reported yet. A zero inside that "
              f"window is a BLIND SPOT, not evidence of non-delivery.")
        print("       Live signal for the unreported window = dk_events sessions, not this report.")
    return mark


def spend_today():
    """Read-only TikTok spend check for the DramaKorean campaign."""
    # account timezone is UTC-5
    today = _acct_now().strftime("%Y-%m-%d")
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
    delivery_watermark(today)
    return total


def reconcile(counts):
    """Ad-side clicks vs page-side landed sessions.

    Round 1 lost 266 clicks -> 249 sessions (~6%). A sudden widening of that gap
    means clicks are not reaching the page at all (redirect/CDN/consent), which
    is a completely different bug from 'they arrive and bounce'. Always read it
    against the watermark: the report lags, dk_events does not, so a fresh
    window legitimately shows more sessions than clicks.
    """
    today = _acct_now().strftime("%Y-%m-%d")
    print("\n--- AD-SIDE RECONCILIATION (today, acct tz) ---")
    mark = delivery_watermark(today)
    params = {
        "advertiser_id": ADVERTISER_ID, "report_type": "BASIC",
        "data_level": "AUCTION_CAMPAIGN", "dimensions": json.dumps(["campaign_id"]),
        "metrics": json.dumps(["spend", "clicks", "impressions"]),
        "start_date": today, "end_date": today, "page_size": "100",
    }
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"https://business-api.tiktok.com/open_api/v1.3/report/integrated/get/?{q}",
        headers={"Access-Token": TIKTOK_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"  (reconciliation failed: {e})")
        return
    clicks = imps = 0
    for row in (data.get("data") or {}).get("list", []):
        if row["dimensions"].get("campaign_id") == "1871949785120465":
            clicks += int(float(row["metrics"].get("clicks", 0) or 0))
            imps += int(float(row["metrics"].get("impressions", 0) or 0))
    landed = counts.get("page_view", 0)
    print(f"  reported impressions {imps} -> clicks {clicks}  vs  landed sessions {landed}")
    if clicks and landed:
        print(f"  click -> landing: {pct(landed, clicks)}"
              " (round 1 baseline: 249/266 = 93.6%)")
        if landed < 0.7 * clicks:
            print("    !! clicks are not reaching the page -- check the landing URL, "
                  "not the funnel")
    elif not clicks and landed:
        print("  clicks unreported but sessions are landing -> pure report lag, "
              "trust dk_events")
    elif clicks and not landed:
        print("    !! clicks reported but ZERO sessions landed -- landing URL is broken")
    else:
        print("  nothing on either side yet"
              + (f" (report only complete through {mark})" if mark else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-07-29T00:00:00Z")
    ap.add_argument("--json", help="read rows from a local JSON file instead of the network")
    ap.add_argument("--spend", action="store_true", help="TikTok spend check only")
    ap.add_argument("--recon", action="store_true",
                    help="also compare TikTok clicks against landed sessions")
    ap.add_argument("--path", default="/learn-korean/episode1/",
                    help="scope the funnel to one page ('' = all pages pooled)")
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
    counts = report(rows, a.path or None)
    if a.recon:
        reconcile(counts)
