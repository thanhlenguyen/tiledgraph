# =============================================================================
# app.py — Geo-Routing API (FastAPI + PostGIS + pgRouting)
#
# WHAT THIS FILE DOES:
#   This is a web API server that answers geographic routing questions, like:
#     - "What is the fastest route from point A to point B?"
#     - "Visit all these locations in the most efficient order (TSP)?"
#     - "Where is the nearest hospital from my location?"
#     - "How far can I drive in 5 minutes from here?"
#
# HOW IT WORKS:
#   1. A client (browser, mobile app) sends an HTTP request to an endpoint.
#   2. This server validates the inputs, queries a PostgreSQL/PostGIS database
#      that has road network data loaded, and returns GeoJSON results.
#   3. The client displays the results on a map (e.g. Leaflet, Mapbox).

#
# KEY TECHNOLOGIES:
#   - FastAPI    : modern, fast (high-performance) web framework for building APIs with Python
#   - psycopg2   : Python driver for PostgreSQL databases
#   - PostGIS    : PostgreSQL extension for geographic data (shapes, distances)
#   - pgRouting  : PostgreSQL extension for graph/network routing algorithms
#   - Pydantic    : data validation and settings management using Python type annotations
#   - Uvicorn     : lightning-fast ASGI server for Python web apps
#
# OPTIMIZATIONS IN THIS VERSION:
#   - Connection pool (reuse DB connections instead of opening a new one each time)
#   - Dynamic BBOX (only load road edges near the route, not the whole world)
#   - One-to-many Dijkstra (single DB call instead of a loop for nearest facility)
#   - Cost matrix for TSP (single DB call for all pairwise distances)
#   - Single UNION ALL geometry fetch for TSP legs (one round-trip to DB)
# =============================================================================

from contextlib import asynccontextmanager, contextmanager  # for the "with db_connection()" pattern
from collections import defaultdict     # dict that auto-creates missing keys
import json                             # parse / produce JSON
import logging                          # write messages to the console/log file
import os
from typing import Annotated      # for type annotations with extra validation metadata

import psycopg2                         # talk to PostgreSQL
import psycopg2.pool                    # thread-safe pool of reusable DB connections
import uvicorn                          # ASGI server to run the FastAPI app
from fastapi import FastAPI, HTTPException, Query   # web framework and error handling
from fastapi.middleware.cors import CORSMiddleware  # allow cross-origin requests (e.g. from a frontend running on a different port)
from psycopg2.extras import RealDictCursor  # return query rows as dicts (col_name → value)
from pydantic import BaseModel, field_validator, model_validator    # for defining and validating request/response schemas with type annotations

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO) # Log to console with INFO level (change to DEBUG for more verbose output)
logger = logging.getLogger(__name__)    # Create a logger for this module (use __name__ to include the module name in logs)

# ---------------------------------------------------------------------------
# DATABASE CONNECTION POOL
#   Opening a new database connection for every HTTP request is slow (~10-50 ms).
#   A pool pre-opens a fixed number of connections and hands them out on demand.
#   When done, the connection goes back to the pool instead of being closed.
#
# ThreadedConnectionPool: safe for Flask's multi-threaded request handling.
# minconn=2  : always keep at least 2 connections open (warm and ready).
# maxconn=20 : never open more than 20 simultaneous connections.
# psycopg2 is synchronous, so we keep the ThreadedConnectionPool.  For a fully async setup you would swap in asyncpg,
#  but that requires rewriting every query.  This hybrid is the safe migration.)
# ---------------------------------------------------------------------------

_pool: psycopg2.pool.ThreadedConnectionPool | None = None

# Read DB credentials from environment variables — NEVER hard-code passwords.
# Set these in docker-compose.yml, a .env file, or your deployment config.
_DB_KWARGS = dict(
    host=os.getenv("POSTGRES_HOST", "postgis"),
    database=os.getenv("POSTGRES_DB", "geodb"),
    user=os.getenv("POSTGRES_USER"),        # None if unset → psycopg2 will fail clearly
    password=os.getenv("POSTGRES_PASSWORD"),
    port=int(os.getenv("POSTGRES_PORT", "5432")),
    connect_timeout=10,                     # give up after 10 s if the DB is unreachable
    options="-c random_page_cost=1.1 -c effective_cache_size=2GB",
)


def _init_pool() -> None:
    """Create the global connection pool at server startup."""
    global _pool
    try:
        _pool = psycopg2.pool.ThreadedConnectionPool(minconn=2, maxconn=20, **_DB_KWARGS)
        logger.info("Connection pool created.")
    except Exception as exc:
        # Log the problem but don't crash — /health will report "unhealthy" until
        # the DB becomes reachable and the pool is recreated on the next restart.
        logger.error("Failed to create pool: %s", exc)
        _pool = None


# ---------------------------------------------------------------------------
# The pool is created on startup and cleanly closed on shutdown.
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_pool()          # ← startup
    yield
    if _pool:
        _pool.closeall()  # ← shutdown (close every pooled connection)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Geo-Routing API",
    description="Point-to-point routing, TSP, nearest facility, and service area using pgRouting.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Tighten in production (list your frontend origins)
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# DB helpers (identical to Flask version)
# ---------------------------------------------------------------------------

def _get_conn():
    """
    Borrow a connection from the pool.
    Raises RuntimeError if the pool is gone or exhausted.
    """
    if _pool:
        try:
            return _pool.getconn()
        except Exception as exc:
            logger.error("Pool exhausted: %s", exc)
    # No silent fallback with hardcoded credentials — surface the failure.
    raise RuntimeError("Database connection pool unavailable.")


def _put_conn(conn) -> None:
    """
    Return a borrowed connection to the pool.
    Falls back to conn.close() if the pool itself has disappeared.
    """
    if _pool:
        try:
            _pool.putconn(conn)
            return
        except Exception as exc:
            logger.error("Failed to return connection to pool: %s", exc)
    # Last resort: at least close the socket so we don't leak file descriptors.
    try:
        conn.close()
    except Exception:
        pass


@contextmanager
def db_connection():
    """
    Context manager that guarantees the connection is ALWAYS returned to
    the pool, even if an exception is raised mid-handler.

    Usage:
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT ...")
        # ← conn is already back in the pool here, no matter what happened above
    """
    conn = _get_conn()
    try:
        yield conn          # hand the connection to the code inside the `with` block
    finally:
        _put_conn(conn)     # always runs, even if an exception propagated

# ---------------------------------------------------------------------------
# Shared DB helpers (unchanged from Flask version)
# ---------------------------------------------------------------------------

def _snap(cur, lon: float, lat: float, radius_m: int = 1000):
    """
    Find the nearest road-network vertex within `radius_m` metres.

    WHY "SNAP"?
      Users click anywhere on a map.  pgRouting only knows about vertices
      that exist in the road topology.  We must find the closest vertex
      ("snap" the arbitrary point onto the network) before routing.

    GEOGRAPHY vs GEOMETRY:
      PostGIS has two coordinate systems:
        - geometry: planar math in degrees (fast but imprecise over long distances)
        - geography: spherical math in metres (accurate anywhere on Earth)
      We cast to ::geography so the WHERE radius is in real metres, not degrees.
      (1 degree ≈ 111 km at the equator — degrees are useless as a distance unit.)

    The ORDER BY uses the fast planar <-> operator for sorting (index-friendly),
    while the WHERE uses accurate geography distance to filter.

    Returns a row dict with at least {"id": ..., "dist_m": ...}, or None.
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
    return cur.fetchone() # → returns None if no row found, or a dict with "id" and "dist_m" if found


def _bbox_buffer(start_lon, start_lat, end_lon, end_lat) -> float:
    """
    Compute how much to expand the bounding box around a route.

    WHY A BOUNDING BOX?
      pgRouting must load the road graph into memory before it can solve a route.
      Loading ALL roads in the city/country is very slow.
      Instead, we only load roads inside a rectangle that covers the route.

    WHY DYNAMIC?
      A fixed 0.1° buffer (~11 km) is too small for cross-city routes and
      wastefully large for a 200 m walk.  20% of the route extent is a good
      balance: it ensures detours can still be found without loading too much.

    Floor 0.05° ≈ 5.5 km  — always enough room to find a detour.
    Cap  1.00° ≈ 111 km   — prevents loading the entire country for huge routes.

    Returns the buffer in degrees (suitable for ST_Expand in PostGIS).
    """
    span = max(abs(end_lon - start_lon), abs(end_lat - start_lat))
    return max(0.05, min(span * 0.20, 1.0))


# ---------------------------------------------------------------------------
# PYDANTIC REQUEST / RESPONSE MODELS
#
#   With Pydantic, we declare the schema once and FastAPI automatically:
#     - Parses and coerces types (string "1.23" → float 1.23)
#     - Returns a structured 422 error if any value is invalid
#     - Documents every field in /docs
#
# The @field_validator decorators replace the old bounds-check if-blocks.
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
# ENDPOINT: GET /health
# ===========================================================================

@app.get("/health", summary="Liveness check")
def health():
    """Returns 200 if the DB is reachable, 500 otherwise."""
    try:
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")   # lightest possible DB query
            cur.close()
        return {"status": "healthy", "db": "connected"}
    except Exception as exc:
        logger.error("Health check failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ===========================================================================
# ENDPOINT: GET /route
#
# ALGORITHM: pgr_ksp  (k-Shortest Paths via Yen's algorithm)
#   Yen's algorithm finds the k cheapest paths one at a time:
#     1st path  = Dijkstra's shortest path (optimal)
#     2nd path  = shortest path that differs from path 1 by at least one edge
#     3rd path  = shortest path that differs from paths 1 & 2 by at least one edge
#   This gives genuinely different route choices, not just minor variations.
# ===========================================================================

@app.get("/route", summary="A-to-B routing with alternative paths")
def get_route(
    start_lon: Annotated[float, Query(ge=-180, le=180, description="Start longitude")],
    start_lat: Annotated[float, Query(ge=-90,  le=90,  description="Start latitude")],
    end_lon:   Annotated[float, Query(ge=-180, le=180, description="End longitude")],
    end_lat:   Annotated[float, Query(ge=-90,  le=90,  description="End latitude")],
    alternatives: Annotated[int,  Query(ge=1, le=3, description="Number of routes (1-3)")] = 1,
    optimization: Annotated[str,  Query(description="fastest | shortest")] = "fastest",
):
    """
    Return 1–3 alternative A-to-B routes.

    Query parameters:
        start_lon, start_lat  — origin  (WGS-84 decimal degrees)
        end_lon,   end_lat    — destination
        alternatives          — how many routes to return: 1 (default), 2, or 3
        optimization          — "fastest" (default) or "shortest" (by distance)

    Response (alternatives=1):
        { type, features[], total_distance_km, duration_minutes, ... }

    Response (alternatives>1):
        { routes: [ {type, features[], ...}, ... ], count }
    """
    if optimization not in ("fastest", "shortest"):
        _bad_request("optimization must be 'fastest' or 'shortest'.")

    try:
        with db_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            # Abort slow queries after 20 s — prevents blocking the pool forever
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
            #
            # pgRouting's "inner query" works like this:
            #   pgr_ksp( '<SQL that returns edges>', start_id, end_id, k )
            #
            # The SQL inside pgr_ksp is re-parsed by PostgreSQL itself — the
            # psycopg2 %s placeholders are NOT available inside it.  So we
            # must embed the values using Python's .format().
            #
            # SAFETY: every value we .format() in is either:
            #   - A column name we chose ourselves (cost_col / rev_cost_col)
            #   - A float we computed from validated user input (bbox numbers)
            # No raw user strings are ever interpolated here.
            buf = _bbox_buffer(start_lon, start_lat, end_lon, end_lat)

            # Choose cost column based on optimization mode:
            #   "fastest"  → cost      = travel time in seconds (set by the data import)
            #   "shortest" → length_m  = physical road length in metres
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

            # Run pgr_ksp and join the edge geometries
            #
            # heap_paths => false  : return exactly `alternatives` final paths.
            #   (heap_paths => true would return every intermediate path explored
            #    by Yen's algorithm — potentially thousands of rows — not wanted.)
            #
            # The LEFT JOIN brings in the physical geometry (geom) and road
            # attributes for each edge in each path so we can draw it on the map.

            cur.execute(
                """
                SELECT  p.path_id, 
                        p.seq, 
                        p.node, 
                        p.edge, 
                        p.agg_cost,
                        ST_AsGeoJSON(w.geom) AS geojson,
                        w.length_m, 
                        w.cost AS travel_cost, 
                        w.id AS edge_id
                FROM pgr_ksp(
                    %s, 
                    %s, %s, %s,
                    directed => true, 
                    heap_paths => false
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

        # Group rows by path_id and build GeoJSON FeatureCollections ---
        #
        # pgr_ksp returns one row per edge in each path, tagged with path_id.
        # We group them to reconstruct each path as an ordered list of edges.
        paths: dict[int, list] = {}
        for row in rows:
            paths.setdefault(row["path_id"], []).append(row)

        routes = []
        for path_rows in sorted(paths.values(), key=lambda r: r[0]["path_id"]):
            # Skip any path that didn't fully reach the destination
            # (can happen when the graph is disconnected near the endpoint)
            if path_rows[-1]["node"] != end_vid:
                continue

            features        = []
            total_length_m  = 0.0
            total_cost      = 0.0

            for r in path_rows:
                if not r["geojson"]:
                    continue   # last row has edge = -1 (arrival marker), skip
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
                # ----------------------------------------------------------------
                # Response keys are intentionally flat and match the JS frontend:
                #   data.routes[i].features        — GeoJSON segments
                #   data.routes[i].total_distance_km
                #   data.routes[i].duration_minutes
                #   data.routes[i].alternative_rank
                # Single-route responses (alternatives=1) omit the `routes` wrapper
                # and expose the same flat keys directly on the root object.
                # ----------------------------------------------------------------
                "type":              "FeatureCollection",
                "features":          features,
                "total_distance_km": round(total_length_m / 1000, 2),
                "duration_minutes":  round(total_cost / 60, 1),
                "alternative_rank":  len(routes) + 1,  # 1-based rank
                "optimization":      optimization,
            })

        if not routes:
            _not_found("Routing graph is disconnected; no path exists.")

        # Always return ALL found routes so the frontend can show a
        #        "choose your route" panel regardless of how many were asked for.
        #
        # Response shape is always:
        #   {
        #     "routes":       [ <FeatureCollection>, ... ],   ← 1 to 3 items
        #     "count":        N,                              ← how many came back
        #     "optimization": "fastest" | "shortest",
        #     "requested":    N                               ← what the user asked for
        #   }
        #
        # Each FeatureCollection inside routes[] has:
        #   features[]          — GeoJSON LineString segments to draw on the map
        #   total_distance_km   — total road distance
        #   duration_minutes    — estimated travel time
        #   alternative_rank    — 1 = optimal, 2 = first alternative, 3 = second
        #
        # NOTE: pgr_ksp may return fewer paths than requested when the graph is
        # sparse (e.g. asked for 3 but only 2 distinct paths exist).
        # `count` reflects how many were actually found, not what was requested.
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
# ENDPOINT: POST /route/tsp  — Travelling Salesman Problem
#
# ALGORITHM: pgr_TSP (nearest-neighbour heuristic)
#   Given N waypoints, find the shortest loop that visits all of them.
#   Exact TSP is NP-hard (infeasible for large N), so pgRouting uses a
#   greedy nearest-neighbour heuristic: fast and good enough for ≤ 10 stops.
#
# OPTIMIZATIONS vs a naïve loop:
#   - pgr_dijkstraCostMatrix:  one DB call computes all N*(N-1) pairwise costs
#   - Combined UNION ALL geometry fetch: one DB call retrieves all leg geometries
# ---------------------------------------------------------------------------

@app.post("/route/tsp", summary="Travelling Salesman Problem routing")
def get_tsp_route(body: TSPRequest):
    """
    Find the most efficient visit order for a set of waypoints.

    POST body: { "points": [[lon1, lat1], [lon2, lat2], ..., [lonN, latN]] }
    Minimum 3 points, maximum 10.

    Response:
        {
          type, segments, total_distance_km, duration_minutes,
          waypoint_order,   ← original indices in the optimal visit order
          segment_count, waypoint_count, optimization
        }
    """
    validated = body.points  # Already validated by Pydantic

    try:
        with db_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SET statement_timeout = '60s'")  # TSP can take longer

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
                "FROM topology.ways "
                "WHERE cost > 0 "
                "AND geom && ST_Expand("
                "ST_MakeEnvelope({x1},{y1},{x2},{y2},4326),{buf})"
            ).format(
                x1=min(all_lons), y1=min(all_lats),
                x2=max(all_lons), y2=max(all_lats),
                buf=tsp_buf,
            )

            # Cost matrix — one query for all N*(N-1) pairwise costs
            #
            # pgr_dijkstraCostMatrix returns a table with one row per
            # (start, end) pair:  { start_vid, end_vid, agg_cost }
            # This replaces an N*(N-1) loop of individual Dijkstra calls.
            cur.execute(
                """
                SELECT start_vid, end_vid, agg_cost
                FROM pgr_dijkstraCostMatrix(%s, %s, directed => true)
                """,
                (tsp_inner_sql, vertex_ids),
            )
            matrix_rows = cur.fetchall()

            # A fully connected graph needs N*(N-1) entries.
            # If fewer rows come back, some waypoints are unreachable from others.
            expected = len(vertex_ids) * (len(vertex_ids) - 1)
            if not matrix_rows or len(matrix_rows) < expected:
                _not_found("Could not compute cost matrix between all waypoints.")

            # Solve TSP using the cost matrix --------------------------
            #
            # pgr_TSP expects a table of (start_vid, end_vid, agg_cost).
            # We embed the matrix data as a VALUES(...) literal inside dollar-quotes.
            # Dollar-quoting ($$ ... $$) avoids single-quote conflicts in the values.
            #
            # IMPORTANT: the values here (integers and floats) come from the DB's
            # own output — they are not user-supplied strings, so format() is safe.
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
            # tsp_path has N+1 rows: the last row closes the loop back to start

            if not tsp_path or len(tsp_path) < 2:
                _not_found("Could not solve TSP.")

            # Build legs (pairs of consecutive vertices in TSP order) --
            #
            # Example: if TSP returns [A, C, B, D, A]
            #   legs = [(A,C), (C,B), (B,D), (D,A)]
            legs = [
                (int(tsp_path[i]["node"]), int(tsp_path[i + 1]["node"]))
                for i in range(len(tsp_path) - 1)
            ]

            # Fetch route geometry for all legs in ONE query -----------
            #
            # We UNION ALL individual pgr_dijkstra calls, each tagged with a
            # leg_index literal.  This gives us all geometries in one round-trip
            # and preserves per-leg identity (two legs from the same start vertex
            # won't merge, because each has its own literal leg_index tag).
            leg_selects = []
            for leg_idx, (start_v, end_v) in enumerate(legs):
                leg_selects.append(
                    # {idx}, {start}, {end} are Python ints — safe to .format()
                    # $pgrouting$ ... $pgrouting$ is dollar-quoting to avoid
                    # single-quote conflicts inside the inner SQL string.
                    """
                    SELECT {idx} AS leg_index, p.seq,
                           ST_AsGeoJSON(w.geom) AS geojson, w.length_m
                    FROM pgr_dijkstra($pgrouting${inner}$pgrouting$,
                                      {start},{end}, directed => true) AS p
                    JOIN topology.ways w ON p.edge = w.id
                    WHERE p.edge != -1
                    """.format(
                        idx=leg_idx, 
                        inner=tsp_inner_sql, 
                        start=start_v, 
                        end=end_v
                        )
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
                "properties": {
                    "length_m": round(row["length_m"], 2),
                    "leg_index": row["leg_index"],
                },
            })

        segments = [
            {"type": "FeatureCollection", "features": legs_features[i]}
            for i in range(len(legs))
        ]

        # Totals 
        total_distance_m = sum(r["length_m"] for r in seg_rows)
        total_cost       = float(tsp_path[-1]["agg_cost"]) if tsp_path else 0.0

        # Map each TSP vertex ID back to its original 0-based input index
        # so the frontend can highlight waypoints in visit order.
        node_to_idx  = {vid: i for i, vid in enumerate(vertex_ids)}
        waypoint_order = []
        for r in tsp_path:
            node = int(r["node"])
            if node in node_to_idx:
                waypoint_order.append(node_to_idx[node])

        # pgr_TSP closes the loop, so the last entry duplicates the first.
        # Trim to exactly N entries (one per input waypoint).
        # if len(waypoint_order) > len(validated):
        #     waypoint_order = waypoint_order[:len(validated)]

        return {
            "type":              "FeatureCollection",
            "segments":          segments,          # per-leg route lines
            "features":          [],                # kept for backward compatibility
            "total_distance_km": round(total_distance_m / 1000, 2),
            "duration_minutes":  round(total_cost / 60, 1),
            "segment_count":     len(segments),
            "waypoint_count":    len(validated),
            "waypoint_order":    waypoint_order,    # 0-based indices in visit order
            "optimization":      "tsp",
        }

    except HTTPException:
        raise
    except Exception:
        logger.exception("TSP error")
        raise HTTPException(status_code=500, detail="Internal server error while solving TSP.")


# ===========================================================================
# ENDPOINT: GET /nearest_facility
#
# ALGORITHM:
#   1. pgr_dijkstraCost with LATERAL: single query ranks facilities by travel time
#   2. pgr_dijkstra (one-to-many): single query fetches all route geometries
#
# Both replace the original per-facility loop (N separate DB calls → 2 calls).
# ---------------------------------------------------------------------------

# Whitelist of allowed facility types — prevents SQL injection via the `type` param
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
    """
    Return the k nearest facilities of a given type, with travel routes.

    Query parameters:
        lon, lat           — incident location (WGS-84)
        type               — hospital | fire station | police | clinic
        limit              — 1-5 (default 5)
        max_distance_km    — search radius: 1-15 km (default 5)
        routes             — include route geometry: true (default) | false

    Response:
        {
          incident: {lon, lat},
          type, search_radius_km, count,
          facilities: [
            { id, name, type, address, travel_minutes, crow_distance_km,
              route: { type: "FeatureCollection", features: [...] } },
            ...
          ]
        }
    """
    facility_type = type.lower().strip()
    if facility_type not in _ALLOWED_FACILITY_TYPES:
        _bad_request(f"type must be one of: {', '.join(sorted(_ALLOWED_FACILITY_TYPES))}.")

    try:
        with db_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SET statement_timeout = '30s'")

            # Snap the incident location

            click_node = _snap(cur, lon, lat)
            if not click_node:
                _not_found("Location is more than 1 km from the road network.")
            click_vid = click_node["id"]

            # BBOX centred on the incident, large enough to cover max_distance_km.
            # We add 20% margin and convert km to degrees (1° ≈ 111.32 km).
            fac_buf = min((max_distance_km * 1.2) / 111.32, 1.0)
            fac_inner_sql = (
                "SELECT id, source, target, cost, reverse_cost "
                "FROM topology.ways "
                "WHERE cost > 0 "
                "AND geom && ST_Expand(ST_MakePoint({lon},{lat})::geometry,{buf})"
            ).format(lon=lon, lat=lat, buf=fac_buf)

            # --- 3. Find candidates + travel cost in ONE query ---------------
            #
            # The query uses a CTE + LATERAL join:
            #
            #   WITH click_pt AS (...)        ← the incident point as geography
            #   , candidates AS (...)         ← up to 10 facilities in range
            #   SELECT c.*, d.agg_cost        ← join each candidate to its route cost
            #   FROM candidates c
            #   CROSS JOIN LATERAL (
            #       SELECT agg_cost
            #       FROM pgr_dijkstraCost(...)  ← routes incident→facility
            #   ) d
            #
            # LATERAL means: for each row in `candidates`, run the subquery
            # once with that row's nearest_vertex_id.  PostgreSQL executes
            # N Dijkstra calls but fuses them into one planner node — far
            # faster than N separate Python/DB round-trips.
            #
            # LOWER(p.type) = LOWER(%s) makes the type match case-insensitive.
            # ST_Centroid handles Point and MultiPoint facility geometries.
            lateral_sql = """
                WITH click_pt AS (
                    SELECT ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography AS geog
                ),
                candidates AS (
                    SELECT p.id, p.name, p.type, p.address, p.nearest_vertex_id,
                           ST_X(ST_Centroid(p.geom)) AS facility_lon,
                           ST_Y(ST_Centroid(p.geom)) AS facility_lat,
                           ST_Distance(p.geom::geography,
                                        (SELECT geog FROM click_pt)) / 1000.0
                               AS crow_distance_km
                    FROM topology.places p
                    WHERE LOWER(p.type) = LOWER(%s)
                      AND p.nearest_vertex_id IS NOT NULL
                      AND ST_DWithin(p.geom::geography,
                                    (SELECT geog FROM click_pt),
                                    %s * 1000)
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

            cur.execute(
                lateral_sql, 
                (lon, lat, facility_type, max_distance_km, lon, lat, click_vid, limit)
            )
            facilities = cur.fetchall()

            if not facilities:
                cur.close()
                return {"message": f"No {facility_type} found within {max_distance_km} km.", "count": 0, "facilities": []}

            # --- 4. Fetch route geometries — one-to-many Dijkstra ------------
            #
            # pgr_dijkstra accepts an array of end_vids and returns one result
            # set covering all paths simultaneously.  Far faster than routing
            # to each facility in a separate query.
            route_by_target: dict[int, list] = {}
            if routes:
                target_vids = [f["nearest_vertex_id"] for f in facilities]
                geom_sql = (
                    """
                    SELECT p.end_vid,
                           p.seq,
                           ST_AsGeoJSON(w.geom) AS geojson,
                           w.length_m
                    FROM pgr_dijkstra($pgrouting${inner}$pgrouting$,
                                      %s, %s, directed => true) AS p
                    JOIN topology.ways w ON p.edge = w.id
                    WHERE p.edge != -1
                    ORDER BY p.end_vid, p.seq
                    """
                ).format(inner=fac_inner_sql)
                cur.execute(geom_sql, (click_vid, [int(v) for v in target_vids]))
                for row in cur.fetchall():
                    route_by_target.setdefault(row["end_vid"], []).append(row)

            cur.close()

        # Assemble final response 
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
                         "properties": {
                             "seq": r["seq"], 
                             "length_m": round(r["length_m"], 2)}}
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
#
# ALGORITHM: pgr_drivingDistance
#   Starting from one vertex, expand outward along the road graph until
#   the cumulative travel cost exceeds `minutes * 60` seconds.
#   All edges reached within the time limit are returned.
#
# POST-PROCESSING:
#   ST_ConcaveHull(collected_edges, 0.90) wraps all reachable edges in a
#   polygon that approximates the true reachable area.  0.90 = 90% target
#   ratio (higher = tighter hull, lower = more convex/blobby).
# ---------------------------------------------------------------------------
@app.get("/service_area", summary="Compute reachable area (isochrone)")
def service_area(
    lon:     Annotated[float, Query(ge=-180, le=180)],
    lat:     Annotated[float, Query(ge=-90,  le=90)],
    minutes: Annotated[float, Query(ge=1, le=20)] = 5,
):
    """
    Return the area reachable within `minutes` drive of (lon, lat).

    Query parameters:
        lon, lat   — centre point (WGS-84)
        minutes    — drive time budget: 1-20 (default 5)

    Response:
        {
          service_point: {lon, lat},
          time_minutes,
          reachable_network,   ← MultiLineString of all reachable road edges
          service_area,        ← Polygon concave hull of the reachable area
          edge_count
        }
    """
    try:
        with db_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SET statement_timeout = '30s'")

            # Snap centre point to road network 
            node = _snap(cur, lon, lat)
            if not node:
                _not_found("Service point is more than 1 km from the road network.")

            # Estimate BBOX from worst-case driving speed (50 km/h) 
            #
            # max_reach_m  = minutes * 60 s * 13.9 m/s  (50 km/h = 13.9 m/s)
            # max_reach_deg ≈ max_reach_m / 111320  (metres per degree)
            sa_reach_deg = (minutes * 60 * 13.9) / 111_320
            sa_buf       = min(sa_reach_deg * 1.3, 1.0)  # 30 % margin, capped at 1°

            sa_inner_sql = (
                "SELECT id, source, target, cost, reverse_cost "
                "FROM topology.ways "
                "WHERE cost > 0 "
                "AND geom && ST_Expand(ST_MakePoint({lon},{lat})::geometry,{buf})"
            ).format(lon=lon, lat=lat, buf=sa_buf)

            # Compute reachable edges and wrap in a concave hull
            #
            # pgr_drivingDistance(sql, start_vid, max_cost, directed)
            #   → returns all edges reachable within max_cost (seconds here)
            #
            # directed => false because roads work both ways for this analysis
            # (we want the area reachable FROM here, going in any direction)
            #
            # ST_Union: merge all edge linestrings into one MultiLineString
            # ST_ConcaveHull: compute a tight polygon around them

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
            "reachable_network": json.loads(result["geom_union"]),  # all road lines
            "service_area":      json.loads(result["hull"]),        # the catchment polygon
            "edge_count":        result["edge_count"],
        }

    except HTTPException:
        raise
    except Exception:
        logger.exception("Service area error")
        raise HTTPException(status_code=500, detail="Internal server error.")


# ---------------------------------------------------------------------------
# Entry point
#
# Run with:
#   uvicorn app:app --host 0.0.0.0 --port 5000 --workers 4
#
# Or in development (auto-reload):
#   uvicorn app:app --host 0.0.0.0 --port 5000 --reload
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=5000, reload=False)
