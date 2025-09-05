import os, math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ---------- Config ----------
NWS_STATION = os.getenv("NWS_STATION", "KMPR")  # McPherson, KS
# NWS requires a User-Agent string with contact info per their policy.
# Example: "McPherson-Weather (mikep@mcphersonpower.com)"
NWS_USER_AGENT = os.getenv("NWS_USER_AGENT", "McPherson-Weather (contact@example.com)")
LOCAL_TZ = ZoneInfo("America/Chicago")
BASE_URL = "https://api.weather.gov"

HOURS_TO_KEEP = 48
POLL_MINUTES = 5

# In-memory ring buffer of observations (oldest -> newest)
HISTORY = []  # each: {"ts", "temp_c", "dewpoint_c", "rh", "pressure_pa"}

app = FastAPI(title="McPherson Weather (KMPR)")
app.mount("/", StaticFiles(directory="static", html=True), name="static")

# ---------- Utilities ----------
def c_to_f(c):
    return None if c is None else (c * 9/5 + 32)

def pa_to_inhg(pa):
    return None if pa is None else (pa / 3386.389)

def clamp(val, lo, hi):
    return max(lo, min(hi, val))

def rh_from_t_and_td(temp_c, dewpoint_c):
    """Compute RH% from T (°C) and Td (°C) via Magnus."""
    if temp_c is None or dewpoint_c is None:
        return None
    a, b = 17.625, 243.04
    es = math.exp(a * temp_c / (b + temp_c))
    e  = math.exp(a * dewpoint_c / (b + dewpoint_c))
    return clamp(100.0 * (e / es), 0.0, 100.0)

def wetbulb_stull_c(temp_c, rh_percent):
    """
    Stull (2011) wet-bulb approximation (T in °C, RH in %).
    Valid roughly -20..50°C and 5..99% RH.
    """
    if temp_c is None or rh_percent is None:
        return None
    RH = clamp(rh_percent, 5.0, 99.0)
    T = temp_c
    Tw = (T * math.atan(0.151977 * (RH + 8.313659)**0.5)
          + math.atan(T + RH)
          - math.atan(RH - 1.676331)
          + 0.00391838 * RH**1.5 * math.atan(0.023101 * RH)
          - 4.686035)
    return Tw

def parse_obs_feature(feature):
    """Map an NWS observation feature -> normalized dict."""
    props = feature.get("properties", {})
    ts_iso = props.get("timestamp")  # ISO8601 Z
    temp_c = (props.get("temperature") or {}).get("value")
    dew_c  = (props.get("dewpoint") or {}).get("value")
    rh     = (props.get("relativeHumidity") or {}).get("value")
    # Prefer barometricPressure, else seaLevelPressure (Pa)
    p_pa   = (props.get("barometricPressure") or {}).get("value")
    if p_pa is None:
        p_pa = (props.get("seaLevelPressure") or {}).get("value")

    # Compute RH if missing but dewpoint present
    if rh is None and temp_c is not None and dew_c is not None:
        rh = rh_from_t_and_td(temp_c, dew_c)

    return {"ts": ts_iso, "temp_c": temp_c, "dewpoint_c": dew_c, "rh": rh, "pressure_pa": p_pa}

def prune_history():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_TO_KEEP)
    keep = []
    for row in HISTORY:
        try:
            ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        except Exception:
            continue
        if ts >= cutoff:
            keep.append(row)
    HISTORY[:] = keep

def latest_ts():
    return HISTORY[-1]["ts"] if HISTORY else None

def _to_rfc3339_no_us(dt_utc):
    """Return UTC RFC3339 string like 2025-09-05T14:08:00Z (no microseconds)."""
    dt_utc = dt_utc.astimezone(timezone.utc).replace(microsecond=0)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

# ---------- NWS fetchers (with pagination + safe limit) ----------
async def fetch_observations(start_iso=None, end_iso=None, limit=1000):
    """
    Fetch observations from NWS with pagination.
    - Uses limit=1000 (safe).
    - Accepts RFC3339 timestamps (no microseconds).
    - Follows 'next' links until exhausted.
    """
    headers = {"User-Agent": NWS_USER_AGENT, "Accept": "application/geo+json"}
    base = f"{BASE_URL}/stations/{NWS_STATION}/observations"
    params = {}
    if start_iso: params["start"] = start_iso
    if end_iso:   params["end"] = end_iso
    if limit:     params["limit"] = str(limit)

    results = []
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        url = base
        while True:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            feats = data.get("features", []) or []
            for f in feats:
                results.append(parse_obs_feature(f))

            # pagination: follow "next" link if present
            next_url = None
            for link in data.get("links", []) or []:
                if link.get("rel") == "next" and link.get("href"):
                    next_url = link["href"]
                    break
            if not next_url:
                break
            url, params = next_url, None  # next_url already includes params

    results.sort(key=lambda x: x.get("ts") or "")
    return results

async def backfill_48h():
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(hours=HOURS_TO_KEEP)
    start_iso = _to_rfc3339_no_us(start_dt)
    end_iso   = _to_rfc3339_no_us(end_dt)
    obs = await fetch_observations(start_iso, end_iso, limit=1000)
    HISTORY.clear()
    HISTORY.extend(obs)
    prune_history()

async def poll_latest():
    headers = {"User-Agent": NWS_USER_AGENT, "Accept": "application/geo+json"}
    url = f"{BASE_URL}/stations/{NWS_STATION}/observations/latest"
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        r = await client.get(url)
        if r.status_code != 200:
            return
        feat = r.json().get("properties", {})
        row = parse_obs_feature({"properties": feat})
        ts = row["ts"]
        if ts and ts != latest_ts():
            HISTORY.append(row)
            prune_history()

# ---------- FastAPI lifecycle ----------
@app.on_event("startup")
async def on_startup():
    await backfill_48h()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(poll_latest, "interval", minutes=POLL_MINUTES)
    scheduler.start()

# ---------- API ----------
@app.get("/api/current")
async def api_current():
    if not HISTORY:
        return JSONResponse({})
    row = HISTORY[-1]
    t_c  = row["temp_c"]
    rh   = row["rh"]
    tw_c = wetbulb_stull_c(t_c, rh) if (t_c is not None and rh is not None) else None
    p_inhg = pa_to_inhg(row["pressure_pa"])
    ts_utc = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
    ts_local = ts_utc.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")
    return {
        "timestamp_local": ts_local,
        "station": NWS_STATION,
        "temperature_F": None if t_c is None else round(c_to_f(t_c), 1),
        "dry_bulb_F": None if t_c is None else round(c_to_f(t_c), 1),
        "wet_bulb_F": None if tw_c is None else round(c_to_f(tw_c), 1),
        "humidity_percent": None if rh is None else round(rh, 0),
        "pressure_inHg": None if p_inhg is None else round(p_inhg, 2),
    }

@app.get("/api/history")
async def api_history():
    out = []
    for row in HISTORY:
        t_c  = row["temp_c"]
        rh   = row["rh"]
        tw_c = wetbulb_stull_c(t_c, rh) if (t_c is not None and rh is not None) else None
        p_inhg = pa_to_inhg(row["pressure_pa"])
        ts_utc = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        out.append({
            "timestamp_local": ts_utc.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M"),
            "temperature_F": None if t_c is None else round(c_to_f(t_c), 1),
            "dry_bulb_F": None if t_c is None else round(c_to_f(t_c), 1),
            "wet_bulb_F": None if tw_c is None else round(c_to_f(tw_c), 1),
            "humidity_percent": None if rh is None else round(rh, 0),
            "pressure_inHg": None if p_inhg is None else round(p_inhg, 2),
        })
    return out

@app.get("/api/history.csv")
async def api_history_csv():
    headers = ["timestamp_local","temperature_F","dry_bulb_F","wet_bulb_F","humidity_percent","pressure_inHg"]
    lines = [",".join(headers)]
    for r in (await api_history()):
        vals = [r.get(h) if r.get(h) is not None else "" for h in headers]
        lines.append(",".join(str(v) for v in vals))
    csv_text = "\n".join(lines)
    return PlainTextResponse(csv_text, media_type="text/csv")
