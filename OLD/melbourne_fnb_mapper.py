#!/usr/bin/env python3
"""
melbourne_fnb_mapper.py

Pipeline:
  extract      Pull all F&B points-of-interest from OpenStreetMap (free, no key)
  reviews      Enrich those venues with Google Places rating/review data
  foottraffic  Merge a commercial foot-traffic vendor CSV (absolute counts)
  map          Build a standalone interactive HTML map (modern UI)

Only external dependency: requests  (pip install requests)
"""

import argparse
import csv as _csv
import json
import math
import os
import sys
import time
from datetime import datetime

try:
    import requests
except ImportError:
    requests = None

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
DEFAULT_BBOX = (-37.86, 144.90, -37.76, 145.02)
OSM_AMENITIES = ["restaurant", "bar", "pub", "cafe", "fast_food",
                 "food_court", "ice_cream", "biergarten"]
OSM_SHOPS = ["bubble_tea", "coffee", "confectionery", "pastry"]
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
W_VOLUME = 0.50
W_MOMENTUM = 0.30
W_QUALITY = 0.20
PLACES_MATCH_RADIUS_M = 80
NAME_SIM_THRESHOLD = 0.55

MASTER_FIELDS = [
    "osm_id", "osm_type", "name", "category", "cuisine",
    "lat", "lon", "address", "suburb", "website", "opening_hours",
    "rating", "review_count", "review_velocity_per_week", "popularity_review",
    "foot_monthly", "foot_daily_avg", "dwell_min", "foot_source", "popularity_foot",
]
JS_FIELDS = [
    "name", "category", "cuisine", "suburb", "lat", "lon",
    "rating", "review_count", "review_velocity_per_week", "popularity_review",
    "foot_monthly", "foot_daily_avg", "dwell_min", "popularity_foot", "website",
]

# ---------------------------------------------------------------------------
# UTILITIES
# ---------------------------------------------------------------------------

def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def name_similarity(a, b):
    from difflib import SequenceMatcher
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(_csv.DictReader(f))


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})
    print(f"  -> wrote {len(rows)} rows to {path}")


def to_float(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default

# ---------------------------------------------------------------------------
# STEP 1 - EXTRACT
# ---------------------------------------------------------------------------

def build_overpass_query(bbox):
    s, w, n, e = bbox
    box = f"{s},{w},{n},{e}"
    am = "|".join(OSM_AMENITIES)
    sh = "|".join(OSM_SHOPS)
    return f"""
[out:json][timeout:180];
(
  node["amenity"~"^({am})$"]({box});
  way ["amenity"~"^({am})$"]({box});
  node["shop"~"^({sh})$"]({box});
  way ["shop"~"^({sh})$"]({box});
  node["cuisine"~"bubble_tea"]({box});
  way ["cuisine"~"bubble_tea"]({box});
);
out center tags;
"""


def cmd_extract(args):
    if requests is None:
        sys.exit("This step needs requests. Run: pip install requests")
    bbox = tuple(float(x) for x in args.bbox.split(",")) if args.bbox else DEFAULT_BBOX
    query = build_overpass_query(bbox)
    print(f"[extract] bbox = {bbox}")

    data = None
    for url in OVERPASS_URLS:
        try:
            print(f"  querying {url} ...")
            r = requests.post(url, data={"data": query}, timeout=200)
            r.raise_for_status()
            data = r.json()
            break
        except Exception as ex:
            print(f"  ! {url} failed: {ex}")
            time.sleep(2)
    if data is None:
        sys.exit("All Overpass mirrors failed. Try again later or a smaller --bbox.")

    rows = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        if el["type"] == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            c = el.get("center", {})
            lat, lon = c.get("lat"), c.get("lon")
        if lat is None or lon is None:
            continue
        category = tags.get("amenity") or tags.get("shop") or ""
        addr_parts = [tags.get("addr:housenumber", ""), tags.get("addr:street", "")]
        address = " ".join(p for p in addr_parts if p).strip()
        rows.append({
            "osm_id": el.get("id", ""),
            "osm_type": el.get("type", ""),
            "name": name,
            "category": category,
            "cuisine": tags.get("cuisine", ""),
            "lat": lat,
            "lon": lon,
            "address": address,
            "suburb": tags.get("addr:suburb", ""),
            "website": tags.get("website", tags.get("contact:website", "")),
            "opening_hours": tags.get("opening_hours", ""),
        })

    seen, deduped = set(), []
    for r in rows:
        key = (r["name"].lower(), round(float(r["lat"]), 5), round(float(r["lon"]), 5))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    fields = ["osm_id", "osm_type", "name", "category", "cuisine",
              "lat", "lon", "address", "suburb", "website", "opening_hours"]
    write_csv(args.out, deduped, fields)
    print(f"[extract] {len(deduped)} unique venues.")

# ---------------------------------------------------------------------------
# STEP 2 - REVIEWS
# ---------------------------------------------------------------------------

def places_text_search(name, lat, lon, key):
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": ("places.displayName,places.location,places.rating,"
                             "places.userRatingCount,places.reviews"),
    }
    body = {
        "textQuery": name,
        "locationBias": {"circle": {
            "center": {"latitude": float(lat), "longitude": float(lon)},
            "radius": 200.0}},
        "maxResultCount": 3,
    }
    r = requests.post(url, headers=headers, data=json.dumps(body), timeout=30)
    r.raise_for_status()
    return r.json().get("places", [])


def recent_velocity_from_reviews(reviews):
    times = []
    for rv in reviews or []:
        t = rv.get("publishTime")
        if t:
            try:
                times.append(datetime.fromisoformat(t.replace("Z", "+00:00")))
            except ValueError:
                pass
    if len(times) < 2:
        return None
    span_days = (max(times) - min(times)).days or 1
    return round(len(times) / span_days * 7.0, 3)


def cmd_reviews(args):
    if requests is None:
        sys.exit("This step needs requests. Run: pip install requests")
    if not args.key:
        sys.exit("Google Places API key required: --key YOUR_KEY")

    # --limit only caps how many venues we CALL the API for.
    # We still write ALL venues back, so the CSV is never truncated.
    all_venues = read_csv(args.infile)
    venues = all_venues[:args.limit] if args.limit else all_venues

    hist_path = "review_history.csv"
    prev = {}
    if os.path.exists(hist_path):
        for row in read_csv(hist_path):
            prev[row.get("key", "")] = row

    hist_rows = []
    now = datetime.utcnow().isoformat()

    for i, v in enumerate(venues, 1):
        name, lat, lon = v.get("name"), v.get("lat"), v.get("lon")
        if not (name and lat and lon):
            continue
        try:
            places = places_text_search(name, lat, lon, args.key)
        except Exception as ex:
            print(f"  [{i}/{len(venues)}] {name}: search error {ex}")
            continue

        best, best_sim = None, 0.0
        for p in places:
            disp = (p.get("displayName") or {}).get("text", "")
            loc = p.get("location") or {}
            plat, plon = loc.get("latitude"), loc.get("longitude")
            if plat is None or plon is None:
                continue
            dist = haversine_m(float(lat), float(lon), plat, plon)
            sim = name_similarity(name, disp)
            if dist <= PLACES_MATCH_RADIUS_M and sim >= NAME_SIM_THRESHOLD and sim > best_sim:
                best, best_sim = p, sim

        if not best:
            print(f"  [{i}/{len(venues)}] {name}: no match")
            v["rating"] = v.get("rating", "")
            v["review_count"] = v.get("review_count", "")
            continue

        rating = best.get("rating", "")
        rc = best.get("userRatingCount", "")
        v["rating"] = rating
        v["review_count"] = rc

        key = f"{name}|{lat}|{lon}"
        vel = None
        if key in prev:
            try:
                prev_rc = float(prev[key].get("review_count", 0) or 0)
                prev_t = datetime.fromisoformat(prev[key].get("ts"))
                days = max((datetime.utcnow() - prev_t).days, 1)
                vel = round((float(rc or 0) - prev_rc) / days * 7.0, 3)
            except Exception:
                vel = None
        if vel is None:
            vel = recent_velocity_from_reviews(best.get("reviews"))
        v["review_velocity_per_week"] = vel if vel is not None else ""

        hist_rows.append({"key": key, "review_count": rc, "ts": now})
        print(f"  [{i}/{len(venues)}] {name}: matched (r={rating}, n={rc})")
        time.sleep(args.sleep)

    fields = list(all_venues[0].keys()) if all_venues else MASTER_FIELDS
    for extra in ("rating", "review_count", "review_velocity_per_week"):
        if extra not in fields:
            fields.append(extra)
    if hist_rows:
        write_csv(hist_path, hist_rows, ["key", "review_count", "ts"])

    compute_review_popularity(all_venues)
    if "popularity_review" not in fields:
        fields.append("popularity_review")
    write_csv(args.out, all_venues, fields)

# ---------------------------------------------------------------------------
# STEP 3 - FOOT TRAFFIC
# ---------------------------------------------------------------------------

def cmd_foottraffic(args):
    venues = read_csv(args.infile)
    vendor = read_csv(args.vendor)
    radius = args.match_radius

    for v in venues:
        vlat, vlon = to_float(v.get("lat")), to_float(v.get("lon"))
        if vlat is None or vlon is None:
            continue
        best, best_d = None, radius + 1
        for row in vendor:
            rlat = to_float(row.get("latitude"))
            rlon = to_float(row.get("longitude"))
            if rlat is None or rlon is None:
                continue
            d = haversine_m(vlat, vlon, rlat, rlon)
            if d <= radius and d < best_d and \
               name_similarity(v.get("name", ""), row.get("venue_name", "")) >= 0.4:
                best, best_d = row, d
        if best:
            v["foot_monthly"] = best.get("monthly_visits", "")
            v["foot_daily_avg"] = best.get("daily_avg_visits", "")
            v["dwell_min"] = best.get("dwell_time_min", "")
            v["foot_source"] = best.get("source", "")

    compute_review_popularity(venues)
    compute_foot_popularity(venues)
    write_csv(args.out, venues, MASTER_FIELDS)

# ---------------------------------------------------------------------------
# SCORING
# ---------------------------------------------------------------------------

def _minmax(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return (0.0, 1.0)
    lo, hi = min(vals), max(vals)
    return (lo, hi if hi > lo else lo + 1.0)


def compute_review_popularity(venues):
    vols = [math.log1p(to_float(v.get("review_count"), 0) or 0) for v in venues]
    moms = [to_float(v.get("review_velocity_per_week")) for v in venues]
    vlo, vhi = _minmax(vols)
    mlo, mhi = _minmax(moms)
    for v, vol in zip(venues, vols):
        rc = to_float(v.get("review_count"))
        if rc is None:
            v["popularity_review"] = ""
            continue
        nv = (vol - vlo) / (vhi - vlo)
        mom = to_float(v.get("review_velocity_per_week"))
        nm = 0.0 if mom is None else (mom - mlo) / (mhi - mlo)
        q = (to_float(v.get("rating"), 0) or 0) / 5.0
        score = 100 * (W_VOLUME * nv + W_MOMENTUM * nm + W_QUALITY * q)
        v["popularity_review"] = round(score, 1)


def compute_foot_popularity(venues):
    foots = [math.log1p(to_float(v.get("foot_monthly"), 0) or 0) for v in venues]
    flo, fhi = _minmax([f for f, v in zip(foots, venues)
                        if to_float(v.get("foot_monthly")) is not None])
    for v, f in zip(venues, foots):
        fm = to_float(v.get("foot_monthly"))
        if fm is None:
            v["popularity_foot"] = ""
        else:
            v["popularity_foot"] = round(100 * (f - flo) / (fhi - flo), 1)

# ---------------------------------------------------------------------------
# STEP 4 - MAP
# ---------------------------------------------------------------------------

HTML_HEAD = (
    "<!DOCTYPE html>\n"
    "<html><head><meta charset=\"utf-8\"/>\n"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\"/>\n"
    "<title>Melbourne F&B Popularity Map</title>\n"
    "<link rel=\"stylesheet\" href=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.css\"/>\n"
    "<script src=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js\"></script>\n"
    "<script src=\"https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js\"></script>\n"
    "<script src=\"https://html2canvas.hertzen.com/dist/html2canvas.min.js\"></script>\n"
)

HTML_BODY = r"""<style>
  :root{
    --brand:#22c9a9; --brand-2:#5eead4; --brand-soft:#E6FBF5;
    --ink:#2b2440; --muted:#8a8399; --line:#ece8f5;
    --chip-ink:#c9d1d9; --chip-active:#5eead4;
  }
  html,body,#map{height:100%;margin:0;font-family:'Segoe UI',Inter,Arial,sans-serif}
  .leaflet-tile-pane{filter:grayscale(1) contrast(.9) brightness(1.05)}
  .leaflet-top.leaflet-left{top:104px}
  .leaflet-popup-pane{z-index:1200}
  .leaflet-popup-content-wrapper{box-shadow:0 6px 22px rgba(0,0,0,.28)}
  .pop b{font-size:14px}
  table.cmp{border-collapse:collapse;margin-top:4px;font-size:12px}
  table.cmp td{border:1px solid #ddd;padding:2px 6px}

  .rating-card{background:#fff;border-radius:14px;padding:10px 11px 11px;width:194px;
      box-shadow:0 10px 24px rgba(34,201,169,.16),0 2px 6px rgba(0,0,0,.06);font-size:12px;color:var(--ink)}
  .rc-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:7px}
  .rc-title{font-weight:700;font-size:13px}
  .rc-clear{background:none;border:none;color:var(--brand);font-weight:600;cursor:pointer;
      font-size:11.5px;padding:2px 4px;border-radius:6px}
  .rc-clear:hover{background:var(--brand-soft)}
  .rate-row{display:flex;align-items:center;justify-content:space-between;padding:5px 9px;margin:3px 0;
      border:1.5px solid var(--line);border-radius:9px;cursor:pointer;transition:all .15s ease;background:#fff}
  .rate-row:hover{border-color:var(--brand-2);background:var(--brand-soft)}
  .rate-row.sel{border-color:var(--brand);background:var(--brand-soft);box-shadow:0 3px 9px rgba(34,201,169,.18)}
  .rate-row .lbl-txt{font-size:12px;font-weight:600;color:var(--ink)}
  .rate-row .chk{width:15px;height:15px;border-radius:5px;border:2px solid #cfc8e3;
      display:inline-block;position:relative;flex:0 0 auto;background:#fff}
  .rate-row.sel .chk{border-color:var(--brand);background:var(--brand)}
  .rate-row.sel .chk::after{content:'';position:absolute;left:4px;top:1px;width:3px;height:7px;
      border:solid #fff;border-width:0 2px 2px 0;transform:rotate(45deg)}
  .rc-sub{margin-top:8px;padding-top:8px;border-top:1px solid var(--line);display:flex;flex-direction:column;gap:6px}
  .rc-sub label{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:11px}
  .rc-sub input[type=number]{width:66px;border:1.5px solid var(--line);border-radius:7px;padding:3px 6px;font-size:11px;color:var(--ink)}
  .rc-apply{margin-top:9px;width:100%;border:none;cursor:pointer;color:#06342c;
      background:linear-gradient(90deg,var(--brand),var(--brand-2));padding:8px;border-radius:22px;
      font-size:12.5px;font-weight:700;box-shadow:0 6px 14px rgba(34,201,169,.35)}
  .rc-apply:hover{filter:brightness(1.05)}

  .top-cuisine-bar{position:fixed;left:8px;right:8px;top:8px;display:flex;gap:6px;
      flex-wrap:wrap;justify-content:center;align-content:flex-start;z-index:1001;padding:7px 10px;
      background:rgba(24,26,33,.82);backdrop-filter:blur(8px);border-radius:14px;box-shadow:0 8px 24px rgba(0,0,0,.28)}
  .cuisine-btn{background:transparent;border:1.5px solid rgba(255,255,255,.18);border-radius:10px;
      padding:4px 10px;cursor:pointer;color:var(--chip-ink);font-size:11px;font-weight:500;
      transition:all .15s ease;white-space:nowrap;line-height:1.3}
  .cuisine-btn:hover{border-color:var(--chip-active);color:#fff}
  .cuisine-btn.active{background:var(--chip-active);border-color:var(--chip-active);color:#0b3b34;
      font-weight:600;box-shadow:0 4px 12px rgba(94,234,212,.3)}
  .cuisine-btn:focus{outline:none}
  /* chip that has an active second-layer selection gets a ring */
  .cuisine-btn.has-sub{box-shadow:0 0 0 2px var(--chip-active) inset}
  /* right-click second-layer menu */
  .cuisine-menu{position:fixed;z-index:3000;background:#fff;border-radius:12px;padding:6px;
      box-shadow:0 12px 30px rgba(0,0,0,.28);min-width:160px;max-height:340px;overflow:auto;font-size:12px}
  .cuisine-menu .cm-title{font-weight:700;color:var(--ink);padding:6px 10px 4px;font-size:12.5px;
      border-bottom:1px solid var(--line);margin-bottom:4px}
  .cuisine-menu .cm-row{padding:6px 10px;border-radius:8px;cursor:pointer;color:var(--ink)}
  .cuisine-menu .cm-row:hover{background:var(--brand-soft)}
  .cuisine-menu .cm-row.on{background:var(--brand);color:#06342c;font-weight:600}

  .export-btn{position:fixed;right:12px;top:96px;z-index:1002;border:none;cursor:pointer;
      color:#06342c;background:linear-gradient(90deg,var(--brand),var(--brand-2));
      padding:10px 16px;border-radius:24px;font-size:13px;font-weight:700;
      box-shadow:0 8px 18px rgba(34,201,169,.35)}
  .export-btn:hover{filter:brightness(1.05)}
  .export-panel{position:fixed;top:0;right:0;height:100%;width:440px;max-width:94vw;background:#fff;
      z-index:2000;box-shadow:-10px 0 34px rgba(0,0,0,.28);display:none;flex-direction:column}
  .export-panel.open{display:flex}
  .ep-head{background:#159b84;color:#fff;padding:14px 16px;font-weight:700;font-size:15px;
      display:flex;justify-content:space-between;align-items:center}
  .ep-close{cursor:pointer;background:none;border:none;color:#fff;font-size:20px;line-height:1}
  .ep-body{overflow:auto;flex:1}
  table.exp{width:100%;border-collapse:collapse;font-size:12.5px}
  table.exp thead th{background:#159b84;color:#fff;text-align:left;padding:9px 12px;position:sticky;top:0}
  table.exp thead th.num{text-align:right}
  table.exp td{padding:8px 12px;border-bottom:1px solid #eee;color:var(--ink)}
  table.exp td.num{text-align:right;font-variant-numeric:tabular-nums}
  table.exp tr:nth-child(even){background:#f6fbfa}
  .ep-actions{padding:12px 16px;border-top:1px solid #eee;display:flex;gap:10px;align-items:center}
  .ep-dl{border:none;cursor:pointer;color:#06342c;background:linear-gradient(90deg,var(--brand),var(--brand-2));
      padding:9px 16px;border-radius:22px;font-size:13px;font-weight:700}
  .ep-count{color:var(--muted);font-size:12px}
</style></head><body>
<div id="cuisineBar" class="top-cuisine-bar" aria-label="Cuisine selector"></div>
<button id="exportBtn" class="export-btn" type="button">&#9776; Show list</button>
<div id="exportPanel" class="export-panel">
  <div class="ep-head"><span>Venues by Rating</span>
    <button id="epClose" class="ep-close" type="button">&times;</button></div>
  <div class="ep-body" id="epBody"></div>
  <div class="ep-actions"><button id="epDownload" class="ep-dl" type="button">&#11015; Export PNG</button>
    <span class="ep-count" id="epCount"></span></div>
</div>
<div id="map"></div>
<script>
/*__DATA__*/
const map = L.map('map').setView(CENTER, 14);
L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
  {maxZoom:19, subdomains:'abcd', attribution:'&copy; OpenStreetMap &copy; CARTO'}).addTo(map);

const POPUP_OPTS = {autoPan:true, keepInView:true,
  autoPanPaddingTopLeft:[20,140], autoPanPaddingBottomRight:[160,20]};

function ratingColor(r){
    if(r===''||r==null) return '#9e9e9e';
    return r<3.5 ? '#FF7F00' : r<4.0 ? '#FFC04D' : r<4.3 ? '#C7E86A' :
                 r<4.6 ? '#66C2A5' : r<4.9 ? '#2CA25F' : '#006837';
}
function radius(){ return 7; }
function popup(v){
  return `<div class="pop"><b>${v.name}</b><br><i>${v.category}${v.cuisine?' &middot; '+v.cuisine:''}</i>
    <table class="cmp">
      <tr><td>Rating</td><td>${v.rating||'-'} &#9733;</td></tr>
      <tr><td>Review count</td><td>${v.review_count||'-'}</td></tr>
      <tr><td>Review score</td><td>${v.popularity_review||'-'}/100</td></tr>
    </table></div>`;
}
// If a venue has no cuisine tag, fall back to its OSM category (e.g. pub, cafe).
function titleCase(s){ return s.replace(/\b\w/g, c=>c.toUpperCase()); }
function normaliseCuisine(raw, category){
    let s = (raw||'').toString().toLowerCase().trim().replace(/[_\-]+/g,' ');
    if(!s) s = (category||'').toString().toLowerCase().trim().replace(/[_\-]+/g,' ');
    if(!s) return 'Other';
    const map = [
      [/\b(bubble tea|boba)\b/,'Bubble Tea'],
      [/\b(juice|smoothie|tea|drinks|beverage)\b/,'Drinks & Juice'],
      [/\b(bakery|pastry|patisserie|bagel|croissant)\b/,'Bakery'],
      [/\b(coffee|cafe|breakfast|brunch)\b/,'Cafe'],
      [/\b(pub)\b/,'Pub'],
      [/\b(bar|wine|beer|cocktail|biergarten|brewery)\b/,'Bar'],
      [/\b(pizza|italian|pasta|trattoria)\b/,'Italian'],
      [/\b(chinese|dumpling|dumplings|yum cha|cantonese|szechuan|sichuan|hotpot|hot pot)\b/,'Chinese'],
      [/\b(japanese|sushi|ramen|izakaya|yakitori)\b/,'Japanese'],
      [/\b(korean|kbbq|bibimbap)\b/,'Korean'],
      [/\b(thai)\b/,'Thai'],
      [/\b(vietnamese|pho|banh mi)\b/,'Vietnamese'],
      [/\b(indian|curry|tandoori)\b/,'Indian'],
      [/\b(asian|noodle|noodles|malaysian|indonesian|singaporean|dim sum)\b/,'Other Asian'],
      [/\b(african|ethiopian|somali|somalian|eritrean|nigerian|sudanese|kenyan|ghanaian|senegalese|moroccan|egyptian|tunisian|algerian)\b/,'African'],
      [/\b(mediterranean|greek|lebanese|turkish|middle eastern|falafel|kebab)\b/,'Mediterranean'],
      [/\b(fish and chips|fish & chips|fish n chips)\b/,'Fish & Chips'],
      [/\b(burger|burgers)\b/,'Burgers'],
      [/\b(fried chicken|chicken)\b/,'Fried Chicken'],
      [/\b(fast food|food court|hot dog|sandwich|takeaway)\b/,'Other Fast Food'],
      [/\b(ice cream|gelato|dessert|frozen yoghurt|chocolate|donut|doughnut|confectionery)\b/,'Dessert'],
      [/\b(steak|grill|bbq|barbecue|american)\b/,'Grill & BBQ'],
      [/\b(seafood|fish)\b/,'Seafood'],
      [/\b(mexican|burrito|taco|tacos|latin)\b/,'Mexican & Latin'],
      [/\b(restaurant)\b/,'Restaurant'],
      [/\b(vegan|vegetarian|salad|healthy|poke)\b/,'Healthy & Veg'],
    ];
    for(const [re,label] of map){ if(re.test(s)) return label; }
    // Unknown-but-present tag: show it prettified rather than dumping in Other.
    return titleCase(s);
}
// Proper formatting for known second-layer terms; everything else is title-cased.
const SUB_FIX = {
  'bubble tea':'Bubble Tea','fish and chips':'Fish & Chips','fish n chips':'Fish & Chips',
  'ice cream':'Ice Cream','hot dog':'Hot Dog','fried chicken':'Fried Chicken',
  'coffee shop':'Coffee Shop','fine dining':'Fine Dining','yum cha':'Yum Cha',
  'dim sum':'Dim Sum','banh mi':'Banh Mi','bbq':'BBQ','kebab':'Kebab',
  'steak house':'Steakhouse','somali':'Somalian','somalian':'Somalian',
  'middle eastern':'Middle Eastern','south indian':'South Indian','north indian':'North Indian',
};
function prettifySub(t){
    let s = (t||'').toString().toLowerCase().replace(/[_\-]+/g,' ').trim();
    if(!s) return '';
    if(SUB_FIX[s]) return SUB_FIX[s];
    return titleCase(s);
}
// The specific second-layer values for a venue (a venue can list several, e.g. "chinese;dumpling").
function subCuisineLabels(raw, category){
    let base = (raw||'').toString().toLowerCase().trim();
    if(!base) base = (category||'').toString().toLowerCase().trim();
    if(!base) return [];
    const parts = base.split(/[;,\/]+/).map(prettifySub).filter(Boolean);
    return Array.from(new Set(parts));
}
// second-layer state
const selectedSubCuisines = new Set();
const subMap = {};            // top-level bucket -> Set(second-layer labels)
let ctxMenuEl = null;
function closeCtxMenu(){ if(ctxMenuEl){ ctxMenuEl.remove(); ctxMenuEl = null; } }
function markChipSubState(bucket){
    document.querySelectorAll('.cuisine-btn').forEach(b=>{
        if(b.dataset.cuisine !== bucket) return;
        const subs = subMap[bucket] || new Set();
        let any = false; subs.forEach(s=>{ if(selectedSubCuisines.has(s)) any = true; });
        b.classList.toggle('has-sub', any);
    });
}
function openCuisineMenu(bucket, x, y){
    closeCtxMenu();
    const subs = Array.from(subMap[bucket] || []).sort();
    if(subs.length === 0) return;
    const menu = document.createElement('div');
    menu.className = 'cuisine-menu';
    menu.style.left = Math.min(x, window.innerWidth - 190) + 'px';
    menu.style.top  = y + 'px';
    const title = document.createElement('div');
    title.className = 'cm-title';
    title.textContent = bucket + ' \u2192 sub-type';
    menu.appendChild(title);
    subs.forEach(s=>{
        const row = document.createElement('div');
        row.className = 'cm-row' + (selectedSubCuisines.has(s) ? ' on' : '');
        row.textContent = s;
        row.addEventListener('click', ()=>{
            if(selectedSubCuisines.has(s)) selectedSubCuisines.delete(s);
            else selectedSubCuisines.add(s);
            row.classList.toggle('on');
            markChipSubState(bucket);
            applyFilters();
        });
        menu.appendChild(row);
    });
    document.body.appendChild(menu);
    ctxMenuEl = menu;
}
document.addEventListener('click', (e)=>{ if(ctxMenuEl && !ctxMenuEl.contains(e.target)) closeCtxMenu(); });
const reviewLayer = L.layerGroup();
const reviewMarkers = [];
const reviewHeatAll = [];
VENUES.forEach(v=>{
  if(v.lat===''||v.lon==='') return;
  const lat=+v.lat, lon=+v.lon;
  const rating = v.rating===''?null:+v.rating;
  const r = L.circleMarker([lat,lon],{radius:radius(),fillColor:ratingColor(rating),color:'#333',
      weight:0.5,fillOpacity:.95}).bindPopup(popup(v), POPUP_OPTS);
  r._rating = rating;
  const rc = (v.review_count===''||v.review_count==null)?null:(isFinite(+v.review_count)?+v.review_count:null);
  r._review_count = rc;
  r._cuisine = normaliseCuisine(v.cuisine, v.category);
  r._subs = subCuisineLabels(v.cuisine, v.category);
  if(!subMap[r._cuisine]) subMap[r._cuisine] = new Set();
  r._subs.forEach(s=>subMap[r._cuisine].add(s));
  r._venue = v;
  reviewMarkers.push(r);
  if(rating!=null) reviewHeatAll.push([lat,lon,(rating/5)]);
});
const reviewHeatL = L.heatLayer(reviewHeatAll,{radius:28,blur:20,maxZoom:16});
reviewMarkers.forEach(m=>m.addTo(reviewLayer));
reviewLayer.addTo(map);

const filterCtrl = L.control({position:'topleft'});
filterCtrl.onAdd = function(){
    const d = L.DomUtil.create('div','rating-card');
    L.DomEvent.disableClickPropagation(d);
    L.DomEvent.disableScrollPropagation(d);
    const BUCKETS = [['b5','4.9+'],['b4','4.6\u20134.8'],['b3','4.3\u20134.5'],
                     ['b2','4.0\u20134.2'],['b1','3.5\u20133.9'],['b0','<3.5']];
    function rrow(id,label){
      return `<div class="rate-row sel ratingBucket" data-id="${id}">
                <span class="lbl-txt">${label}</span><span class="chk"></span></div>`;
    }
    d.innerHTML = `
      <div class="rc-head"><span class="rc-title">Filter by Rating</span>
        <button type="button" class="rc-clear" id="rcClear">Clear All</button></div>
      ${BUCKETS.map(b=>rrow(b[0],b[1])).join('')}
      <div class="rc-sub">
        <label><input type="checkbox" id="showNoRating"> Show venues with no rating</label>
        <label>Min reviews <input id="minReviews" type="number" min="0" value="200"></label>
      </div>
      <button type="button" class="rc-apply" id="rcApply">Apply \u2713</button>`;
    return d;
};
filterCtrl.addTo(map);

function buildCuisineSelector(){
    const counts = {};
    VENUES.forEach(v=>{ const c = normaliseCuisine(v.cuisine, v.category); counts[c]=(counts[c]||0)+1; });
    const order = ['Cafe','Bakery','Bar','Pub','Restaurant','Italian','Chinese','Japanese','Korean','Thai',
      'Vietnamese','Indian','Other Asian','Mediterranean','Burgers','Fried Chicken','Fish & Chips',
      'Other Fast Food','Grill & BBQ','Seafood','Mexican & Latin','Bubble Tea','Drinks & Juice',
      'Dessert','Healthy & Veg'];
    // known cuisines first (in order), then any leftover labels alphabetically, 'Other' last
    const known = order.filter(c=>counts[c]);
    const extras = Object.keys(counts).filter(c=>!order.includes(c) && c!=='Other').sort();
    const arr = known.concat(extras);
    if(counts['Other']) arr.push('Other');
    const bar = document.getElementById('cuisineBar');
    if(!bar) return;
    bar.innerHTML='';
    arr.forEach(c=>{
        const b = document.createElement('button');
        b.className = 'cuisine-btn';
        b.textContent = `${c} (${counts[c]})`;
        b.dataset.cuisine = c;
        b.type = 'button';
        b.title = 'Left-click to filter \u2022 Right-click for sub-types';
        b.addEventListener('click', ()=>{ b.classList.toggle('active'); applyFilters(); });
        b.addEventListener('contextmenu', (e)=>{ e.preventDefault(); e.stopPropagation();
            openCuisineMenu(c, e.clientX, e.clientY); });
        bar.appendChild(b);
    });
}
function bucketForRating(r){
    if(r==null) return 'none';
    if(r<3.5) return 'b0';
    if(r<4.0) return 'b1';
    if(r<4.3) return 'b2';
    if(r<4.6) return 'b3';
    if(r<4.9) return 'b4';
    return 'b5';
}
function applyFilters(){
    const checked = new Set();
    document.querySelectorAll('.rate-row.ratingBucket.sel').forEach(el=>checked.add(el.dataset.id));
    const showNone = !!document.getElementById('showNoRating') && document.getElementById('showNoRating').checked;
    const minReviewsEl = document.getElementById('minReviews');
    const minReviews = minReviewsEl ? (parseInt(minReviewsEl.value,10)||0) : 0;
    reviewLayer.clearLayers();
    const newHeat = [];
    window.__visible = [];
    const selectedCuisines = new Set();
    document.querySelectorAll('.cuisine-btn.active').forEach(b=>selectedCuisines.add(b.dataset.cuisine));
    reviewMarkers.forEach(m=>{
        const b = m._rating==null ? 'none' : bucketForRating(m._rating);
        const rc = m._review_count==null ? 0 : m._review_count;
        const rcOk = (m._review_count==null) ? showNone : (rc>=minReviews);
        const c = m._cuisine || '';
        const anyTop = selectedCuisines.size > 0;
        const anySub = selectedSubCuisines.size > 0;
        let cuisineOk;
        if(!anyTop && !anySub){
            cuisineOk = true;
        } else {
            const topMatch = anyTop && selectedCuisines.has(c);
            const subMatch = anySub && (m._subs || []).some(s=>selectedSubCuisines.has(s));
            cuisineOk = topMatch || subMatch;
        }
        if(((b==='none' && showNone) || (b!=='none' && checked.has(b))) && rcOk && cuisineOk){
            m.addTo(reviewLayer);
            window.__visible.push(m._venue);
            if(m._rating!=null) newHeat.push([m.getLatLng().lat, m.getLatLng().lng, (m._rating/5)]);
        }
    });
    try{ reviewHeatL.setLatLngs(newHeat); }catch(e){}
}
function rankedVenues(){
    const rows = (window.__visible || []).slice();
    rows.sort((a,b)=>{
        const ra = a.rating===''||a.rating==null ? -1 : +a.rating;
        const rb = b.rating===''||b.rating==null ? -1 : +b.rating;
        if(rb!==ra) return rb-ra;
        const ca = a.review_count===''?0:+a.review_count;
        const cb = b.review_count===''?0:+b.review_count;
        return cb-ca;
    });
    return rows;
}
function buildExportTable(){
    const rows = rankedVenues();
    let h = `<table class="exp"><thead><tr><th>#</th><th>Venue</th><th>Suburb</th>`
          + `<th class="num">Rating</th><th class="num">Reviews</th></tr></thead><tbody>`;
    rows.forEach((v,i)=>{
        const rating = (v.rating===''||v.rating==null)?'-':(+v.rating).toFixed(1);
        const rc = (v.review_count===''||v.review_count==null)?'-':Number(v.review_count).toLocaleString();
        const sub = v.suburb || '-';
        h += `<tr><td class="num">${i+1}</td><td>${v.name}</td><td>${sub}</td>`
           + `<td class="num">${rating}</td><td class="num">${rc}</td></tr>`;
    });
    h += `</tbody></table>`;
    document.getElementById('epBody').innerHTML = h;
    document.getElementById('epCount').textContent = `${rows.length} venues`;
}
function downloadPNG(){
    const node = document.querySelector('#epBody table.exp');
    if(!node){ return; }
    if(typeof html2canvas === 'undefined'){
        alert('PNG export needs an internet connection (html2canvas failed to load).');
        return;
    }
    html2canvas(node, {backgroundColor:'#ffffff', scale:2}).then(canvas=>{
        const a = document.createElement('a');
        a.href = canvas.toDataURL('image/png');
        a.download = 'venues_by_rating.png';
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
    });
}
document.getElementById('exportBtn').addEventListener('click', ()=>{
    buildExportTable();
    document.getElementById('exportPanel').classList.add('open');
});
document.getElementById('epClose').addEventListener('click', ()=>
    document.getElementById('exportPanel').classList.remove('open'));
document.getElementById('epDownload').addEventListener('click', downloadPNG);
document.querySelectorAll('.rate-row.ratingBucket').forEach(row=>
    row.addEventListener('click', ()=>{ row.classList.toggle('sel'); applyFilters(); }) );
const rcClear = document.getElementById('rcClear');
if(rcClear) rcClear.addEventListener('click', ()=>{
    document.querySelectorAll('.rate-row.ratingBucket').forEach(r=>r.classList.remove('sel'));
    applyFilters();
});
const rcApply = document.getElementById('rcApply');
if(rcApply) rcApply.addEventListener('click', applyFilters);
const showNoRatingEl = document.getElementById('showNoRating');
if(showNoRatingEl) showNoRatingEl.addEventListener('change', applyFilters);
const minReviewsInput = document.getElementById('minReviews');
if(minReviewsInput) minReviewsInput.addEventListener('input', applyFilters);
buildCuisineSelector();
applyFilters();
</script></body></html>
"""

HTML_TEMPLATE = HTML_HEAD + HTML_BODY


def cmd_map(args):
    infile = getattr(args, "infile", None)
    if not infile:
        infile = "venues_reviews.csv" if os.path.exists("venues_reviews.csv") else None
    if not infile:
        sys.exit("map: no input CSV. Use --in venues_reviews.csv")

    rows = read_csv(infile)

    venues, lats, lons = [], [], []
    for row in rows:
        v = {k: (row.get(k, "") or "") for k in JS_FIELDS}
        try:
            la = float(v["lat"]); lo = float(v["lon"])
        except (TypeError, ValueError):
            continue
        lats.append(la); lons.append(lo)
        venues.append(v)

    if not venues:
        sys.exit(f"map: no venues with valid lat/lon in {infile}")

    center = [sum(lats) / len(lats), sum(lons) / len(lons)]

    data_js = (
        "const VENUES = " + json.dumps(venues, ensure_ascii=False) + ";\n"
        "const CENTER = " + json.dumps(center) + ";"
    )
    html = HTML_TEMPLATE.replace("/*__DATA__*/", data_js)

    out = getattr(args, "out", None) or "melbourne_fnb_map.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[map] wrote {len(venues)} venues -> {out}")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Map + score Melbourne F&B venues (OSM + reviews + foot traffic).")
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("extract", help="Pull F&B venues from OpenStreetMap (free).")
    pe.add_argument("--bbox", help='"S,W,N,E" e.g. "-37.86,144.90,-37.76,145.02"')
    pe.add_argument("--out", default="venues_base.csv")
    pe.set_defaults(func=cmd_extract)

    pr = sub.add_parser("reviews", help="Enrich with Google Places reviews.")
    pr.add_argument("--in", dest="infile", required=True)
    pr.add_argument("--key", required=True, help="Google Places API key")
    pr.add_argument("--out", default="venues_reviews.csv")
    pr.add_argument("--sleep", type=float, default=0.1)
    pr.add_argument("--limit", type=int, default=0)
    pr.set_defaults(func=cmd_reviews)

    pf = sub.add_parser("foottraffic", help="Merge licensed foot-traffic vendor CSV.")
    pf.add_argument("--in", dest="infile", required=True)
    pf.add_argument("--vendor", required=True)
    pf.add_argument("--match-radius", type=float, default=60.0)
    pf.add_argument("--out", default="venues_master.csv")
    pf.set_defaults(func=cmd_foottraffic)

    pm = sub.add_parser("map", help="Build the interactive HTML map from a CSV.")
    pm.add_argument("--in", dest="infile", help="input CSV (default venues_reviews.csv)")
    pm.add_argument("--out", default="melbourne_fnb_map.html")
    pm.set_defaults(func=cmd_map)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
