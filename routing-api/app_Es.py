"""
GeoRouting Lab — app.py
FastAPI proxy/orchestration layer in front of:
  • Valhalla       (routing, matrix, isochrone)
  • VROOM          (VRP / TSP optimisation)
  • Elasticsearch  (nearest-facility geo_distance query)

Key rules
---------
* Never use `async with client` inside handlers — the shared client must stay open.
* Surface upstream HTTP errors with their original status codes.
* /optimize_route handles both VRP (vehicle has start+end) and TSP (open tour, no end).
"""

import asyncio
import math
import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("georouting")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
VALHALLA_URL = os.getenv("VALHALLA_URL", "http://valhalla:8002")
VROOM_URL    = os.getenv("VROOM_URL",    "http://vroom:3000")

# Elasticsearch — used for the nearest-facility geo_distance query.
# ES_URL must point to the Elasticsearch HTTP endpoint (no trailing slash).
# ES_FACILITY_INDEX is the index that holds facility documents.
ES_URL            = os.getenv("ES_URL",            "http://localhost:9200")
ES_FACILITY_INDEX = os.getenv("ES_FACILITY_INDEX", "facilities")

# Valhalla costing model for facility routing
VALHALLA_COSTING = "auto"

# How many candidates to fetch from ES before travel-time ranking.
# We over-fetch (limit × this factor) because crow-fly ≠ drive-time order.
CANDIDATE_MULTIPLIER = 3

# Seconds before a single Valhalla call is abandoned
VALHALLA_TIMEOUT_S = 25.0

# Whitelist — prevents SQL injection via the `type` query parameter
_ALLOWED_FACILITY_TYPES = frozenset({"hospital", "fire station", "police", "clinic"})


# ---------------------------------------------------------------------------
# Lifespan — shared async HTTP client
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
    yield
    await client.aclose()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="GeoRouting Lab API",
    description="Proxy for Valhalla routing + VROOM optimisation + Elasticsearch facility search",
    version="3.3.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client: httpx.AsyncClient = None  # type: ignore



# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
async def _forward(method: str, url: str, payload: dict) -> dict:
    """
    Forward a JSON payload to an upstream service.
    Preserves the upstream status code and body in any HTTPException raised.
    """
    try:
        resp = await client.request(method, url, json=payload)
    except httpx.RequestError as exc:
        logger.error("Upstream unreachable: %s -> %s", url, exc)
        raise HTTPException(status_code=503, detail=f"Upstream unreachable: {exc}")

    if resp.status_code != 200:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text or f"Upstream returned {resp.status_code}"
        logger.warning("Upstream error %d from %s: %s", resp.status_code, url, detail)
        raise HTTPException(status_code=resp.status_code, detail=detail)

    return resp.json()


def _valhalla_location(lon: float, lat: float) -> dict:
    return {"lon": lon, "lat": lat}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine great-circle distance in kilometres (fallback when ES sort value absent)."""
    R  = 6371.0
    dL = math.radians(lat2 - lat1)
    dO = math.radians(lon2 - lon1)
    a  = math.sin(dL / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dO / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/")
async def root():
    return {"message": "GeoRouting Lab API", "services": ["Valhalla", "VROOM", "Elasticsearch"]}

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/health/valhalla")
async def health_valhalla():
    try:
        resp = await client.get(f"{VALHALLA_URL}/status")
        return {"status": "ok", "detail": resp.json()}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))

@app.get("/health/vroom")
async def health_vroom():
    try:
        resp = await client.get(f"{VROOM_URL}/health")
        return {"status": "ok", "detail": resp.json()}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))

@app.get("/health/elasticsearch")
async def health_elasticsearch():
    try:
        resp = await client.get(f"{ES_URL}/_cluster/health")
        return {"status": "ok", "detail": resp.json()}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ---------------------------------------------------------------------------
# Valhalla — Route
# ---------------------------------------------------------------------------
@app.post("/route")
async def valhalla_route(request: Request):
    """
    Point-to-point or multi-stop route.
    Payload locations must use {lat, lon} objects (not arrays).
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    logger.debug("Route request: %s", payload)
    return await _forward("POST", f"{VALHALLA_URL}/route", payload)


# ---------------------------------------------------------------------------
# Valhalla — Matrix
# ---------------------------------------------------------------------------
@app.post("/matrix")
async def valhalla_matrix(request: Request):
    """Time/distance cost matrix."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    return await _forward("POST", f"{VALHALLA_URL}/matrix", payload)


# ---------------------------------------------------------------------------
# Valhalla — Isochrone
# ---------------------------------------------------------------------------
@app.post("/isochrone")
async def valhalla_isochrone(request: Request):
    """
    Reachability polygons.
    Contour colors must be sent WITHOUT '#' prefix.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    return await _forward("POST", f"{VALHALLA_URL}/isochrone", payload)


# ---------------------------------------------------------------------------
# VROOM — Raw optimise
# ---------------------------------------------------------------------------
@app.post("/optimize")
async def vroom_optimize(request: Request):
    """Raw VROOM VRP/TSP. geometry flag injected server-side."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if "options" not in payload:
        payload["options"] = {}
    payload["options"]["g"] = True

    return await _forward("POST", VROOM_URL, payload)


# ---------------------------------------------------------------------------
# VROOM — Optimise + decode geometry → GeoJSON
# ---------------------------------------------------------------------------
@app.post("/optimize_route")
async def optimize_route(request: Request):
    """
    Optimise with VROOM then decode each route's encoded polyline into a
    GeoJSON FeatureCollection for direct MapLibre consumption.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if "options" not in payload:
        payload["options"] = {}
    payload["options"]["g"] = True

    vroom_result = await _forward("POST", VROOM_URL, payload)
    logger.debug("VROOM result routes: %d", len(vroom_result.get("routes", [])))

    features = []
    for route in vroom_result.get("routes", []):
        encoded = route.get("geometry")
        if encoded:
            coords = _decode_polyline6(encoded)
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {
                    "vehicle_id": route.get("vehicle"),
                    "duration":   route.get("duration"),
                    "distance":   route.get("distance"),
                }
            })

    return {
        "vroom":  vroom_result,
        "geojson": {"type": "FeatureCollection", "features": features}
    }


# ---------------------------------------------------------------------------
# NEAREST FACILITY — Elasticsearch geo_distance + Valhalla matrix + routes
# ---------------------------------------------------------------------------
#
# THREE-STEP ALGORITHM  (identical contract to the PostGIS version)
# ─────────────────────────────────────────────────────────────────
# Step 1  Elasticsearch geo_distance query: find up to
#         (limit × CANDIDATE_MULTIPLIER) facilities of the requested type
#         within max_distance_km, sorted by ascending crow-fly distance.
#         We over-fetch because crow-fly order ≠ drive-time order.
#
# Step 2  Valhalla /sources_to_targets (matrix): one HTTP call returns the
#         travel time from the incident to every candidate simultaneously.
#         Candidates are re-sorted by travel_seconds and trimmed to `limit`.
#
# Step 3  Valhalla /route (concurrent): one call per surviving facility,
#         run with asyncio.gather so total wait ≈ the slowest single call.
#         Returns per-maneuver GeoJSON LineString Features.
#
# ELASTICSEARCH INDEX CONTRACT
# ─────────────────────────────
# Index: $ES_FACILITY_INDEX  (default: "facilities")
#
# Required mapping:
#   {
#     "mappings": {
#       "properties": {
#         "id":       { "type": "keyword"   },
#         "name":     { "type": "text",
#                       "fields": { "keyword": { "type": "keyword" } } },
#         "type":     { "type": "keyword"   },  ← lowercase, matches _ALLOWED_FACILITY_TYPES
#         "address":  { "type": "text"      },
#         "location": { "type": "geo_point" }   ← { "lat": …, "lon": … }
#       }
#     }
#   }
#
# Example document:
#   {
#     "id":       "fac-001",
#     "name":     "King Fahad Medical City",
#     "type":     "hospital",
#     "address":  "Northern Ring Branch Rd, Riyadh",
#     "location": { "lat": 24.7611, "lon": 46.6653 }
#   }
# ---------------------------------------------------------------------------

@app.get("/nearest_facility", summary="Find and route to nearest POIs via Elasticsearch + Valhalla")
async def nearest_facility(
    lon:             Annotated[float, Query(ge=-180, le=180)],
    lat:             Annotated[float, Query(ge=-90,  le=90)],
    type:            str   = "hospital",
    limit:           Annotated[int,   Query(ge=1, le=10)]   = 5,
    max_distance_km: Annotated[float, Query(ge=1, le=20)]   = 5.0,
    routes:          bool  = True,
):
    """
    Return the k nearest facilities of a given type, ranked by driving time,
    with optional per-maneuver route geometry powered by Valhalla.

    Query parameters
    ----------------
    lon, lat           — incident location (WGS-84)
    type               — hospital | fire station | police | clinic
    limit              — 1–10 results (default 5)
    max_distance_km    — search radius 1–20 km (default 5)
    routes             — include GeoJSON route geometry (default true)

    Response (unchanged from PostGIS version — frontend needs no changes)
    --------
    {
      incident: {lon, lat},
      type, search_radius_km, count,
      facilities: [
        {
          id, name, type, address,
          facility_lon, facility_lat,
          travel_minutes, travel_seconds,
          crow_distance_km,
          route: { type: "FeatureCollection", features: [...] }
        }, ...
      ]
    }
    """
    facility_type = type.lower().strip()
    if facility_type not in _ALLOWED_FACILITY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"type must be one of: {', '.join(sorted(_ALLOWED_FACILITY_TYPES))}."
        )

    # ------------------------------------------------------------------
    # STEP 1 — Elasticsearch geo_distance query
    # ------------------------------------------------------------------
    # bool/filter query: no relevance scoring, just spatial + type filter.
    # _geo_distance sort returns the exact crow-fly distance in km for each
    # hit as sort[0], which we surface as crow_distance_km in the response.
    #
    # WHY filter NOT must/should?
    #   Filter clauses bypass scoring entirely and are cached by ES —
    #   much faster for purely geo + keyword queries.
    #
    # WHY term NOT match on `type`?
    #   `type` is a keyword field; term is an exact match, no analysis.
    #   The whitelist already lower-cased the value, so this is safe.
    # ------------------------------------------------------------------
    candidate_limit = limit * CANDIDATE_MULTIPLIER

    es_query = {
        "size": candidate_limit,
        "query": {
            "bool": {
                "filter": [
                    {
                        "geo_distance": {
                            "distance": f"{max_distance_km}km",
                            "location": {"lat": lat, "lon": lon},
                        }
                    },
                    {
                        "term": {"type": facility_type}
                    },
                ]
            }
        },
        "sort": [
            {
                "_geo_distance": {
                    "location":      {"lat": lat, "lon": lon},
                    "order":         "asc",
                    "unit":          "km",
                    "distance_type": "arc",
                }
            }
        ],
        "_source": ["id", "name", "type", "address", "location"],
    }

    try:
        es_resp = await client.post(
            f"{ES_URL}/{ES_FACILITY_INDEX}/_search",
            json=es_query,
            timeout=15.0,
        )
    except httpx.RequestError as exc:
        logger.error("Elasticsearch unreachable: %s", exc)
        raise HTTPException(status_code=503, detail="Search service unavailable.")

    if es_resp.status_code != 200:
        logger.error("ES error %d: %s", es_resp.status_code, es_resp.text[:300])
        raise HTTPException(status_code=502, detail="Search service returned an error.")

    hits = es_resp.json().get("hits", {}).get("hits", [])

    if not hits:
        return {
            "message":    f"No {facility_type} found within {max_distance_km} km.",
            "count":      0,
            "facilities": [],
        }

    # Parse each ES hit into a flat candidate dict.
    # sort[0] = crow-fly distance in km (from _geo_distance sort).
    # location may be {"lat":…,"lon":…} dict OR "lat,lon" string.
    candidates = []
    for hit in hits:
        src = hit.get("_source", {})
        loc = src.get("location", {})

        if isinstance(loc, str):
            parts = loc.split(",")
            f_lat = float(parts[0].strip())
            f_lon = float(parts[1].strip())
        else:
            f_lat = float(loc.get("lat", 0))
            f_lon = float(loc.get("lon", 0))

        sort_vals    = hit.get("sort", [None])
        crow_dist_km = (
            round(float(sort_vals[0]), 2)
            if sort_vals and sort_vals[0] is not None
            else round(_haversine_km(lat, lon, f_lat, f_lon), 2)
        )

        candidates.append({
            "id":               hit.get("_id") or src.get("id", ""),
            "name":             src.get("name", "Unknown"),
            "type":             src.get("type", facility_type),
            "address":          src.get("address", ""),
            "facility_lat":     f_lat,
            "facility_lon":     f_lon,
            "crow_distance_km": crow_dist_km,
        })

    logger.info(
        "ES returned %d candidate(s) for type=%s within %.1f km",
        len(candidates), facility_type, max_distance_km,
    )

    # ------------------------------------------------------------------
    # STEP 2 — Valhalla matrix: one HTTP call for all travel times
    # ------------------------------------------------------------------
    targets = [
        _valhalla_location(c["facility_lon"], c["facility_lat"])
        for c in candidates
    ]

    matrix_payload = {
        "sources": [_valhalla_location(lon, lat)],
        "targets": targets,
        "costing": VALHALLA_COSTING,
        "units":   "km",
    }

    try:
        matrix_resp = await _forward(
            "POST", f"{VALHALLA_URL}/sources_to_targets", matrix_payload
        )
    except HTTPException as exc:
        logger.warning("Valhalla matrix failed: %s", exc.detail)
        raise HTTPException(status_code=502, detail="Routing matrix unavailable.")

    time_row = matrix_resp.get("sources_to_targets", [[]])[0]

    ranked = []
    for candidate, entry in zip(candidates, time_row):
        t_sec = entry.get("time")
        if t_sec is None:
            continue
        row = dict(candidate)
        row["travel_seconds"] = round(float(t_sec), 1)
        row["travel_minutes"] = round(float(t_sec) / 60.0, 1)
        ranked.append(row)

    ranked.sort(key=lambda x: x["travel_seconds"])
    ranked = ranked[:limit]

    if not ranked:
        return {
            "message":    f"No routable {facility_type} found within {max_distance_km} km.",
            "count":      0,
            "facilities": [],
        }

    # ------------------------------------------------------------------
    # STEP 3 — Valhalla /route: geometry for each facility (concurrent)
    # ------------------------------------------------------------------
    if routes:
        async def _fetch_route(facility: dict, rank: int) -> dict:
            payload = {
                "locations": [
                    _valhalla_location(lon, lat),
                    _valhalla_location(facility["facility_lon"], facility["facility_lat"]),
                ],
                "costing":      VALHALLA_COSTING,
                "units":        "km",
                "shape_format": "geojson",
            }
            try:
                resp = await client.post(
                    f"{VALHALLA_URL}/route", json=payload, timeout=VALHALLA_TIMEOUT_S
                )
            except httpx.RequestError as exc:
                logger.warning("Route failed for %s: %s", facility["name"], exc)
                return {"type": "FeatureCollection", "features": []}

            if resp.status_code not in (200,):
                return {"type": "FeatureCollection", "features": []}

            trip = resp.json().get("trip", {})
            legs = trip.get("legs", [])
            if not legs:
                return {"type": "FeatureCollection", "features": []}

            leg        = legs[0]
            all_coords = leg.get("shape", [])
            maneuvers  = leg.get("maneuvers", [])
            features   = []
            for seq, m in enumerate(maneuvers):
                start_idx  = m.get("begin_shape_index", 0)
                end_idx    = m.get("end_shape_index", len(all_coords) - 1)
                seg_coords = all_coords[start_idx : end_idx + 1]
                if len(seg_coords) < 2:
                    continue
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": seg_coords},
                    "properties": {
                        "seq":           seq,
                        "length_m":      round(m.get("length", 0) * 1000, 2),
                        "time_s":        round(m.get("time", 0), 1),
                        "facility_name": facility["name"],
                        "facility_rank": rank,
                    },
                })
            return {"type": "FeatureCollection", "features": features}

        route_results = await asyncio.gather(
            *[_fetch_route(f, rank=i + 1) for i, f in enumerate(ranked)]
        )
        for entry, route_fc in zip(ranked, route_results):
            entry["route"] = route_fc
    else:
        for entry in ranked:
            entry["route"] = None

    return {
        "incident":         {"lon": lon, "lat": lat},
        "type":             facility_type,
        "search_radius_km": max_distance_km,
        "count":            len(ranked),
        "facilities":       ranked,
    }

# ---------------------------------------------------------------------------
# Polyline decoder — precision 6 (Valhalla / VROOM) → [[lng, lat], ...]
# ---------------------------------------------------------------------------
def _decode_polyline6(encoded: str) -> list:
    coords, index, lat, lng = [], 0, 0, 0
    length = len(encoded)
    while index < length:
        result, shift = 0, 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        lat += ~(result >> 1) if (result & 1) else (result >> 1)

        result, shift = 0, 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        lng += ~(result >> 1) if (result & 1) else (result >> 1)

        coords.append([lng / 1e6, lat / 1e6])
    return coords


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=5000, log_level="info")
