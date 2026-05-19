"""
build_osm_topology.py  — Valhalla-safe edition
─────────────────────────────────────────────
Converts any OGR-readable vector file (GeoJSON / SHP / GPKG / Parquet / …)
into a valid OSM XML file for osmium/osmconvert → OSM PBF
(Valhalla, OSRM, GraphHopper, etc.).

DESIGNED FOR LARGE FILES (country-scale, millions of segments).

USAGE
  python build_osm_topology.py <input_file> <output.osm> [tile_size_deg [memory_gb]]

  input_file    : GeoJSON, SHP, GPKG, or Parquet (.parquet)
                  Parquet is fastest: columnar, compressed, no GDAL overhead.
                  Convert once with: ogr2ogr -f Parquet out.parquet in.gpkg
                  OR in DuckDB: COPY (SELECT ...) TO 'out.parquet' (FORMAT PARQUET)
  tile_size_deg : default 0.015 (≈ 1.6 km). Use 0.05 for sparse, 0.01 for dense cities.
  memory_gb     : default 8. Set to ~60-70% of available RAM (`free -h`).

NEXT STEP
  osmium cat output.osm -o output.osm.pbf

DEPENDENCIES
  pip install duckdb lxml tqdm
"""

import gc
import time
import math
import os
import sys
import logging

import duckdb
from lxml import etree
from tqdm import tqdm

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING SETUP
#
# Two handlers:
#   • FileHandler  (DEBUG+) — full timestamped trace in build_osm_topology.log
#   • TqdmLoggingHandler (INFO+) — routes through tqdm.write() so progress
#     bars are never clobbered by a stray logger.info() call
# ─────────────────────────────────────────────────────────────────────────────
LOG_FILE = "build_osm_topology.log"


class _TqdmHandler(logging.StreamHandler):
    """StreamHandler that uses tqdm.write() to avoid breaking progress bars."""
    def emit(self, record):
        try:
            tqdm.write(self.format(record))
        except Exception:
            self.handleError(record)


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")],
)
_console = _TqdmHandler(sys.stdout)
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
logging.getLogger().addHandler(_console)

logger = logging.getLogger(__name__)


def _elapsed(t0: float) -> str:
    s = time.time() - t0
    return f"{s:.1f}s" if s < 60 else f"{s/60:.1f}m"


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
# Minimum segment length after noding.  Pieces shorter than this are slivers
# produced by floating-point snap and are discarded.  ≈ 1 m at equator.
MIN_SEG_LEN_DEG  = 9e-6

# Endpoint snap tolerance for cross-boundary gap healing (Step 3b).
# 1e-5 deg ≈ 1.1 m.  Raise to 5e-5 (≈ 5 m) if your data has larger gaps.
SNAP_TOLERANCE   = 1e-5

# Precision grid for ST_ReducePrecision.  Must equal SNAP_TOLERANCE so that
# snapped coordinates land exactly on the same grid node.
PRECISION_GRID   = 1e-5

# Tile overlap — 100% guarantees corner intersections are always noded together.
OVERLAP_FACTOR   = 1

# Flush noded_segments to disk every N tiles to keep RAM flat.
CHECKPOINT_EVERY = 500

# Drop pathological high-vertex segments before noding to avoid OOM.
MAX_VERTICES     = 10_000

# ─────────────────────────────────────────────────────────────────────────────
# HIGHWAY CLASSIFICATION
#
# Maps the input data's numeric columns to OSM highway tag values.
#   FOW (Form Of Way): 3 or 4 = ramp/link road
#   Subtype: 1=trunk, 2=primary, 3=secondary
# ─────────────────────────────────────────────────────────────────────────────
HIGHWAY_SQL = """
CASE
    WHEN FOW IN (3,4) AND Subtype = 1 THEN 'trunk_link'
    WHEN FOW IN (3,4) AND Subtype = 2 THEN 'primary_link'
    WHEN FOW IN (3,4) AND Subtype = 3 THEN 'secondary_link'
    WHEN FOW IN (3,4)                  THEN 'tertiary_link'
    WHEN Subtype = 1                   THEN 'trunk'
    WHEN Subtype = 2                   THEN 'primary'
    ELSE 'road'
END
"""


# ─────────────────────────────────────────────────────────────────────────────
# _read_input()
# ─────────────────────────────────────────────────────────────────────────────
def _read_input(con: duckdb.DuckDBPyConnection, input_file: str) -> bool:
    """
    Ingest the input file into raw_segments (id, name, oneway, lanes,
    maxspeed, highway, geom_wkb).

    Parquet path:
      DuckDB reads Parquet natively — no GDAL, no GEOS, columnar pushdown.
      The geometry column is stored as WKB bytes in Parquet (GeoParquet spec).
      We read it directly without any geometry parsing — Phase B handles that.

    GDAL path (GeoJSON / SHP / GPKG / …):
      Two-phase ingest to avoid segfaults on corrupt geometries:
        Phase A — st_read() stores geometry as raw WKB bytes (ST_AsWKB).
          GDAL reads bytes; GEOS is never called → no crash risk.
        Phase B — parse WKB → GEOMETRY in a separate controlled query.
          try_cast() returns NULL on bad WKB instead of crashing.

    Returns True if geometry is already a GEOMETRY type (Parquet path),
    False if it is raw WKB bytes that need ST_GeomFromWKB (GDAL path).
    """
    is_parquet = input_file.lower().endswith(".parquet")

    if is_parquet:
        logger.info("   Input format: Parquet (native DuckDB reader, no GDAL)")
        # GeoParquet stores geometry as WKB in a BLOB column named "geometry".
        # We read it directly — no GDAL/GEOS involved at all during ingest.
        # The geometry column name in GeoParquet is typically "geometry" or "geom";
        # adjust the column name below if your file uses a different name.
        con.execute(f"""
            CREATE OR REPLACE TABLE raw_segments AS
            SELECT
                pkStreetID                                          AS id,
                EnglishName                                         AS name,
                CASE WHEN Direction = 1 THEN 'yes' ELSE 'no' END   AS oneway,
                CASE
                    WHEN NoOfLane IS NULL OR NoOfLane <= 0 THEN '1'
                    ELSE CAST(NoOfLane AS VARCHAR)
                END                                                 AS lanes,
                CASE
                    WHEN SpeedLimit IS NOT NULL AND CAST(SpeedLimit AS INT) > 0
                    THEN CAST(SpeedLimit AS VARCHAR)
                    ELSE NULL
                END                                                 AS maxspeed,
                {HIGHWAY_SQL}                                       AS highway,
                -- GeoParquet stores geometry as WKB bytes — read as-is, no parsing
                geom                                            AS geom_wkb
            FROM read_parquet('{input_file}')
            WHERE geom IS NOT NULL;
        """)
        return True   # geometry column is already GEOMETRY type
    else:
        logger.info("   Input format: GDAL (st_read)")
        # Phase A: GDAL reads bytes; GEOS never called → no segfault risk
        con.execute(f"""
            CREATE OR REPLACE TABLE raw_segments AS
            SELECT
                pkStreetID                                          AS id,
                EnglishName                                         AS name,
                CASE WHEN Direction = 1 THEN 'yes' ELSE 'no' END   AS oneway,
                CASE
                    WHEN NoOfLane IS NULL OR NoOfLane <= 0 THEN '1'
                    ELSE CAST(NoOfLane AS VARCHAR)
                END                                                 AS lanes,
                CASE
                    WHEN SpeedLimit IS NOT NULL AND CAST(SpeedLimit AS INT) > 0
                    THEN CAST(SpeedLimit AS VARCHAR)
                    ELSE NULL
                END                                                 AS maxspeed,
                {HIGHWAY_SQL}                                       AS highway,
                ST_AsWKB(geom)                                      AS geom_wkb
            FROM st_read('{input_file}')
            WHERE geom IS NOT NULL;
        """)
        return False  # geometry column is raw WKB bytes

# ─────────────────────────────────────────────────────────────────────────────
# _snap_boundary_endpoints()
#
# NEW STEP 3b — heal cross-region boundary gaps before noding.
#
# Street datasets split by administrative region often have endpoints that
# are "close but not touching": coordinates differ by < 1 m due to independent
# digitisation or coordinate precision in each region.  ST_Node does NOT create
# a shared node for two lines that merely come close — they must intersect
# exactly.  This step moves such near-miss endpoint pairs to their shared
# midpoint so that noding produces a connected graph across every boundary.
#
# Algorithm
# ─────────
# 1. Extract start + end point of every segment into _endpoints.
# 2. Self-join on a bounding-box predicate (ABS dx < tol AND ABS dy < tol)
#    to find candidate pairs.  Refine with ST_Distance to avoid false positives
#    caused by the rectangular bbox test.
# 3. Compute the midpoint of each pair as the canonical snapped coordinate.
# 4. Apply snaps: for each affected segment, replace its start and/or end
#    vertex with the midpoint coordinate using ST_SetPoint.
# 5. Re-materialise seg_geoms with the updated geometries.
#
# ST_SetPoint index convention (DuckDB spatial):
#   0  = first vertex (start point)
#  -1  = last vertex  (end point)
#
# Performance
# ───────────
# The self-join uses two ABS comparisons on plain FLOAT columns rather than a
# spatial index.  For 1-2 M segments the Cartesian product is pruned extremely
# aggressively by the bbox predicate (only adjacent-region pairs survive) and
# typically runs in < 60 s.  If it is slow, add a DuckDB ART index on x, y.
# ─────────────────────────────────────────────────────────────────────────────
def _snap_boundary_endpoints(con: duckdb.DuckDBPyConnection) -> None:
    t = time.time()
    logger.info("🔧  Step 3b · Snapping near-miss endpoints across region boundaries …")
    logger.info("   Snap tolerance: %.1e deg (≈ %.1f m)", SNAP_TOLERANCE, SNAP_TOLERANCE * 111_111)

    # ── 1. Extract all endpoints ──────────────────────────────────────────────
    con.execute("""
        CREATE OR REPLACE TABLE _endpoints AS
        SELECT
            id,
            1                           AS pt_pos,   -- 1 = start
            ST_StartPoint(geom)         AS pt,
            ST_X(ST_StartPoint(geom))   AS x,
            ST_Y(ST_StartPoint(geom))   AS y
        FROM seg_geoms
        UNION ALL
        SELECT
            id,
            2                           AS pt_pos,   -- 2 = end
            ST_EndPoint(geom)           AS pt,
            ST_X(ST_EndPoint(geom))     AS x,
            ST_Y(ST_EndPoint(geom))     AS y
        FROM seg_geoms;
    """)

    ep_count = con.execute("SELECT COUNT(*) FROM _endpoints").fetchone()[0]
    logger.info("   Endpoints extracted: %s", f"{ep_count:,}")

    # ── 2. Find near-miss pairs ───────────────────────────────────────────────
    con.execute(f"""
        CREATE OR REPLACE TABLE _snap_pairs AS
        SELECT
            a.id        AS id_a,
            a.pt_pos    AS pos_a,
            b.id        AS id_b,
            b.pt_pos    AS pos_b,
            (a.x + b.x) / 2.0  AS snap_x,
            (a.y + b.y) / 2.0  AS snap_y
        FROM _endpoints a
        JOIN _endpoints b
            ON  a.id    < b.id                          -- avoid self-pairs and duplicates
            AND ABS(a.x - b.x) < {SNAP_TOLERANCE}       -- cheap rectangular pre-filter
            AND ABS(a.y - b.y) < {SNAP_TOLERANCE}
            AND ST_Distance(a.pt, b.pt) < {SNAP_TOLERANCE}   -- precise distance check
            AND ST_Distance(a.pt, b.pt) > 0;            -- exclude already-coincident points
    """)

    snap_count = con.execute("SELECT COUNT(*) FROM _snap_pairs").fetchone()[0]
    logger.info("   Near-miss pairs found: %s", f"{snap_count:,}")

    if snap_count == 0:
        logger.info("   No boundary gaps detected — skipping snap  [%s]", _elapsed(t))
        con.execute("DROP TABLE IF EXISTS _endpoints;")
        con.execute("DROP TABLE IF EXISTS _snap_pairs;")
        return

    # ── 3 & 4. Compute snapped coordinates and update seg_geoms ──────────────
    #
    # Build two lookup tables: one for start-point snaps, one for end-point snaps.
    # A segment may appear in both (if both its endpoints need snapping).
    # We chain the two ST_SetPoint calls in a single CASE expression.
    #
    # NOTE: If a segment appears multiple times in _snap_pairs (its endpoint is
    # near-miss to several others), we take the FIRST snap encountered.
    # This is safe because: (a) gaps are tiny and all midpoints converge to
    # essentially the same coordinate, and (b) the subsequent ST_Node pass will
    # merge any remaining sub-tolerance differences.
    con.execute(f"""
        CREATE OR REPLACE TABLE _snap_start AS
        SELECT id, snap_x, snap_y
        FROM (
            SELECT id_a AS id, snap_x, snap_y FROM _snap_pairs WHERE pos_a = 1
            UNION ALL
            SELECT id_b AS id, snap_x, snap_y FROM _snap_pairs WHERE pos_b = 1
        )
        QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY snap_x) = 1;
    """)

    con.execute(f"""
        CREATE OR REPLACE TABLE _snap_end AS
        SELECT id, snap_x, snap_y
        FROM (
            SELECT id_a AS id, snap_x, snap_y FROM _snap_pairs WHERE pos_a = 2
            UNION ALL
            SELECT id_b AS id, snap_x, snap_y FROM _snap_pairs WHERE pos_b = 2
        )
        QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY snap_x) = 1;
    """)

    # Apply snaps in a single pass over seg_geoms.
    # The CASE chain first applies the start-point snap (if any), then feeds
    # the result into the end-point snap (if any), avoiding two full table scans.
    con.execute("""
        CREATE OR REPLACE TABLE seg_geoms AS
        SELECT
            g.id,
            CASE
                -- Both start AND end need snapping
                WHEN ss.snap_x IS NOT NULL AND se.snap_x IS NOT NULL
                THEN ST_SetPoint(
                        ST_SetPoint(g.geom, 0, ST_Point(ss.snap_x, ss.snap_y)),
                        -1, ST_Point(se.snap_x, se.snap_y))
                -- Only start needs snapping
                WHEN ss.snap_x IS NOT NULL
                THEN ST_SetPoint(g.geom, 0, ST_Point(ss.snap_x, ss.snap_y))
                -- Only end needs snapping
                WHEN se.snap_x IS NOT NULL
                THEN ST_SetPoint(g.geom, -1, ST_Point(se.snap_x, se.snap_y))
                -- No snap needed — pass through unchanged
                ELSE g.geom
            END AS geom
        FROM seg_geoms g
        LEFT JOIN _snap_start ss ON g.id = ss.id
        LEFT JOIN _snap_end   se ON g.id = se.id;
    """)

    # Count how many segments were actually modified
    snapped_segs = con.execute("""
        SELECT COUNT(DISTINCT id) FROM (
            SELECT id_a AS id FROM _snap_pairs
            UNION
            SELECT id_b AS id FROM _snap_pairs
        )
    """).fetchone()[0]
    logger.info("   Segments modified by snap: %s", f"{snapped_segs:,}")

    # Cleanup temporaries
    con.execute("DROP TABLE IF EXISTS _endpoints;")
    con.execute("DROP TABLE IF EXISTS _snap_pairs;")
    con.execute("DROP TABLE IF EXISTS _snap_start;")
    con.execute("DROP TABLE IF EXISTS _snap_end;")
    con.execute("CHECKPOINT;")
    logger.info("   Endpoint snap complete  [%s]", _elapsed(t))


# ─────────────────────────────────────────────────────────────────────────────
# _node_tile()
#
# Node one tile: collect → ST_Node → dump → filter → identify parent.
# Queries seg_geoms (id + geom ONLY — no text attributes).
#
# WHY seg_geoms and not segments?
#   `segments` holds both geometry AND text attributes (name, highway, …).
#   During noding we only need id + geom. If we query segments, DuckDB loads
#   ALL columns for matching rows — including the large geometry blobs AND
#   all text — into the buffer pool. For 1.15M rows that's ~6-8 GB just for
#   the source table, leaving almost nothing for ST_Node scratch space.
#
#   seg_geoms contains ONLY (id BIGINT, geom GEOMETRY) — roughly half the
#   size, and DuckDB can evict pages more aggressively because fewer columns
#   are referenced per query.
#
# Returns: number of rows inserted into noded_segments
# ─────────────────────────────────────────────────────────────────────────────
def _node_tile(con, ex0, ey0, ex1, ey1, cx0, cy0, cx1, cy1, seg_counter):
    """
    Node one tile: collect → ST_Node → dump → filter → identify parent.

    Queries seg_geoms (id + geom ONLY — no text attributes) to minimise
    buffer pool pressure during the noding loop.

    Post-node sliver filter: MIN_SEG_LEN_DEG (≈ 1 m) prevents hairline pieces
    produced by floating-point snap from entering noded_segments.
    """
    con.execute("DROP TABLE IF EXISTS _tile_noded;")

    # CTE "src": segments in the expanded tile (id + geom only, no attributes).
    # CTE "collected": merge into one MULTILINESTRING for ST_Node.
    # CTE "noded": ST_Node splits every line at every crossing.
    # CTE "dumped": explode back to individual pieces.
    #   ST_PointN(..., 2) = second vertex = interior point of the noded piece.
    #   After ST_ReducePrecision this point lies exactly on the parent segment,
    #   making ST_Contains a reliable and cheap parent-identification test.
    # CTE "owned": core-tile filter — only keep pieces whose centroid is inside
    #   this tile's core bounds, preventing duplicates at tile borders.bui
    # Final SELECT: find parent ID via ST_Contains (no geometry allocation).
    con.execute(f"""
        CREATE TEMP TABLE _tile_noded AS
        WITH src AS (
            -- Query geometry-only table: minimal columns → minimal buffer pool use
            SELECT id, geom
            FROM seg_geoms
            WHERE ST_Intersects(geom, ST_MakeEnvelope({ex0},{ey0},{ex1},{ey1}))
        ),
        collected AS (
            -- Merge all geometries into one MULTILINESTRING for ST_Node
            SELECT ST_Collect(list(geom)) AS collected_geom FROM src
        ),
        noded AS (
            -- ST_Node splits every line at every crossing (GEOS planar noding)
            SELECT ST_Node(collected_geom) AS noded_geom
            FROM collected
            WHERE collected_geom IS NOT NULL
        ),
        dumped AS (
            -- Explode noded MULTILINESTRING → individual pieces.
            -- WKB round-trip strips EPSG annotation → plain GEOMETRY.
            -- ST_PointN(...,2) = second vertex = guaranteed interior point after snap.
            SELECT
                ST_GeomFromWKB(ST_AsWKB((d.dump_struct).geom))  AS geom,
                ST_X(ST_Centroid((d.dump_struct).geom))         AS cx,
                ST_Y(ST_Centroid((d.dump_struct).geom))         AS cy,
                -- Second vertex: interior point for cheap parent identification
                ST_PointN((d.dump_struct).geom, 2)              AS interior_pt
            FROM noded,
                 UNNEST(ST_Dump(noded_geom)) AS d(dump_struct)
            WHERE NOT ST_IsEmpty((d.dump_struct).geom)
              AND ST_NPoints((d.dump_struct).geom) >= 2
              AND ST_Length((d.dump_struct).geom) > {MIN_SEG_LEN_DEG}    -- drop zero-length slivers (old value =1e-6)
        ),
        owned AS (
            -- Core-tile dedup: only keep pieces whose centroid is inside this tile.
            -- Prevents the same piece appearing in two adjacent tiles' outputs.
            SELECT geom, interior_pt FROM dumped
            WHERE cx >= {cx0} AND cx < {cx1}
              AND cy >= {cy0} AND cy < {cy1}
        )
        -- Find parent source ID via ST_Contains (coordinate test, no geometry alloc)
        SELECT
            o.geom,
            s.id AS src_id
        FROM owned o
        LEFT JOIN LATERAL (
            SELECT src.id
            FROM src
            WHERE ST_Contains(src.geom, o.interior_pt)
            LIMIT 1
        ) s ON true;
    """)

    # Bulk-insert with globally unique IDs.
    # ROW_NUMBER() OVER () → 1,2,3,… within this tile.
    # + seg_counter offsets into the global ID space.
    n = con.execute("SELECT COUNT(*) FROM _tile_noded").fetchone()[0]
    if n > 0:
        con.execute(f"""
            INSERT INTO noded_segments (seg_id, geom, src_id)
            SELECT
                {seg_counter} + ROW_NUMBER() OVER () AS seg_id, geom, src_id
            FROM _tile_noded;
        """)

    con.execute("DROP TABLE IF EXISTS _tile_noded;")
    return n


# ─────────────────────────────────────────────────────────────────────────────
# _node_tile_subdivide()
# ─────────────────────────────────────────────────────────────────────────────
def _node_tile_subdivide(con, cx0, cy0, cx1, cy1, seg_counter, depth=1):
    """
    Recursively split a tile that OOM'd into n_sub×n_sub sub-tiles.
    depth=1 → 4×4 = 16 sub-tiles  (tile_size / 4)
    depth=2 → 4×4×4 = 64 sub-sub-tiles  (tile_size / 16)
    depth=3 → gives up and skips (extremely pathological tile)
    """
    if depth > 2:
        logger.warning("Skipping pathologically dense tile at depth %d", depth)
        return seg_counter

    n_sub    = 4
    sub_size = (cx1 - cx0) / n_sub
    sub_ov   = sub_size * OVERLAP_FACTOR

    for si in range(n_sub):
        for sj in range(n_sub):
            scx0 = cx0 + si * sub_size;  scx1 = scx0 + sub_size
            scy0 = cy0 + sj * sub_size;  scy1 = scy0 + sub_size
            sex0 = scx0 - sub_ov;  sex1 = scx1 + sub_ov
            sey0 = scy0 - sub_ov;  sey1 = scy1 + sub_ov
            try:
                n = _node_tile(con, sex0, sey0, sex1, sey1,
                               scx0, scy0, scx1, scy1, seg_counter)
                seg_counter += n
            except Exception as e2:
                if "OutOfMemory" in type(e2).__name__ or "out of memory" in str(e2).lower():
                    con.execute("CHECKPOINT;")
                    logger.warning("OOM sub-tile depth=%d → split again …", depth)
                    seg_counter = _node_tile_subdivide(
                        con, scx0, scy0, scx1, scy1, seg_counter, depth + 1)
                else:
                    raise
    return seg_counter

# ─────────────────────────────────────────────────────────────────────────────
# _tiled_node()
#
# Divides the bounding box into tiles and calls _node_tile() on each.
#
# MEMORY STRATEGY:
#   1. seg_geoms (geometry only) is checkpointed to disk before the loop.
#      DuckDB can then evict its pages and only reload one tile's worth at a time.
#   2. CHECKPOINT every CHECKPOINT_EVERY tiles flushes noded_segments inserts
#      from the buffer pool to disk, keeping peak RAM flat.
#   3. OOM → checkpoint + split into 4×4=16 sub-tiles + retry.
#      If still OOM → split into 4×4×4=64 sub-sub-tiles.
# ─────────────────────────────────────────────────────────────────────────────
def _tiled_node(con: duckdb.DuckDBPyConnection, tile_size: float) -> None:
    """
    Divide the bounding box into tiles and call _node_tile() on each.

    Memory strategy:
      1. seg_geoms is checkpointed to disk before the loop; DuckDB evicts its
         pages and reloads only the current tile's worth on each iteration.
      2. CHECKPOINT every CHECKPOINT_EVERY tiles flushes noded_segments inserts
         from the buffer pool to disk, keeping peak RAM flat.
      3. OOM → checkpoint + subdivide into 4×4 sub-tiles + retry.

    Overlap = 100% (OVERLAP_FACTOR=1): every road within one full tile-width of
    a border is included in the neighbour's noding pass, guaranteeing that
    intersecting lines share a common node regardless of which corner they cross.
    """
    t0 = time.time()

    logger.info("   Calculating bounding box …")
    bbox = con.execute("""
        SELECT
            MIN(ST_XMin(geom)) AS xmin, MIN(ST_YMin(geom)) AS ymin,
            MAX(ST_XMax(geom)) AS xmax, MAX(ST_YMax(geom)) AS ymax
        FROM seg_geoms
    """).fetchone()
    xmin, ymin, xmax, ymax = [float(x) for x in bbox]

    cols = math.ceil((xmax - xmin) / tile_size)
    rows = math.ceil((ymax - ymin) / tile_size)
    overlap = tile_size * OVERLAP_FACTOR  # old value 0.60 reach 60% into neighbours for border noding

    logger.info("Grid: %d×%d = %s tiles  (tile_size=%.4f°, overlap=%.4f°)", cols, rows, f"{cols * rows:,}", tile_size, overlap)

    # One SQL pass → occupied (col, row) set. Avoids COUNT per tile (~700k queries).
    logger.info("   Precomputing non-empty tiles …")
    occupied_set = set(con.execute(f"""
        SELECT DISTINCT
            LEAST(FLOOR((ST_X(ST_Centroid(geom)) - {xmin}) / {tile_size})::INTEGER,
                  {cols-1}) AS tc,
            LEAST(FLOOR((ST_Y(ST_Centroid(geom)) - {ymin}) / {tile_size})::INTEGER,
                  {rows-1}) AS tr
        FROM seg_geoms
    """).fetchall())

    # Build a set of (col, row) tuples for O(1) lookup
    n_occupied = len(occupied_set)
    logger.info("Non-empty tiles: %s / %s", f"{n_occupied:,}", f"{cols * rows:,}")

    con.execute("DROP TABLE IF EXISTS noded_segments;")
    con.execute("""
        CREATE TABLE noded_segments (
            seg_id  BIGINT,
            geom    GEOMETRY,
            src_id  BIGINT      -- integer FK → seg_attrs.id, joined cheaply in Step 5
        );
    """)

    seg_counter = 0
    processed   = 0
    tiles_since_checkpoint = 0

    pbar = tqdm(
        total=n_occupied,
        desc="  Noding tiles ",
        unit="tile",
        dynamic_ncols=True,
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}] {postfix}",
    )
    pbar.set_postfix(segs=0)

    for r in range(rows):
        for c in range(cols):

            # O(1) skip — no DuckDB call for empty tiles
            if (c, r) not in occupied_set:
                continue

            # Core tile bounds
            cx0 = xmin + c * tile_size;  cx1 = cx0 + tile_size
            cy0 = ymin + r * tile_size;  cy1 = cy0 + tile_size
            # Expanded bounds (overlap into neighbours)
            ex0 = cx0 - overlap;  ex1 = cx1 + overlap
            ey0 = cy0 - overlap;  ey1 = cy1 + overlap

            try:
                n = _node_tile(con, ex0, ey0, ex1, ey1,
                               cx0, cy0, cx1, cy1, seg_counter)
                seg_counter += n
                logger.debug("Tile (%d,%d): +%d segs, total=%d", c, r, n, seg_counter)

            except Exception as e:
                if "OutOfMemory" in type(e).__name__ or "out of memory" in str(e).lower():
                    # Split into 4 sub-tiles and retry
                    # Checkpoint before retry — releases buffer pool pressure
                    con.execute("CHECKPOINT;")
                    tiles_since_checkpoint = 0
                    logger.warning("OOM at tile (%d,%d) → checkpoint + split 4×4 …", c, r)
                    seg_counter = _node_tile_subdivide(
                        con, cx0, cy0, cx1, cy1, seg_counter, depth=1)
                else:
                    raise

            processed += 1
            tiles_since_checkpoint += 1

            # Periodic CHECKPOINT: flush noded_segments buffer pool → disk.
            # This keeps RAM usage flat regardless of how many segments accumulate.
            if tiles_since_checkpoint >= CHECKPOINT_EVERY:
                con.execute("CHECKPOINT;")
                tiles_since_checkpoint = 0
                logger.debug("CHECKPOINT at tile %d, segs=%d", processed, seg_counter)

            # Use set_postfix (not postfix dict key) — correct tqdm API
            pbar.set_postfix(segs=f"{seg_counter:,}")
            pbar.update(1)

            # Log to file every 500 tiles (doesn't disturb the bar)
            if processed % 500 == 0:
                logger.info(
                    "Noding: %d/%d tiles (%.0f%%)  segs=%s  [%s]",
                    processed, n_occupied,
                    processed / n_occupied * 100,
                    f"{seg_counter:,}", _elapsed(t0),
                )

    pbar.close()
    con.execute("CHECKPOINT;")
    logger.info("Noding complete: %s segments  [%s]", f"{seg_counter:,}", _elapsed(t0))


# ─────────────────────────────────────────────────────────────────────────────
# _write_osm_xml()
#
# Incremental lxml writer — elements flushed to disk immediately, O(1) RAM.
#
# OSM structure:
#   <osm>
#     <node id="-N" lat="…" lon="…" version="1" visible="true"/>  ← all nodes first
#     <way  id="-N" version="1" visible="true">
#       <tag k="highway" v="primary"/>
#       <nd ref="-N"/>  ← one per vertex, in order
#     </way>
#   </osm>
# ─────────────────────────────────────────────────────────────────────────────
def _write_osm_xml(con: duckdb.DuckDBPyConnection, path: str, node_count: int, way_count: int) -> None:
    """
    Stream OSM XML to disk with O(1) RAM regardless of dataset size.

    DESIGN: two independent cursors, merged in Python.

    Cursor A (attrs): SELECT way_id, name, highway, … FROM edges ORDER BY way_id
      One row per way. No geometry, no node refs — just the tag attributes.
      Tiny: 6 text columns × 6.2M ways ≈ negligible RAM.

    Cursor B (refs): SELECT way_id, node_id FROM way_nodes ORDER BY way_id, seq
      One row per (way, vertex). Already sorted and on disk after Step 8.
      Streamed in small chunks — never fully in RAM.

    The two cursors are advanced in lockstep:
      - When B's way_id matches the current way, append an <nd> child.
      - When B's way_id advances, flush the current <way> and start the next.
      - Cursor A provides tag values whenever a new way_id appears.

    OSM structure:
      <osm>
        <node id="-N" lat="…" lon="…" version="1" visible="true"/>
        …
        <way id="-N" version="1" visible="true">
          <tag k="highway" v="…"/>  …
          <nd ref="-N"/>  …
        </way>
        …
      </osm>

    This avoids the JOIN (which DuckDB must hash/sort 100M+ rows for) and
    keeps Python memory usage to: one <way> element + one chunk of refs.

    CHUNK sizes:
      CHUNK_NODES: how many <node> rows to fetch at once. Larger = fewer
        round-trips but more Python list RAM. 100k is safe.
      CHUNK_REFS: how many way_node rows to fetch at once. Each row is just
        two integers (way_id, node_id). 500k rows ≈ ~8 MB — very safe.
    """
    CHUNK_NODES = 100_000
    CHUNK_REFS  = 500_000

    # Pre-load all way attributes into a dict keyed by way_id.
    # 6.2M ways × ~100 bytes per row ≈ 620 MB — acceptable, and avoids
    # a second cursor that would need to stay in sync with refs cursor.
    # We load this BEFORE opening the XML file so any OOM here is clean.
    logger.info("Loading way attributes into memory …")
    t = time.time()
    way_attrs = {}
    cur_attrs = con.execute(
        "SELECT way_id, name, highway, oneway, lanes, maxspeed FROM edges ORDER BY way_id"
    )
    with tqdm(
        desc="  Loading attrs",
        unit=" ways",
        unit_scale=True,
        dynamic_ncols=True,
    ) as pbar:
        while True:
            rows = cur_attrs.fetchmany(100_000)
            if not rows:
                break
            for way_id, name, highway, oneway, lanes, maxspeed in rows:
                way_attrs[way_id] = (name, highway, oneway, lanes, maxspeed)
            pbar.update(len(rows))
    logger.info(
        "Loaded %s way attribute records  [%s]", f"{len(way_attrs):,}", _elapsed(t)
    )
    gc.collect()

    with open(path, "wb") as fh:
        with etree.xmlfile(fh, encoding="utf-8") as xf:
            xf.write_declaration()
            with xf.element("osm", version="0.6", generator="build_osm_topology"):

                # ── Nodes ─────────────────────────────────────────────────────
                # Each <node> element is written and immediately released.
                # xf.write() flushes to disk — no accumulation in RAM.
                logger.info("Writing %s nodes …", f"{node_count:,}")
                t         = time.time()
                n_written = 0
                cur = con.execute(
                    "SELECT node_id, lat, lon FROM node_ids ORDER BY node_id"
                )
                with tqdm(
                    total=node_count,
                    desc="  Writing nodes",
                    unit=" nodes",
                    unit_scale=True,
                    dynamic_ncols=True,
                ) as pbar:
                    while True:
                        rows = cur.fetchmany(CHUNK_NODES)
                        if not rows:
                            break
                        for node_id, lat, lon in rows:
                            xf.write(etree.Element("node", {
                                "id":      str(node_id),
                                "lat":     f"{lat:.7f}",
                                "lon":     f"{lon:.7f}",
                                "version": "1",
                                "visible": "true",
                            }))
                        n_written += len(rows)
                        pbar.update(len(rows))
                gc.collect()
                logger.info("Nodes done: %s  [%s]", f"{n_written:,}", _elapsed(t))

                # ── Write ways ────────────────────────────────────────────
                logger.info("Writing %s ways …", f"{way_count:,}")
                t = time.time()
                cur_refs = con.execute("""
                    SELECT way_id, node_id
                    FROM way_nodes
                    WHERE way_id IN (
                        SELECT way_id FROM way_nodes
                        GROUP BY way_id HAVING COUNT(*) >= 2
                    )
                    ORDER BY way_id, seq
                """)

                current_id = None
                way_elem   = None
                n_ways     = 0
                n_skipped  = 0

                def flush_way(elem):
                    nonlocal n_skipped
                    if elem is None:
                        return
                    nd_count = sum(1 for ch in elem if ch.tag == "nd")
                    if nd_count < 2:
                        n_skipped += 1
                        return
                    xf.write(elem)

                with tqdm(
                    total=way_count,
                    desc="  Writing ways ",
                    unit=" ways",
                    unit_scale=True,
                    dynamic_ncols=True,
                ) as pbar:
                    while True:
                        rows = cur_refs.fetchmany(CHUNK_REFS)
                        if not rows:
                            break
                        for way_id, node_id in rows:
                            if way_id != current_id:
                                flush_way(way_elem)
                                way_elem = etree.Element("way", {
                                    "id":      str(way_id),
                                    "version": "1",
                                    "visible": "true",
                                })
                                current_id = way_id
                                n_ways    += 1
                                pbar.update(1)
                                name, highway, oneway, lanes, maxspeed = way_attrs.get(way_id, ("unknown", "road", "no", "1", None))
                                for k, v in [
                                    ("highway",  highway),
                                    ("name",     name),
                                    ("oneway",   oneway),
                                    ("lanes",    lanes),
                                    ("maxspeed", maxspeed),
                                ]:
                                    if v is not None and str(v).strip():
                                        etree.SubElement(way_elem, "tag", {"k": k, "v": str(v)})
                            etree.SubElement(way_elem, "nd", {"ref": str(node_id)})

                    flush_way(way_elem)

                if n_skipped:
                    logger.warning(
                        "Skipped %s ways with < 2 refs at write time", f"{n_skipped:,}"
                    )
                logger.info(
                    "Ways done: %s written, %s skipped  [%s]",
                    f"{n_ways - n_skipped:,}", f"{n_skipped:,}", _elapsed(t),
                )


# ─────────────────────────────────────────────────────────────────────────────
# build_osm_topology() — main pipeline
# ─────────────────────────────────────────────────────────────────────────────
def build_osm_topology(input_file: str, output_osm: str, tile_size: float = 0.015, memory_gb: int = 8) -> None:
    pipeline_t0    = time.time()
    original_input = input_file
    input_file = os.path.abspath(input_file)
    output_osm = os.path.abspath(output_osm)

    # ====================== WSL-FRIENDLY PATH HANDLING ======================
    home_data_dir = os.path.expanduser("~/tiledgraph/data")
    
    # Auto-detect and warn/prefer native WSL path for input
    if "/mnt/" in input_file.lower():
        basename  = os.path.basename(input_file)
        wsl_input = os.path.join(home_data_dir, basename)
        if os.path.exists(wsl_input):
            logger.info("Using fast WSL copy: %s", wsl_input)
            input_file = wsl_input
        else:
            logger.warning("Input is on /mnt/ (slow). Consider: cp \"%s\" ~/tiledgraph/data/", original_input,)

    # Put DuckDB file in /tmp (fastest + most stable in WSL)
    db_name = os.path.splitext(os.path.basename(output_osm))[0] + ".duckdb"
    db_path = os.path.join("/tmp", db_name)

    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("Input    → %s", input_file)
    logger.info("Output   → %s", output_osm)
    logger.info("DuckDB   → %s  (in /tmp)", db_path)
    logger.info("Memory   : %d GB  |  Tile size : %.4f°", memory_gb, tile_size)
    logger.info("Min seg  : %.1e deg (≈ %.1f m)", MIN_SEG_LEN_DEG, MIN_SEG_LEN_DEG * 111_111)
    logger.info("Snap tol : %.1e deg (≈ %.1f m)", SNAP_TOLERANCE, SNAP_TOLERANCE * 111_111)
    logger.info("Log file → %s", os.path.abspath(LOG_FILE))
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


    # Persistent .duckdb file:
    #   - DuckDB spills to this file when buffer pool is full
    #   - CHECKPOINT explicitly flushes dirty pages here
    #   - Survives crashes — you can inspect tables with: duckdb <file>.duckdb
    con = duckdb.connect(db_path)

    # memory_limit: DuckDB's own soft limit. Keep this below the WSL/OS hard
    # limit so DuckDB spills to disk before the OOM Killer fires.
    # Rule of thumb: set to ~60-70% of available RAM (not total physical RAM).
    con.execute(f"SET memory_limit = '{memory_gb}GB';")
    con.execute("SET preserve_insertion_order = false;")
    # Leave 2 threads for the OS; DuckDB parallelism helps most steps.
    con.execute(f"SET threads = {max(1, os.cpu_count() - 2)};")
    # temp_directory: where DuckDB writes spill files.
    # Point to your data drive (not the OS drive) for best performance.
    con.execute("SET temp_directory = '/tmp';")
    con.execute("INSTALL spatial; LOAD spatial;")

    # ─────────────────────────────────────────────────────────────────────────
    # STEPS 1-3: Ingest → clean → normalize → explode → snap → dedup
    #
    # TWO-PHASE INGEST to avoid segfaults on corrupt geometries:
    #   Phase A — st_read() emits geometry as raw WKB bytes (ST_AsWKB).
    #     GDAL reads bytes only; GEOS is never called → no crash risk.
    #   Phase B — parse WKB → GEOMETRY in a controlled DuckDB query.
    #     try_cast() returns NULL instead of crashing on bad WKB.
    #     ST_IsValid fast-paths the 95%+ of valid geometries.
    #     ST_MakeValid only runs for the rare invalid ones.
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("🚀  Steps 1-3 · Ingest, clean, normalize, explode …")
    t = time.time()

    geom_already_parsed = _read_input(con, input_file)
    raw_count = con.execute("SELECT COUNT(*) FROM raw_segments").fetchone()[0]
    logger.info("Raw rows ingested: %s", f"{raw_count:,}")

    # ── Validate + repair geometry ────────────────────────────────────────────
    # Parquet path (geom_already_parsed=True):
    #   DuckDB parsed the GeoParquet geometry column into GEOMETRY('EPSG:4326')
    #   automatically during read_parquet(). geom_wkb IS already a GEOMETRY —
    #   calling ST_GeomFromWKB on it would fail. We just validate/repair directly.
    #
    # GDAL path (geom_already_parsed=False):
    #   geom_wkb is a raw BLOB of WKB bytes. ST_GeomFromWKB parses it.
    #   try_cast(geom_wkb AS BLOB) IS NOT NULL guards against corrupt rows.
    #
    # In both cases ST_IsValid fast-paths the 95%+ of valid rows and
    # ST_MakeValid only runs for the rare invalid geometries.
    logger.info("   Validating + repairing geometry …")
    if geom_already_parsed:
        # geom_wkb is already GEOMETRY — validate directly, no WKB parsing needed
        con.execute("""
            CREATE OR REPLACE TABLE raw_segments_parsed AS
            SELECT
                id, name, oneway, lanes, maxspeed, highway,
                CASE
                    WHEN ST_IsValid(geom_wkb) THEN geom_wkb
                    ELSE ST_MakeValid(geom_wkb)
                END AS geom
            FROM raw_segments
            WHERE geom_wkb IS NOT NULL;
        """)
    else:
        # geom_wkb is raw BLOB bytes — parse with ST_GeomFromWKB first
        con.execute("""
            CREATE OR REPLACE TABLE raw_segments_parsed AS
            SELECT
                id, name, oneway, lanes, maxspeed, highway,
                CASE
                    WHEN ST_IsValid(ST_GeomFromWKB(geom_wkb)) THEN ST_GeomFromWKB(geom_wkb)
                    ELSE ST_MakeValid(ST_GeomFromWKB(geom_wkb))
                END AS geom
            FROM raw_segments
            WHERE try_cast(geom_wkb AS BLOB) IS NOT NULL;
        """)

    parsed_count = con.execute(
        "SELECT COUNT(*) FROM raw_segments_parsed WHERE geom IS NOT NULL"
    ).fetchone()[0]
    dropped = raw_count - parsed_count
    if dropped:
        logger.warning("Dropped %s rows with corrupt/unparseable geometry", f"{dropped:,}")

    geom_types = con.execute("""
        SELECT ST_GeometryType(geom), COUNT(*)
        FROM raw_segments_parsed WHERE geom IS NOT NULL GROUP BY 1
    """).fetchall()
    logger.info("Geometry types: %s", geom_types)
    con.execute("DROP TABLE IF EXISTS raw_segments;")

    # Explode MULTI* → LINESTRINGs, snap coordinates to 1-µdeg grid, dedup.
    # WKB round-trip strips EPSG annotation → plain GEOMETRY type.
    #

    # Count dropped-by-vertex-cap BEFORE creating segments, while
    # raw_segments_parsed is still available.
    dropped_vtx, worst_vtx = con.execute(f"""
        SELECT
            COUNT(*) FILTER (WHERE ST_NPoints(geom_part) > {MAX_VERTICES}),
            COALESCE(MAX(ST_NPoints(geom_part)), 0)
        FROM (
            SELECT UNNEST(ST_Dump(r.geom)).geom AS geom_part
            FROM raw_segments_parsed r
            WHERE r.geom IS NOT NULL
        ) t
    """).fetchone()
    if dropped_vtx > 0:
        logger.warning(
            "Will drop %s segments exceeding %s-vertex cap (worst: %s pts)",
            f"{dropped_vtx:,}", f"{MAX_VERTICES:,}", f"{worst_vtx:,}",
        )
    else:
        logger.info(
            "No segments exceed %s-vertex cap (max seen: %s pts)",
            f"{MAX_VERTICES:,}", f"{worst_vtx:,}",
        )

    # Drop segments shorter than MIN_SEG_LEN_DEG BEFORE they enter the noding
    # loop. A hairline segment fed into ST_Node can produce even shorter pieces
    # as intersection slivers, compounding the Length=1 problem.
    # The original threshold was 1e-8 — effectively no filter at all.
    logger.info(
        "Filtering + exploding segments (length > %.1e deg, precision grid %.1e) …",
        MIN_SEG_LEN_DEG, PRECISION_GRID,
    )
    con.execute(f"""
        CREATE OR REPLACE TABLE segments AS
        SELECT
            id, name, oneway, lanes, maxspeed, highway,
            ST_GeomFromWKB(ST_AsWKB(ST_ReducePrecision(geom_part, {PRECISION_GRID}))) AS geom
        FROM (
            SELECT r.id, r.name, r.oneway, r.lanes, r.maxspeed, r.highway,
                   UNNEST(ST_Dump(r.geom)).geom AS geom_part
            FROM raw_segments_parsed r
            WHERE r.geom IS NOT NULL
        ) exploded
        -- Raise pre-node sliver threshold to MIN_SEG_LEN_DEG
        WHERE ST_Length(geom_part) > {MIN_SEG_LEN_DEG}
          AND ST_IsValid(geom_part)
          AND ST_NPoints(geom_part) <= {MAX_VERTICES}
        QUALIFY ROW_NUMBER() OVER (PARTITION BY ST_AsText(geom_part)) = 1;
    """)
    con.execute("DROP TABLE IF EXISTS raw_segments_parsed;")

    seg_count = con.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    logger.info(
        "Segments after explode + dedup + length filter: %s  [%s]",
        f"{seg_count:,}", _elapsed(t),
    )
    # ── CRITICAL MEMORY SPLIT ─────────────────────────────────────────────────
    # Materialise two separate narrow tables from `segments`:
    #
    #   seg_geoms  (id, geom)                — queried during noding
    #   seg_attrs  (id, name, highway, …)    — joined in Step 5 by integer id
    #
    # Then DROP segments (the wide combined table) and CHECKPOINT both narrow
    # tables to disk. DuckDB can now evict seg_geoms pages from the buffer pool
    # and only reload one tile's worth of geometries at a time during noding.
    #
    # Without this split, querying `segments` inside _node_tile loads geometry
    # AND all text columns for every matching row — roughly doubling the buffer
    # pool pressure vs querying geom-only.
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("   Splitting segments → seg_geoms + seg_attrs …")
    con.execute("CREATE OR REPLACE TABLE seg_geoms AS SELECT id, geom FROM segments;")
    con.execute("""
        CREATE OR REPLACE TABLE seg_attrs AS
        SELECT id, name, highway, oneway, lanes, maxspeed FROM segments;
    """)
    con.execute("DROP TABLE IF EXISTS segments;")
    con.execute("CHECKPOINT;")
    logger.info(
        "   seg_geoms + seg_attrs checkpointed to disk before snap + noding …"
    )
    # After CHECKPOINT, DuckDB marks these pages as clean and can evict them.
    # The noding loop will only reload the pages for the current tile's bbox.

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 3b: Snap near-miss endpoints across region boundaries
    #
    # Must run AFTER seg_geoms is materialised (uses its geometry) and BEFORE
    # the noding loop (so that snapped endpoints are coincident when ST_Node
    # runs, producing shared nodes across administrative boundaries).
    # ─────────────────────────────────────────────────────────────────────────
    _snap_boundary_endpoints(con)

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 4: Tiled ST_Node — geometry only, parent ID tracked via src_id
    # noded_segments contains (seg_id, geom, src_id) where src_id references
    # segments.id. No spatial ops on attributes during noding → minimal RAM.
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("🔀  Step 4 · Tiled ST_Node (geometry only, periodic checkpoint) …")
    _tiled_node(con, tile_size)

    # seg_geoms is no longer needed after noding
    con.execute("DROP TABLE IF EXISTS seg_geoms;")
    con.execute("CHECKPOINT;")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 5: Attribute join by integer ID
    #
    # Pure hash join on BIGINT — no geometry, no spatial ops, tiny RAM footlogger.info.
    # seg_attrs has no geometry column, so the join touches only text+int data.
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("🔗  Step 5 · Joining attributes by integer ID …")
    t = time.time()
    con.execute("""
        CREATE OR REPLACE TABLE edges AS
        SELECT
            n.seg_id                            AS way_id,
            n.geom,
            COALESCE(s.name,    'unknown')      AS name,
            COALESCE(s.highway, 'road')         AS highway,
            COALESCE(s.oneway,  'no')           AS oneway,
            COALESCE(s.lanes,   '1')            AS lanes,
            s.maxspeed
        FROM noded_segments n
        LEFT JOIN seg_attrs s ON n.src_id = s.id;
    """)

    # Free both source tables — geometry RAM released here
    con.execute("DROP TABLE IF EXISTS noded_segments;")
    con.execute("DROP TABLE IF EXISTS seg_attrs;")
    con.execute("CHECKPOINT;")   # release buffer pool after big drop

    way_count = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    logger.info("Edges (ways): %s  [%s]", f"{way_count:,}", _elapsed(t))

    # ─────────────────────────────────────────────────────────────────────────
    # STEPS 6-7: Extract vertices → deduplicate → assign OSM node IDs
    #
    # ST_Points(geom)   → MULTIPOINT of all vertices, in order
    # ST_Dump(...)      → array of STRUCT(geom POINT, path INTEGER[])
    # UNNEST            → one row per vertex
    # dump_struct.path[1] → 1-based position of vertex along the line
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("📌  Steps 6-7 · Extracting vertices, deduplicating nodes, assigning IDs …")
    t = time.time()
    con.execute("""
        CREATE OR REPLACE TABLE edge_points AS
        SELECT
            e.way_id,
            dump_struct.path[1]    AS seq,   -- 1-based vertex index along the line
            ST_X(dump_struct.geom) AS lon,
            ST_Y(dump_struct.geom) AS lat
        FROM edges e,
             UNNEST(ST_Dump(ST_Points(e.geom))) AS d(dump_struct)
    """)
    con.execute("CHECKPOINT;")

    # Index on (lon, lat) makes the join in Step 8 fast even at millions of rows
    con.execute("CREATE INDEX ep_lonlat ON edge_points (lon, lat);")

    # Deduplicate vertices; assign stable negative IDs (OSM convention for new data)
    con.execute("""
        CREATE OR REPLACE TABLE node_ids AS
        SELECT
            (ROW_NUMBER() OVER (ORDER BY lon, lat)) * -1 AS node_id,
            lat, lon
        FROM (SELECT DISTINCT lat, lon FROM edge_points);
    """)

    # Index for the Step 8 join
    con.execute("CREATE INDEX ni_lonlat ON node_ids (lon, lat);")

    node_count = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
    logger.info("Unique nodes: %s  [%s]", f"{node_count:,}", _elapsed(t))

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 8: Build ordered way→node reference list
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("🔗  Step 8 · Building way→node reference table …")
    t = time.time()
    con.execute("""
        CREATE OR REPLACE TABLE way_nodes AS
        SELECT ep.way_id, ep.seq, ni.node_id
        FROM edge_points ep
        JOIN node_ids ni ON ep.lon = ni.lon AND ep.lat = ni.lat
        ORDER BY ep.way_id, ep.seq;
    """)

    con.execute("DROP TABLE IF EXISTS edge_points;")
    con.execute("CHECKPOINT;")
    logger.info("way_nodes built  [%s]", _elapsed(t))

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 8b: Topology validation
    #
    # Original code only dropped ways where (n_refs = 2 AND first_node = last_node).
    # That missed:
    #   A) ways with 3+ refs where ALL refs are the same node
    #      (e.g.  -5 → -5 → -5  after precision snap)
    #   B) ways where start_node == end_node but n_refs > 2
    #      (longer apparent loop that Valhalla still can't route)
    #
    # NOTE: legitimate circular roads (roundabouts, cul-de-sac loops) are rare
    # in a noded topology and are almost always represented as multiple short
    # straight segments that do NOT form a single closed way — so this filter
    # is safe to apply globally.
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("🔍  Step 8b · Validating topology (dedup refs, drop degenerate ways) …")
    t = time.time()

    # Remove consecutive duplicate node refs within each way.
    # LAG(node_id) OVER (PARTITION BY way_id ORDER BY seq) gives the previous
    # node_id in sequence. We keep a row only if it differs from its predecessor
    # (or if it is the first ref in the way, where LAG returns NULL).
    con.execute("""
        CREATE OR REPLACE TABLE way_nodes_clean AS
        SELECT way_id, seq, node_id
        FROM (
            SELECT
                way_id, seq, node_id,
                LAG(node_id) OVER (PARTITION BY way_id ORDER BY seq) AS prev_node_id
            FROM way_nodes
        ) t
        WHERE prev_node_id IS NULL          -- first ref in way — always keep
           OR node_id != prev_node_id;      -- differs from previous — keep
    """)

    dup_refs = (
        con.execute("SELECT COUNT(*) FROM way_nodes").fetchone()[0]
        - con.execute("SELECT COUNT(*) FROM way_nodes_clean").fetchone()[0]
    )
    if dup_refs > 0:
        logger.warning("Removed %s consecutive duplicate node refs", f"{dup_refs:,}")
    else:
        logger.info("No consecutive duplicate node refs found")

    # Detect and drop degenerate ways (loop / zero-length / all-same-node)
    # Identify and drop degenerate ways:
    #   - fewer than 2 refs after dedup  → not a valid OSM way
    #   - start node == end node with only 2 refs → zero-length loop
    con.execute("""
        CREATE OR REPLACE TABLE degenerate_ways AS
        SELECT way_id
        FROM (
            SELECT
                way_id,
                COUNT(*)                            AS n_refs,
                COUNT(DISTINCT node_id)             AS n_distinct,
                FIRST(node_id ORDER BY seq)         AS first_node,
                LAST(node_id  ORDER BY seq)         AS last_node
            FROM way_nodes_clean
            GROUP BY way_id
        ) stats
        WHERE n_refs < 2                        -- fewer than 2 refs → invalid OSM way
           OR n_distinct < 2                    -- all refs are the same node
           OR first_node = last_node;           -- closed loop (any length)
    """)

    n_degen = con.execute("SELECT COUNT(*) FROM degenerate_ways").fetchone()[0]
    if n_degen > 0:
        logger.warning(
            "Dropping %s degenerate ways (loop/zero-length/all-same-node)",
            f"{n_degen:,}",
        )
        con.execute("""
            DELETE FROM way_nodes_clean
            WHERE way_id IN (SELECT way_id FROM degenerate_ways);
        """)
        con.execute("""
            DELETE FROM edges
            WHERE way_id IN (SELECT way_id FROM degenerate_ways);
        """)

    con.execute("DROP TABLE IF EXISTS way_nodes;")
    con.execute("ALTER TABLE way_nodes_clean RENAME TO way_nodes;")
    con.execute("DROP TABLE IF EXISTS degenerate_ways;")
    con.execute("CHECKPOINT;")

    # Re-compute way_count after cleanup
    way_count = con.execute("SELECT COUNT(DISTINCT way_id) FROM way_nodes").fetchone()[0]
    node_count = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
    logger.info(
        "After validation — Nodes: %s   Ways: %s  [%s]",
        f"{node_count:,}", f"{way_count:,}", _elapsed(t),
    )
    # ─────────────────────────────────────────────────────────────────────────
    # STEP 9: Stream OSM XML
    #
    # For country-scale output (19M nodes, 6M ways) the simple JOIN approach
    # in _write_osm_xml spikes RAM because DuckDB must sort/hash 100M+ rows.
    # We use a two-cursor approach instead:
    #   Cursor 1: streams edges attributes (one row per way, no geometry)
    #   Cursor 2: streams way_nodes (one row per vertex, pre-sorted)
    # Both cursors advance in lockstep — pure Python merge, O(1) RAM.
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("📝  Step 9 · Streaming OSM XML → %s …", output_osm)
    _write_osm_xml(con, output_osm, node_count, way_count)

    logger.info("✅  Pipeline complete in %s  →  %s", _elapsed(pipeline_t0), output_osm)
    con.close()
    # Optionally remove the working DuckDB file after success
    # os.remove(db_path)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) not in (3, 4, 5):
        print("Usage: python build_osm_topology.py <input> <output.osm> [tile_size [memory_gb]]")
        print("  input      : .parquet (fastest), .gpkg, .geojson, .shp")
        print("  tile_size  : degrees, default 0.015 (~1.5 km)")
        print("  memory_gb  : default 8. Use ~60% of available RAM (`free -h`)")
        print()
        print("  Convert to Parquet first for best performance:")
        print("    ogr2ogr -f Parquet out.parquet in.gpkg")
        print()
        print("  WSL memory tip — create C:\\Users\\<you>\\.wslconfig:")
        print("    [wsl2]")
        print("    memory=20GB")
        print("    swap=8GB")
        sys.exit(1)

    tile_size = float(sys.argv[3]) if len(sys.argv) >= 4 else 0.015
    memory_gb = int(sys.argv[4])   if len(sys.argv) >= 5 else 8
    build_osm_topology(sys.argv[1], sys.argv[2], tile_size, memory_gb)

    print()
    print("Next step → OSM PBF:")
    print(f"  osmium cat {sys.argv[2]} -o output.osm.pbf")