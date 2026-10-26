"""
GeoRouting Lab — analytics.py
=============================
Phase spatial analytics, as a FastAPI router mounted at /analytics
(the browser calls /api/analytics/...). Every analysis is also exposed to the
map assistant as an agent tool (see AGENT_TOOLS at the bottom).

  ┌──────────────────────────────┬──────────────────────────────────────────────┐
  │ Endpoint                     │ Answers                                       │
  ├──────────────────────────────┼──────────────────────────────────────────────┤
  │ POST /analytics/hexbin       │ Where is demand / where do deliveries fail?   │
  │ POST /analytics/coverage     │ What % of demand is within N min of a facility│
  │ POST /analytics/hotspots     │ Where do failures cluster (statistically)?    │
  │ POST /analytics/site_selection│ Where should the next N facilities go?       │
  │ POST /analytics/delivery_points/learn   │ Learn real drop points from GPS    │
  │ POST /analytics/delivery_points/offsets │ Addresses whose pin is wrong        │
  └──────────────────────────────┴──────────────────────────────────────────────┘

KEY IDEA: everything is aggregated into H3 hexagons first.
  22 million address points are too many to route to individually, but a city
  has only a few thousand hexagons. We route to hexagon centres (weighted by how
  many addresses each contains), which is fast and accurate enough for planning.

Every analysis returns the same shape:
  { "summary": {...small, for the agent...},
    "stats":   [[label, value], ...]  for the sidebar,
    "legend":  [{"color", "label"}], for the sidebar,
    "layers":  [{kind, label, color, opacity, geojson}]  same format as agent.py }
"""

import asyncio
import logging
import math
import os
import time

import h3
import numpy as np
import shapely
from fastapi import APIRouter, Header, HTTPException, Request
from shapely.geometry import shape
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel, Field, model_validator

import app as core   # reuse db_connection(), client, VALHALLA_URL, _ALLOWED_FACILITY_TYPES

logger = logging.getLogger("georouting.analytics")
router = APIRouter(prefix="/analytics", tags=["Analytics"])


# =============================================================================
# CONFIGURATION
# =============================================================================

MAX_AREA_KM2         = float(os.getenv("ANALYTICS_MAX_AREA_KM2", "3000"))   # protects the server

# --- Protecting Valhalla (shared with the Route / Facility tabs and the agent) ---
# Max isochrone requests in flight for ALL analytics requests together.
VALHALLA_CONCURRENCY = int(os.getenv("ANALYTICS_VALHALLA_CONCURRENCY", "3"))
# Max coverage / site-selection runs at the same time. Extra ones get "busy" (HTTP 429).
MAX_HEAVY            = int(os.getenv("ANALYTICS_MAX_HEAVY", "1"))
# Max isochrones one analysis may request (facilities + candidates).
MAX_ISOCHRONES       = int(os.getenv("ANALYTICS_MAX_ISOCHRONES", "250"))
ISO_TIMEOUT_S        = float(os.getenv("ANALYTICS_ISO_TIMEOUT_S", "30"))
# Stop early when Valhalla is clearly not answering: this many failures and no success yet.
BREAKER_FAILURES     = 3
# Cache isochrones in memory: re-running with another hexagon size is instant.
ISO_CACHE_TTL_S      = int(os.getenv("ANALYTICS_ISO_CACHE_TTL_S", "3600"))
ISO_CACHE_MAX        = 3000
ADMIN_TOKEN          = os.getenv("ANALYTICS_ADMIN_TOKEN")    # if set, required for /learn
SQL_TIMEOUT          = "60s"

# Fastest plausible speed per profile (km/h). Used ONLY to decide which
# facilities just outside the map could still serve people inside it.
MAX_SPEED_KMH = {"auto": 100, "bicycle": 25, "pedestrian": 6}

RESOLUTIONS = {7, 8, 9}        # columns h3_r7 / h3_r8 / h3_r9 exist in the tables

# Colours tuned for the dark basemap
SEQ_PALETTE  = ["#253a6e", "#2f6db5", "#2a9d8f", "#8ac926", "#ffd23f"]       # low → high
HOT_COLORS   = {3: "#ff3b3b", 2: "#ff7a59", 1: "#ffb38a"}                     # 99/95/90 %
COLD_COLORS  = {-3: "#3b6cff", -2: "#6f95ff", -1: "#a9c1ff"}
COV_COLORS   = {"ok": "#22d98a", "near": "#f5a623", "far": "#f43f5e"}
SITE_COLORS  = ["#a855f7", "#22d3ee", "#facc15", "#fb923c", "#e879f9",
                "#4ade80", "#f9a8d4", "#818cf8", "#86efac", "#fde68a"]


# =============================================================================
# REQUEST MODELS
# An "area" is either a bbox [west, south, east, north] or a circle.
# The sidebar sends the current map view as a bbox.
# =============================================================================

class Area(BaseModel):
    bbox: list[float] | None = Field(None, min_length=4, max_length=4)
    center: dict | None = None          # {"lon", "lat"}
    radius_km: float | None = Field(None, gt=0, le=40)

    @model_validator(mode="after")
    def _one_kind(self):
        if not self.bbox and not (self.center and self.radius_km):
            raise ValueError("area needs either bbox or center + radius_km")
        return self


class HexbinReq(BaseModel):
    area: Area
    dataset: str = Field("demand", pattern="^(demand|deliveries)$")
    metric: str = Field("count", pattern="^(count|failed|failure_rate)$")
    resolution: int = 8
    days: int = Field(30, ge=1, le=365)


class CoverageReq(BaseModel):
    area: Area
    facility_type: str = "hospital"
    minutes: int = Field(15, ge=1, le=60)
    profile: str = Field("auto", pattern="^(auto|bicycle|pedestrian)$")
    direction: str = Field("to_facility", pattern="^(to_facility|from_facility)$")
    resolution: int = 8


class HotspotReq(BaseModel):
    area: Area
    method: str = Field("gi_star", pattern="^(gi_star|dbscan)$")
    # failure_rate (default) finds places where deliveries fail MORE than usual.
    # failed (raw count) mostly highlights busy areas — more deliveries, more failures.
    metric: str = Field("failure_rate", pattern="^(failed|failure_rate)$")
    resolution: int = 8
    days: int = Field(30, ge=1, le=365)
    min_attempts: int = Field(5, ge=1)            # failure_rate: ignore cells with fewer attempts
    smoothing: float = Field(20, ge=0, le=500)    # failure_rate: pseudo-attempts at the area average
    eps_m: float = Field(250, ge=20, le=2000)     # dbscan: neighbour distance
    min_points: int = Field(8, ge=3, le=500)      # dbscan: points to form a cluster


class SiteReq(BaseModel):
    area: Area
    n_sites: int = Field(5, ge=1, le=20)
    minutes: int = Field(10, ge=1, le=60)
    profile: str = Field("auto", pattern="^(auto|bicycle|pedestrian)$")
    direction: str = Field("to_facility", pattern="^(to_facility|from_facility)$")
    existing_facility_type: str | None = None     # keep these facilities, add new ones
    candidates: list[dict] | None = None          # [{"lon","lat","label"}]; default = hexagon centres
    max_candidates: int = Field(80, ge=5, le=300)
    resolution: int = 8
    method: str = Field("greedy", pattern="^(greedy|exact)$")


class LearnReq(BaseModel):
    min_deliveries: int = Field(3, ge=2, le=50)
    inlier_radius_m: float = Field(150, ge=20, le=1000)
    max_offset_m: float = Field(500, ge=50, le=5000)


class OffsetsReq(BaseModel):
    area: Area
    min_offset_m: float = Field(30, ge=0)
    min_confidence: float = Field(0.5, ge=0, le=1)
    limit: int = Field(300, ge=1, le=2000)


# =============================================================================
# SHARED HELPERS
# =============================================================================

class AnalyticsError(Exception):
    """A problem the user (or the agent) can fix: area too big, no data, …"""


def _haversine_m(lon1, lat1, lon2, lat2) -> float:
    r = math.radians
    a = (math.sin(r(lat2 - lat1) / 2) ** 2
         + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lon2 - lon1) / 2) ** 2)
    return 12_742_000 * math.asin(math.sqrt(a))


def _bbox(area: Area | dict) -> tuple[float, float, float, float]:
    """Turn an Area into (west, south, east, north) and enforce the size limit."""
    if isinstance(area, dict):
        area = Area(**area)
    if area.bbox:
        w, s, e, n = area.bbox
    else:
        lon, lat, rk = float(area.center["lon"]), float(area.center["lat"]), area.radius_km
        dlat = rk / 111.32
        dlon = rk / (111.32 * math.cos(math.radians(lat)))
        w, s, e, n = lon - dlon, lat - dlat, lon + dlon, lat + dlat
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise AnalyticsError("Invalid area. bbox must be [west, south, east, north].")
    km2 = (_haversine_m(w, s, e, s) / 1000) * (_haversine_m(w, s, w, n) / 1000)
    if km2 > MAX_AREA_KM2:
        raise AnalyticsError(f"Area is {km2:,.0f} km², the limit is {MAX_AREA_KM2:,.0f} km². "
                             "Zoom in or choose a smaller area.")
    return w, s, e, n


def _res(resolution: int) -> int:
    if resolution not in RESOLUTIONS:
        raise AnalyticsError(f"resolution must be one of {sorted(RESOLUTIONS)}")
    return resolution


def _facility_type(t: str | None) -> str | None:
    if t is None:
        return None
    t = t.lower().strip()
    if t not in core._ALLOWED_FACILITY_TYPES:
        raise AnalyticsError(f"facility type must be one of {sorted(core._ALLOWED_FACILITY_TYPES)}")
    return t


def _query_sync(sql: str, params) -> list[dict]:
    with core.db_connection() as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(f"SET statement_timeout = '{SQL_TIMEOUT}'")
                cur.execute(sql, params)
                rows = cur.fetchall() if cur.description else []
            conn.commit()
            return rows
        except Exception:
            conn.rollback()
            raise


async def _query(sql: str, params=None) -> list[dict]:
    """Run SQL in a worker thread so the server keeps answering other requests."""
    try:
        return await asyncio.to_thread(_query_sync, sql, params or {})
    except RuntimeError as exc:                       # pool unavailable
        raise HTTPException(503, f"Database unavailable: {exc}")


def _hex_feature(cell: str, **props) -> dict:
    ring = [[round(lng, 6), round(lat, 6)] for lat, lng in h3.cell_to_boundary(cell)]
    ring.append(ring[0])
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {"cell": cell, **props}}


def _point(lon, lat, **props) -> dict:
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
            "properties": props}


def _fc(features) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


def _layer(kind, label, features, color=None, opacity=None) -> dict:
    return {"kind": kind, "label": label, "color": color, "opacity": opacity, "geojson": _fc(features)}


def _cell_center(cell: str) -> tuple[float, float]:
    lat, lng = h3.cell_to_latlng(cell)
    return lng, lat


def _quantile_breaks(values: list[float], k: int = 5) -> list[float]:
    v = sorted(values)
    if not v:
        return []
    return [v[min(len(v) - 1, int(len(v) * i / k))] for i in range(1, k)]


def _class_color(value: float, breaks: list[float]) -> str:
    for i, b in enumerate(breaks):
        if value <= b:
            return SEQ_PALETTE[i]
    return SEQ_PALETTE[len(breaks)] if breaks else SEQ_PALETTE[-1]


def _fmt(v: float, pct=False) -> str:
    return f"{v * 100:.1f}%" if pct else (f"{v:,.0f}" if abs(v) >= 10 else f"{v:,.2f}")


def _breaks_legend(breaks: list[float], lo: float, hi: float, pct=False) -> list[dict]:
    edges = [lo, *breaks, hi]
    return [{"color": SEQ_PALETTE[i], "label": f"{_fmt(edges[i], pct)} – {_fmt(edges[i + 1], pct)}"}
            for i in range(len(edges) - 1)]


async def _demand_cells(bbox, res: int) -> dict[str, float]:
    """Sum of demand weight per H3 cell inside the area."""
    rows = await _query(
        f"""SELECT h3_r{res} AS cell, SUM(weight)::float AS w
            FROM analytics.demand_points
            WHERE geom && ST_MakeEnvelope(%(w)s, %(s)s, %(e)s, %(n)s, 4326) AND h3_r{res} IS NOT NULL
            GROUP BY 1""",
        dict(zip("wsen", bbox)))
    return {r["cell"]: r["w"] for r in rows}


async def _facilities(bbox, ftype: str, pad_m: float = 0) -> list[dict]:
    """Facilities of one type inside the area, plus a margin (facilities just
    outside the map still serve people inside it)."""
    w, s, e, n = bbox
    dlat = pad_m / 111_320
    dlon = pad_m / (111_320 * math.cos(math.radians((s + n) / 2)))
    return await _query(
        """SELECT id, name, ST_X(ST_Centroid(geom)) AS lon, ST_Y(ST_Centroid(geom)) AS lat
           FROM topology.places
           WHERE LOWER(type) = %(t)s
             AND geom && ST_MakeEnvelope(%(w)s, %(s)s, %(e)s, %(n)s, 4326)
           LIMIT 300""",
        {"t": ftype, "w": w - dlon, "s": s - dlat, "e": e + dlon, "n": n + dlat})


# =============================================================================
# REACHABILITY ENGINE — Valhalla isochrones (replaces the old travel-time matrix)
#
# Old way: "how many seconds from this facility to each of 1,259 hexagons?"
#          → huge matrix requests, slow, and they piled up in Valhalla.
# New way: "give me the area reachable within 15 min" (ONE request per site),
#          then test which hexagon centres fall inside, locally, in milliseconds.
#          Valhalla stops searching at the time limit, so each request is small.
#
# Safety rails:
#   • _VALHALLA_SEM  — at most VALHALLA_CONCURRENCY requests for ALL users together
#   • _HEAVY_SEM     — one coverage/site analysis at a time; others get "busy"
#   • breaker        — stop after BREAKER_FAILURES failures with no success
#   • cache          — same site + same settings is never asked twice within an hour
#   • cancellation   — endpoints cancel the work if the browser goes away
# =============================================================================

class BusyError(AnalyticsError):
    """Another heavy analysis is running."""


_VALHALLA_SEM = asyncio.Semaphore(VALHALLA_CONCURRENCY)
_HEAVY_SEM = asyncio.Semaphore(MAX_HEAVY)
_iso_cache: dict[tuple, tuple[float, list]] = {}


def heavy(func):
    """Decorator: allow only MAX_HEAVY coverage/site analyses at once."""
    async def wrapper(req):
        if _HEAVY_SEM.locked():
            raise BusyError("Another coverage or site analysis is running. Try again in a moment.")
        async with _HEAVY_SEM:
            return await func(req)
    wrapper.__name__ = func.__name__
    return wrapper


async def _isochrones(sites: list[tuple], minutes: list[int], profile: str,
                      direction: str) -> tuple[list, int]:
    """
    One Valhalla isochrone per site. Returns (geoms, failures) where
    geoms[i] = [polygon for each minute value, ascending] or None if it failed.

    direction 'to_facility'   → area FROM which people reach the site (reverse isochrone)
              'from_facility' → area the site can reach (ambulance, delivery van)
    """
    if len(sites) > MAX_ISOCHRONES:
        raise AnalyticsError(f"This needs {len(sites)} travel-time areas (limit {MAX_ISOCHRONES}). "
                             "Zoom in, or lower the number of candidates.")
    contours = sorted({int(m) for m in minutes})
    out: list = [None] * len(sites)
    st = {"ok": 0, "fail": 0}
    now = time.time()

    async def one(i: int):
        lon, lat = sites[i]
        key = (round(lon, 5), round(lat, 5), profile, direction, tuple(contours))
        hit = _iso_cache.get(key)
        if hit and now - hit[0] < ISO_CACHE_TTL_S:
            out[i] = hit[1]
            st["ok"] += 1
            return
        async with _VALHALLA_SEM:
            if st["fail"] >= BREAKER_FAILURES and st["ok"] == 0:
                return                                   # breaker open: don't add load
            body = {"locations": [{"lon": lon, "lat": lat}], "costing": profile,
                    "contours": [{"time": m} for m in contours],
                    "polygons": True, "denoise": 0.3, "generalize": 60,
                    "reverse": direction == "to_facility"}
            try:
                resp = await core.client.post(f"{core.VALHALLA_URL}/isochrone",
                                              json=body, timeout=ISO_TIMEOUT_S)
                resp.raise_for_status()
                feats = resp.json().get("features", [])
            except Exception as exc:
                st["fail"] += 1
                # type name matters: a timeout prints an empty message otherwise
                logger.warning("Isochrone failed for site %d (%.5f, %.5f): %s %s",
                               i, lon, lat, type(exc).__name__, exc)
                return
        by_minute = {}
        for f in feats:
            try:
                g = shape(f["geometry"])
                if not g.is_valid:
                    g = shapely.make_valid(g)
                by_minute[int(round(float(f["properties"]["contour"])))] = g
            except Exception:
                continue
        geoms = [by_minute.get(m) for m in contours]
        out[i] = geoms
        st["ok"] += 1
        if len(_iso_cache) >= ISO_CACHE_MAX:
            _iso_cache.clear()
        _iso_cache[key] = (now, geoms)

    await asyncio.gather(*[one(i) for i in range(len(sites))])
    if sites and st["ok"] == 0:
        raise AnalyticsError("Valhalla did not return any travel-time area (timeouts or errors). "
                             "Check that the valhalla container is healthy: docker stats / docker logs valhalla.")
    if st["fail"]:
        logger.warning("%d of %d isochrones failed; coverage may be underestimated.", st["fail"], len(sites))
    return out, st["fail"]


def _inside(geom, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Indexes of the points (hexagon centres) that fall inside geom."""
    if geom is None or geom.is_empty:
        return np.array([], dtype=int)
    minx, miny, maxx, maxy = geom.bounds
    idx = np.nonzero((xs >= minx) & (xs <= maxx) & (ys >= miny) & (ys <= maxy))[0]
    if len(idx) == 0:
        return idx
    return idx[shapely.contains_xy(geom, xs[idx], ys[idx])]


def _failed_note(failures: int, total: int) -> list:
    return [["⚠ Areas not computed", f"{failures} of {total} (results may be low)"]] if failures else []


# =============================================================================
# 1. HEXBIN — demand or delivery density per hexagon
# =============================================================================

async def hexbin(req: HexbinReq) -> dict:
    bbox, res = _bbox(req.area), _res(req.resolution)
    p = dict(zip("wsen", bbox), days=req.days)

    if req.dataset == "demand":
        if req.metric != "count":
            raise AnalyticsError("Demand data only supports metric 'count'.")
        cells = await _demand_cells(bbox, res)
        values, title = cells, "Demand (weighted addresses)"
    else:
        rows = await _query(
            f"""SELECT h3_r{res} AS cell, COUNT(*)::int AS n,
                       COUNT(*) FILTER (WHERE status = 'failed')::int AS f
                FROM analytics.deliveries
                WHERE geom && ST_MakeEnvelope(%(w)s, %(s)s, %(e)s, %(n)s, 4326)
                  AND attempted_at >= now() - make_interval(days => %(days)s)
                GROUP BY 1""", p)
        if req.metric == "count":
            values, title = {r["cell"]: r["n"] for r in rows}, f"Delivery attempts, last {req.days} days"
        elif req.metric == "failed":
            values, title = {r["cell"]: r["f"] for r in rows if r["f"]}, f"Failed attempts, last {req.days} days"
        else:
            values = {r["cell"]: r["f"] / r["n"] for r in rows if r["n"] >= 5}
            title = f"Failure rate (cells with ≥5 attempts), last {req.days} days"

    if not values:
        return {"summary": {"cells": 0, "message": "No data in this area."},
                "stats": [["Cells", "0"]], "legend": [], "layers": []}

    pct = req.metric == "failure_rate"
    vals = list(values.values())
    breaks = _quantile_breaks(vals)
    feats = [_hex_feature(c, value=round(v, 4), color=_class_color(v, breaks)) for c, v in values.items()]
    top = sorted(values.items(), key=lambda kv: -kv[1])[:5]
    total = sum(vals)

    return {
        "summary": {"title": title, "cells": len(values),
                    "total": round(total, 2) if not pct else None,
                    "top_cells": [{"cell": c, "value": round(v, 4),
                                   "lon": round(_cell_center(c)[0], 5), "lat": round(_cell_center(c)[1], 5)}
                                  for c, v in top]},
        "stats": [["Analysis", title], ["Hexagons", f"{len(values):,}"],
                  ["Total" if not pct else "Median rate",
                   _fmt(total) if not pct else _fmt(sorted(vals)[len(vals) // 2], True)],
                  ["Highest cell", _fmt(top[0][1], pct)]],
        "legend": _breaks_legend(breaks, min(vals), max(vals), pct),
        "layers": [_layer("polygon", title, feats, opacity=0.6)],
    }


# =============================================================================
# 2. COVERAGE — share of demand within N minutes of the nearest facility
# =============================================================================

@heavy
async def coverage(req: CoverageReq) -> dict:
    bbox, res, ftype = _bbox(req.area), _res(req.resolution), _facility_type(req.facility_type)
    near_min = max(req.minutes + 1, round(req.minutes * 1.5))
    reach_m = MAX_SPEED_KMH[req.profile] * 1000 / 60 * near_min

    cells = await _demand_cells(bbox, res)
    if not cells:
        raise AnalyticsError("No demand points in this area. Load analytics.demand_points first.")
    facs = await _facilities(bbox, ftype, pad_m=reach_m)

    cell_ids = list(cells)
    xs = np.array([_cell_center(c)[0] for c in cell_ids])
    ys = np.array([_cell_center(c)[1] for c in cell_ids])

    # band per cell: 0 = within the limit, 1 = within 1.5× the limit, 2 = farther / unreachable
    band = np.full(len(cell_ids), 2)
    who = [None] * len(cell_ids)
    failures = 0
    if facs:
        geoms, failures = await _isochrones([(f["lon"], f["lat"]) for f in facs],
                                            [req.minutes, near_min], req.profile, req.direction)
        for fi, g in enumerate(geoms):
            if g is None:
                continue
            for b in (1, 0):                                  # outer ring first, inner overrides
                for j in _inside(g[b], xs, ys):
                    if b < band[j] or (b == band[j] and who[j] is None):
                        band[j], who[j] = b, facs[fi]["name"]

    weights = np.array([cells[c] for c in cell_ids])
    total = float(weights.sum())
    covered = float(weights[band == 0].sum())
    names = {0: "ok", 1: "near", 2: "far"}
    labels = {0: f"≤ {req.minutes} min", 1: f"{req.minutes}–{near_min} min", 2: f"> {near_min} min"}
    feats = [_hex_feature(c, demand=round(cells[c], 1), color=COV_COLORS[names[int(band[j])]],
                          minutes=labels[int(band[j])], nearest=who[j])
             for j, c in enumerate(cell_ids)]
    uncovered = sorted(((cells[c], c) for j, c in enumerate(cell_ids) if band[j] != 0), reverse=True)

    pct = covered / total if total else 0
    fac_pins = [_point(f["lon"], f["lat"], label="+", title=f["name"]) for f in facs]
    return {
        "summary": {"facility_type": ftype, "minutes": req.minutes, "profile": req.profile,
                    "direction": req.direction, "facilities_considered": len(facs),
                    "facilities_failed": failures,
                    "total_demand": round(total), "covered_demand": round(covered),
                    "coverage_pct": round(pct * 100, 1),
                    "largest_uncovered_cells": [
                        {"lon": round(_cell_center(c)[0], 5), "lat": round(_cell_center(c)[1], 5),
                         "demand": round(w)} for w, c in uncovered[:5]]},
        "stats": [["Coverage", f"{pct * 100:.1f}%"],
                  ["Covered demand", f"{covered:,.0f} of {total:,.0f}"],
                  [f"{ftype.title()} facilities used", str(len(facs))],
                  ["Hexagons", f"{len(cell_ids):,}"], *_failed_note(failures, len(facs))],
        "legend": [{"color": COV_COLORS["ok"],   "label": f"≤ {req.minutes} min (covered)"},
                   {"color": COV_COLORS["near"], "label": f"{req.minutes}–{near_min} min"},
                   {"color": COV_COLORS["far"],  "label": f"> {near_min} min or not reachable"}],
        "layers": [_layer("polygon", f"{ftype} coverage", feats, opacity=0.5),
                   _layer("points", f"Existing {ftype}", fac_pins, color="#0f172a")],
    }


# =============================================================================
# 3. HOTSPOTS — statistically significant clusters of failed deliveries
# =============================================================================

def _gi_star(values: dict[str, float]) -> dict[str, float]:
    """
    Getis-Ord Gi* z-score for every hexagon, using the hexagon and its 6
    direct neighbours (H3 grid_disk k=1) as the neighbourhood.
      z ≥ 1.96  → hot spot  (95% confidence)
      z ≤ -1.96 → cold spot
    """
    n = len(values)
    if n < 10:
        raise AnalyticsError(f"Only {n} hexagons have data; need at least 10 for hotspot statistics.")
    xs = list(values.values())
    mean = sum(xs) / n
    s = math.sqrt(max(sum(x * x for x in xs) / n - mean ** 2, 0))
    if s == 0:
        return {c: 0.0 for c in values}
    z = {}
    for cell in values:
        neigh = [c for c in h3.grid_disk(cell, 1) if c in values]   # includes the cell itself
        wsum = len(neigh)                                            # binary weights
        local = sum(values[c] for c in neigh)
        denom = s * math.sqrt((n * wsum - wsum ** 2) / (n - 1)) if n > 1 else 0
        z[cell] = (local - mean * wsum) / denom if denom else 0.0
    return z


def _z_class(z: float) -> int:
    for level, cut in ((3, 2.576), (2, 1.96), (1, 1.645)):
        if z >= cut:
            return level
        if z <= -cut:
            return -level
    return 0


async def hotspots(req: HotspotReq) -> dict:
    bbox = _bbox(req.area)
    p = dict(zip("wsen", bbox), days=req.days)

    if req.method == "dbscan":
        return await _hotspots_dbscan(req, bbox, p)

    res = _res(req.resolution)
    rows = await _query(
        f"""SELECT h3_r{res} AS cell, COUNT(*)::int AS n,
                   COUNT(*) FILTER (WHERE status = 'failed')::int AS f
            FROM analytics.deliveries
            WHERE geom && ST_MakeEnvelope(%(w)s, %(s)s, %(e)s, %(n)s, 4326)
              AND attempted_at >= now() - make_interval(days => %(days)s)
            GROUP BY 1""", p)
    stats_by_cell = {r["cell"]: r for r in rows}
    if req.metric == "failed":
        values = {c: float(r["f"]) for c, r in stats_by_cell.items()}
    else:
        # Smoothed rate: (failed + k·avg) / (attempts + k). A cell with 2 failures
        # out of 5 is pulled toward the average; 40 out of 100 stays high.
        # This stops small, noisy cells from looking like hot spots.
        tot_n = sum(r["n"] for r in rows) or 1
        avg = sum(r["f"] for r in rows) / tot_n
        k = req.smoothing
        values = {c: (r["f"] + k * avg) / (r["n"] + k)
                  for c, r in stats_by_cell.items() if r["n"] >= req.min_attempts}

    z = _gi_star(values)
    feats, hot = [], []
    for cell, zc in z.items():
        cls = _z_class(zc)
        if cls == 0:
            continue
        r = stats_by_cell[cell]
        feats.append(_hex_feature(cell, z=round(zc, 2), failed=r["f"], attempts=r["n"],
                                  color=HOT_COLORS.get(cls) or COLD_COLORS[cls]))
        if cls >= 2:
            hot.append((zc, cell))
    hot.sort(reverse=True)

    reasons = []
    if hot:
        reasons = await _query(
            f"""SELECT COALESCE(failure_reason, 'unknown') AS reason, COUNT(*)::int AS n
                FROM analytics.deliveries
                WHERE status = 'failed' AND h3_r{res} = ANY(%(cells)s)
                  AND attempted_at >= now() - make_interval(days => %(days)s)
                GROUP BY 1 ORDER BY 2 DESC LIMIT 5""",
            {"cells": [c for _, c in hot], "days": req.days})

    top = [{"lon": round(_cell_center(c)[0], 5), "lat": round(_cell_center(c)[1], 5), "z": round(zc, 2),
            "failed": stats_by_cell[c]["f"], "attempts": stats_by_cell[c]["n"]} for zc, c in hot[:5]]
    n_cold = sum(1 for zc in z.values() if _z_class(zc) <= -2)
    return {
        "summary": {"method": "Getis-Ord Gi*", "metric": req.metric, "days": req.days,
                    "cells_analyzed": len(z), "hot_cells_95": len(hot), "cold_cells_95": n_cold,
                    "top_hotspots": top, "failure_reasons_in_hotspots": reasons},
        "stats": [["Hexagons analysed", f"{len(z):,}"], ["Hot spots (95%+)", str(len(hot))],
                  ["Cold spots (95%+)", str(n_cold)],
                  ["Top reason in hot spots", f"{reasons[0]['reason']} ({reasons[0]['n']})" if reasons else "—"]],
        "legend": [{"color": HOT_COLORS[3], "label": "Hot spot, 99%"},
                   {"color": HOT_COLORS[2], "label": "Hot spot, 95%"},
                   {"color": HOT_COLORS[1], "label": "Hot spot, 90%"},
                   {"color": COLD_COLORS[-2], "label": "Cold spot, 95%+"}],
        "layers": [_layer("polygon", "Failure hot spots", feats, opacity=0.6)],
    }


async def _hotspots_dbscan(req: HotspotReq, bbox, p) -> dict:
    """Density clusters of failed attempts (ST_ClusterDBSCAN). Clusters must
    involve at least 3 different addresses, so one bad address is not a 'cluster'."""
    lat_c = (bbox[1] + bbox[3]) / 2
    p.update(eps=req.eps_m / math.cos(math.radians(lat_c)),   # metres → Web-Mercator units
             minpts=req.min_points)
    rows = await _query(
        """WITH f AS (
               SELECT id, address_id, failure_reason, geom
               FROM analytics.deliveries
               WHERE status = 'failed'
                 AND geom && ST_MakeEnvelope(%(w)s, %(s)s, %(e)s, %(n)s, 4326)
                 AND attempted_at >= now() - make_interval(days => %(days)s)),
           c AS (
               SELECT *, ST_ClusterDBSCAN(ST_Transform(geom, 3857), eps := %(eps)s,
                                          minpoints := %(minpts)s) OVER () AS cid
               FROM f)
           SELECT cid, COUNT(*)::int AS failed, COUNT(DISTINCT address_id)::int AS addresses,
                  MODE() WITHIN GROUP (ORDER BY failure_reason) AS top_reason,
                  ST_X(ST_Centroid(ST_Collect(geom))) AS lon, ST_Y(ST_Centroid(ST_Collect(geom))) AS lat,
                  ST_AsGeoJSON(ST_Buffer(ST_ConvexHull(ST_Collect(geom))::geography, 40)::geometry, 6) AS hull
           FROM c WHERE cid IS NOT NULL
           GROUP BY cid HAVING COUNT(DISTINCT address_id) >= 3
           ORDER BY failed DESC LIMIT 50""", p)

    import json as _json
    feats = [{"type": "Feature", "geometry": _json.loads(r["hull"]),
              "properties": {"failed": r["failed"], "addresses": r["addresses"],
                             "color": HOT_COLORS[3 if i < 3 else 2 if i < 10 else 1]}}
             for i, r in enumerate(rows)]
    pins = [_point(r["lon"], r["lat"], label=str(i + 1),
                   title=f"{r['failed']} failed · {r['addresses']} addresses · {r['top_reason'] or 'unknown'}")
            for i, r in enumerate(rows[:10])]
    return {
        "summary": {"method": "DBSCAN", "eps_m": req.eps_m, "min_points": req.min_points,
                    "clusters": len(rows),
                    "top_clusters": [{"lon": round(r["lon"], 5), "lat": round(r["lat"], 5),
                                      "failed": r["failed"], "addresses": r["addresses"],
                                      "top_reason": r["top_reason"]} for r in rows[:5]]},
        "stats": [["Clusters", str(len(rows))],
                  ["Largest cluster", f"{rows[0]['failed']} failures" if rows else "—"],
                  ["Neighbour distance", f"{req.eps_m:.0f} m"]],
        "legend": [{"color": HOT_COLORS[3], "label": "Top 3 clusters"},
                   {"color": HOT_COLORS[2], "label": "Clusters 4–10"},
                   {"color": HOT_COLORS[1], "label": "Other clusters"}],
        "layers": [_layer("polygon", "Failure clusters", feats, opacity=0.45),
                   _layer("points", "Cluster ranks", pins, color="#ff3b3b")],
    }


# =============================================================================
# 4. SITE SELECTION — Maximal Coverage Location Problem (MCLP)
#    "Choose N sites so that the most demand is within T minutes of a site."
# =============================================================================

def _solve_exact(cover_sets, weights, n_sites, time_limit_s=20):
    """Optimal MCLP with OR-Tools CP-SAT. Returns chosen candidate indices or None."""
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        return None
    m = cp_model.CpModel()
    x = [m.NewBoolVar(f"x{i}") for i in range(len(cover_sets))]
    covering = {}
    for i, cs in enumerate(cover_sets):
        for j in cs:
            covering.setdefault(j, []).append(i)
    y = {j: m.NewBoolVar(f"y{j}") for j in covering}
    for j, cands in covering.items():
        m.Add(y[j] <= sum(x[i] for i in cands))
    m.Add(sum(x) == n_sites)
    scale = 100   # CP-SAT needs integer weights
    m.Maximize(sum(int(round(weights[j] * scale)) * y[j] for j in covering))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_workers = 4
    if solver.Solve(m) not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    return [i for i in range(len(x)) if solver.Value(x[i])]


@heavy
async def site_selection(req: SiteReq) -> dict:
    bbox, res = _bbox(req.area), _res(req.resolution)
    reach_m = MAX_SPEED_KMH[req.profile] * 1000 / 60 * req.minutes

    cells = await _demand_cells(bbox, res)
    if not cells:
        raise AnalyticsError("No demand points in this area. Load analytics.demand_points first.")
    cell_ids = list(cells)
    xs = np.array([_cell_center(c)[0] for c in cell_ids])
    ys = np.array([_cell_center(c)[1] for c in cell_ids])
    weights = [cells[c] for c in cell_ids]
    failures = 0

    # 1. Demand already served by existing facilities counts as covered
    already, existing = set(), []
    if req.existing_facility_type:
        ftype = _facility_type(req.existing_facility_type)
        existing = await _facilities(bbox, ftype, pad_m=reach_m)
        if existing:
            geoms, failures = await _isochrones([(f["lon"], f["lat"]) for f in existing],
                                                [req.minutes], req.profile, req.direction)
            for g in geoms:
                if g is not None:
                    already.update(int(j) for j in _inside(g[0], xs, ys))

    # 2. Candidate sites: given by the user, or the centres of coarser (res-1)
    #    hexagons with the most demand — one candidate every ~1-2 km
    if req.candidates:
        cands = [(float(c["lon"]), float(c["lat"]), c.get("label") or f"Candidate {i + 1}")
                 for i, c in enumerate(req.candidates[:req.max_candidates])]
    else:
        parent_w: dict[str, float] = {}
        for c, w in cells.items():
            p = h3.cell_to_parent(c, res - 1)
            parent_w[p] = parent_w.get(p, 0) + w
        best_parents = sorted(parent_w, key=lambda p: -parent_w[p])[:req.max_candidates]
        cands = [(*_cell_center(p), f"Hex {p[-6:]}") for p in best_parents]

    # 3. Which demand cells can each candidate serve within the time limit?
    geoms, cand_fail = await _isochrones([(lo, la) for lo, la, _ in cands],
                                         [req.minutes], req.profile, req.direction)
    failures += cand_fail
    cover_sets = [set() if g is None else {int(j) for j in _inside(g[0], xs, ys)} - already
                  for g in geoms]

    # 4. Greedy: repeatedly add the candidate that covers the most NEW demand.
    #    Simple, fast, and proven to reach ≥ 63% of the optimum (usually ~95%+).
    covered, chosen, gains = set(), [], []
    for _ in range(req.n_sites):
        best_i, best_gain = None, 0.0
        for i, cs in enumerate(cover_sets):
            if i in chosen:
                continue
            g = sum(weights[j] for j in cs - covered)
            if g > best_gain:
                best_i, best_gain = i, g
        if best_i is None:
            break                                    # nothing left to gain
        chosen.append(best_i)
        gains.append(best_gain)
        covered |= cover_sets[best_i]

    method_used = "greedy"
    if req.method == "exact":
        opt = await asyncio.to_thread(_solve_exact, cover_sets, weights, len(chosen) or req.n_sites)
        if opt is not None:
            opt_cov = set().union(*[cover_sets[i] for i in opt]) if opt else set()
            if sum(weights[j] for j in opt_cov) > sum(weights[j] for j in covered) + 1e-6:
                # re-order the optimal set by marginal gain so ranks still make sense
                chosen, gains, covered = [], [], set()
                for _ in opt:
                    i = max((i for i in opt if i not in chosen),
                            key=lambda i: sum(weights[j] for j in cover_sets[i] - covered))
                    gains.append(sum(weights[j] for j in cover_sets[i] - covered))
                    chosen.append(i)
                    covered |= cover_sets[i]
            method_used = "exact (OR-Tools)"
        else:
            method_used = "greedy (OR-Tools not installed or no solution)"

    total = sum(weights)
    before = sum(weights[j] for j in already)
    after = before + sum(gains)

    # 5. Map: which site covers each cell (first chosen wins)
    owner = {}
    for rank, i in enumerate(chosen):
        for j in cover_sets[i]:
            owner.setdefault(j, rank)
    feats = []
    for j, cell in enumerate(cell_ids):
        if j in already:
            color, who = "#3b9eff", "existing"
        elif j in owner:
            color, who = SITE_COLORS[owner[j] % len(SITE_COLORS)], f"site {owner[j] + 1}"
        else:
            color, who = "#5b6577", "uncovered"
        feats.append(_hex_feature(cell, demand=round(weights[j], 1), served_by=who, color=color))

    sites, cum = [], before
    for rank, (i, g) in enumerate(zip(chosen, gains), start=1):
        cum += g
        sites.append({"rank": rank, "lon": round(cands[i][0], 6), "lat": round(cands[i][1], 6),
                      "label": cands[i][2], "added_demand": round(g),
                      "cumulative_coverage_pct": round(cum / total * 100, 1)})
    site_pins = [_point(s["lon"], s["lat"], label=str(s["rank"]),
                        color=SITE_COLORS[(s["rank"] - 1) % len(SITE_COLORS)],
                        title=f"Site {s['rank']}: +{s['added_demand']:,} demand → {s['cumulative_coverage_pct']}%")
                 for s in sites]
    exist_pins = [_point(f["lon"], f["lat"], label="+", title=f["name"]) for f in existing]

    return {
        "summary": {"method": method_used, "minutes": req.minutes, "profile": req.profile,
                    "candidates_evaluated": len(cands), "travel_areas_failed": failures, "total_demand": round(total),
                    "coverage_before_pct": round(before / total * 100, 1),
                    "coverage_after_pct": round(after / total * 100, 1),
                    "sites": sites,
                    "note": "Candidate points are hexagon centres; check land availability on site."},
        "stats": [["Coverage before", f"{before / total * 100:.1f}%"],
                  ["Coverage after", f"{after / total * 100:.1f}%"],
                  ["Sites chosen", f"{len(sites)} of {req.n_sites}"],
                  ["Candidates evaluated", str(len(cands))], ["Method", method_used],
                  *([["Note", "Nothing left to cover: existing sites already reach all demand "
                              "within the time limit. Try a shorter time limit."]]
                    if not sites and before >= total - 1e-6 else []),
                  *_failed_note(failures, len(cands) + len(existing))],
        "legend": ([{"color": "#3b9eff", "label": "Already covered (existing)"}] if req.existing_facility_type else [])
                  + [{"color": SITE_COLORS[k % len(SITE_COLORS)], "label": f"Covered by site {k + 1}"}
                     for k in range(len(sites))]
                  + [{"color": "#5b6577", "label": "Still uncovered"}],
        "layers": [_layer("polygon", "Site coverage", feats, opacity=0.5),
                   _layer("points", "Proposed sites", site_pins, color="#a855f7"),
                   *([_layer("points", "Existing facilities", exist_pins, color="#0f172a")] if exist_pins else [])],
    }


# =============================================================================
# 5. DELIVERY-POINT LEARNING
#    For each address: where do drivers REALLY stand when they deliver?
#    1. take successful deliveries with GPS (ignore GPS > max_offset_m away)
#    2. median point of the drops
#    3. drop outliers farther than inlier_radius_m from that median
#    4. median again = learned point; spread = median distance to it
#    confidence = enough data × few outliers × tight spread
# =============================================================================

LEARN_SQL = """
WITH drops AS (
    SELECT d.address_id, d.drop_geom
    FROM analytics.deliveries d
    JOIN analytics.demand_points a ON a.id = d.address_id
    WHERE d.status = 'delivered' AND d.drop_geom IS NOT NULL
      AND ST_DWithin(d.drop_geom::geography, a.geom::geography, %(max_offset)s)
),
m1 AS (
    SELECT address_id, COUNT(*) AS n_total,
           ST_SetSRID(ST_MakePoint(
               percentile_cont(0.5) WITHIN GROUP (ORDER BY ST_X(drop_geom)),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY ST_Y(drop_geom))), 4326) AS c1
    FROM drops GROUP BY address_id HAVING COUNT(*) >= %(min_n)s
),
inliers AS (
    SELECT d.address_id, d.drop_geom, m1.n_total
    FROM drops d JOIN m1 USING (address_id)
    WHERE ST_DWithin(d.drop_geom::geography, m1.c1::geography, %(inlier)s)
),
m2 AS (
    SELECT address_id, MAX(n_total) AS n_total, COUNT(*) AS n_used,
           ST_SetSRID(ST_MakePoint(
               percentile_cont(0.5) WITHIN GROUP (ORDER BY ST_X(drop_geom)),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY ST_Y(drop_geom))), 4326) AS learned
    FROM inliers GROUP BY address_id HAVING COUNT(*) >= %(min_n)s
),
spread AS (
    SELECT i.address_id,
           percentile_cont(0.5) WITHIN GROUP (
               ORDER BY ST_Distance(i.drop_geom::geography, m2.learned::geography)) AS spread_m
    FROM inliers i JOIN m2 USING (address_id) GROUP BY i.address_id
),
upsert AS (
    INSERT INTO analytics.learned_delivery_points AS l
        (address_id, official_geom, learned_geom, n_used, n_total, offset_m, spread_m, confidence, updated_at)
    SELECT m2.address_id, a.geom, m2.learned, m2.n_used, m2.n_total,
           ST_Distance(a.geom::geography, m2.learned::geography),
           s.spread_m,
           ROUND((LEAST(1.0, m2.n_used / 5.0)
                  * (m2.n_used::float / m2.n_total)
                  * EXP(-s.spread_m / 40.0))::numeric, 3),
           now()
    FROM m2 JOIN spread s USING (address_id)
    JOIN analytics.demand_points a ON a.id = m2.address_id
    ON CONFLICT (address_id) DO UPDATE SET
        official_geom = EXCLUDED.official_geom, learned_geom = EXCLUDED.learned_geom,
        n_used = EXCLUDED.n_used, n_total = EXCLUDED.n_total, offset_m = EXCLUDED.offset_m,
        spread_m = EXCLUDED.spread_m, confidence = EXCLUDED.confidence, updated_at = now(),
        -- a reviewed point goes back to 'pending' only if it moved more than 10 m
        review_status = CASE WHEN ST_Distance(l.learned_geom::geography, EXCLUDED.learned_geom::geography) > 10
                             THEN 'pending' ELSE l.review_status END
    RETURNING offset_m, confidence
)
SELECT COUNT(*)::int AS addresses,
       COALESCE(percentile_cont(0.5) WITHIN GROUP (ORDER BY offset_m), 0)::float AS median_offset_m,
       COUNT(*) FILTER (WHERE offset_m > 30)::int  AS over_30m,
       COUNT(*) FILTER (WHERE offset_m > 100)::int AS over_100m,
       COUNT(*) FILTER (WHERE confidence >= 0.5)::int AS confident
FROM upsert
"""


async def learn_delivery_points(req: LearnReq) -> dict:
    t0 = time.time()
    rows = await _query(LEARN_SQL, {"max_offset": req.max_offset_m, "min_n": req.min_deliveries,
                                    "inlier": req.inlier_radius_m})
    r = rows[0] if rows else {}
    return {"summary": {**r, "seconds": round(time.time() - t0, 1)},
            "stats": [["Addresses learned", f"{r.get('addresses', 0):,}"],
                      ["Median offset", f"{r.get('median_offset_m', 0):.1f} m"],
                      ["Offset > 30 m", f"{r.get('over_30m', 0):,}"],
                      ["Offset > 100 m", f"{r.get('over_100m', 0):,}"],
                      ["Confident (≥ 0.5)", f"{r.get('confident', 0):,}"]],
            "legend": [], "layers": []}


async def delivery_point_offsets(req: OffsetsReq) -> dict:
    bbox = _bbox(req.area)
    rows = await _query(
        """SELECT l.address_id, a.short_address, l.offset_m, l.spread_m, l.confidence, l.n_used,
                  l.review_status,
                  ST_X(l.official_geom) AS olon, ST_Y(l.official_geom) AS olat,
                  ST_X(l.learned_geom)  AS llon, ST_Y(l.learned_geom)  AS llat
           FROM analytics.learned_delivery_points l
           JOIN analytics.demand_points a ON a.id = l.address_id
           WHERE l.official_geom && ST_MakeEnvelope(%(w)s, %(s)s, %(e)s, %(n)s, 4326)
             AND l.offset_m >= %(min_off)s AND l.confidence >= %(min_conf)s
           ORDER BY l.offset_m DESC LIMIT %(limit)s""",
        {**dict(zip("wsen", bbox)), "min_off": req.min_offset_m,
         "min_conf": req.min_confidence, "limit": req.limit})

    def col(off):
        return "#f43f5e" if off > 100 else "#f5a623" if off > 50 else "#facc15"

    lines = [{"type": "Feature",
              "geometry": {"type": "LineString", "coordinates": [[r["olon"], r["olat"]], [r["llon"], r["llat"]]]},
              "properties": {"color": col(r["offset_m"])}} for r in rows]
    learned_pins = [_point(r["llon"], r["llat"], label="●",
                           title=f"{r['short_address'] or r['address_id']}: moved {r['offset_m']:.0f} m "
                                 f"(confidence {r['confidence']:.2f}, {r['n_used']} drops)")
                    for r in rows[:200]]     # markers are heavier than lines; cap them
    offs = [r["offset_m"] for r in rows]
    return {
        "summary": {"addresses": len(rows),
                    "median_offset_m": round(sorted(offs)[len(offs) // 2], 1) if offs else 0,
                    "largest": [{"address": r["short_address"] or r["address_id"], "offset_m": round(r["offset_m"]),
                                 "confidence": r["confidence"], "learned_lon": round(r["llon"], 6),
                                 "learned_lat": round(r["llat"], 6)} for r in rows[:5]]},
        "stats": [["Addresses to review", f"{len(rows):,}"],
                  ["Median offset", f"{sorted(offs)[len(offs) // 2]:.0f} m" if offs else "—"],
                  ["Largest offset", f"{offs[0]:.0f} m" if offs else "—"]],
        "legend": [{"color": "#facc15", "label": f"{req.min_offset_m:.0f}–50 m"},
                   {"color": "#f5a623", "label": "50–100 m"},
                   {"color": "#f43f5e", "label": "> 100 m"}],
        "layers": [_layer("line", "Official → learned point", lines, color="#f5a623"),
                   _layer("points", "Learned delivery points", learned_pins, color="#22d98a")],
    }


# =============================================================================
# HTTP ENDPOINTS
# =============================================================================

async def _run(func, req, request: Request | None = None):
    """Run an analysis. If the browser disconnects (closed tab, pressed Clear,
    nginx timeout), cancel the work so Valhalla isn't loaded for nobody."""
    t0 = time.time()
    task = asyncio.create_task(func(req))
    try:
        while request is not None and not task.done():
            await asyncio.wait({task}, timeout=1.0)
            if not task.done() and await request.is_disconnected():
                task.cancel()
                logger.info("Client disconnected; cancelled %s after %.1fs", func.__name__, time.time() - t0)
                raise HTTPException(499, "Client closed the request.")
        out = await task
    except BusyError as exc:
        raise HTTPException(429, str(exc))
    except AnalyticsError as exc:
        raise HTTPException(400, str(exc))
    out["seconds"] = round(time.time() - t0, 2)
    logger.info("%s finished in %.2fs", func.__name__, out["seconds"])
    return out


@router.post("/hexbin")
async def ep_hexbin(req: HexbinReq, request: Request):         return await _run(hexbin, req, request)


@router.post("/coverage")
async def ep_coverage(req: CoverageReq, request: Request):     return await _run(coverage, req, request)


@router.post("/hotspots")
async def ep_hotspots(req: HotspotReq, request: Request):      return await _run(hotspots, req, request)


@router.post("/site_selection")
async def ep_site_selection(req: SiteReq, request: Request):   return await _run(site_selection, req, request)


@router.post("/delivery_points/offsets")
async def ep_offsets(req: OffsetsReq, request: Request):       return await _run(delivery_point_offsets, req, request)


@router.post("/delivery_points/learn")
async def ep_learn(req: LearnReq, x_admin_token: str | None = Header(None)):
    """Re-learn all delivery points. Writes to the database, so it is protected
    by ANALYTICS_ADMIN_TOKEN when that variable is set. Run nightly via cron."""
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(401, "Admin token required (header X-Admin-Token).")
    return await _run(learn_delivery_points, req)


@router.get("/status")
async def ep_status():
    """Row counts — quick check that data is loaded."""
    rows = await _query(
        """SELECT (SELECT COUNT(*) FROM analytics.demand_points)::int AS demand_points,
                  (SELECT COUNT(*) FROM analytics.demand_points WHERE h3_r8 IS NULL)::int AS demand_missing_h3,
                  (SELECT COUNT(*) FROM analytics.deliveries)::int AS deliveries,
                  (SELECT COUNT(*) FROM analytics.deliveries WHERE h3_r8 IS NULL)::int AS deliveries_missing_h3,
                  (SELECT COUNT(*) FROM analytics.learned_delivery_points)::int AS learned_points""")
    return {**rows[0], "heavy_analysis_running": _HEAVY_SEM.locked(),
            "valhalla_slots_free": _VALHALLA_SEM._value, "isochrone_cache_size": len(_iso_cache)}


# =============================================================================
# AGENT TOOLS — imported by agent.py. Read-only: the agent never runs /learn.
# =============================================================================

_AREA_SCHEMA = {
    "type": "object",
    "description": "Area to analyse. Use the current map view bbox from the context unless the user names "
                   "a point and radius.",
    "properties": {
        "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
                 "description": "[west, south, east, north]"},
        "center": {"type": "object", "properties": {"lon": {"type": "number"}, "lat": {"type": "number"}}},
        "radius_km": {"type": "number", "maximum": 40},
    },
}
_FTYPES = sorted(core._ALLOWED_FACILITY_TYPES)

AGENT_TOOLS = [
    {"name": "area_heatmap",
     "description": "H3 hexagon heatmap of demand (addresses) or deliveries (attempts, failed attempts, "
                    "failure rate) in an area. Use for 'where is demand', 'where do deliveries fail'.",
     "input_schema": {"type": "object", "required": ["area"], "properties": {
         "area": _AREA_SCHEMA,
         "dataset": {"type": "string", "enum": ["demand", "deliveries"], "default": "demand"},
         "metric": {"type": "string", "enum": ["count", "failed", "failure_rate"], "default": "count"},
         "resolution": {"type": "integer", "enum": [7, 8, 9], "default": 8},
         "days": {"type": "integer", "default": 30}}}},
    {"name": "coverage_analysis",
     "description": "Share of demand (addresses) within N minutes of the nearest existing facility of a type, "
                    "with the largest uncovered areas. direction 'to_facility' = people travel there (clinic), "
                    "'from_facility' = service travels out (ambulance, fire).",
     "input_schema": {"type": "object", "required": ["area", "facility_type"], "properties": {
         "area": _AREA_SCHEMA,
         "facility_type": {"type": "string", "enum": _FTYPES},
         "minutes": {"type": "integer", "minimum": 1, "maximum": 60, "default": 15},
         "profile": {"type": "string", "enum": ["auto", "bicycle", "pedestrian"], "default": "auto"},
         "direction": {"type": "string", "enum": ["to_facility", "from_facility"], "default": "to_facility"}}}},
    {"name": "find_hotspots",
     "description": "Statistically significant hot spots of FAILED deliveries (Getis-Ord Gi* on hexagons), "
                    "or density clusters (method 'dbscan'), with the main failure reasons.",
     "input_schema": {"type": "object", "required": ["area"], "properties": {
         "area": _AREA_SCHEMA,
         "method": {"type": "string", "enum": ["gi_star", "dbscan"], "default": "gi_star"},
         "metric": {"type": "string", "enum": ["failure_rate", "failed"], "default": "failure_rate",
                    "description": "failure_rate = where failures are unusually common (default). "
                                   "failed = raw counts, mostly reflects delivery volume."},
         "days": {"type": "integer", "default": 30}}}},
    {"name": "suggest_sites",
     "description": "Recommend locations for N NEW sites (lockers, branches, clinics, hubs) that cover the most "
                    "demand within T minutes, optionally on top of existing facilities of a type. "
                    "Returns ranked sites with coverage before/after. Can take 30-90 s on big areas.",
     "input_schema": {"type": "object", "required": ["area", "n_sites"], "properties": {
         "area": _AREA_SCHEMA,
         "n_sites": {"type": "integer", "minimum": 1, "maximum": 20},
         "minutes": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
         "profile": {"type": "string", "enum": ["auto", "bicycle", "pedestrian"], "default": "auto"},
         "existing_facility_type": {"type": "string", "enum": _FTYPES},
         "method": {"type": "string", "enum": ["greedy", "exact"], "default": "greedy"}}}},
    {"name": "delivery_point_corrections",
     "description": "Addresses whose official pin is far from where drivers actually deliver (learned from GPS), "
                    "for review. Read-only.",
     "input_schema": {"type": "object", "required": ["area"], "properties": {
         "area": _AREA_SCHEMA,
         "min_offset_m": {"type": "number", "default": 30},
         "min_confidence": {"type": "number", "default": 0.5}}}},
]


def _agent_wrap(func, model):
    async def tool(**kwargs):
        try:
            out = await func(model(**kwargs))
        except AnalyticsError as exc:
            return {"error": str(exc)}, []
        except ValueError as exc:                     # pydantic validation
            return {"error": f"Invalid arguments: {exc}"}, []
        return out["summary"], out["layers"]
    return tool


AGENT_FUNCS = {
    "area_heatmap":               _agent_wrap(hexbin, HexbinReq),
    "coverage_analysis":          _agent_wrap(coverage, CoverageReq),
    "find_hotspots":              _agent_wrap(hotspots, HotspotReq),
    "suggest_sites":              _agent_wrap(site_selection, SiteReq),
    "delivery_point_corrections": _agent_wrap(delivery_point_offsets, OffsetsReq),
}