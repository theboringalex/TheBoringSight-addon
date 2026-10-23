"""Analytics engine - the "what does it predict" part.

Everything here is deliberately transparent statistics (no black boxes):
  * anomaly detection   - robust z-score vs. the same hour of day (median/MAD)
  * forecasting         - hour-of-week seasonal profile + decaying level correction,
                          with a built-in backtest against a naive baseline
  * presence prediction - probability of "on"/"home" per hour of week
  * predictive maintenance - battery depletion (linear trend), availability
  * link analysis       - de-seasonalised correlations between sensors
  * energy              - daily consumption, projection and outlier days
"""
import time
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import numpy as np

from .config import cfg

TZ = ZoneInfo(cfg.tz)

# Minimum "believable" spread per device class, so tiny MADs don't create noise.
FLOORS = {
    "temperature": 0.7, "humidity": 3.0, "power": 100.0, "co2": 60.0, "pressure": 1.5,
    "voltage": 2.0, "current": 0.2, "pm25": 5.0, "pm10": 8.0, "moisture": 4.0,
    "apparent_power": 100.0, "volatile_organic_compounds": 50.0,
}
ANOMALY_KINDS = set(FLOORS.keys())


# --------------------------------------------------------------- time helpers
@lru_cache(maxsize=500000)
def local_parts(h: int):
    dt = datetime.fromtimestamp(h * 3600, TZ)
    return dt.hour, dt.weekday(), dt.toordinal()


def parts(hrs):
    if len(hrs) == 0:
        z = np.array([], dtype=int)
        return z, z, z
    arr = np.array([local_parts(int(h)) for h in hrs], dtype=int)
    return arr[:, 0], arr[:, 1], arr[:, 2]


def hourly_series(db, entity_id: str, days: int, field: str = "mean"):
    """Dense hourly series (forward-filled). Returns (hours, values)."""
    now_h = int(time.time() // 3600)
    since = now_h - days * 24
    rows = db.query(
        "SELECT hour, mean, last FROM hourly WHERE entity_id=? AND hour>=? ORDER BY hour",
        (entity_id, since),
    )
    if not rows:
        return np.array([], dtype=int), np.array([], dtype=float)
    first = rows[0]["hour"]
    n = now_h - first + 1
    hrs = np.arange(first, now_h + 1)
    mean = np.full(n, np.nan)
    last = np.full(n, np.nan)
    for r in rows:
        i = r["hour"] - first
        if 0 <= i < n:
            mean[i] = r["mean"]
            last[i] = r["last"]
    out = mean if field == "mean" else last
    prev = np.nan
    for i in range(n):
        if np.isnan(last[i]):
            last[i] = prev
        prev = last[i]
        if np.isnan(out[i]):
            out[i] = last[i]
    return hrs, out


# -------------------------------------------------------------------- anomaly
def _baseline(vals, hod, target_hod, upto):
    v = vals[:upto]
    m = (hod[:upto] == target_hod) & ~np.isnan(v)
    x = v[m]
    if len(x) < 5:
        return None
    med = float(np.median(x))
    mad = float(np.median(np.abs(x - med)))
    return med, 1.4826 * mad, len(x)


def anomaly_check(db, entity_id: str, current: float, kind: str, z_thr: float):
    hrs, vals = hourly_series(db, entity_id, 28)
    if len(vals) < 24 * 5:
        return None
    hod, _, _ = parts(hrs)
    idx = len(vals) - 1
    b = _baseline(vals, hod, hod[idx], idx)
    if not b:
        return None
    med, sc, n = b
    floor = max(FLOORS.get(kind, 0.0), 0.02 * max(abs(med), 1.0))
    sc = max(sc, floor)
    z = (current - med) / sc
    if abs(z) < z_thr:
        return None
    # persistence: current hour mean anomalous too, or previous hour same direction
    persistent = False
    if not np.isnan(vals[idx]) and abs((vals[idx] - med) / sc) >= 0.75 * z_thr:
        persistent = True
    elif idx >= 1 and not np.isnan(vals[idx - 1]):
        b2 = _baseline(vals, hod, hod[idx - 1], idx - 1)
        if b2:
            zp = (vals[idx - 1] - b2[0]) / max(b2[1], floor)
            if zp * z > 0 and abs(zp) >= 0.5 * z_thr:
                persistent = True
    if not persistent:
        return None
    return {"z": round(float(z), 2), "baseline": round(med, 3), "scale": round(sc, 3),
            "samples": n, "value": round(float(current), 3)}


# ------------------------------------------------------------------- forecast
def _fit(vals, how, hod):
    prof = np.full(168, np.nan)
    for k in range(168):
        x = vals[how == k]
        x = x[~np.isnan(x)]
        if len(x):
            prof[k] = np.median(x[-4:])
    hodprof = np.full(24, np.nan)
    for h in range(24):
        x = vals[hod == h]
        x = x[~np.isnan(x)]
        if len(x):
            hodprof[h] = np.median(x)
    overall = float(np.nanmean(vals))
    for k in range(168):
        if np.isnan(prof[k]):
            prof[k] = hodprof[k % 24] if not np.isnan(hodprof[k % 24]) else overall
    res = vals - prof[how]
    med = np.nanmedian(res)
    sigma = 1.4826 * float(np.nanmedian(np.abs(res - med)))
    sigma = max(sigma, 0.02 * max(abs(overall), 1.0), 1e-6) * 1.5
    r_now = float(np.nanmean(res[-3:]))
    return prof, sigma, r_now


def _predict(prof, r_now, h_last: int, k: int):
    hod, wd, _ = local_parts(h_last + k)
    return float(prof[wd * 24 + hod] + r_now * (0.7 ** k))


def forecast(db, entity_id: str, horizon: int = 24, days: int = 28):
    hrs, vals = hourly_series(db, entity_id, days)
    n = len(vals)
    if n < 48:
        return {"method": "insufficient_data", "points": [], "samples": n}
    hod, wd, _ = parts(hrs)
    how = wd * 24 + hod
    prof, sigma, r_now = _fit(vals, how, hod)
    h_last = int(hrs[-1])
    pts = []
    for k in range(1, horizon + 1):
        y = _predict(prof, r_now, h_last, k)
        band = 1.28 * sigma * (1 + 0.02 * k)
        pts.append({"ts": (h_last + k) * 3600, "yhat": round(y, 3),
                    "lo": round(y - band, 3), "hi": round(y + band, 3)})
    out = {"method": "seasonal_profile+level", "points": pts, "samples": n, "sigma": round(sigma, 3)}
    # backtest on the last 24 h (profile fit without them)
    if n >= 24 * 4:
        cut = n - 24
        p2, _, r2 = _fit(vals[:cut], how[:cut], hod[:cut])
        h_cut = int(hrs[cut - 1])
        e_model, e_naive, e_day, cnt = 0.0, 0.0, 0.0, 0
        for k in range(1, 25):
            actual = vals[cut - 1 + k] if cut - 1 + k < n else np.nan
            if np.isnan(actual):
                continue
            e_model += abs(actual - _predict(p2, r2, h_cut, k))
            e_naive += abs(actual - vals[cut - 1])
            e_day += abs(actual - vals[cut - 1 + k - 24])
            cnt += 1
        if cnt:
            out["backtest"] = {"mae_model": round(e_model / cnt, 3), "mae_persistence": round(e_naive / cnt, 3),
                               "mae_yesterday": round(e_day / cnt, 3), "hours": cnt}
    return out


# ------------------------------------------------------------------- presence
def presence_profile(db, entity_id: str, on_states=("on", "home"), days: int = 28):
    now = time.time()
    since = now - days * 86400
    prev = db.one("SELECT ts, state FROM states WHERE entity_id=? AND ts<? ORDER BY ts DESC LIMIT 1",
                  (entity_id, since))
    rows = db.query("SELECT ts, state FROM states WHERE entity_id=? AND ts>=? ORDER BY ts",
                    (entity_id, since))
    seq = ([(since, prev["state"])] if prev else []) + [(r["ts"], r["state"]) for r in rows]
    if not seq:
        return None
    secs: dict = {}
    for i, (t, s) in enumerate(seq):
        t_end = seq[i + 1][0] if i + 1 < len(seq) else now
        if s not in on_states or t_end <= t:
            continue
        h = int(t // 3600)
        h_end = int((t_end - 1e-6) // 3600)
        for hh in range(h, h_end + 1):
            lo, hi = max(t, hh * 3600), min(t_end, (hh + 1) * 3600)
            secs[hh] = secs.get(hh, 0.0) + max(0.0, hi - lo)
    first_h = max(int(seq[0][0] // 3600), int(since // 3600))
    cur_h = int(now // 3600)
    sums, cnt = [0.0] * 168, [0] * 168
    for h in range(first_h, cur_h):
        hod, wd, _ = local_parts(h)
        k = wd * 24 + hod
        sums[k] += min(1.0, secs.get(h, 0.0) / 3600)
        cnt[k] += 1
    prof = [round(sums[k] / cnt[k], 3) if cnt[k] else None for k in range(168)]
    nxt = []
    for k in range(1, 25):
        hod, wd, _ = local_parts(cur_h + k)
        nxt.append({"ts": (cur_h + k) * 3600, "p": prof[wd * 24 + hod]})
    return {"profile": prof, "next24": nxt, "weeks": max(cnt) if cnt else 0}


# ---------------------------------------------------------------- maintenance
def battery_forecast(db, entity_id: str):
    hrs, vals = hourly_series(db, entity_id, 30, field="last")
    if len(vals) < 72:
        return None
    ok = ~np.isnan(vals)
    hrs, vals = hrs[ok], vals[ok]
    jumps = np.where(np.diff(vals) > 10)[0]          # battery replaced -> restart trend
    if len(jumps):
        hrs, vals = hrs[jumps[-1] + 1:], vals[jumps[-1] + 1:]
    if len(vals) < 72:
        return None
    days_idx = (hrs // 24)
    uniq = np.unique(days_idx)
    if len(uniq) < 4:
        return None
    means = np.array([vals[days_idx == d].mean() for d in uniq])
    slope, intercept = np.polyfit(uniq - uniq[0], means, 1)
    current = float(vals[-1])
    out = {"current": round(current, 1), "slope_per_day": round(float(slope), 3), "days_observed": int(len(uniq))}
    if slope < -0.05:
        out["days_to_15"] = round(max(0.0, (current - 15) / -slope), 1)
        out["days_to_empty"] = round(max(0.0, current / -slope), 1)
    return out


# ------------------------------------------------------------- link analysis
def _deseason(hours, vals):
    hod = np.array([local_parts(int(h))[0] for h in hours])
    out = vals.copy()
    for h in range(24):
        m = hod == h
        if m.any():
            out[m] = vals[m] - vals[m].mean()
    return out


def correlations(db, entity_id: str, candidates: list, days: int = 14, top: int = 8, deseason: bool = True):
    since = int(time.time() // 3600) - days * 24

    def load(eid):
        rows = db.query("SELECT hour, mean FROM hourly WHERE entity_id=? AND hour>=?", (eid, since))
        return {r["hour"]: r["mean"] for r in rows}

    base = load(entity_id)
    if len(base) < 48:
        return []
    res = []
    for cid in candidates:
        if cid == entity_id:
            continue
        other = load(cid)
        common = sorted(set(base) & set(other))
        if len(common) < 48:
            continue
        a = np.array([base[h] for h in common])
        b = np.array([other[h] for h in common])
        if deseason:
            a, b = _deseason(common, a), _deseason(common, b)
        if a.std() < 1e-9 or b.std() < 1e-9:
            continue
        r = float(np.corrcoef(a, b)[0, 1])
        if np.isfinite(r):
            res.append({"entity_id": cid, "r": round(r, 3), "n": len(common)})
    res.sort(key=lambda x: -abs(x["r"]))
    return res[:top]


# --------------------------------------------------------------------- energy
def energy_daily(db, entity_id: str, days: int = 21):
    hrs, last = hourly_series(db, entity_id, days + 1, field="last")
    if len(last) < 48:
        return []
    ok = ~np.isnan(last)
    hrs, last = hrs[ok], last[ok]
    _, _, day = parts(hrs)
    days_u = list(dict.fromkeys(day.tolist()))
    ends = {d: float(last[day == d][-1]) for d in days_u}
    out = []
    for i in range(1, len(days_u)):
        d, p = days_u[i], days_u[i - 1]
        delta = ends[d] - ends[p]
        if delta < 0:                       # meter reset
            delta = ends[d]
        out.append({"date": datetime.fromordinal(d).strftime("%Y-%m-%d"), "kwh": round(delta, 3),
                    "complete": d != days_u[-1]})
    return out


def energy_projection(db, entity_id: str):
    daily = energy_daily(db, entity_id, 21)
    full = [d["kwh"] for d in daily if d["complete"]]
    if len(full) < 3:
        return None
    avg = float(np.mean(full[-7:]))
    today = next((d["kwh"] for d in daily if not d["complete"]), 0.0)
    hrs, last = hourly_series(db, entity_id, 15, field="last")
    ok = ~np.isnan(last)
    hrs, last = hrs[ok], last[ok]
    hod, _, _ = parts(hrs)
    delta = np.diff(last, prepend=last[0])
    delta[delta < 0] = 0
    hprof = np.array([delta[hod == h].mean() if (hod == h).any() else 0.0 for h in range(24)])
    cur_h = local_parts(int(time.time() // 3600))[0]
    share = float(hprof[:cur_h + 1].sum() / hprof.sum()) if hprof.sum() > 0 else (cur_h + 1) / 24
    projected = max(today, today + avg * (1 - share))
    return {"avg_last7": round(avg, 3), "today_so_far": round(today, 3),
            "projected_today": round(projected, 3), "share_elapsed": round(share, 3)}
