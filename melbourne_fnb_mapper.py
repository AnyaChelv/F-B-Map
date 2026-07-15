#!/usr/bin/env python3
"""
melbourne_fnb_mapper.py

Map every F&B venue in Melbourne (bars, restaurants, cafes, bubble-tea shops, etc.)
and score how popular each one is using TWO independent, comparable methods:
(A) REVIEWS  -> rating x review_count x review_velocity  (from Google Places)
(B) FOOT     -> ABSOLUTE monthly foot count              (from a licensed vendor)

Pipeline (each step writes a CSV you can inspect):
  extract      Pull all F&B points-of-interest from OpenStreetMap (free, no key)
  reviews      Enrich those venues with Google Places rating/review data
  foottraffic  Merge a commercial foot-traffic vendor CSV (absolute counts)
  map          Build a standalone interactive HTML map (modern UI)

Only external dependency: requests  (pip install requests)
Everything else is Python standard library. Map = self-contained Leaflet HTML.

Author: built for Anya Chelvathurai (Crown Resorts) - competitor F&B analysis
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
    requests = None  # only needed for extract and reviews

# ---------------------------------------------------------------------------
# CONFIG - tweak these transparently
# ---------------------------------------------------------------------------

# Default bounding box: Melbourne CBD + inner suburbs (S, W, N, E).
# Widen this for Greater Melbourne, or pass --bbox "S,W,N,E".
DEFAULT_BBOX = (-37.86, 144.90, -37.76, 145.02)

# OSM amenity/shop tags that count as "F&B"
OSM_AMENITIES = ["restaurant", "bar", "pub", "cafe", "fast_food",
                 "food_court", "ice_cream", "biergarten"]
OSM_SHOPS = ["bubble_tea", "coffee", "confectionery", "pastry"]

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# How the REVIEW popularity score (0-100) is composed. Fully transparent + tunable.
W_VOLUME = 0.50    # weight on review_count (log-scaled)  -> total footfall proxy
W_MOMENTUM = 0.30  # weight on review_velocity            -> how "hot" right now
W_QUALITY = 0.20   # weight on star rating                -> quality signal

# Matching tolerances
PLACES_MATCH_RADIUS_M = 80    # max metres between OSM point and Places match
NAME_SIM_THRESHOLD = 0.55     # 0..1 fuzzy name similarity to accept a match

# Fields the front-end JS reads for each venue (also the master CSV schema).
MASTER_FIELDS = [
    "osm_id", "osm_type", "name", "category", "cuisine",
    "lat", "lon", "address", "suburb", "website", "opening_hours",
    # reviews (method A)
    "rating", "review_count", "review_velocity_per_week", "popularity_review",
    # foot traffic (method B) - ABSOLUTE
    "foot_monthly", "foot_daily_avg", "dwell_min", "foot_source", "popularity_foot",
]

# Fields injected into the HTML map.
JS_FIELDS = [
    "name", "category", "cuisine", "lat", "lon",
    "rating", "review_count", "review_velocity_per_week", "popularity_review",
    "foot_monthly", "foot_daily_avg", "dwell_min", "popularity_foot", "website",
]

# ---------------------------------------------------------------------------
# SMALL UTILITIES
# ---------------------------------------------------------------------------

def haversine_m(lat1, lon1, lat2, lon2):
    "Great-circle distance in metres."
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def name_similarity(a, b):
    "Fuzzy string similarity 0..1 using stdlib difflib (no external deps)."
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
# STEP 1 - EXTRACT venues from OpenStreetMap (Overpass API, free, no key)
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
        except Exception as ex:  # try the next mirror
            print(f"  ! {url} failed: {ex}")
            time.sleep(2)
    if data is None:
        sys.exit("All Overpass mirrors failed. Try again later or a smaller --bbox.")

    rows = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue  # skip unnamed POIs
        if el["type"] == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:  # way/relation -> use 'center'
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

    # de-dup by (name, rounded coord)
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
# STEP 2 - REVIEWS: enrich with Google Places (rating, count, velocity)
# ---------------------------------------------------------------------------

def places_text_search(name, lat, lon, key):
    "Google Places API (New) Text Search, biased to the venue's coordinate."
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
    """First-run estimate: reviews/week implied by the (<=5) recent reviews Google
    returns, using their publishTime. Rough but gives a same-day number."""
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

    venues = read_csv(args.infile)
    if args.limit:
        venues = venues[:args.limit]

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
        # TRUE tracked velocity from previous run, else same-day estimate
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

    # write output + append history snapshot
    fields = list(venues[0].keys()) if venues else MASTER_FIELDS
    for extra in ("rating", "review_count", "review_velocity_per_week"):
        if extra not in fields:
            fields.append(extra)
    write_csv(args.out, venues, fields)
    if hist_rows:
        write_csv(hist_path, hist_rows, ["key", "review_count", "ts"])

    # score after enrichment
    compute_review_popularity(venues)
    write_csv(args.out, venues, fields + (["popularity_review"] if "popularity_review" not in fields else []))

# ---------------------------------------------------------------------------
# STEP 3 - FOOT TRAFFIC: merge a licensed vendor CSV (ABSOLUTE counts)
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
# SCORING - normalise both methods to 0..100 so they are comparable
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
# STEP 4 - MAP: standalone interactive Leaflet HTML (modern UI)
#   Data is injected as JSON at the /*__DATA__*/ token, so the browser
#   ALWAYS has valid `const VENUES` and `const CENTER` (no paste errors).
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Melbourne F&B Popularity Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
<style>
  :root{
    --brand:#6C3EF4; --brand-2:#8B5CFF; --brand-soft:#F1ECFF;
    --ink:#2b2440; --muted:#8a8399; --line:#ece8f5;
    --chip-ink:#c9d1d9; --chip-active:#5eead4;
  }
  html,body,#map{height:100%;margin:0;font-family:'Segoe UI',Inter,Arial,sans-serif}
  /* Almost-grayscale basemap so coloured venue dots stand out */
  .leaflet-tile-pane{filter:grayscale(1) contrast(.9) brightness(1.05)}
  .legend{background:#fff;padding:10px 12px;border-radius:8px;line-height:1.5;
          box-shadow:0 1px 6px rgba(0,0,0,.3);font-size:12px;max-width:230px}
  .legend b{font-size:13px}
  .swatch{display:inline-block;width:12px;height:12px;border-radius:50%;
          margin-right:6px;vertical-align:middle}
  .pop b{font-size:14px}
  table.cmp{border-collapse:collapse;margin-top:4px;font-size:12px}
  table.cmp td{border:1px solid #ddd;padding:2px 6px}
  .rating-card{background:#fff;border-radius:18px;padding:16px 16px 14px;width:260px;
      box-shadow:0 12px 30px rgba(76,62,244,.14),0 2px 6px rgba(0,0,0,.06);font-size:13px;color:var(--ink)}
  .rc-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
  .rc-title{font-weight:700;font-size:15px}
  .rc-clear{background:none;border:none;color:var(--brand);font-weight:600;cursor:pointer;
      font-size:12.5px;padding:2px 4px;border-radius:6px}
  .rc-clear:hover{background:var(--brand-soft)}
  .rate-row{display:flex;align-items:center;justify-content:space-between;padding:8px 12px;margin:5px 0;
      border:1.5px solid var(--line);border-radius:12px;cursor:pointer;transition:all .15s ease;background:#fff}
  .rate-row:hover{border-color:var(--brand-2);background:var(--brand-soft)}
  .rate-row.sel{border-color:var(--brand);background:var(--brand-soft);box-shadow:0 4px 12px rgba(108,62,244,.15)}
  .rate-row .lbl-txt{font-size:12.5px;font-weight:600;color:var(--ink)}
  .rate-row .chk{width:18px;height:18px;border-radius:6px;border:2px solid #cfc8e3;
      display:inline-block;position:relative;flex:0 0 auto;background:#fff}
  .rate-row.sel .chk{border-color:var(--brand);background:var(--brand)}
  .rate-row.sel .chk::after{content:'';position:absolute;left:5px;top:1px;width:4px;height:9px;
      border:solid #fff;border-width:0 2px 2px 0;transform:rotate(45deg)}
  .rc-sub{margin-top:10px;padding-top:10px;border-top:1px solid var(--line);display:flex;flex-direction:column;gap:8px}
  .rc-sub label{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:12px}
  .rc-sub input[type=number]{width:80px;border:1.5px solid var(--line);border-radius:8px;padding:4px 8px;font-size:12px;color:var(--ink)}
  .rc-apply{margin-top:12px;width:100%;border:none;cursor:pointer;color:#fff;
      background:linear-gradient(90deg,var(--brand),var(--brand-2));padding:11px;border-radius:26px;
      font-size:14px;font-weight:600;box-shadow:0 8px 18px rgba(108,62,244,.35)}
  .rc-apply:hover{filter:brightness(1.05)}
  .top-cuisine-bar{position:fixed;left:50%;top:12px;transform:translateX(-50%);display:flex;gap:8px;
      flex-wrap:wrap;justify-content:center;z-index:1001;padding:8px 10px;max-width:82vw;
      background:rgba(24,26,33,.82);backdrop-filter:blur(8px);border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,.28)}
  .cuisine-btn{background:transparent;border:1.5px solid rgba(255,255,255,.18);border-radius:12px;
      padding:7px 14px;cursor:pointer;color:var(--chip-ink);font-size:13px;font-weight:500;
      transition:all .15s ease;white-space:nowrap}
  .cuisine-btn:hover{border-color:var(--chip-active);color:#fff}
  .cuisine-btn.active{background:var(--chip-active);border-color:var(--chip-active);color:#0b3b34;
      font-weight:600;box-shadow:0 4px 12px rgba(94,234,212,.3)}
  .cuisine-btn:focus{outline:none}
</style></head><body>
<div id="cuisineBar" class="top-cuisine-bar" aria-label="Cuisine selector"></div>
<div id="map"></div>
<script>
/*__DATA__*/
const map = L.map('map').setView(CENTER, 14);
L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
  {maxZoom:19, subdomains:'abcd', attribution:'&copy; OpenStreetMap &copy; CARTO'}).addTo(map);

function ratingColor(r){
    if(r===''||r==null) return '#9e9e9e';
    return r<3.5 ? '#FF7F00' : r<4.0 ? '#FFC04D' : r<4.3 ? '#C7E86A' :
                 r<4.6 ? '#66C2A5' : r<4.9 ? '#2CA25F' : '#006837';
}
function radius(){ return 4; }
function scoreColor(score){
    if(score===''||score==null) return '#9e9e9e';
    return score>80?'#800026':score>60?'#BD0026':score>40?'#E31A1C':
                 score>20?'#FC4E2A':score>0?'#FEB24C':'#FFEDA0';
}
function radiusScore(score){ return score===''||score==null?3:3+(score/100)*10; }
function popup(v){
  return `<div class="pop"><b>${v.name}</b><br><i>${v.category}${v.cuisine?' &middot; '+v.cuisine:''}</i>
    <table class="cmp">
      <tr><td><b>Method A - Reviews</b></td><td></td></tr>
      <tr><td>Rating</td><td>${v.rating||'-'} &#9733;</td></tr>
      <tr><td>Review count</td><td>${v.review_count||'-'}</td></tr>
      <tr><td>Velocity (/wk)</td><td>${v.review_velocity_per_week||'-'}</td></tr>
      <tr><td>Review score</td><td>${v.popularity_review||'-'}/100</td></tr>
      <tr><td><b>Method B - Foot traffic</b></td><td></td></tr>
      <tr><td>Monthly visits (ABS)</td><td>${v.foot_monthly?Number(v.foot_monthly).toLocaleString():'-'}</td></tr>
      <tr><td>Daily avg</td><td>${v.foot_daily_avg||'-'}</td></tr>
      <tr><td>Dwell (min)</td><td>${v.dwell_min||'-'}</td></tr>
      <tr><td>Foot score</td><td>${v.popularity_foot||'-'}/100</td></tr>
    </table>
    ${v.website?`<a href="${v.website}" target="_blank">website</a>`:''}</div>`;
}
function normaliseCuisine(raw){
    let s = (raw||'').toString().toLowerCase().trim().replace(/[_\-]+/g,' ');
    if(!s) return '';
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
      [/\b(mediterranean|greek|lebanese|turkish|middle eastern|falafel|kebab)\b/,'Mediterranean'],
      [/\b(fish and chips|fish & chips|fish n chips)\b/,'Fish & Chips'],
      [/\b(burger|burgers)\b/,'Burgers'],
      [/\b(fried chicken|chicken)\b/,'Fried Chicken'],
      [/\b(fast food|hot dog|sandwich|takeaway)\b/,'Other Fast Food'],
      [/\b(steak|grill|bbq|barbecue|american)\b/,'Grill & BBQ'],
      [/\b(seafood|fish)\b/,'Seafood'],
      [/\b(mexican|burrito|taco|tacos|latin)\b/,'Mexican & Latin'],
      [/\b(dessert|ice cream|gelato|frozen yoghurt|chocolate|donut|doughnut|confectionery)\b/,'Dessert'],
      [/\b(vegan|vegetarian|salad|healthy|poke)\b/,'Healthy & Veg'],
    ];
    for(const [re,label] of map){ if(re.test(s)) return label; }
    return 'Other';
}
const reviewLayer = L.layerGroup();
const reviewMarkers = [];
const reviewHeatAll = [];
VENUES.forEach(v=>{
  if(v.lat===''||v.lon==='') return;
  const lat=+v.lat, lon=+v.lon;
  const rating = v.rating===''?null:+v.rating;
  const r = L.circleMarker([lat,lon],{radius:radius(),fillColor:ratingColor(rating),color:'#333',
      weight:0.5,fillOpacity:.95}).bindPopup(popup(v));
  r._rating = rating;
  const rc = (v.review_count===''||v.review_count==null)?null:(isFinite(+v.review_count)?+v.review_count:null);
  r._review_count = rc;
  r._cuisine = normaliseCuisine(v.cuisine);
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
    VENUES.forEach(v=>{ const c = normaliseCuisine(v.cuisine); if(c){ counts[c]=(counts[c]||0)+1; } });
    const order = ['Cafe','Bakery','Bar','Pub','Italian','Chinese','Japanese','Korean','Thai',
      'Vietnamese','Indian','Other Asian','Mediterranean','Burgers','Fried Chicken','Fish & Chips',
      'Other Fast Food','Grill & BBQ','Seafood','Mexican & Latin','Bubble Tea','Drinks & Juice',
      'Dessert','Healthy & Veg','Other'];
    const arr = order.filter(c=>counts[c]);
    const bar = document.getElementById('cuisineBar');
    if(!bar) return;
    bar.innerHTML='';
    arr.forEach(c=>{
        const b = document.createElement('button');
        b.className = 'cuisine-btn';
        b.textContent = `${c} (${counts[c]})`;
        b.dataset.cuisine = c;
        b.type = 'button';
        b.addEventListener('click', ()=>{ b.classList.toggle('active'); applyFilters(); });
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
    const selectedCuisines = new Set();
    document.querySelectorAll('.cuisine-btn.active').forEach(b=>selectedCuisines.add(b.dataset.cuisine));
    reviewMarkers.forEach(m=>{
        const b = m._rating==null ? 'none' : bucketForRating(m._rating);
        const rc = m._review_count==null ? 0 : m._review_count;
        const rcOk = (m._review_count==null) ? showNone : (rc>=minReviews);
        const c = m._cuisine || '';
        const cuisineOk = (selectedCuisines.size===0) ? true : selectedCuisines.has(c);
        if(((b==='none' && showNone) || (b!=='none' && checked.has(b))) && rcOk && cuisineOk){
            m.addTo(reviewLayer);
            if(m._rating!=null) newHeat.push([m.getLatLng().lat, m.getLatLng().lng, (m._rating/5)]);
        }
    });
    try{ reviewHeatL.setLatLngs(newHeat); }catch(e){}
}
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


def cmd_map(args):
    """Read the CSV and write a standalone interactive HTML map.

    Data is injected as JSON at the /*__DATA__*/ token, so `VENUES` and
    `CENTER` are ALWAYS valid in the browser (no manual paste required).
    """
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
