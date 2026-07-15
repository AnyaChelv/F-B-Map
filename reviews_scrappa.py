#!/usr/bin/env python3
"""
reviews_scrappa.py - Ratings enrichment via Scrappa. PARALLEL + INCREMENTAL SAVE
+ credit-exhaustion handling + resume.
"""
import argparse, csv, math, sys, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

SEARCH_URL  = "https://scrappa.co/api/maps/simple-search"
REVIEWS_URL = "https://scrappa.co/api/maps/reviews"

MATCH_RADIUS_M = 250
NAME_SIM = 0.40
W_VOLUME, W_MOMENTUM, W_QUALITY = 0.50, 0.30, 0.20

FIELDS = ["osm_id","osm_type","name","category","cuisine","lat","lon","address",
  "suburb","website","opening_hours","rating","review_count",
  "review_velocity_per_week","popularity_review","foot_monthly","foot_daily_avg",
  "dwell_min","foot_source","popularity_foot"]

_local = threading.local()
def session():
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    return _local.s

_print_lock = threading.Lock()
def log(msg):
    with _print_lock:
        print(msg, flush=True)

_stop = threading.Event()
_403_count = 0
_403_lock = threading.Lock()

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

def safe_write(path, rows, fn):
    try:
        with open(path,"w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=fn); w.writeheader()
            for r in rows: w.writerow({k:r.get(k,"") for k in fn})
        return path
    except PermissionError:
        alt=path.replace(".csv","")+f"_{datetime.now():%Y%m%d_%H%M%S}.csv"
        with open(alt,"w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=fn); w.writeheader()
            for r in rows: w.writerow({k:r.get(k,"") for k in fn})
        log(f"  WARN '{path}' was locked (open in Excel?). Saved to '{alt}' instead.")
        return alt

def scrappa_get(url,params,key,max_retries=5):
    headers={"x-api-key":key,"accept":"application/json"}
    for attempt in range(1,max_retries+1):
        r=session().get(url,headers=headers,params=params,timeout=40)
        if r.status_code in (429,503):
            wait=int(r.headers.get("Retry-After", 2**attempt))
            time.sleep(wait); continue
        r.raise_for_status()
        return r.json()
    return {}

def extract_from_search(item):
    bid   = item.get("business_id") or item.get("google_id") or item.get("place_id")
    name  = item.get("name") or item.get("title") or ""
    coords= item.get("coordinates") or item.get("gps") or {}
    lat   = tf(item.get("latitude") or item.get("lat") or coords.get("lat"))
    lon   = tf(item.get("longitude") or item.get("lng") or item.get("lon") or coords.get("lng"))
    rating= tf(item.get("rating") or item.get("stars"))
    rcount= item.get("review_count") or item.get("reviews") or item.get("reviews_count") or item.get("user_ratings_total")
    try: rcount=int(rcount)
    except (TypeError,ValueError): rcount=None
    return bid,name,lat,lon,rating,rcount

def search_results(payload):
    if isinstance(payload,list): return payload
    for k in ("results","items","data","local_results","places"):
        v=payload.get(k)
        if isinstance(v,list): return v
    return []

def velocity_from_reviews(bid,key):
    try:
        data=scrappa_get(REVIEWS_URL,{"business_id":bid,"sort":2,"limit":20},key)
    except requests.RequestException:
        return None
    ts=[]
    for it in data.get("items",[]) or []:
        t=it.get("timestamp")
        if isinstance(t,(int,float)):
            ts.append(datetime.fromtimestamp(t/1000,tz=timezone.utc))
    if len(ts)<2: return None
    span=(max(ts)-min(ts)).days or 1
    return round(len(ts)/span*7.0,3)

def enrich_one(v, key, want_velocity, sleep):
    global _403_count
    if _stop.is_set():
        return v, False
    lat,lon=tf(v["lat"]),tf(v["lon"])
    q=f'{v["name"]} {v.get("suburb","")} Melbourne'.strip()
    try:
        payload=scrappa_get(SEARCH_URL,{"query":q,"ll":f"{lat},{lon}"},key)
    except requests.HTTPError as ex:
        code=getattr(ex.response,"status_code",None)
        if code==403:
            with _403_lock:
                _403_count+=1
                if _403_count>=5 and not _stop.is_set():
                    _stop.set()
                    log("  STOP: Repeated 403 (out of credits/forbidden). Saving progress...")
        else:
            log(f"  {v['name']}: {ex}")
        return v, False
    except requests.RequestException as ex:
        log(f"  {v['name']}: {ex}")
        return v, False
    best,bs=None,0.0
    for it in search_results(payload):
        bid,nm,clat,clon,rating,rcount=extract_from_search(it)
        if clat is not None and clon is not None and lat is not None and lon is not None:
            if hav(lat,lon,clat,clon)>MATCH_RADIUS_M: continue
        s=sim(v["name"],nm)
        if s>=NAME_SIM and s>bs:
            best,bs=(bid,rating,rcount),s
    ok=False
    if best:
        bid,rating,rcount=best; ok=True
        v["rating"]="" if rating is None else round(rating,1)
        v["review_count"]="" if rcount is None else rcount
        if want_velocity and bid:
            vel=velocity_from_reviews(bid,key)
            v["review_velocity_per_week"]="" if vel is None else vel
    if sleep: time.sleep(sleep)
    return v, ok

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

def already_done(v):
    return str(v.get("review_count","")).strip() != "" or str(v.get("rating","")).strip() != ""

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--in",dest="infile",required=True)
    p.add_argument("--key",required=True,help="Scrappa API key (x-api-key)")
    p.add_argument("--out",default="venues_reviews.csv")
    p.add_argument("--workers",type=int,default=10)
    p.add_argument("--sleep",type=float,default=0.0)
    p.add_argument("--limit",type=int,default=0)
    p.add_argument("--velocity",action="store_true")
    p.add_argument("--save-every",type=int,default=100,help="checkpoint interval")
    p.add_argument("--resume",action="store_true",help="skip venues already enriched in --out")
    a=p.parse_args()

    venues=read_csv(a.infile)

    if a.resume:
        try:
            prior={r["osm_id"]:r for r in read_csv(a.out)}
            carried=0
            for v in venues:
                pr=prior.get(v["osm_id"])
                if pr and already_done(pr):
                    v.update({k:pr.get(k,"") for k in FIELDS}); carried+=1
            log(f"  resume: carried over {carried} already-enriched venues.")
        except FileNotFoundError:
            log("  resume: no existing output found, starting fresh.")

    if a.limit: venues=venues[:a.limit]
    todo=[v for v in venues if not already_done(v)]
    total=len(todo); matched=sum(1 for v in venues if already_done(v)); done=0
    t0=time.time()
    log(f"  {len(venues)} venues total | {total} to fetch | {len(venues)-total} already done")

    def checkpoint():
        score(venues); safe_write(a.out, venues, FIELDS)

    try:
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs={ex.submit(enrich_one, v, a.key, a.velocity, a.sleep): v for v in todo}
            for fut in as_completed(futs):
                _, ok = fut.result()
                done+=1
                if ok: matched+=1
                if done % 25 == 0 or done==total:
                    rate=done/max(time.time()-t0,0.1)
                    log(f"  {done}/{total} done | {matched} matched | {rate:.1f}/s")
                if done % a.save_every == 0:
                    checkpoint()
                    log(f"    checkpoint saved ({done} processed)")
    except KeyboardInterrupt:
        log("  Ctrl-C caught - saving progress before exit...")

    score(venues)
    out=safe_write(a.out, venues, FIELDS)
    log(f"[reviews-scrappa] matched {matched}/{len(venues)} in {time.time()-t0:.0f}s. Saved -> {out}")

if __name__=="__main__":
    main()