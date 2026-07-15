#!/usr/bin/env python3
"""
scrappa_velocity.py  --  fill review_velocity_per_week for ALL venues, safely.

SAFETY (fixes the "overwrote my CSV with 50 rows" problem):
  * ALWAYS keeps every row of your input CSV.
  * Writes to a NEW file by default (never overwrites the input).
  * --limit ONLY caps how many API calls are made this run; all other rows are
    still written out unchanged.
  * Checkpoint (scrappa_velocity_cache.csv) keyed by osm_id -> re-run RESUMES,
    nothing already fetched is lost or re-charged.

ENDPOINTS (from Scrappa docs):
  search : GET /api/maps/simple-search?query=...&limit=5     header X-API-KEY
  reviews: GET /api/maps/reviews?business_id=0x..:0x..&sort=2&pages=3   header X-API-KEY

USAGE
  # one-call sanity check (spends ~1-2 credits, writes nothing):
  python scrappa_velocity.py --selftest --key YOUR_KEY

  # test 50 (full CSV still written out):
  python scrappa_velocity.py --in venues_reviews.csv --key YOUR_KEY --limit 50

  # full run (resumes from checkpoint; keep workers LOW - reviews endpoint is heavy):
  python scrappa_velocity.py --in venues_reviews.csv --key YOUR_KEY --workers 3
"""

import argparse, csv, os, re, sys, time, threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

# ------------------------------------------------------------------ CONFIG ---
SEARCH_URL  = "https://scrappa.co/api/maps/simple-search"
REVIEWS_URL = "https://scrappa.co/api/maps/reviews"
CACHE = "scrappa_velocity_cache.csv"

UNIT_DAYS = {"minute":1/1440,"hour":1/24,"day":1,"week":7,"month":30,"year":365}

_print_lock = threading.Lock()
def log(*a):
    with _print_lock:
        print(*a, flush=True)

def parse_relative_date(s, now=None):
    """'2 weeks ago' / 'a day ago' -> approx datetime."""
    if not s: return None
    now = now or datetime.now()
    s = str(s).lower().strip()
    m = re.search(r"(a|an|\d+)\s+(minute|hour|day|week|month|year)", s)
    if not m: return None
    n = 1 if m.group(1) in ("a","an") else int(m.group(1))
    return now - timedelta(days=n*UNIT_DAYS[m.group(2)])

def _get_with_retry(sess, url, key, params, tries=4):
    """GET with exponential backoff on 503/429/5xx (reviews endpoint is heavy)."""
    delay = 1.5
    last = None
    for _ in range(tries):
        r = sess.get(url, headers={"X-API-KEY": key}, params=params, timeout=45)
        if r.status_code == 403:
            raise RuntimeError("403")
        if r.status_code in (429, 500, 502, 503, 504):
            last = r
            time.sleep(delay)
            delay *= 2
            continue
        return r
    return last

def find_business_id(sess, name, lat, lon, key):
    """simple-search; return business_id of the result closest to our coords."""
    params = {"query": name, "limit": 5}
    r = _get_with_retry(sess, SEARCH_URL, key, params)
    if r is None: return None
    if r.status_code == 404: return None
    r.raise_for_status()
    data = r.json()
    results = data.get("results") or data.get("items") or data.get("data") or []
    if not results: return None
    try:
        tlat, tlon = float(lat), float(lon)
    except (TypeError, ValueError):
        tlat = tlon = None
    def dist(res):
        c = res.get("coordinates") or {}
        rlat = c.get("lat", res.get("latitude"))
        rlon = c.get("lng", res.get("longitude"))
        if tlat is None or rlat is None: return 9e9
        try:
            return (float(rlat)-tlat)**2 + (float(rlon)-tlon)**2
        except (TypeError, ValueError):
            return 9e9
    best = min(results, key=dist)
    return (best.get("business_id") or best.get("data_id")
            or best.get("place_id") or best.get("cid"))

def fetch_review_dates(sess, business_id, key, pages, sleep):
    """Newest reviews' dates for one venue (sort=2), several pages in one call."""
    params = {"business_id": business_id, "sort": 2, "pages": max(1, pages)}
    r = _get_with_retry(sess, REVIEWS_URL, key, params)
    if r is None: return []               # stayed 503 -> skip; re-run will retry
    if r.status_code == 404: return []
    r.raise_for_status()
    data = r.json()
    dates = []
    for rv in (data.get("reviews") or []):
        d = parse_relative_date(rv.get("date") or rv.get("relative_date"))
        if d: dates.append(d)
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
            w.writerow([osm_id, velocity, business_id or "", datetime.now().isoformat()])

# ---------------------------------------------------------------- SELFTEST ---
def selftest(key):
    sess = requests.Session()
    log("[selftest] searching for 'coffee' ...")
    r = _get_with_retry(sess, SEARCH_URL, key, {"query":"coffee","limit":3})
    log(f"  search HTTP {getattr(r,'status_code','?')}")
    if r is None or r.status_code != 200:
        log("  body:", getattr(r,"text","")[:300]); return
    data = r.json()
    results = data.get("results") or data.get("items") or data.get("data") or []
    log(f"  results returned: {len(results)}")
    if not results:
        log("  raw:", str(data)[:300]); return
    bid = (results[0].get("business_id") or results[0].get("data_id")
           or results[0].get("place_id"))
    log(f"  first business_id: {bid}")
    log("[selftest] fetching reviews for that id ...")
    r2 = _get_with_retry(sess, REVIEWS_URL, key, {"business_id":bid,"sort":2,"pages":1})
    log(f"  reviews HTTP {getattr(r2,'status_code','?')}")
    if r2 is None or r2.status_code != 200:
        log("  body:", getattr(r2,"text","")[:300]); return
    d2 = r2.json()
    revs = d2.get("reviews") or []
    log(f"  reviews returned: {len(revs)}")
    if revs:
        log(f"  sample date field: {revs[0].get('date') or revs[0].get('relative_date')!r}")
        dates = [parse_relative_date(rv.get('date') or rv.get('relative_date')) for rv in revs]
        dates = [d for d in dates if d]
        log(f"  computed velocity/wk: {velocity_from_dates(dates)}")
    log("[selftest] DONE - if you see reviews + a velocity above, the full run will work.")

# -------------------------------------------------------------------- MAIN ---
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile")
    ap.add_argument("--key", required=True)
    ap.add_argument("--out", default="venues_reviews_velocity.csv")
    ap.add_argument("--pages", type=int, default=3, help="review pages per venue")
    ap.add_argument("--workers", type=int, default=3, help="KEEP LOW (reviews endpoint is heavy)")
    ap.add_argument("--sleep", type=float, default=0.2)
    ap.add_argument("--limit", type=int, default=0, help="cap API CALLS this run (0=all)")
    ap.add_argument("--recompute", action="store_true", help="ignore cache, refetch")
    ap.add_argument("--selftest", action="store_true", help="one search+reviews call, then exit")
    args = ap.parse_args()

    if args.selftest:
        selftest(args.key); return
    if not args.infile:
        sys.exit("--in is required (or use --selftest)")

    rows = list(csv.DictReader(open(args.infile, encoding="utf-8")))
    if not rows: sys.exit("no rows in " + args.infile)
    fields = list(rows[0].keys())
    if "review_velocity_per_week" not in fields:
        fields.append("review_velocity_per_week")
    log(f"[load] {len(rows)} rows from {args.infile}")

    def has_vel(r):
        v = r.get("review_velocity_per_week")
        return v is not None and str(v).strip() != ""

    cache = {} if args.recompute else load_cache()
    for r in rows:
        c = cache.get(r.get("osm_id"))
        if c and str(c.get("velocity","")).strip() != "":
            r["review_velocity_per_week"] = c["velocity"]
    log(f"[cache] {sum(1 for r in rows if has_vel(r))} venues already have velocity")

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
        try:
            bid = find_business_id(sess, name, r.get("lat"), r.get("lon"), args.key)
            if not bid:
                append_cache(oid, "", ""); return
            dates = fetch_review_dates(sess, bid, args.key, args.pages, args.sleep)
            vel = velocity_from_dates(dates)
            r["review_velocity_per_week"] = vel
            append_cache(oid, vel, bid)
        except RuntimeError:
            stop.set()
            log("  !! 403 from Scrappa (out of credits/invalid key). Stopping; progress saved.")
        except Exception as ex:
            log(f"  ! {name}: {ex}")

    done = 0
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(work, r) for r in todo]
            for fut in as_completed(futs):
                fut.result(); done += 1
                if done % 100 == 0: log(f"  ...{done}/{len(todo)}")
    except KeyboardInterrupt:
        stop.set(); log("interrupted - writing what we have")

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})

    filled = sum(1 for r in rows if has_vel(r))
    log(f"[done] velocity filled: {filled}/{len(rows)} ({100*filled//len(rows)}%)")
    log(f"[done] wrote ALL {len(rows)} rows -> {args.out}")
    log(f"[done] checkpoint kept in {CACHE} (re-run to continue)")

if __name__ == "__main__":
    main()
