#!/usr/bin/env python3
"""
reviews_fsq.py - FREE-ish ratings enrichment via Foursquare Places API (NEW endpoint).
Usage:
  python reviews_fsq.py --in venues_real.csv --key YOUR_FSQ_KEY --out venues_reviews.csv --limit 50
Uses the NEW Foursquare Places API (legacy v3 deprecated 15 May 2026):
  endpoint:  https://places-api.foursquare.com/places/search
  auth:      Authorization: Bearer <Service API Key>
  version:   X-Places-Api-Version: 2025-06-17
"""
import argparse, csv, math, sys, time
from datetime import datetime, timezone
try:
    import requests
except ImportError:
    sys.exit("pip install requests")

# --- Foursquare NEW Places API config ---
FSQ_URL = "https://places-api.foursquare.com/places/search"
FSQ_API_VERSION = "2025-06-17"

MATCH_RADIUS_M = 120
NAME_SIM = 0.50
W_VOLUME, W_MOMENTUM, W_QUALITY = 0.50, 0.30, 0.20

FIELDS = ["osm_id","osm_type","name","category","cuisine","lat","lon","address",
  "suburb","website","opening_hours","rating","review_count",
  "review_velocity_per_week","popularity_review","foot_monthly","foot_daily_avg",
  "dwell_min","foot_source","popularity_foot"]

def sim(a,b):
    from difflib import SequenceMatcher
    if not a or not b: return 0.0
    return SequenceMatcher(None,a.lower().strip(),b.lower().strip()).ratio()

def hav(a,b,c,d):
    R=6371000.0; p1,p2=math.radians(a),math.radians(c)
    dp=math.radians(c-a); dl=math.radians(d-b)
    x=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(x))

def tf(x,d=None):
    try: return float(x)
    except (TypeError,ValueError): return d

def read_csv(p):
    with open(p,newline="",encoding="utf-8") as f: return list(csv.DictReader(f))

def write_csv(p,rows,fn):
    with open(p,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fn); w.writeheader()
        for r in rows: w.writerow({k:r.get(k,"") for k in fn})
    print(f"  -> wrote {len(rows)} rows to {p}")

def fsq_search(name,lat,lon,key,max_retries=5):
    headers={"Authorization":f"Bearer {key}",
             "X-Places-Api-Version":FSQ_API_VERSION,
             "accept":"application/json"}
    params={"query":name,"ll":f"{lat},{lon}","radius":250,
            "fields":"fsq_place_id,name,latitude,longitude,rating,stats,popularity,price",
            "limit":5}
    for attempt in range(1,max_retries+1):
        r=requests.get(FSQ_URL,headers=headers,params=params,timeout=30)
        if r.status_code==429:
            if attempt==1:  # show the diagnostic once
                print("      429 body:", r.text[:200])
                print("      X-RateLimit-Limit:", r.headers.get("X-RateLimit-Limit"),
                      "| Remaining:", r.headers.get("X-RateLimit-Remaining"))
            wait=int(r.headers.get("Retry-After", 2**attempt))
            print(f"      rate-limited (429); waiting {wait}s (attempt {attempt}/{max_retries})")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json().get("results",[])
    print("      giving up after repeated 429s")
    return []

def get_coords(c):
    """New API returns flat latitude/longitude; fall back to geocodes.main if present."""
    lat=c.get("latitude"); lon=c.get("longitude")
    if lat is None or lon is None:
        g=(c.get("geocodes") or {}).get("main") or {}
        lat=g.get("latitude"); lon=g.get("longitude")
    return tf(lat), tf(lon)

def minmax(v):
    v=[x for x in v if x is not None]
    if not v: return (0.0,1.0)
    lo,hi=min(v),max(v); return (lo, hi if hi>lo else lo+1.0)

def score(venues):
    vols=[math.log1p(tf(x.get("review_count"),0) or 0) for x in venues]
    moms=[tf(x.get("review_velocity_per_week")) for x in venues]
    vlo,vhi=minmax(vols); mlo,mhi=minmax(moms)
    for v,vol in zip(venues,vols):
        if tf(v.get("review_count")) is None: v["popularity_review"]=""; continue
        nv=(vol-vlo)/(vhi-vlo)
        m=tf(v.get("review_velocity_per_week"))
        nm=0.0 if m is None else (m-mlo)/(mhi-mlo)
        q=(tf(v.get("rating"),0) or 0)/5.0
        v["popularity_review"]=round(100*(W_VOLUME*nv+W_MOMENTUM*nm+W_QUALITY*q),1)

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--in",dest="infile",required=True)
    p.add_argument("--key",required=True,help="Foursquare Service API key")
    p.add_argument("--out",default="venues_reviews.csv")
    p.add_argument("--history",default="review_history_fsq.csv")
    p.add_argument("--sleep",type=float,default=1.0)
    p.add_argument("--limit",type=int,default=0,help="test on first N venues (0=all)")
    a=p.parse_args()

    venues=read_csv(a.infile)
    if a.limit: venues=venues[:a.limit]

    hist={}
    try:
        for h in read_csv(a.history):
            hist.setdefault(h["osm_id"],[]).append((h["timestamp"],int(float(h["review_count"]))))
    except FileNotFoundError: pass

    now=datetime.now(timezone.utc).isoformat(); newhist=[]; matched=0
    for i,v in enumerate(venues,1):
        lat,lon=tf(v["lat"]),tf(v["lon"])
        try: cands=fsq_search(v["name"],lat,lon,a.key)
        except requests.RequestException as ex:
            print(f"  [{i}/{len(venues)}] {v['name']}: API error {ex}"); cands=[]
        best,bs=None,0.0
        for c in cands:
            clat,clon=get_coords(c)
            if clat is None or clon is None: continue
            d=hav(lat,lon,clat,clon)
            if d>MATCH_RADIUS_M: continue
            s=sim(v["name"],c.get("name",""))
            if s>=NAME_SIM and s>bs: best,bs=c,s
        if best:
            matched+=1
            fsq_rating=best.get("rating")  # 0-10
            v["rating"]="" if fsq_rating is None else round(fsq_rating/2,1)  # ->0-5
            rc=(best.get("stats") or {}).get("total_ratings") or 0
            v["review_count"]=rc
            vel=None; prev=sorted(hist.get(v["osm_id"],[]))
            if prev:
                pts,pc=prev[-1]
                try:
                    days=max((datetime.fromisoformat(now)-datetime.fromisoformat(pts)).days,1)
                    vel=round((rc-pc)/days*7.0,3)
                except ValueError: vel=None
            v["review_velocity_per_week"]="" if vel is None else vel
            newhist.append({"osm_id":v["osm_id"],"timestamp":now,"review_count":rc})
        print(f"  [{i}/{len(venues)}] {v['name']}: {'matched' if best else 'no match'}")
        time.sleep(a.sleep)

    score(venues)
    write_csv(a.out,venues,FIELDS)
    allh=[]
    try: allh=read_csv(a.history)
    except FileNotFoundError: pass
    allh.extend(newhist)
    write_csv(a.history,allh,["osm_id","timestamp","review_count"])
    print(f"[reviews-fsq] matched {matched}/{len(venues)}. Re-run in a few days for tracked velocity.")

if __name__=="__main__":
    main()