"""
GeoRouting Lab — app.py
=======================
FastAPI application that acts as a smart proxy between the frontend map
and four backend services:

  ┌─────────────────┬────────────────────────────────────────────────┐
  │ Service         │ Used for                                       │
  ├─────────────────┼────────────────────────────────────────────────┤
  │ Valhalla        │ Turn-by-turn routing, travel-time matrix,      │
  │                 │ isochrone (reachability) polygons              │
  │ VROOM           │ Vehicle Routing Problem / TSP optimisation     │
  │ PostGIS         │ /nearest_facility_pg — spatial query via SQL   │
  │ Elasticsearch   │ /nearest_facility_es  — geo_distance query      │
  └─────────────────┴────────────────────────────────────────────────┘

All endpoints live under /api/ (proxied by Nginx → port 5000).
"""

# =============================================================================
# IMPORTS
# =============================================================================

import asyncio                        # run multiple Valhalla route calls at once
import logging                        # write messages to the server log
import math                           # haversine distance calculation
import os                             # read environment variables
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated          # type hints with Query validation

import httpx                          # async HTTP client (Valhalla, VROOM, ES)
import psycopg2                       # PostgreSQL driver
import psycopg2.pool                  # connection pool — reuses DB connections
from psycopg2.extras import RealDictCursor   # cursor that returns dicts, not tuples
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("georouting")


# =============================================================================
# CONFIGURATION
# All values can be overridden with environment variables (set in docker-compose).
# =============================================================================

# Upstream service URLs
VALHALLA_URL = os.getenv("VALHALLA_URL", "http://valhalla:8002")
VROOM_URL    = os.getenv("VROOM_URL",    "http://vroom:3000")
ES_URL       = os.getenv("ES_URL",       "http://elasticsearch:9200")

# Elasticsearch index that holds POI / facility documents
ES_FACILITY_INDEX = os.getenv("ES_FACILITY_INDEX", "pois")

# Valhalla costing profile used for facility routing ("auto" = car)
VALHALLA_COSTING = "auto"

# Before ranking by drive time we fetch this many times `limit` from the
# data source.  Crow-fly nearest ≠ drive-time nearest, so we need extra
# candidates to avoid missing the truly closest facility.
CANDIDATE_MULTIPLIER = 3

# Give up on a single Valhalla /route call after this many seconds
VALHALLA_TIMEOUT_S = 25.0

# Only these facility types are accepted — blocks unexpected values and
# prevents SQL / query injection via the `type` URL parameter.
_ALLOWED_FACILITY_TYPES = frozenset({"hospital", "fire station", "police", "clinic"})

# Typesense configuration
TS_URL = os.getenv(
    "TS_URL",
    f"{os.getenv('TYPESENSE_PROTOCOL', 'http')}://"
    f"{os.getenv('TYPESENSE_HOST', 'typesense')}:"
    f"{os.getenv('TYPESENSE_PORT', '8108')}",
)

TS_API_KEY = os.getenv("TYPESENSE_API_KEY", "xyz")
TS_FACILITY_COLLECTION = os.getenv("TS_FACILITY_COLLECTION", "pois")

# =============================================================================
# POSTGRESQL CONNECTION POOL
#
# Opening a fresh database connection for every request takes 10–50 ms.
# A pool keeps a small number of connections open and lends them out on
# demand, cutting that overhead to near zero.
#
# minconn=2  → always keep 2 connections warm and ready
# maxconn=20 → never open more than 20 at the same time
# =============================================================================

_pool: psycopg2.pool.ThreadedConnectionPool | None = None

# Credentials come from environment variables — never hard-code passwords.
_DB_KWARGS = dict(
    host=os.getenv("POSTGRES_HOST",     "postgis"),
    database=os.getenv("POSTGRES_DB",   "geodb"),
    user=os.getenv("POSTGRES_USER"),
    password=os.getenv("POSTGRES_PASSWORD"),
    port=int(os.getenv("POSTGRES_PORT", "5432")),
    connect_timeout=10,
    options="-c random_page_cost=1.1 -c effective_cache_size=2GB",
)


def _init_pool() -> None:
    """Create the connection pool at server startup. Logs an error but does
    not crash if the database is unreachable — /health will report 'degraded'."""
    global _pool
    try:
        _pool = psycopg2.pool.ThreadedConnectionPool(minconn=2, maxconn=20, **_DB_KWARGS)
        logger.info("PostgreSQL connection pool created.")
    except Exception as exc:
        logger.error("Failed to create DB pool: %s", exc)
        _pool = None


def _get_conn():
    """Borrow a connection from the pool. Raises RuntimeError if unavailable."""
    if _pool:
        try:
            return _pool.getconn()
        except Exception as exc:
            logger.error("Pool exhausted: %s", exc)
    raise RuntimeError("Database connection pool unavailable.")


def _put_conn(conn) -> None:
    """Return a borrowed connection to the pool (or close it as a last resort)."""
    if _pool:
        try:
            _pool.putconn(conn)
            return
        except Exception as exc:
            logger.error("Failed to return connection to pool: %s", exc)
    try:
        conn.close()
    except Exception:
        pass


@contextmanager
def db_connection():
    """
    Context manager that guarantees the connection is always returned to the
    pool — even if an exception is raised inside the `with` block.

    Usage:
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT ...")
    """
    conn = _get_conn()
    try:
        yield conn
    finally:
        _put_conn(conn)


# =============================================================================
# APPLICATION LIFESPAN
# FastAPI calls this on startup (before serving requests) and on shutdown.
# We create the shared HTTP client and DB pool here so they are reused across
# all requests instead of being created and destroyed for every call.
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    # Create shared async HTTP client (used by all Valhalla/VROOM proxies)
    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
    logger.info("HTTP client created.")
    _init_pool()               # ← start up
    yield                      # ← application runs here, handling requests
    await client.aclose()
    logger.info("HTTP client closed.")
    if _pool:
        _pool.closeall()
        logger.info("PostgreSQL pool closed.")


# =============================================================================
# APP SETUP
# =============================================================================

app = FastAPI(
    title="GeoRouting Lab API",
    description="Valhalla routing · VROOM optimisation · PostGIS & Elasticsearch facility search",
    version="3.4.0",
    lifespan=lifespan,
)

# Allow the browser frontend (any origin) to call this API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared async HTTP client — created in lifespan(), used everywhere below.
client: httpx.AsyncClient = None  # type: ignore


# =============================================================================
# HEALTH ENDPOINTS
# Used by monitoring tools and the /health card in the frontend.
# =============================================================================

@app.get("/", tags=["Health"])
async def root():
    """API root — returns service name and available backends."""
    return {
        "message":  "GeoRouting Lab API",
        "services": ["Valhalla", "VROOM", "PostGIS", "Elasticsearch"],
    }


@app.get("/health", tags=["Health"])
async def health():
    """
    Check all four backend services and return a combined status.
    Returns 'healthy' if everything is up, 'degraded' if anything is down.
    """
    svc = {}

    try:
        await client.get(f"{VALHALLA_URL}/status", timeout=2)
        svc["valhalla"] = "ok"
    except Exception:
        svc["valhalla"] = "unreachable"

    try:
        await client.get(f"{VROOM_URL}/health", timeout=2)
        svc["vroom"] = "ok"
    except Exception:
        svc["vroom"] = "unreachable"

    try:
        await client.get(f"{ES_URL}/_cluster/health", timeout=2)
        svc["elasticsearch"] = "ok"
    except Exception:
        svc["elasticsearch"] = "unreachable"

    try:
        with db_connection() as conn:
            conn.cursor().execute("SELECT 1")
        svc["postgis"] = "ok"
    except Exception as exc:
        logger.error("DB health check failed: %s", exc)
        svc["postgis"] = "unreachable"

    overall = "degraded" if any(v != "ok" for v in svc.values()) else "healthy"
    return {"status": overall, "services": svc}


@app.get("/health/elasticsearch", tags=["Health"])
async def health_elasticsearch():
    """Detailed Elasticsearch cluster health (status, name)."""
    try:
        resp = await client.get(f"{ES_URL}/_cluster/health", timeout=5.0)
        data = resp.json()
        return {
            "status":         "ok",
            "es_url":         ES_URL,
            "cluster_name":   data.get("cluster_name"),
            "cluster_status": data.get("status"),   # "green" | "yellow" | "red"
        }
    except Exception as exc:
        return {"status": "error", "es_url": ES_URL, "error": str(exc)}

# =============================================================================
# UTILITY FUNCTIONS
# Small helpers reused by multiple endpoints.
# =============================================================================

async def _forward(method: str, url: str, payload: dict) -> dict:
    """
    Send a JSON request to an upstream service and return its JSON response.
    If the upstream returns an error, we forward the same HTTP status code
    and error body to the browser — this makes debugging much easier than
    always returning a generic 500.
    """
    try:
        resp = await client.request(method, url, json=payload)
    except httpx.RequestError as exc:
        logger.error("Upstream unreachable: %s → %s", url, exc)
        raise HTTPException(status_code=503, detail=f"Upstream unreachable: {exc}")

    if resp.status_code != 200:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text or f"Upstream returned HTTP {resp.status_code}"
        logger.warning("Upstream error %d from %s: %s", resp.status_code, url, detail)
        raise HTTPException(status_code=resp.status_code, detail=detail)

    return resp.json()


def _valhalla_location(lon: float, lat: float) -> dict:
    """Build a Valhalla location object from WGS-84 coordinates."""
    return {"lon": lon, "lat": lat}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the straight-line (crow-fly) distance between two points in km.
    Used as a fallback when Elasticsearch does not return a sort value.
    """
    R  = 6371.0
    dL = math.radians(lat2 - lat1)
    dO = math.radians(lon2 - lon1)
    a  = (math.sin(dL / 2) ** 2
          + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
          * math.sin(dO / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _parse_shape(leg: dict) -> list:
    """
    Extract the coordinate list from a Valhalla route leg.
    Valhalla can return the shape as either:
      • An encoded polyline6 string (default, all versions)
      • A list of [lon, lat] pairs (when shape_format=geojson is honoured)
    Returns a list of [lon, lat] pairs either way.
    """
    raw = leg.get("shape", "")
    return _decode_polyline6(raw) if isinstance(raw, str) else raw


def _decode_polyline6(encoded: str) -> list:
    """
    Decode a Google Polyline6-encoded string into a list of [lon, lat] pairs.
    Valhalla and VROOM both use this format for compact geometry encoding.
    """
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


def _build_route_features(
    all_coords: list, maneuvers: list, facility_name: str, rank: int
) -> list:
    """
    Split a Valhalla route's full coordinate array into per-maneuver GeoJSON
    LineString Features. Each maneuver (e.g. "turn left", "continue") gets
    its own Feature with metadata like distance and time.
    """
    features = []
    for seq, m in enumerate(maneuvers):
        start_idx  = m.get("begin_shape_index", 0)
        end_idx    = m.get("end_shape_index", len(all_coords) - 1)
        seg_coords = all_coords[start_idx : end_idx + 1]
        if len(seg_coords) < 2:
            continue          # skip zero-length maneuvers
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": seg_coords},
            "properties": {
                "seq":           seq,
                "length_m":      round(m.get("length", 0) * 1000, 2),
                "time_s":        round(m.get("time", 0), 1),
                "facility_name": facility_name,
                "facility_rank": rank,
            },
        })
    return features


# =============================================================================
# VALHALLA ENDPOINTS
# Thin proxies that forward requests from the frontend to Valhalla and return
# the response unchanged (errors included).
# =============================================================================

@app.post("/route_val", tags=["Routing"])
async def valhalla_route(request: Request):
    """
    Point-to-point or multi-stop routing.
    Expects a Valhalla /route JSON payload with {lat, lon} location objects.
    Returns up to three route alternatives with encoded polyline geometry.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")
    return await _forward("POST", f"{VALHALLA_URL}/route", payload)


@app.post("/matrix", tags=["Routing"])
async def valhalla_matrix(request: Request):
    """
    Travel-time matrix (many origins → many destinations in one call).
    Used internally by the nearest-facility endpoints to rank candidates.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")
    return await _forward("POST", f"{VALHALLA_URL}/matrix", payload)


@app.post("/isochrone", tags=["Routing"])
async def valhalla_isochrone(request: Request):
    """
    Isochrone / service-area polygons.
    Returns GeoJSON polygons showing how far you can travel in N minutes.
    Contour colors must be sent WITHOUT the '#' prefix (Valhalla requirement).
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")
    return await _forward("POST", f"{VALHALLA_URL}/isochrone", payload)


# =============================================================================
# VROOM ENDPOINTS
# VROOM solves Vehicle Routing Problems (VRP) and Travelling Salesman Problems
# (TSP) — it finds the optimal visit order for a set of stops.
# =============================================================================

@app.post("/optimize", tags=["VRP"])
async def vroom_optimize(request: Request):
    """
    Raw VROOM VRP/TSP optimisation.
    We inject `"options": {"g": true}` so VROOM always returns route geometry.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")
    payload.setdefault("options", {})["g"] = True
    return await _forward("POST", VROOM_URL, payload)


@app.post("/optimize_route", tags=["VRP"])
async def optimize_route(request: Request):
    """
    VROOM optimisation + geometry decoding.
    Calls VROOM, then decodes each route's encoded polyline6 geometry into
    a GeoJSON FeatureCollection ready for MapLibre to render directly.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")
    payload.setdefault("options", {})["g"] = True

    vroom_result = await _forward("POST", VROOM_URL, payload)

    # Decode each vehicle route from polyline6 string → GeoJSON LineString
    features = []
    for route in vroom_result.get("routes", []):
        encoded = route.get("geometry")
        if encoded:
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString",
                             "coordinates": _decode_polyline6(encoded)},
                "properties": {
                    "vehicle_id": route.get("vehicle"),
                    "duration":   route.get("duration"),
                    "distance":   route.get("distance"),
                },
            })

    return {
        "vroom":   vroom_result,
        "geojson": {"type": "FeatureCollection", "features": features},
    }


# =============================================================================
# NEAREST FACILITY — SHARED HELPERS
# Both facility endpoints (PostGIS and Elasticsearch) share the same
# Valhalla matrix step and route-fetch step. Keeping them in one place
# means a bug fix or improvement only needs to happen once.
# =============================================================================

async def _rank_by_travel_time(
    lon: float, lat: float, candidates: list, limit: int
) -> list:
    """
    Re-rank candidates from crow-fly order to actual drive-time order.

    HOW IT WORKS
    ─────────────
    1. We send all candidate facility locations to Valhalla's matrix API
       in a single HTTP call (much faster than N individual route calls).
    2. Valhalla returns the drive time from the incident to each candidate.
    3. We attach the time to each candidate, sort, and keep the top `limit`.
    4. Candidates Valhalla cannot reach (e.g. on an island) are dropped.
    """
    targets = [_valhalla_location(c["facility_lon"], c["facility_lat"])
               for c in candidates]

    matrix_resp = await _forward(
        "POST",
        f"{VALHALLA_URL}/sources_to_targets",
        {
            "sources": [_valhalla_location(lon, lat)],   # the incident point
            "targets": targets,                           # one per candidate
            "costing": VALHALLA_COSTING,
            "units":   "km",
        },
    )

    # sources_to_targets returns a 2-D list: [source_index][target_index].
    # We have one source, so we always read row [0].
    time_row = matrix_resp.get("sources_to_targets", [[]])[0]

    ranked = []
    for candidate, entry in zip(candidates, time_row):
        t_sec = entry.get("time")
        if t_sec is None:
            continue   # unreachable — skip
        row = dict(candidate)
        row["travel_seconds"] = round(float(t_sec), 1)
        row["travel_minutes"] = round(float(t_sec) / 60.0, 1)
        ranked.append(row)

    ranked.sort(key=lambda x: x["travel_seconds"])
    return ranked[:limit]


async def _fetch_facility_route(
    origin_lon: float, origin_lat: float,
    facility: dict,
    rank: int,
) -> dict:
    """
    Fetch the turn-by-turn route from the incident to one facility.

    Returns a GeoJSON FeatureCollection where each Feature is one road
    segment (maneuver) of the route, with distance and time metadata.
    Returns an empty FeatureCollection on any error so the rest of the
    results are not affected by one failed route call.
    """
    payload = {
        "locations": [
            _valhalla_location(origin_lon, origin_lat),
            _valhalla_location(facility["facility_lon"], facility["facility_lat"]),
        ],
        "costing":      VALHALLA_COSTING,
        "units":        "km",
        # shape_format=geojson asks Valhalla to return coordinates as a list
        # instead of an encoded string. Falls back gracefully if unsupported.
        "shape_format": "geojson",
    }

    try:
        resp = await client.post(
            f"{VALHALLA_URL}/route", json=payload, timeout=VALHALLA_TIMEOUT_S
        )
    except httpx.RequestError as exc:
        logger.warning("Route call failed for '%s': %s", facility["name"], exc)
        return {"type": "FeatureCollection", "features": []}

    if resp.status_code != 200:
        logger.warning("Valhalla returned %s for '%s'", resp.status_code, facility["name"])
        return {"type": "FeatureCollection", "features": []}

    trip = resp.json().get("trip", {})
    legs = trip.get("legs", [])
    if not legs:
        return {"type": "FeatureCollection", "features": []}

    all_coords = _parse_shape(legs[0])
    if not all_coords:
        return {"type": "FeatureCollection", "features": []}

    return {
        "type": "FeatureCollection",
        "features": _build_route_features(
            all_coords, legs[0].get("maneuvers", []), facility["name"], rank
        ),
    }


# =============================================================================
# NEAREST FACILITY — PostGIS  →  GET /nearest_facility_pg
#
# DATA SOURCE: topology.places table in PostgreSQL/PostGIS
#
# Required table schema:
#   CREATE TABLE topology.places (
#     id      SERIAL PRIMARY KEY,
#     name    TEXT,
#     type    TEXT,    -- matches _ALLOWED_FACILITY_TYPES
#     address TEXT,
#     geom    GEOMETRY(Point, 4326)
#   );
#   CREATE INDEX ON topology.places USING GIST (geom);
# =============================================================================

@app.get("/nearest_facility_pg", tags=["Facility"])
async def nearest_facility_pg(
    lon:             Annotated[float, Query(ge=-180, le=180)],
    lat:             Annotated[float, Query(ge=-90,  le=90)],
    type:            str   = "hospital",
    limit:           Annotated[int,   Query(ge=1, le=10)]   = 5,
    max_distance_km: Annotated[float, Query(ge=1, le=20)]   = 5.0,
    routes:          bool  = True,
):
    """
    Find the nearest facilities using PostGIS, then route to each one.

    ALGORITHM (3 steps)
    ────────────────────
    1. PostGIS ST_DWithin: find candidates within max_distance_km using the
       spatial index. We fetch limit×3 candidates because crow-fly nearest
       ≠ drive-time nearest.
    2. Valhalla matrix: one HTTP call ranks all candidates by actual drive time.
    3. Valhalla /route (concurrent): fetch turn-by-turn geometry for the
       surviving facilities using asyncio.gather (parallel, not sequential).
    """
    facility_type = type.lower().strip()
    if facility_type not in _ALLOWED_FACILITY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"type must be one of: {', '.join(sorted(_ALLOWED_FACILITY_TYPES))}.",
        )

    # ── Step 1: PostGIS spatial query ────────────────────────────────────────
    try:
        with db_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SET statement_timeout = '15s'")
            cur.execute(
                """
                SELECT
                    p.id,
                    p.name,
                    p.type,
                    p.address,
                    ST_X(ST_Centroid(p.geom)) AS facility_lon,
                    ST_Y(ST_Centroid(p.geom)) AS facility_lat,
                    ROUND(
                        (ST_Distance(
                            p.geom::geography,
                            ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
                        ) / 1000.0)::numeric, 2
                    ) AS crow_distance_km
                FROM topology.places p
                WHERE LOWER(p.type) = LOWER(%s)
                  AND ST_DWithin(
                        p.geom::geography,
                        ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                        %s * 1000      -- convert km to metres
                      )
                ORDER BY p.geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)
                LIMIT %s
                """,
                (lon, lat, facility_type,
                 lon, lat, max_distance_km,
                 lon, lat,
                 limit * CANDIDATE_MULTIPLIER),
            )
            candidates = cur.fetchall()
    except RuntimeError as exc:
        logger.warning("DB unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable.")
    except Exception:
        logger.exception("PostGIS query failed.")
        raise HTTPException(status_code=500, detail="Database error.")

    if not candidates:
        return {"incident": {"lon": lon, "lat": lat}, "type": facility_type,
                "search_radius_km": max_distance_km, "count": 0, "facilities": [],
                "message": f"No {facility_type} found within {max_distance_km} km."}

    # ── Step 2: Rank by drive time ───────────────────────────────────────────
    ranked = await _rank_by_travel_time(lon, lat, list(candidates), limit)
    if not ranked:
        return {"incident": {"lon": lon, "lat": lat}, "type": facility_type,
                "search_radius_km": max_distance_km, "count": 0, "facilities": [],
                "message": f"No routable {facility_type} found within {max_distance_km} km."}

    # ── Step 3: Fetch route geometry (all facilities in parallel) ────────────
    if routes:
        route_results = await asyncio.gather(
            *[_fetch_facility_route(lon, lat, f, i + 1) for i, f in enumerate(ranked)]
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


# =============================================================================
# NEAREST FACILITY — Elasticsearch  →  GET /nearest_facility_es
#
# DATA SOURCE: Elasticsearch "pois" index
#
# Required index mapping (abbreviated):
#   {
#     "mappings": {
#       "properties": {
#         "geometry":   { "type": "geo_point" },   ← spatial filter + sort
#         "properties": {                           ← all other data lives here
#           "properties": {
#             "type":    { "type": "text",
#                          "fields": { "keyword": { "type": "keyword" } } },
#             "name":    { "type": "text" },
#             "address": { "type": "text" }
#           }
#         }
#       }
#     }
#   }
#
# DOCUMENT STRUCTURE (actual stored format):
#   {
#     "geometry":   { "type": "Point", "coordinates": [lon, lat] },
#     "properties": { "type": "hospital", "name": "…", "address": "…", … }
#   }
#
# IMPORTANT: the geometry field stores GeoJSON (with a "coordinates" array),
# not Elasticsearch's flat {"lat":…,"lon":…} format. Both are valid for
# geo_point fields, and the code below handles both.
# =============================================================================

@app.get("/nearest_facility_es", tags=["Facility"])
async def nearest_facility_es(
    lon:             Annotated[float, Query(ge=-180, le=180)],
    lat:             Annotated[float, Query(ge=-90,  le=90)],
    type:            str   = "hospital",
    limit:           Annotated[int,   Query(ge=1, le=10)]   = 5,
    max_distance_km: Annotated[float, Query(ge=1, le=20)]   = 5.0,
    routes:          bool  = True,
):
    """
    Find the nearest facilities using Elasticsearch, then route to each one.

    ALGORITHM (3 steps — identical contract to /nearest_facility_pg)
    ─────────────────────────────────────────────────────────────────
    1. ES geo_distance filter: find candidates within max_distance_km.
       Sorted by crow-fly distance so Valhalla matrix gets the best candidates.
    2. Valhalla matrix: one HTTP call ranks all candidates by actual drive time.
    3. Valhalla /route (concurrent): fetch turn-by-turn geometry in parallel.

    The response shape is identical to /nearest_facility_pg so the frontend
    does not need to know which backend was used.
    """
    facility_type = type.lower().strip()
    if facility_type not in _ALLOWED_FACILITY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"type must be one of: {', '.join(sorted(_ALLOWED_FACILITY_TYPES))}.",
        )

    # ── Step 1: Elasticsearch geo_distance query ─────────────────────────────
    #
    # We use a bool/filter query (no scoring) with two clauses:
    #   geo_distance  — only return documents within the search radius
    #   term          — only return documents of the requested facility type
    #
    # The _geo_distance sort returns the crow-fly distance in km as sort[0]
    # for each hit, which we expose as crow_distance_km in the response.
    es_query = {
        "size": limit * CANDIDATE_MULTIPLIER,
        "query": {
            "bool": {
                "filter": [
                    {
                        # Spatial filter: "geometry" is the geo_point field.
                        "geo_distance": {
                            "distance": f"{max_distance_km}km",
                            "geometry": {"lat": lat, "lon": lon},
                        }
                    },
                    {
                        # Type filter: data is nested under "properties.type".
                        # Using .keyword subfield for exact, case-sensitive match.
                        # facility_type is already lowercased by the whitelist check above.
                        "term": {"properties.type.keyword": facility_type}
                    },
                ]
            }
        },
        "sort": [
            {
                # Sort by crow-fly distance ascending.
                # The sort value (in km) is returned as sort[0] in each hit.
                "_geo_distance": {
                    "geometry":      {"lat": lat, "lon": lon},
                    "order":         "asc",
                    "unit":          "km",
                    "distance_type": "arc",   # most accurate; use "plane" for speed
                }
            }
        ],
        # Only fetch the fields we need — keeps network transfer small
        "_source": ["geometry", "properties"],
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
        logger.error("ES error %d: %s", es_resp.status_code, es_resp.text[:400])
        raise HTTPException(status_code=502, detail="Search service returned an error.")

    hits = es_resp.json().get("hits", {}).get("hits", [])
    if not hits:
        return {"message": f"No {facility_type} found within {max_distance_km} km.",
                "count": 0, "facilities": []}

    # ── Parse ES hits into flat candidate dicts ──────────────────────────────
    #
    # Each hit looks like:
    #   { "_id": "abc", "_source": { "geometry": {…}, "properties": {…} },
    #     "sort": [1.23] }          ← sort[0] is the crow-fly distance in km
    candidates = []
    for hit in hits:
        src   = hit.get("_source", {})
        props = src.get("properties", {})   # name, type, address live here
        loc   = src.get("geometry",   {})   # geo_point field

        # Parse coordinates — geometry can arrive in three formats:
        if isinstance(loc, str):
            # "lat,lon" plain string
            parts = loc.split(",")
            f_lat, f_lon = float(parts[0].strip()), float(parts[1].strip())
        elif "coordinates" in loc:
            # GeoJSON object: coordinates = [longitude, latitude]  (note: lon first)
            f_lon = float(loc["coordinates"][0])
            f_lat = float(loc["coordinates"][1])
        else:
            # Flat ES geo_point: {"lat": …, "lon": …}
            f_lat = float(loc.get("lat", 0))
            f_lon = float(loc.get("lon", 0))

        # sort[0] is the crow-fly distance in km returned by _geo_distance sort
        sort_vals    = hit.get("sort", [None])
        crow_dist_km = (
            round(float(sort_vals[0]), 2)
            if sort_vals and sort_vals[0] is not None
            else round(_haversine_km(lat, lon, f_lat, f_lon), 2)
        )

        candidates.append({
            "id":               hit.get("_id") or str(props.get("id", "")),
            "name":             props.get("name") or props.get("name_ar") or "Unknown",
            "type":             props.get("type", facility_type),
            "address":          props.get("address") or props.get("address_ar") or "",
            "facility_lat":     f_lat,
            "facility_lon":     f_lon,
            "crow_distance_km": crow_dist_km,
        })

    logger.info("ES: %d candidate(s) for type=%s within %.1f km",
                len(candidates), facility_type, max_distance_km)

    # ── Step 2: Rank by drive time ───────────────────────────────────────────
    ranked = await _rank_by_travel_time(lon, lat, candidates, limit)
    if not ranked:
        return {"message": f"No routable {facility_type} found within {max_distance_km} km.",
                "count": 0, "facilities": []}

    # ── Step 3: Fetch route geometry (all facilities in parallel) ────────────
    if routes:
        route_results = await asyncio.gather(
            *[_fetch_facility_route(lon, lat, f, i + 1) for i, f in enumerate(ranked)]
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

# =============================================================================
# NEAREST FACILITY — Typesense  →  GET /nearest_facility_ts
#
# DATA SOURCE: Typesense "pois" collection
#
# Required collection schema:
#   {
#     "name": "pois",
#     "fields": [
#       {"name": "name",     "type": "string"},
#       {"name": "address",  "type": "string",   "optional": true},
#       {"name": "type",     "type": "string",   "facet": true},
#       {"name": "class",    "type": "string",   "facet": true, "optional": true},
#       {"name": "location", "type": "geopoint"}          ← [lat, lon]
#     ]
#   }
#
# NOTE: Typesense geopoints are [lat, lon] (lat first), unlike GeoJSON.
# =============================================================================

@app.get("/nearest_facility_ts", tags=["Facility"])
async def nearest_facility_ts(
    lon:             Annotated[float, Query(ge=-180, le=180)],
    lat:             Annotated[float, Query(ge=-90,  le=90)],
    type:            str   = "hospital",
    limit:           Annotated[int,   Query(ge=1, le=10)]   = 5,
    max_distance_km: Annotated[float, Query(ge=1, le=20)]   = 5.0,
    routes:          bool  = True,
):
    """
    Find the nearest facilities using Typesense, then route to each one.

    ALGORITHM (3 steps — identical contract to /nearest_facility_pg and _es)
    ─────────────────────────────────────────────────────────────────────────
    1. Typesense geo filter: find candidates within max_distance_km,
       sorted by crow-fly distance.
    2. Valhalla matrix: one HTTP call ranks all candidates by actual drive time.
    3. Valhalla /route (concurrent): fetch turn-by-turn geometry in parallel.
    """
    facility_type = type.lower().strip()
    if facility_type not in _ALLOWED_FACILITY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"type must be one of: {', '.join(sorted(_ALLOWED_FACILITY_TYPES))}.",
        )

    def _empty(message: str) -> dict:
        return {"incident": {"lon": lon, "lat": lat}, "type": facility_type,
                "search_radius_km": max_distance_km, "count": 0,
                "facilities": [], "message": message}

    # ── Step 1: Typesense geo search ─────────────────────────────────────────
    #
    # filter_by:
    #   location:(lat, lon, R km)  — radius filter on the geopoint field
    #   type:=`hospital`           — exact match; backticks escape special chars
    # sort_by:
    #   location(lat, lon):asc     — nearest first; distance comes back in
    #                                hit["geo_distance_meters"]["location"]
    ts_params = {
        "q":         "*",
        "query_by":  "name",
        "filter_by": (
            f"location:({lat}, {lon}, {max_distance_km} km) "
            f"&& type:=`{facility_type}`"
        ),
        "sort_by":   f"location({lat}, {lon}):asc",
        "per_page":  limit * CANDIDATE_MULTIPLIER,   # Typesense max is 250
        "include_fields": "id,name,type,address,location",
    }

    try:
        ts_resp = await client.get(
            f"{TS_URL}/collections/{TS_FACILITY_COLLECTION}/documents/search",
            params=ts_params,
            headers={"X-TYPESENSE-API-KEY": TS_API_KEY},
            timeout=15.0,
        )
    except httpx.RequestError as exc:
        logger.error("Typesense unreachable: %s", exc)
        raise HTTPException(status_code=503, detail="Search service unavailable.")

    if ts_resp.status_code != 200:
        logger.error("Typesense error %d: %s", ts_resp.status_code, ts_resp.text[:400])
        raise HTTPException(status_code=502, detail="Search service returned an error.")

    hits = ts_resp.json().get("hits", [])
    if not hits:
        return _empty(f"No {facility_type} found within {max_distance_km} km.")

    # ── Parse Typesense hits into flat candidate dicts ───────────────────────
    #
    # Each hit looks like:
    #   { "document": {"id": "…", "name": "…", "location": [lat, lon], …},
    #     "geo_distance_meters": {"location": 1234} }
    candidates = []
    for hit in hits:
        doc = hit.get("document", {})
        loc = doc.get("location")
        if not loc or len(loc) != 2:
            continue

        f_lat, f_lon = float(loc[0]), float(loc[1])   # [lat, lon]

        dist_m = hit.get("geo_distance_meters", {}).get("location")
        crow_dist_km = (
            round(dist_m / 1000.0, 2)
            if dist_m is not None
            else round(_haversine_km(lat, lon, f_lat, f_lon), 2)
        )

        candidates.append({
            "id":               doc.get("id", ""),
            "name":             doc.get("name") or "Unknown",
            "type":             doc.get("type", facility_type),
            "address":          doc.get("address") or "",
            "facility_lat":     f_lat,
            "facility_lon":     f_lon,
            "crow_distance_km": crow_dist_km,
        })

    logger.info("Typesense: %d candidate(s) for type=%s within %.1f km",
                len(candidates), facility_type, max_distance_km)

    if not candidates:
        return _empty(f"No {facility_type} found within {max_distance_km} km.")

    # ── Step 2: Rank by drive time ───────────────────────────────────────────
    ranked = await _rank_by_travel_time(lon, lat, candidates, limit)
    if not ranked:
        return _empty(f"No routable {facility_type} found within {max_distance_km} km.")

    # ── Step 3: Fetch route geometry (all facilities in parallel) ────────────
    if routes:
        route_results = await asyncio.gather(
            *[_fetch_facility_route(lon, lat, f, i + 1) for i, f in enumerate(ranked)]
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
# =============================================================================
# AGENTIC LAYER  →  POST /agent/chat   (browser calls /api/agent/chat)
#
# agent.py reuses the helpers above (client, _forward, nearest_facility_pg …)
# as tools for the AI model. The `if` guard avoids a double import when the
# file is started with `python app.py` (uvicorn then re-imports it as "app").
# =============================================================================

import sys as _sys
if __name__ != "__main__" and "agent" not in _sys.modules:
    # Normal start: uvicorn imports app first, then we load the agent.
    # If something imports agent FIRST (a test, `python -c "import agent"`),
    # agent is still loading, so we skip here instead of crashing.
    from agent import router as agent_router
    app.include_router(agent_router)


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=5000, log_level="info")