#!/usr/bin/env python3
"""plexsync — bidirectional, API-level watch-state diff engine for two Plex servers.

Reads watched + in-progress state from BOTH servers, matches items by their Plex
GUID (server-agnostic), and writes the NEWER state to the other side
(newest lastViewedAt wins). It is:
  - API-level (no DB copy, no identity/auth risk — the safe way, unlike a DB mirror)
  - bidirectional (A<->B), newest-wins per item
  - positive-state only (propagates watched / in-progress; never marks unwatched)
  - batched (caps writes per run; a scheduled timer converges over runs — writes
    are idempotent, so once in sync it does nothing)

Env: PLEX_TOKEN, A_URL, B_URL.  Default is DRY-RUN (reports the diff, writes nothing).
Run with --apply to write, --limit N to cap writes per run.
"""
import os, sys, json, time, urllib.request, urllib.parse, argparse

TOKEN = os.environ["PLEX_TOKEN"]
A_URL = os.environ.get("A_URL", "http://192.168.1.105:32400").rstrip("/")
B_URL = os.environ.get("B_URL", "http://192.168.1.93:32400").rstrip("/")
PAGE  = 500

_HDRS = {"Accept": "application/json",
         "X-Plex-Client-Identifier": "plexsync-diff-engine",   # timeline endpoint requires this
         "X-Plex-Product": "plexsync", "X-Plex-Version": "1.0", "X-Plex-Device-Name": "plexsync"}

def _get(url, path, rawq=""):
    full = f"{url}{path}" + (("?" + rawq + "&") if rawq else "?") + "X-Plex-Token=" + urllib.parse.quote(TOKEN)
    req = urllib.request.Request(full, headers=_HDRS)
    with urllib.request.urlopen(req, timeout=25) as r:
        b = r.read()
    return json.loads(b) if b else {}

def sections(url):
    d = _get(url, "/library/sections").get("MediaContainer", {}).get("Directory", [])
    return [(s["key"], s["type"]) for s in d if s["type"] in ("movie", "show")]

def get_state(url):
    """guid -> {vc, vo, lvat, dur, title} for every watched / in-progress item."""
    state = {}
    for sid, stype in sections(url):
        t = 1 if stype == "movie" else 4                     # 1=movie, 4=episode
        for filt in ("viewCount%3E=1", "viewOffset%3E=1"):    # watched, in-progress
            start = 0
            while True:
                mc = _get(url, f"/library/sections/{sid}/all",
                          f"type={t}&{filt}&includeGuids=1"
                          f"&X-Plex-Container-Start={start}&X-Plex-Container-Size={PAGE}"
                          ).get("MediaContainer", {})
                items = mc.get("Metadata", []) or []
                for m in items:
                    g = m.get("guid")
                    if not g:
                        continue
                    rec = {"vc": int(m.get("viewCount", 0)), "vo": int(m.get("viewOffset", 0)),
                           "lvat": int(m.get("lastViewedAt", 0)), "dur": int(m.get("duration", 0)),
                           "title": m.get("title", "")}
                    cur = state.get(g)
                    if not cur or rec["lvat"] > cur["lvat"] or rec["vc"] > cur["vc"]:
                        state[g] = rec
                total = int(mc.get("totalSize", mc.get("size", len(items))))
                start += len(items)
                if not items or start >= total:
                    break
    return state

def resolve(url, guid):
    """Find this guid's ratingKey on `url` (the write target)."""
    mc = _get(url, "/library/all", f"guid={urllib.parse.quote(guid)}").get("MediaContainer", {})
    md = mc.get("Metadata", []) or []
    return md[0]["ratingKey"] if md else None

def write_state(url, rk, rec):
    if rec["vc"] > 0 and rec["vo"] == 0:                      # fully watched -> scrobble
        _get(url, "/:/scrobble", f"key={rk}&identifier=com.plexapp.plugins.library")
        return "watched"
    if rec["vo"] > 0:                                         # in-progress -> set resume offset
        _get(url, "/:/timeline",
             f"ratingKey={rk}&key=/library/metadata/{rk}&identifier=com.plexapp.plugins.library"
             f"&state=paused&time={rec['vo']}&duration={rec['dur'] or rec['vo']}")
        return f"progress {rec['vo']//1000}s"
    return None

TOL_MS = 90_000   # in-progress offsets within 90s count as "already the same"
_ZERO  = {"vc": 0, "vo": 0, "lvat": 0, "dur": 0, "title": ""}

def _canon(rec):
    """Reduce to a category that is STABLE under our own writes, so the sync is
    idempotent and can never ping-pong / inflate viewCount:
      in-progress -> ('progress', offset) ; watched -> ('watched',) ; else ('unwatched',)
    Note: we compare on watched-as-a-boolean, NOT exact viewCount — because
    scrobble increments the count, so comparing exact counts would re-fire forever."""
    if rec.get("vo", 0) > 0: return ("progress", rec["vo"])
    if rec.get("vc", 0) > 0: return ("watched",)
    return ("unwatched",)

def _same(ra, rb):
    ca, cb = _canon(ra), _canon(rb)
    if ca[0] != cb[0]: return False
    if ca[0] == "progress": return abs(ca[1] - cb[1]) <= TOL_MS
    return True   # both watched, or both unwatched

def plan(a, b):
    """(to_B, to_A): only items whose category genuinely differs. Newer real state
    wins; we NEVER propagate 'unwatched' (positive-state only). Because writing
    makes the two sides _same(), the next run skips them — no ping-pong, no
    double-counting."""
    to_B, to_A = [], []
    for g in set(a) | set(b):
        ra = a.get(g) or dict(_ZERO, title=(b.get(g) or {}).get("title", ""))
        rb = b.get(g) or dict(_ZERO, title=(a.get(g) or {}).get("title", ""))
        if _same(ra, rb):
            continue
        if ra["lvat"] >= rb["lvat"]:
            if _canon(ra)[0] != "unwatched": to_B.append((g, ra))
        else:
            if _canon(rb)[0] != "unwatched": to_A.append((g, rb))
    return to_B, to_A

def apply(url, items, limit):
    done = skip = err = 0
    for g, rec in items:
        if done >= limit:
            print(f"    (batch cap {limit} reached; remaining next run)"); break
        try:
            rk = resolve(url, g)
            if not rk:
                skip += 1; continue      # item not present on target
            what = write_state(url, rk, rec)
            if what:
                done += 1
                print(f"    -> {rec['title'][:40]}: {what}")
                time.sleep(0.15)         # gentle rate-limit
        except Exception as e:
            err += 1
            if err <= 5:
                print(f"    !! {rec.get('title','?')[:40]}: {str(e)[:60]}")
    return done, skip, err

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--limit", type=int, default=250, help="max writes per side per run")
    args = ap.parse_args()

    print(f"reading A ({A_URL}) ..."); a = get_state(A_URL)
    print(f"reading B ({B_URL}) ..."); b = get_state(B_URL)
    print(f"  A has state for {len(a)} items | B has state for {len(b)} items")
    to_B, to_A = plan(a, b)
    print(f"  diff: {len(to_B)} to push A->B, {len(to_A)} to push B->A")
    for label, items in (("A->B sample", to_B), ("B->A sample", to_A)):
        for g, rec in items[:5]:
            st = "watched" if (rec["vc"] and not rec["vo"]) else f"@{rec['vo']//1000}s"
            print(f"    {label}: {rec['title'][:45]} ({st})")
    if not args.apply:
        print("DRY-RUN — nothing written. Re-run with --apply to sync.")
    else:
        print(f"applying A->B (cap {args.limit}) ..."); dB, sB, eB = apply(B_URL, to_B, args.limit)
        print(f"applying B->A (cap {args.limit}) ..."); dA, sA, eA = apply(A_URL, to_A, args.limit)
        print(f"DONE: B<-A wrote {dB} (skip {sB} not-present, err {eB}) | A<-B wrote {dA} (skip {sA}, err {eA})")
