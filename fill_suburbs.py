#!/usr/bin/env python3
"""
fill_suburbs.py - fill the `suburb` column using nearest-known-neighbour.

Venues that already have a suburb become "anchors"; every blank-suburb venue
is assigned the suburb of its nearest anchor (great-circle distance).
Adds suburb_source ('osm'/'nearest') and suburb_conf_km (distance to anchor).

Run:  python fill_suburbs.py
Needs: pip install scikit-learn numpy   (falls back to pure-python if missing)
"""
import csv, math, sys

IN  = "venues_reviews.csv"
OUT = "venues_reviews_suburbs.csv"

rows = list(csv.DictReader(open(IN, encoding="utf-8")))
if not rows:
    sys.exit("no rows in " + IN)
fields = list(rows[0].keys())
for extra in ("suburb_source", "suburb_conf_km"):
    if extra not in fields:
        fields.append(extra)

def has(r, k):
    return (r.get(k) or "").strip()

anchors, targets, no_coord = [], [], 0
for r in rows:
    if not (has(r, "lat") and has(r, "lon")):
        no_coord += 1
        continue
    (anchors if has(r, "suburb") else targets).append(r)

print(f"rows={len(rows)} anchors={len(anchors)} targets={len(targets)} no_coord={no_coord}")
if not anchors:
    sys.exit("No venues with a suburb to use as anchors.")

for r in anchors:
    r["suburb_source"] = "osm"
    r["suburb_conf_km"] = 0.0

EARTH_KM = 6371.0088

# --- Fast path: scikit-learn BallTree (haversine). Fallback: pure python. ---
try:
    import numpy as np
    from sklearn.neighbors import BallTree
    A = np.radians([[float(r["lat"]), float(r["lon"])] for r in anchors])
    tree = BallTree(A, metric="haversine")
    if targets:
        Q = np.radians([[float(r["lat"]), float(r["lon"])] for r in targets])
        dist, idx = tree.query(Q, k=1)
        for r, d, i in zip(targets, dist[:, 0], idx[:, 0]):
            r["suburb"] = anchors[int(i)]["suburb"]
            r["suburb_source"] = "nearest"
            r["suburb_conf_km"] = round(float(d) * EARTH_KM, 3)
    print("used scikit-learn BallTree")
except ImportError:
    print("scikit-learn not found - using slower pure-python method")
    import math as m
    alat = [math.radians(float(r["lat"])) for r in anchors]
    alon = [math.radians(float(r["lon"])) for r in anchors]
    def nearest(la, lo):
        la = math.radians(la); lo = math.radians(lo)
        best_i, best = 0, 9e9
        for i in range(len(anchors)):
            dla = alat[i] - la; dlo = alon[i] - lo
            a = math.sin(dla/2)**2 + math.cos(la)*math.cos(alat[i])*math.sin(dlo/2)**2
            d = 2*math.asin(math.sqrt(a))
            if d < best:
                best, best_i = d, i
        return best_i, best*EARTH_KM
    for n, r in enumerate(targets, 1):
        i, dkm = nearest(float(r["lat"]), float(r["lon"]))
        r["suburb"] = anchors[i]["suburb"]
        r["suburb_source"] = "nearest"
        r["suburb_conf_km"] = round(dkm, 3)
        if n % 500 == 0:
            print(f"  ...{n}/{len(targets)}")

with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in fields})

filled = sum(1 for r in rows if has(r, "suburb"))
print(f"suburb now filled: {filled}/{len(rows)} ({100*filled//len(rows)}%)")
print(f"wrote -> {OUT}")