"""
GeoRouting Lab — app.py
FastAPI proxy/orchestration layer in front of:
  • Valhalla  (routing, matrix, isochrone)
  • VROOM     (VRP / TSP optimisation)
  • PostGIS   (nearest-facility spatial queries)

Key rules
---------
* Never use `async with client` inside handlers — the shared client must stay open.
* Surface upstream HTTP errors with their original status codes.
* /optimize_route handles both VRP (vehicle has start+end) and TSP (open tour, no end).
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated

import httpx
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("georouting")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
VALHALLA_URL = os.getenv("VALHALLA_URL", "http://valhalla:8002")
VROOM_URL    = os.getenv("VROOM_URL",    "http://vroom:3000")
DB_DSN       = os.getenv(
    "DB_DSN",
    "host=localhost dbname=gis user=postgres password=postgres"
)

# Valhalla costing model for facility routing
VALHALLA_COSTING = "auto"

# How many candidates to fetch from PostGIS before travel-time ranking.
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
    description="Proxy for Valhalla routing + VROOM optimisation",
    version="3.1.0",
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
# DB helper
# ---------------------------------------------------------------------------
@contextmanager
def db_connection():
    conn = psycopg2.connect(DB_DSN)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
async def _forward(method: str, url: str, payload: dict) -> dict:
    """
    Forward a JSON payload to an upstream service.

    Now we:
      1. Preserve the upstream status code exactly (400, 404, 422, etc.)
      2. Include the full upstream JSON body in the HTTPException detail so
         the browser console and the status bar show a useful message rather
         than a bare 500.
      3. Log the upstream error at WARNING level for server-side visibility.
    """
    try:
        resp = await client.request(method, url, json=payload)
    except httpx.RequestError as exc:
        logger.error("Upstream unreachable: %s -> %s", url, exc)
        raise HTTPException(status_code=503, detail=f"Upstream unreachable: {exc}")

    if resp.status_code != 200:
        # FIX A1: extract and propagate upstream error detail
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text or f"Upstream returned {resp.status_code}"

        logger.warning(
            "Upstream error %d from %s: %s",
            resp.status_code, url, detail
        )
        # Re-raise with the ORIGINAL upstream status code, not 500
        raise HTTPException(status_code=resp.status_code, detail=detail)

    return resp.json()


def _valhalla_location(lon: float, lat: float) -> dict:
    return {"lon": lon, "lat": lat}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/")
async def root():
    return {"message": "GeoRouting Lab API", "services": ["Valhalla", "VROOM", "PostGIS"]}

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


# ---------------------------------------------------------------------------
# Valhalla — Route
# ---------------------------------------------------------------------------
@app.post("/route")
async def valhalla_route(request: Request):
    """
    Point-to-point or multi-stop route.

    Payload locations must use {lat, lon} objects (not arrays).
    Valhalla returns 400 with a descriptive error if points are outside
    the routable area — this is now forwarded to the client as-is.
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
    Reachability polygons (isochrones / service areas).

    Note: contour colors must be sent WITHOUT '#' prefix — Valhalla embeds
    them verbatim into GeoJSON feature properties. The client is responsible
    for re-adding '#' before using them in MapLibre paint expressions.
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
    """
    Raw VROOM VRP/TSP. geometry flag injected server-side.
    """
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

    # Always request geometry
    if "options" not in payload:
        payload["options"] = {}
    payload["options"]["g"] = True

    vroom_result = await _forward("POST", VROOM_URL, payload)
    logger.debug("VROOM result routes: %d", len(vroom_result.get("routes", [])))

    # Decode geometry — this is pure Python; any failure here is a 500
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
# NEAREST FACILITY — PostGIS spatial query + Valhalla matrix + route geometry
# ---------------------------------------------------------------------------
#
# THREE-STEP ALGORITHM
# ────────────────────
# Step 1  PostGIS ST_DWithin: find up to (limit × CANDIDATE_MULTIPLIER)
#         facilities of the requested type within max_distance_km.
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
# TABLE CONTRACT (public.facilities)
# ──────────────────────────────────
#   id       SERIAL PRIMARY KEY
#   name     TEXT
#   type     TEXT   (matches _ALLOWED_FACILITY_TYPES values)
#   address  TEXT
#   geom     GEOMETRY(Point, 4326)   — WGS-84 point, spatial index required
#
# CREATE INDEX ON public.facilities USING GIST (geom);
# ---------------------------------------------------------------------------

@app.get("/nearest_facility", summary="Find and route to nearest POIs via PostGIS + Valhalla")
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

    Response
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
    # STEP 1 — PostGIS: fetch candidate facilities
    # ------------------------------------------------------------------
    # ST_DWithin(geography, geography, metres) uses the spatial index.
    # <-> operator triggers a KNN index scan for ORDER BY.
    # ST_Centroid handles both Point and MultiPoint geometries.
    # We over-fetch by CANDIDATE_MULTIPLIER before routing so that a
    # facility with short crow-fly distance but slow roads is not missed.
    # ------------------------------------------------------------------
    try:
        with db_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SET statement_timeout = '15s'")

            candidate_limit = limit * CANDIDATE_MULTIPLIER

            cur.execute(
                """
                SELECT
                    p.id,
                    p.name,
                    p.type,
                    p.address,
                    ST_X(ST_Centroid(p.geom))  AS facility_lon,
                    ST_Y(ST_Centroid(p.geom))  AS facility_lat,
                    ROUND(
                        (ST_Distance(
                            p.geom::geography,
                            ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
                        ) / 1000.0)::numeric, 2
                    ) AS crow_distance_km
                FROM public.facilities p
                WHERE LOWER(p.type) = LOWER(%s)
                  AND ST_DWithin(
                        p.geom::geography,
                        ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                        %s * 1000
                  )
                ORDER BY p.geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)
                LIMIT %s
                """,
                (
                    lon, lat,
                    facility_type,
                    lon, lat, max_distance_km,
                    lon, lat,
                    candidate_limit,
                ),
            )
            candidates = cur.fetchall()
            cur.close()

    except HTTPException:
        raise
    except Exception:
        logger.exception("PostGIS query failed")
        raise HTTPException(status_code=500, detail="Database error.")

    if not candidates:
        return {
            "message":    f"No {facility_type} found within {max_distance_km} km.",
            "count":      0,
            "facilities": [],
        }

    # ------------------------------------------------------------------
    # STEP 2 — Valhalla matrix: one HTTP call, all travel times at once
    # ------------------------------------------------------------------
    # Build location list in the same order as candidates so we can zip
    # the returned times back to the right row.
    # ------------------------------------------------------------------
    targets = [
        _valhalla_location(float(c["facility_lon"]), float(c["facility_lat"]))
        for c in candidates
    ]

    matrix_payload = {
        "sources":  [_valhalla_location(lon, lat)],
        "targets":  targets,
        "costing":  VALHALLA_COSTING,
        "units":    "km",
    }

    try:
        matrix_resp = await _forward(
            "POST", f"{VALHALLA_URL}/sources_to_targets", matrix_payload
        )
    except HTTPException as exc:
        logger.warning("Valhalla matrix failed: %s", exc.detail)
        raise HTTPException(status_code=502, detail="Routing matrix unavailable.")

    # sources_to_targets[0] = one list per source (we have one source)
    time_row = matrix_resp.get("sources_to_targets", [[]])[0]

    # Attach travel times; drop unreachable facilities (time = null)
    ranked = []
    for facility, entry in zip(candidates, time_row):
        t_sec = entry.get("time")
        if t_sec is None:
            continue
        row = dict(facility)
        row["travel_seconds"] = round(float(t_sec), 1)
        row["travel_minutes"] = round(float(t_sec) / 60.0, 1)
        ranked.append(row)

    # Re-sort by drive time, keep top `limit`
    ranked.sort(key=lambda x: x["travel_seconds"])
    ranked = ranked[:limit]

    if not ranked:
        return {
            "message":    f"No routable {facility_type} found within {max_distance_km} km.",
            "count":      0,
            "facilities": [],
        }

    # ------------------------------------------------------------------
    # STEP 3 — Valhalla /route: fetch geometry for each facility
    #          All calls run concurrently via asyncio.gather.
    # ------------------------------------------------------------------
    if routes:
        async def _fetch_route(facility: dict, rank: int) -> dict:
            """
            Route incident → one facility.  Returns a GeoJSON FeatureCollection
            of per-maneuver LineString Features, or an empty collection on failure.
            """
            payload = {
                "locations": [
                    _valhalla_location(lon, lat),
                    _valhalla_location(
                        float(facility["facility_lon"]),
                        float(facility["facility_lat"]),
                    ),
                ],
                "costing":      VALHALLA_COSTING,
                "units":        "km",
                "shape_format": "geojson",   # ask for raw [lon, lat] arrays
            }

            try:
                resp = await client.post(
                    f"{VALHALLA_URL}/route",
                    json=payload,
                    timeout=VALHALLA_TIMEOUT_S,
                )
            except httpx.RequestError as exc:
                logger.warning("Route request failed for %s: %s", facility["name"], exc)
                return {"type": "FeatureCollection", "features": []}

            if resp.status_code == 404:
                # Valhalla 404 = no route found (valid but unconnected)
                return {"type": "FeatureCollection", "features": []}

            if resp.status_code != 200:
                logger.warning(
                    "Valhalla /route %s for %s", resp.status_code, facility["name"]
                )
                return {"type": "FeatureCollection", "features": []}

            trip = resp.json().get("trip", {})
            legs = trip.get("legs", [])
            if not legs:
                return {"type": "FeatureCollection", "features": []}

            # With two locations Valhalla returns exactly one leg.
            leg        = legs[0]
            all_coords = leg.get("shape", [])     # [[lon, lat], ...] when shape_format=geojson
            maneuvers  = leg.get("maneuvers", [])

            features = []
            for seq, m in enumerate(maneuvers):
                start_idx = m.get("begin_shape_index", 0)
                end_idx   = m.get("end_shape_index", len(all_coords) - 1)
                seg_coords = all_coords[start_idx : end_idx + 1]
                if len(seg_coords) < 2:
                    continue
                features.append({
                    "type": "Feature",
                    "geometry": {
                        "type":        "LineString",
                        "coordinates": seg_coords,
                    },
                    "properties": {
                        "seq":           seq,
                        "length_m":      round(m.get("length", 0) * 1000, 2),
                        "time_s":        round(m.get("time", 0), 1),
                        "facility_name": facility["name"],
                        "facility_rank": rank,
                    },
                })

            return {"type": "FeatureCollection", "features": features}

        # Run all route fetches concurrently
        route_tasks = [
            _fetch_route(f, rank=i + 1) for i, f in enumerate(ranked)
        ]
        route_results = await asyncio.gather(*route_tasks)

        for entry, route_fc in zip(ranked, route_results):
            entry["route"] = route_fc
    else:
        for entry in ranked:
            entry["route"] = None

    # ------------------------------------------------------------------
    # Response
    # ------------------------------------------------------------------
    return {
        "incident":         {"lon": lon, "lat": lat},
        "type":             facility_type,
        "search_radius_km": max_distance_km,
        "count":            len(ranked),
        "facilities":       ranked,
    }


# ---------------------------------------------------------------------------
# Polyline decoder — precision 6 (Valhalla / VROOM)
# Returns [[lng, lat], ...]
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