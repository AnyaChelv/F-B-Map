# Melbourne F&B Popularity Mapper

Map **every** bar / restaurant / cafe / bubble-tea shop in Melbourne and score how
popular each one is using **two independent, comparable methods** so you can judge
them side by side:

| Method | What it measures | Source | Cost |
|---|---|---|---|
| **A - Reviews** | `rating x review_count x review_velocity` (a strong, legit footfall proxy) | Google Places API | ~pay-per-call above free tier |
| **B - Foot traffic** | **ABSOLUTE** monthly visits (real people counted) | Licensed vendor CSV (Techsalerator / xMap / etc.) | enterprise contract |

The **venue layer itself is 100% free** — it comes from OpenStreetMap (no API key).

---

## Install

Only one dependency (everything else is Python standard library):

```bash
pip install requests
```

The map is a **standalone HTML file** — no folium, no server, just open it in a browser.

---

## The 4-step pipeline

Each step writes a CSV you can open/inspect before moving on.

### 1. Extract venues from OpenStreetMap  *(free, no key)*
```bash
python melbourne_fnb_mapper.py extract --out venues_base.csv
# custom area:  --bbox "S,W,N,E"  e.g. "-37.86,144.90,-37.76,145.02"
```

### 2. Enrich with reviews  *(needs a Google Places API key)*
```bash
python melbourne_fnb_mapper.py reviews --in venues_base.csv --key YOUR_GOOGLE_KEY --out venues_reviews.csv
```
- **Review velocity:** the first run can only *estimate* velocity from the ~5 recent
  reviews Google returns. The script logs a snapshot to `review_history.csv`, so from
  the **2nd run onward it computes TRUE tracked velocity** (new reviews ÷ days between runs).
  Re-run it weekly to build momentum data.

### 3. Merge absolute foot traffic  *(needs a licensed vendor CSV)*
```bash
python melbourne_fnb_mapper.py foottraffic --in venues_reviews.csv --vendor vendor_foot.csv --out venues_master.csv
```
Give the vendor `foot_traffic_template.csv` — it's the exact column format the script expects:

```
venue_name,latitude,longitude,monthly_visits,daily_avg_visits,dwell_time_min,source
```
Matching is by nearest coordinate (default 60 m, `--match-radius`) + fuzzy name.

### 4. Build the interactive map
```bash
python melbourne_fnb_mapper.py map --in venues_master.csv --out melbourne_fnb_map.html
```
Open `melbourne_fnb_map.html` in any browser. Top-right control toggles:
- **A: Review popularity** (markers + heatmap)
- **B: Foot traffic ABS** (markers + heatmap)

Click any venue for a **side-by-side comparison table** of both methods. Marker size &
colour = popularity score (0-100). Grey = no data for that method.

---

## Comparing the two methods (the whole point)

- **Reviews** are free-ish, global, and comparable, but skew toward tourist-heavy /
  older venues and lag reality.
- **Foot traffic** is the closest to *absolute* truth but costs money and has thin
  coverage for tiny outlets (e.g. a new bubble-tea kiosk).

Where they **disagree** is the interesting signal: a venue with huge foot traffic but
low review score = under-marketed / captive location; high reviews but low foot count
= punching above its weight / destination venue.

Both scores are min-max normalised to 0-100 (review volume & foot counts are
log-scaled first, since footfall is heavy-tailed). Weights for the review score live at
the top of the script (`W_VOLUME`, `W_MOMENTUM`, `W_QUALITY`) — tune freely.

---

## Notes & gotchas
- **OSM coverage:** great for established venues; brand-new spots may be missing. Re-run
  `extract` periodically.
- **No scraping:** this tool deliberately avoids Google "Popular Times" scraping (ToS
  grey-zone). Reviews use the official Places API.
- **Cost control:** run `reviews` on a filtered precinct (Southbank, Flinders Lane,
  Chinatown, Lygon St) rather than all of Greater Melbourne to keep API spend down.
