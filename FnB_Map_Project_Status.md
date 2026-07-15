# Melbourne F&B Popularity Map — Project Status

**Goal:** Map every restaurant/bar/F&B outlet in Melbourne and score popularity via two layers — **reviews** and **foot traffic**.

---

## ✅ What's Working

| Component | File | Status |
|---|---|---|
| **Venue extraction** (OpenStreetMap, free) | `melbourne_fnb_mapper.py` | ✅ 10,617 Greater Melbourne venues in `venues_real.csv` |
| **Interactive map** (Leaflet HTML) | `melbourne_fnb_map.html` | ✅ Loads CSV, colours by score, A/B layer toggle |
| **Reviews enrichment** (Scrappa) | `reviews_scrappa.py` | ✅ Working — endpoint, auth, parallel, safety features |

---

## 🔧 Key Technical Facts (Locked In)

- **Scrappa endpoints:**
  - Search = `https://scrappa.co/api/maps/simple-search`
  - Reviews = `https://scrappa.co/api/maps/reviews`
  - Auth header: `x-api-key`
  - Reviews needs a `business_id` (`0x...:0x...` format) obtained from search first
- **Environment quirks:**
  - Crown corporate SSL required `pip install pip-system-certs`
  - Overpass API required a custom `User-Agent` header
  - Downloads keep saving as `.url` shortcuts → **copy-paste is the reliable route**
- **Latest `reviews_scrappa.py` features:**
  - `--workers` (parallel requests, 5–10× faster)
  - `--save-every` (incremental checkpoints)
  - `--resume` (skip already-enriched venues)
  - 403 auto-stop (halts + saves when credits run out)
  - Locked-file fallback (writes timestamped file if CSV open in Excel)

---

## ⚠️ Open Issues

1. **Ran out of Scrappa credits** mid-run (403 flood ~venue 9,300) → only 1,642 matched
2. **Double-space query bug** (`"Name  Melbourne"` when suburb is blank) suppressing matches — not yet fixed
3. **Foot-traffic columns empty** (`foot_monthly`, `foot_daily_avg`, `dwell_min`, `foot_source`, `popularity_foot`) — no free source exists; needs paid vendor (Placer/SafeGraph) or the proposed free `footproxy`
4. **No `gl=au` region param** yet on Scrappa calls

---

## 📋 Proposed Next Steps (Awaiting Go-Ahead)

- **(a)** Fix double-space query + add `match_status` diagnostic column + name-only fallback search → higher yield, explainable misses
- **(b)** Add `gl=au` / `hl=en` params for correct Australian results
- **(c)** Two-run velocity tracking (cheaper & more accurate than the 20-review sample estimate)
- **(d)** `--exclude-chains` / suburb filter to save credits
- **(e)** Free `footproxy` scorer for the empty foot-traffic layer

---

## 🎯 Immediate To-Do

1. **Top up Scrappa credits** (or confirm current balance)
2. **Close `venues_reviews.csv` in Excel**
3. Re-run with `--resume` (only fetches the ~9,000 unmatched; no re-spend on completed venues):
   ```bat
   python reviews_scrappa.py --in venues_real.csv --key YOUR_KEY --out venues_reviews.csv --workers 10 --resume
   ```

---

## Data Source Reality Check (Established Earlier)

- **Ratings:** Google Places (paid, ~$275/mo), Yelp (paid), Foursquare (needs billing enabled) all gated. **Scrappa** (purchased) is the working route. Tripadvisor (5k free/mo) and Overture Maps (free, no card, patchy) are alternatives.
- **Foot traffic / visitation:** No API — including Google Places — provides real per-venue visit counts. Only mobile-panel providers (Placer.ai, SafeGraph, Unacast) do, and those are **paid, estimate-based, and patchy for small/AU venues**. Satellite and Google "Popular Times" are relative-only, not absolute.

---

## Recommended Path

Do **(a) + (b) + (d)** together so your credit top-up buys a **clean, filtered, high-yield run**, then load the map. The `footproxy` (e) fills the foot-traffic layer for free as an estimate if you want the second layer populated.

*Status compiled: 15 July 2026*
