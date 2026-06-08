# =============================================================================
# app.py — Geo-Routing API (FastAPI + PostGIS + pgRouting) (Valhalla + VROOM + ElasticSearch)
# =============================================================================

from contextlib import asynccontextmanager, contextmanager
from collections import defaultdict
import json
import logging
import os
from typing import Annotated
import asyncio                        # run multiple Valhalla route calls at once
import math                           # haversine distance calculation
import httpx                          # async HTTP client (Valhalla, VROOM, ES)

import psycopg2
import psycopg2.pool
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel, field_validator, model_validator

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# =============================================================================
# CONFIGURATION
# All values can be overridden with environment variables (set in docker-compose).
# =============================================================================

# Upstream service URLs
VALHALLA_URL = os.getenv("VALHALLA_URL", "https://map-tiles-frontend.address.gov.sa/valhalla")
VROOM_URL    = os.getenv("VROOM_URL",    "https://map-tiles-frontend.address.gov.sa/vroom")
ES_URL       = os.getenv("ES_URL",       "https://non-prd-elastic.address.gov.sa")

# Elasticsearch index that holds POI / facility documents
ES_FACILITY_INDEX = os.getenv("ES_FACILITY_INDEX", "building_pois")

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
_ALLOWED_FACILITY_TYPES = frozenset({"hospital", "fire station", "police"})


# ---------------------------------------------------------------------------
# DATABASE CONNECTION POOL
# For a fully async setup you would swap in asyncpg,
#  but that requires rewriting every query.  This hybrid is the safe migration.)
# ---------------------------------------------------------------------------

_pool: psycopg2.pool.ThreadedConnectionPool | None = None

_DB_KWARGS = dict(
    host=os.getenv("POSTGRES_HOST", "postgis"),
    database=os.getenv("POSTGRES_DB", "geodb"),
    user=os.getenv("POSTGRES_USER"),
    password=os.getenv("POSTGRES_PASSWORD"),
    port=int(os.getenv("POSTGRES_PORT", "5432")),
    connect_timeout=10,
    options="-c random_page_cost=1.1 -c effective_cache_size=2GB",
)


def _init_pool() -> None:
    global _pool
    try:
        _pool = psycopg2.pool.ThreadedConnectionPool(minconn=2, maxconn=20, **_DB_KWARGS)
        logger.info("Connection pool created.")
    except Exception as exc:
        logger.error("Failed to create pool: %s", exc)
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


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Geo-Routing API",
    description="Point-to-point routing, TSP, nearest facility, and service area using pgRouting / Valhalla routing · VROOM optimisation · PostGIS & Elasticsearch facility search",
    version="3.4.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Tighten in production (list your frontend origins)
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

# ---------------------------------------------------------------------------
# Shared DB helpers (unchanged from Flask version)
# ---------------------------------------------------------------------------

def _snap(cur, lon: float, lat: float, radius_m: int = 1000):
    """
    Find the nearest road-network vertex within radius_m metres.
    Uses ::geography so the distance check is in real metres, not degrees.
    """
    cur.execute(
        """
        SELECT id,
               ST_Distance(
                   geom::geography,
                   ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
               ) AS dist_m
        FROM   topology.vertices
        WHERE  ST_DWithin(
                   geom::geography,
                   ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                   %s
               )
        ORDER  BY geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)
        LIMIT  1
        """,
        (lon, lat, lon, lat, radius_m, lon, lat),
    )
    return cur.fetchone()


def _bbox_buffer(start_lon, start_lat, end_lon, end_lat) -> float:
    """
    Dynamic BBOX expansion: 20% of the larger route dimension.
    Floor 0.05° (~5 km), cap 1.0° (~110 km).
    """
    span = max(abs(end_lon - start_lon), abs(end_lat - start_lat))
    return max(0.05, min(span * 0.20, 1.0))

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


# ---------------------------------------------------------------------------
# PYDANTIC REQUEST / RESPONSE MODELS
# ---------------------------------------------------------------------------

class LonLat(BaseModel):
    """
    Reusable mixin for any model that carries a lon/lat pair.
    Inherit from this instead of duplicating the validators.
    """
    lon: float
    lat: float

    @field_validator("lon")
    @classmethod
    def check_lon(cls, v):
        if not (-180.0 <= v <= 180.0):
            raise ValueError(f"Longitude {v} is outside [-180, 180].")
        return v

    @field_validator("lat")
    @classmethod
    def check_lat(cls, v):
        if not (-90.0 <= v <= 90.0):
            raise ValueError(f"Latitude {v} is outside [-90, 90].")
        return v


class TSPRequest(BaseModel):
    """
    POST body for /route/tsp.

    points: list of [lon, lat] pairs — at least 3, at most 10.

    The @model_validator (runs after all field validators) checks the
    per-item coordinate bounds and the list length constraint.
    """
    points: list[list[float]]

    @model_validator(mode="after")
    def check_points(self):
        if len(self.points) < 3:
            raise ValueError("Need at least 3 points for TSP.")
        if len(self.points) > 10:
            raise ValueError(f"Maximum 10 waypoints allowed (got {len(self.points)}).")
        for i, p in enumerate(self.points):
            if len(p) != 2:
                raise ValueError(f"Point {i+1} must be [lon, lat].")
            lon, lat = p
            if not (-180.0 <= lon <= 180.0):
                raise ValueError(f"Point {i+1} longitude {lon} is outside [-180, 180].")
            if not (-90.0 <= lat <= 90.0):
                raise ValueError(f"Point {i+1} latitude {lat} is outside [-90, 90].")
        return self


# Allowed facility types (whitelist prevents SQL injection via the `type` param)
_ALLOWED_FACILITY_TYPES = frozenset({"hospital", "fire station", "police", "clinic"})

# ---------------------------------------------------------------------------
# HELPER: map HTTP 404/400 returns from business logic to HTTPException
# ---------------------------------------------------------------------------

def _not_found(msg: str):
    raise HTTPException(status_code=404, detail=msg)

def _bad_request(msg: str):
    raise HTTPException(status_code=400, detail=msg)


# ===========================================================================
# ENDPOINT: GET /route
# ===========================================================================

@app.get("/route", summary="A-to-B routing with alternative paths")
def get_route(
    # Annotated + Query(ge=..., le=...) replaces _parse_lonlat() entirely.
    # FastAPI rejects out-of-range values with a clear 422 before our code runs.
    start_lon: Annotated[float, Query(ge=-180, le=180, description="Start longitude")],
    start_lat: Annotated[float, Query(ge=-90,  le=90,  description="Start latitude")],
    end_lon:   Annotated[float, Query(ge=-180, le=180, description="End longitude")],
    end_lat:   Annotated[float, Query(ge=-90,  le=90,  description="End latitude")],
    alternatives: Annotated[int,  Query(ge=1, le=3, description="Number of routes (1-3)")] = 1,
    optimization: Annotated[str,  Query(description="fastest | shortest")] = "fastest",
):
    if optimization not in ("fastest", "shortest"):
        _bad_request("optimization must be 'fastest' or 'shortest'.")

    try:
        with db_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SET statement_timeout = '20s'")

            # Snap to road network
            start_node = _snap(cur, start_lon, start_lat)
            end_node   = _snap(cur, end_lon,   end_lat)

            if not start_node:
                _not_found("Start point is more than 1 km from the road network.")
            if not end_node:
                _not_found("End point is more than 1 km from the road network.")

            start_vid, end_vid = start_node["id"], end_node["id"]
            if start_vid == end_vid:
                _bad_request("Start and end snap to the same network node.")

            # Build BBOX and inner SQL
            buf = _bbox_buffer(start_lon, start_lat, end_lon, end_lat)
            cost_col     = "length_m" if optimization == "shortest" else "cost"
            rev_cost_col = "length_m" if optimization == "shortest" else "reverse_cost"

            inner_sql = (
                "SELECT id, source, target, "
                "{cost} AS cost, {rev} AS reverse_cost "
                "FROM topology.ways "
                "WHERE geom && ST_Expand("
                "ST_MakeEnvelope({x1},{y1},{x2},{y2},4326), {buf})"
            ).format(
                cost=cost_col, rev=rev_cost_col,
                x1=min(start_lon, end_lon), y1=min(start_lat, end_lat),
                x2=max(start_lon, end_lon), y2=max(start_lat, end_lat),
                buf=buf,
            )

            cur.execute(
                """
                SELECT p.path_id, p.seq, p.node, p.edge, p.agg_cost,
                       ST_AsGeoJSON(w.geom) AS geojson,
                       w.length_m, w.cost AS travel_cost, w.id AS edge_id
                FROM pgr_ksp(
                    %s, %s, %s, %s,
                    directed => true, heap_paths => false
                ) AS p
                LEFT JOIN topology.ways w ON p.edge = w.id
                ORDER BY p.path_id, p.seq
                """,
                (inner_sql, start_vid, end_vid, alternatives),
            )
            rows = cur.fetchall()
            cur.close()

        if not rows:
            _not_found("No route found between the given points.")

        # Group by path_id and build FeatureCollections
        paths: dict[int, list] = {}
        for row in rows:
            paths.setdefault(row["path_id"], []).append(row)

        routes = []
        for path_rows in sorted(paths.values(), key=lambda r: r[0]["path_id"]):
            if path_rows[-1]["node"] != end_vid:
                continue

            features, total_length_m, total_cost = [], 0.0, 0.0
            for r in path_rows:
                if not r["geojson"]:
                    continue
                total_length_m += r["length_m"]    or 0.0
                total_cost     += r["travel_cost"] or 0.0
                features.append({
                    "type": "Feature",
                    "geometry": json.loads(r["geojson"]),
                    "properties": {
                        "edge_id":  r["edge_id"],
                        "length_m": round(r["length_m"] or 0.0, 2),
                    },
                })

            routes.append({
                "type":              "FeatureCollection",
                "features":          features,
                "total_distance_km": round(total_length_m / 1000, 2),
                "duration_minutes":  round(total_cost / 60, 1),
                "alternative_rank":  len(routes) + 1,
                "optimization":      optimization,
            })

        if not routes:
            _not_found("Routing graph is disconnected; no path exists.")

        # ALWAYS return the same shape regardless of alternatives count.
        # Frontend only needs to handle one response format.
        return {
            "routes":       routes,
            "count":        len(routes),
            "optimization": optimization,
            "requested":    alternatives,
        }

    except HTTPException:
        raise  # Let FastAPI handle our intentional errors
    except Exception:
        logger.exception("Route error")
        raise HTTPException(status_code=500, detail="Internal server error.")


# ===========================================================================
# ENDPOINT: POST /route/tsp
# ===========================================================================

@app.post("/route/tsp", summary="Travelling Salesman Problem routing")
def get_tsp_route(body: TSPRequest):
    """
    Find the most efficient visit order for a set of waypoints.

    POST body: { "points": [[lon1, lat1], [lon2, lat2], ..., [lonN, latN]] }
    Minimum 3 points, maximum 10.
    """
    validated = body.points  # Already validated by Pydantic

    try:
        with db_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SET statement_timeout = '60s'")

            # Snap all waypoints
            vertex_ids = []
            for i, (lon, lat) in enumerate(validated):
                node = _snap(cur, lon, lat)
                if not node:
                    _not_found(f"Point {i+1} ({lon:.6f}, {lat:.6f}) is more than 1 km from the road network.")
                vertex_ids.append(node["id"])

            # Build BBOX inner SQL
            all_lons = [p[0] for p in validated]
            all_lats = [p[1] for p in validated]
            tsp_buf  = _bbox_buffer(min(all_lons), min(all_lats), max(all_lons), max(all_lats))

            tsp_inner_sql = (
                "SELECT id, source, target, cost, reverse_cost "
                "FROM topology.ways WHERE cost > 0 "
                "AND geom && ST_Expand("
                "ST_MakeEnvelope({x1},{y1},{x2},{y2},4326),{buf})"
            ).format(
                x1=min(all_lons), y1=min(all_lats),
                x2=max(all_lons), y2=max(all_lats),
                buf=tsp_buf,
            )

            # Cost matrix — one query for all N*(N-1) pairwise costs
            cur.execute(
                "SELECT start_vid, end_vid, agg_cost "
                "FROM pgr_dijkstraCostMatrix(%s, %s, directed => true)",
                (tsp_inner_sql, vertex_ids),
            )
            matrix_rows = cur.fetchall()

            expected = len(vertex_ids) * (len(vertex_ids) - 1)
            if not matrix_rows or len(matrix_rows) < expected:
                _not_found("Could not compute cost matrix between all waypoints.")

            # Solve TSP
            cur.execute(
                """
                SELECT seq, node, cost, agg_cost
                FROM pgr_TSP(
                    $$SELECT start_vid, end_vid, agg_cost
                      FROM (VALUES {values}) AS t(start_vid, end_vid, agg_cost)$$
                ) ORDER BY seq
                """.format(
                    values=", ".join(
                        f"({r['start_vid']},{r['end_vid']},{r['agg_cost']})"
                        for r in matrix_rows
                    )
                )
            )
            tsp_path = cur.fetchall()
            if not tsp_path or len(tsp_path) < 2:
                _not_found("Could not solve TSP.")

            # Build legs (consecutive vertex pairs)
            legs = [
                (int(tsp_path[i]["node"]), int(tsp_path[i + 1]["node"]))
                for i in range(len(tsp_path) - 1)
            ]

            # Fetch all leg geometries in ONE UNION ALL query
            leg_selects = []
            for leg_idx, (start_v, end_v) in enumerate(legs):
                leg_selects.append(
                    """
                    SELECT {idx} AS leg_index, p.seq,
                           ST_AsGeoJSON(w.geom) AS geojson, w.length_m
                    FROM pgr_dijkstra($pgrouting${inner}$pgrouting$,
                                      {start},{end}, directed => true) AS p
                    JOIN topology.ways w ON p.edge = w.id
                    WHERE p.edge != -1
                    """.format(idx=leg_idx, inner=tsp_inner_sql, start=start_v, end=end_v)
                )
            cur.execute(" UNION ALL ".join(leg_selects) + " ORDER BY leg_index, seq")
            seg_rows = cur.fetchall()
            cur.close()

        # Group by leg_index
        legs_features: dict[int, list] = defaultdict(list)
        for row in seg_rows:
            legs_features[row["leg_index"]].append({
                "type": "Feature",
                "geometry": json.loads(row["geojson"]),
                "properties": {"length_m": round(row["length_m"], 2), "leg_index": row["leg_index"]},
            })

        segments = [
            {"type": "FeatureCollection", "features": legs_features[i]}
            for i in range(len(legs))
        ]

        total_distance_m = sum(r["length_m"] for r in seg_rows)
        total_cost       = float(tsp_path[-1]["agg_cost"]) if tsp_path else 0.0

        node_to_idx    = {vid: i for i, vid in enumerate(vertex_ids)}
        # waypoint_order is N+1 entries e.g. [2,0,1,2] — last entry closes the loop.
        # Keep all N+1 so the frontend can color all N legs and show the closing arrow.
        waypoint_order = [node_to_idx[int(r["node"])] for r in tsp_path if int(r["node"]) in node_to_idx]

        return {
            "type":              "FeatureCollection",
            "segments":          segments,          # N segments, includes the closing leg
            "features":          [],
            "total_distance_km": round(total_distance_m / 1000, 2),
            "duration_minutes":  round(total_cost / 60, 1),
            "segment_count":     len(segments),
            "waypoint_count":    len(validated),
            "waypoint_order":    waypoint_order,    # length N+1, last entry == first
            "optimization":      "tsp",
        }

    except HTTPException:
        raise
    except Exception:
        logger.exception("TSP error")
        raise HTTPException(status_code=500, detail="Internal server error while solving TSP.")


# ===========================================================================
# ENDPOINT: GET /nearest_facility
# ===========================================================================

_ALLOWED_FACILITY_TYPES = frozenset({"hospital", "fire station", "police", "clinic"})


@app.get("/nearest_facility", summary="Find and route to nearest POIs")
def nearest_facility(
    lon:            Annotated[float, Query(ge=-180, le=180)],
    lat:            Annotated[float, Query(ge=-90,  le=90)],
    type:           str   = "hospital",
    limit:          Annotated[int,   Query(ge=1, le=5)]   = 5,
    max_distance_km: Annotated[float, Query(ge=1, le=15)] = 5.0,
    routes:         bool  = True,
):
    facility_type = type.lower().strip()
    if facility_type not in _ALLOWED_FACILITY_TYPES:
        _bad_request(f"type must be one of: {', '.join(sorted(_ALLOWED_FACILITY_TYPES))}.")

    try:
        with db_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SET statement_timeout = '30s'")

            click_node = _snap(cur, lon, lat)
            if not click_node:
                _not_found("Location is more than 1 km from the road network.")
            click_vid = click_node["id"]

            fac_buf = min((max_distance_km * 1.2) / 111.32, 1.0)
            fac_inner_sql = (
                "SELECT id, source, target, cost, reverse_cost "
                "FROM topology.ways WHERE cost > 0 "
                "AND geom && ST_Expand(ST_MakePoint({lon},{lat})::geometry,{buf})"
            ).format(lon=lon, lat=lat, buf=fac_buf)

            lateral_sql = """
                WITH click_pt AS (
                    SELECT ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography AS geog
                ),
                candidates AS (
                    SELECT p.id, p.name, p.type, p.address, p.nearest_vertex_id,
                           ST_X(ST_Centroid(p.geom)) AS facility_lon,
                           ST_Y(ST_Centroid(p.geom)) AS facility_lat,
                           ST_Distance(p.geom::geography,(SELECT geog FROM click_pt))/1000.0
                               AS crow_distance_km
                    FROM topology.places p
                    WHERE LOWER(p.type) = LOWER(%s)
                      AND p.nearest_vertex_id IS NOT NULL
                      AND ST_DWithin(p.geom::geography,(SELECT geog FROM click_pt),%s*1000)
                    ORDER BY p.geom <-> ST_SetSRID(ST_MakePoint(%s,%s),4326)
                    LIMIT 10
                )
                SELECT c.*,
                    ROUND(d.agg_cost::numeric,1) AS travel_seconds,
                    ROUND((d.agg_cost/60.0)::numeric,1) AS travel_minutes
                FROM candidates c
                CROSS JOIN LATERAL (
                    SELECT agg_cost FROM pgr_dijkstraCost(
                        $pgrouting${inner}$pgrouting$,
                        %s, c.nearest_vertex_id, directed => true)
                ) d
                ORDER BY d.agg_cost LIMIT %s
            """.format(inner=fac_inner_sql)

            cur.execute(lateral_sql, (lon, lat, facility_type, max_distance_km, lon, lat, click_vid, limit))
            facilities = cur.fetchall()

            if not facilities:
                cur.close()
                return {"message": f"No {facility_type} found within {max_distance_km} km.", "count": 0, "facilities": []}

            route_by_target: dict[int, list] = {}
            if routes:
                target_vids = [f["nearest_vertex_id"] for f in facilities]
                geom_sql = (
                    "SELECT p.end_vid, p.seq, ST_AsGeoJSON(w.geom) AS geojson, w.length_m "
                    "FROM pgr_dijkstra($pgrouting${inner}$pgrouting$, %s, %s, directed => true) AS p "
                    "JOIN topology.ways w ON p.edge = w.id "
                    "WHERE p.edge != -1 ORDER BY p.end_vid, p.seq"
                ).format(inner=fac_inner_sql)
                cur.execute(geom_sql, (click_vid, [int(v) for v in target_vids]))
                for row in cur.fetchall():
                    route_by_target.setdefault(row["end_vid"], []).append(row)

            cur.close()

        result_facilities = []
        for f in facilities:
            entry = dict(f)
            if routes:
                segs = route_by_target.get(f["nearest_vertex_id"], [])
                entry["route"] = {
                    "type": "FeatureCollection",
                    "features": [
                        {"type": "Feature",
                         "geometry": json.loads(r["geojson"]),
                         "properties": {"seq": r["seq"], "length_m": round(r["length_m"], 2)}}
                        for r in segs
                    ],
                }
            result_facilities.append(entry)

        return {
            "incident":         {"lon": lon, "lat": lat},
            "type":             facility_type,
            "search_radius_km": max_distance_km,
            "count":            len(result_facilities),
            "facilities":       result_facilities,
        }

    except HTTPException:
        raise
    except Exception:
        logger.exception("Nearest facility error")
        raise HTTPException(status_code=500, detail="Internal server error.")


# ===========================================================================
# ENDPOINT: GET /service_area
# ===========================================================================

@app.get("/service_area", summary="Compute reachable area (isochrone)")
def service_area(
    lon:     Annotated[float, Query(ge=-180, le=180)],
    lat:     Annotated[float, Query(ge=-90,  le=90)],
    minutes: Annotated[float, Query(ge=1, le=20)] = 5,
):
    try:
        with db_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SET statement_timeout = '30s'")

            node = _snap(cur, lon, lat)
            if not node:
                _not_found("Service point is more than 1 km from the road network.")

            sa_reach_deg = (minutes * 60 * 13.9) / 111_320
            sa_buf       = min(sa_reach_deg * 1.3, 1.0)

            sa_inner_sql = (
                "SELECT id, source, target, cost, reverse_cost "
                "FROM topology.ways WHERE cost > 0 "
                "AND geom && ST_Expand(ST_MakePoint({lon},{lat})::geometry,{buf})"
            ).format(lon=lon, lat=lat, buf=sa_buf)

            cur.execute(
                """
                WITH reach AS (
                    SELECT edge FROM pgr_drivingDistance(%s, %s, %s, directed := false)
                ),
                edges AS (SELECT w.geom FROM topology.ways w JOIN reach r ON w.id = r.edge)
                SELECT
                    ST_AsGeoJSON(ST_Union(geom))                               AS geom_union,
                    ST_AsGeoJSON(ST_ConcaveHull(ST_Collect(geom), 0.90, true)) AS hull,
                    COUNT(*) AS edge_count
                FROM edges
                """,
                (sa_inner_sql, node["id"], minutes * 60),
            )
            result = cur.fetchone()
            cur.close()

        if not result or result["edge_count"] == 0:
            _not_found("No reachable network found within that time.")

        return {
            "service_point":     {"lon": lon, "lat": lat},
            "time_minutes":      minutes,
            "reachable_network": json.loads(result["geom_union"]),
            "service_area":      json.loads(result["hull"]),
            "edge_count":        result["edge_count"],
        }

    except HTTPException:
        raise
    except Exception:
        logger.exception("Service area error")
        raise HTTPException(status_code=500, detail="Internal server error.")


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
# NEAREST FACILITY — Elasticsearch  →  GET /nearest_facility
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

@app.get("/nearest_facility_val", tags=["Facility"])
async def nearest_facility(
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


# ---------------------------------------------------------------------------
# Entry point
# Run with:
#   uvicorn app:app --host 0.0.0.0 --port 5000 --reload
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=5000, reload=False)