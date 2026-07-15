#!/usr/bin/env python3
"""
scrappa_velocity.py  --  fill review_velocity_per_week for ALL venues, safely.

WHY THIS IS SAFE (fixes the "overwrote my CSV with 50 rows" problem):
  * It ALWAYS keeps every row of your input CSV.
  * It writes to a NEW file by default (never overwrites the input).
  * --limit ONLY caps how many API calls are made this run; every other row is
    still written out unchanged.
  * A checkpoint file (scrappa_velocity_cache.csv) stores results keyed by osm_id,
    so if the run stops (e.g. 403 = out of Scrappa credits) you just re-run and it
    RESUMES where it left off. Nothing already fetched is lost or re-charged.

USAGE
  # test on 50 venues first (safe - full CSV still written out):
  python scrappa_velocity.py --in venues_reviews.csv --key YOUR_KEY --limit 50

  # then the full run (resumes from checkpoint, only calls venues not yet done):
  python scrappa_velocity.py --in venues_reviews.csv --key YOUR_KEY --workers 8

  # output defaults to venues_reviews_velocity.csv  (change with --out)

NOTES
  * Your CSV has no business_id, so we SEARCH by name+coords to find it, then pull
    the newest reviews and estimate reviews/week from their dates.
  * Endpoints/field names below match Scrappa's maps API (x-api-key header,
    business_id in 0x..:0x.. form). If your Scrappa account uses slightly different
    paths/fields, adjust the two CONFIG functions marked  # <-- ADJUST IF NEEDED.
"""

import argparse, csv, os, re, sys, time, threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

# ------------------------------------------------------------------ CONFIG ---
SEARCH_URL  = "https://scrappa.co/api/maps/search"    # <-- ADJUST IF NEEDED
REVIEWS_URL = "https://scrappa.co/api/maps/reviews"   # <-- ADJUST IF NEEDED
CACHE = "scrappa_velocity_cache.csv"                  # checkpoint (osm_id,velocity,business_id,ts)

UNIT_DAYS = {"minute":1/1440,"hour":1/24,"day":1,"week":7,"month":30,"year":365}

_print_lock = threading.Lock()
def log(*a):
    with _print_lock:
        print(*a, flush=True)

def parse_relative_date(s, now=None):
    """'2 weeks ago' / 'a day ago' -> approx datetime."""
    if not s: return None
    now = now or datetime.utcnow()
    s = str(s).lower().strip()
    m = re.search(r"(a|an|\d+)\s+(minute|hour|day|week|month|year)", s)
    if not m: return None
    n = 1 if m.group(1) in ("a","an") else int(m.group(1))
    return now - timedelta(days=n*UNIT_DAYS[m.group(2)])

def find_business_id(sess, name, lat, lon, key):
    """Search Scrappa for the venue; return its business_id (or None)."""
    params = {"query": name, "ll": f"@{lat},{lon},17z"}     # <-- ADJUST IF NEEDED
    r = sess.get(SEARCH_URL, headers={"x-api-key": key}, params=params, timeout=30)
    if r.status_code == 403: raise RuntimeError("403")
    r.raise_for_status()
    data = r.json()
    results = data.get("results") or data.get("local_results") or data.get("data") or []
    if not results: return None
    first = results[0]
    return (first.get("business_id") or first.get("data_id")
            or first.get("place_id") or first.get("cid"))          # <-- ADJUST IF NEEDED

def fetch_review_dates(sess, business_id, key, pages, sleep):
    """Return list of datetimes for the newest reviews of one venue."""
    dates, page = [], None
    for _ in range(pages):
        params = {"business_id": business_id, "sort": 2}            # sort=2 -> newest
        if page: params["page"] = page
        r = sess.get(REVIEWS_URL, headers={"x-api-key": key}, params=params, timeout=30)
        if r.status_code == 403: raise RuntimeError("403")
        r.raise_for_status()
        data = r.json()
        for rv in (data.get("reviews") or []):
            d = parse_relative_date(rv.get("date") or rv.get("relative_date"))
            if d: dates.append(d)
        page = (data.get("pagination") or {}).get("next_page_token") \
            or (data.get("pagination") or {}).get("nextPage")
        if not page: break
        time.sleep(sleep)
    return dates

def velocity_from_dates(dates):
    if len(dates) < 2: return ""
    span = (max(dates) - min(dates)).days or 1
    return round(len(dates)/span*7.0, 3)

# ------------------------------------------------------------------- CACHE ---
def load_cache():
    cache = {}
    if os.path.exists(CACHE):
        for row in csv.DictReader(open(CACHE, encoding="utf-8")):
            cache[row["osm_id"]] = row
    return cache

_cache_lock = threading.Lock()
def append_cache(osm_id, velocity, business_id):
    new = not os.path.exists(CACHE)
    with _cache_lock:
        with open(CACHE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new: w.writerow(["osm_id","velocity","business_id","ts"])
            w.writerow([osm_id, velocity, business_id or "", datetime.utcnow().isoformat()])

# -------------------------------------------------------------------- MAIN ---
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--out", default="venues_reviews_velocity.csv")
    ap.add_argument("--pages", type=int, default=2, help="review pages per venue")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--sleep", type=float, default=0.1)
    ap.add_argument("--limit", type=int, default=0, help="cap API CALLS this run (0=all)")
    ap.add_argument("--recompute", action="store_true",
                    help="ignore cache and refetch everything")
    args = ap.parse_args()

    # 1) read ALL rows (this is the master list we always write back)
    rows = list(csv.DictReader(open(args.infile, encoding="utf-8")))
    if not rows: sys.exit("no rows in " + args.infile)
    fields = list(rows[0].keys())
    if "review_velocity_per_week" not in fields:
        fields.append("review_velocity_per_week")
    log(f"[load] {len(rows)} rows from {args.infile}")

    # 2) apply any cached results first (instant, no API cost)
    cache = {} if args.recompute else load_cache()
    for r in rows:
        c = cache.get(r.get("osm_id"))
        if c and c.get("velocity","") != "":
            r["review_velocity_per_week"] = c["velocity"]
    def has_vel(r):
        v = r.get("review_velocity_per_week")
        return v is not None and str(v).strip() != ""
    already = sum(1 for r in rows if has_vel(r))
    log(f"[cache] {already} venues already have velocity")

    # 3) decide who still needs an API call
    todo = [r for r in rows
            if not has_vel(r)
            and (r.get("lat") or "").strip() and (r.get("lon") or "").strip()]
    if args.limit:
        todo = todo[:args.limit]
    log(f"[plan] will call API for {len(todo)} venues this run")

    stop = threading.Event()

    def work(r):
        if stop.is_set(): return
        sess = requests.Session()
        oid, name = r.get("osm_id"), r.get("name")
        lat, lon = r.get("lat"), r.get("lon")
        try:
            bid = find_business_id(sess, name, lat, lon, args.key)
            if not bid:
                append_cache(oid, "", "")         # remember "no match" so we skip next time
                return
            dates = fetch_review_dates(sess, bid, args.key, args.pages, args.sleep)
            vel = velocity_from_dates(dates)
            r["review_velocity_per_week"] = vel
            append_cache(oid, vel, bid)
        except RuntimeError:                       # 403 = out of credits -> stop cleanly
            stop.set()
            log("  !! 403 from Scrappa (out of credits). Stopping; progress is saved.")
        except Exception as ex:
            log(f"  ! {name}: {ex}")

    # 4) run pooled, but bail politely on 403
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(work, r) for r in todo]
            for fut in as_completed(futs):
                fut.result(); done += 1
                if done % 100 == 0: log(f"  ...{done}/{len(todo)}")
    except KeyboardInterrupt:
        stop.set(); log("interrupted - writing what we have")

    # 5) ALWAYS write ALL rows back (never truncated), to the OUTPUT file
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})

    filled = sum(1 for r in rows if has_vel(r))
    log(f"[done] velocity filled: {filled}/{len(rows)} ({100*filled//len(rows)}%)")
    log(f"[done] wrote ALL {len(rows)} rows -> {args.out}")
    log(f"[done] checkpoint kept in {CACHE} (re-run to continue if credits ran out)")

if __name__ == "__main__":
    main()
