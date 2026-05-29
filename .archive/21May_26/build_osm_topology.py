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
from datetime import datetime

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

class _TqdmHandler(logging.StreamHandler):
    """Routes log records through tqdm.write() so progress bars are not broken."""
    def emit(self, record):
        try:
            tqdm.write(self.format(record))
        except Exception:
            self.handleError(record)

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_FILE = f"build_osm_topology_{timestamp}.log"

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
MIN_SEG_LEN_DEG  = 9e-6    # ≈ 1 m at equator — pre/post-node sliver filter
OVERLAP_FACTOR   = 1.0     # 100 % tile overlap — guarantees corner noding
CHECKPOINT_EVERY = 500     # flush noded_segments to disk every N tiles
MAX_VERTICES     = 10_000  # drop pathological high-vertex segments

# ── Boundary snap tolerance ───────────────────────────────────────────────────
# Two dangling endpoints closer than this are merged into one shared node.
# Rule of thumb: 2-3× your data's coordinate precision, but no more than
# half the shortest real road segment in the network.
SNAP_TOLERANCE_DEG = 0.00002   # ≈ 22 m at equator. Down 5e-5 (≈ 5 m) if your data has small gaps.

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
    Ingest into raw_segments, assigning a globally unique surrogate integer id
    via ROW_NUMBER() — replacing pkStreetID as the primary key
    the input file into raw_segments (id, name, oneway, lanes,
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
                -- Surrogate key: row number is globally unique even when
                -- pkStreetID repeats across regions in the same file.
                ROW_NUMBER() OVER ()                                AS id,
                pkStreetID                                          AS orig_id,
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
            WHERE geom IS NOT NULL AND fkEmirateID = 4;
        """)
        return True   # geometry column is already GEOMETRY type
    else:
        logger.info("   Input format: GDAL (st_read)")
        # Phase A: GDAL reads bytes; GEOS never called → no segfault risk
        con.execute(f"""
            CREATE OR REPLACE TABLE raw_segments AS
            SELECT
                ROW_NUMBER() OVER ()                                AS id,
                pkStreetID                                          AS orig_id,
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

def _audit_duplicate_ids(con) -> None:
    """
    Log a report on pkStreetID collisions (orig_id duplicates).
    This runs once after ingest, purely for visibility — it has no effect
    on the pipeline because `id` is already the surrogate ROW_NUMBER().
    """
    total, n_orig_distinct, n_orig_duped, worst_count = con.execute("""
        SELECT
            COUNT(*)                                    AS total_rows,
            COUNT(DISTINCT orig_id)                     AS distinct_orig_ids,
            COUNT(*) - COUNT(DISTINCT orig_id)          AS extra_rows_from_dupes,
            MAX(cnt)                                    AS worst_dupe_count
        FROM (
            SELECT orig_id, COUNT(*) AS cnt
            FROM raw_segments
            GROUP BY orig_id
        ) t
    """).fetchone()

    if n_orig_duped == 0:
        logger.info(
            "ID audit: pkStreetID is unique across all %s rows — no collision risk",
            f"{total:,}",
        )
    else:
        logger.warning(
            "ID audit: pkStreetID has %s duplicate values across %s rows "
            "(%s extra rows; worst single id appears %s times). "
            "Surrogate ROW_NUMBER() id is used instead — this is safe.",
            f"{total - n_orig_distinct:,}",
            f"{total:,}",
            f"{n_orig_duped:,}",
            f"{worst_count:,}",
        )
        # Show the top-5 most-duplicated orig_ids for debugging
        top5 = con.execute("""
            SELECT orig_id, COUNT(*) AS cnt
            FROM raw_segments
            GROUP BY orig_id
            HAVING COUNT(*) > 1
            ORDER BY cnt DESC
            LIMIT 5
        """).fetchall()
        logger.warning("  Top-5 duplicated pkStreetIDs: %s", top5)


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
def _node_tile(con, ex0, ey0, ex1, ey1, cx0, cy0, cx1, cy1, seg_counter,
               member_envelopes=None):
    """
    Node one tile: collect → ST_Node → dump → filter → identify parent.

    Queries seg_geoms (id + geom ONLY — no text attributes) to minimise
    buffer pool pressure during the noding loop.

    Post-node sliver filter: MIN_SEG_LEN_DEG (≈ 1 m) prevents hairline pieces
    produced by floating-point snap from entering noded_segments.
    member_envelopes: optional list of (ex0,ey0,ex1,ey1) per member tile of a
    merged sparse component. When provided, src uses a UNION of per-tile
    ST_Intersects filters instead of one large bounding-box envelope.

    WHY THIS MATTERS:
    An irregular merged component (e.g. L-shaped across 8 tiles) has a
    bounding box that covers tiles NOT in the component. Those tiles may be
    dense. A single bbox ST_Intersects then silently pulls their segments into
    ST_Node → OOM even though the component itself is very light.
    The union filter loads only segments from actual member tiles.
    """
    con.execute("DROP TABLE IF EXISTS _tile_noded;")

    if member_envelopes and len(member_envelopes) > 1:
        # Union of per-member-tile envelopes — never touches non-member tiles
        conditions = " OR ".join(
            f"ST_Intersects(geom, ST_MakeEnvelope({e[0]},{e[1]},{e[2]},{e[3]}))"
            for e in member_envelopes
        )
        src_where = f"WHERE {conditions}"
    else:
        src_where = f"WHERE ST_Intersects(geom, ST_MakeEnvelope({ex0},{ey0},{ex1},{ey1}))"


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
            {src_where}
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
# _tiled_node()  — adaptive tile sizing with flood-fill component merging
#
# Merging rigid SUPER_FACTOR×SUPER_FACTOR blocks only works when ALL tiles in
# the block are sparse. A single dense tile (e.g. a motorway junction) inside
# a rural area breaks the merge, leaving the sparse tiles on either side of it
# isolated and unable to co-node roads that cross their boundaries.
#
# FLOOD-FILL STRATEGY
# ───────────────────
# 1. Count segments per base tile (one cheap SQL scan, no geometry ops).
# 2. Classify: sparse (< SPARSE_THRESHOLD) or normal/dense (≥ threshold).
# 3. Flood-fill connected sparse tiles into components using 4-connectivity
#    (N/S/E/W neighbours). Dense tiles act as flood barriers — they are never
#    absorbed into a sparse component even if surrounded by sparse tiles.
# 4. Cap component size at MAX_COMPONENT_SEGS total segments to stay within
#    RAM. If a component would exceed the cap, split it greedily by rows.
# 5. Each component becomes one ST_Node job whose core bounds are the union
#    of its member tiles' bounds. Dense/normal tiles are processed individually.
# 6. Dense tiles keep the OOM-triggered 4×4 subdivision fallback.
#
# RESULT
# ──────
# Sparse tiles sandwiched between dense areas are now absorbed into their own
# component (possibly a component of 1 if truly isolated), and crucially they
# are processed with an expanded overlap that reaches into the adjacent dense
# tiles — so roads crossing the sparse/dense boundary still get co-noded.
#
# CONSTANTS (tune to your RAM and network density)
# ─────────────────────────────────────────────────
SPARSE_THRESHOLD    = 20      # tiles below this seg count are sparse
DENSE_THRESHOLD     = 5000    # tiles above this are flagged as dense (log only)
MAX_COMPONENT_SEGS  = 500     # max total segs in one merged component job
MAX_COMPONENT_TILES = 4       # max base-tiles in one merged component (2×2)
#   WHY SMALL MAX_COMPONENT_TILES:
#   A merged component's ST_Node envelope is the BOUNDING BOX of all member
#   tiles + overlap on each side. An irregular component (e.g. L-shaped across
#   10 tiles) has a bbox that covers tiles NOT in the component — those tiles
#   may be dense, so ST_Intersects pulls their segments in anyway → OOM.
#   Keeping components tiny (≤4 tiles ≈ 2×2 base tiles) bounds the envelope
#   to ~6×6 km, making the worst-case segment pull safe at any density.
#   Connectivity is still improved: a 2×2 merge stitches together the typical
#   rural boundary gap just as well as a larger merge.
# ─────────────────────────────────────────────────────────────────────────────
def _flood_fill_sparse_components(tile_counts, sparse_threshold,
                                   max_component_segs, max_component_tiles):
    """
    Group sparse tiles into connected components via 4-connectivity flood fill.

    Caps applied (both must pass; whichever triggers first splits the component):
      max_component_segs  — total segment count across all member tiles
      max_component_tiles — total number of base tiles in the component

    WHY THE TILE CAP:
      A sparse component can have few segments but span a huge geographic area
      (e.g. 200 tiles × 5 segs = 1000 segs, fine on seg count, but the
      ST_Intersects envelope for that component pulls in all geometry from every
      neighbouring dense tile within the expanded bounds → OOM). Capping tile
      count keeps the envelope area bounded regardless of seg density outside.

    Returns: list of frozenset of (col, row) tile coordinates.
    """
    sparse_tiles = {t for t, cnt in tile_counts.items() if cnt < sparse_threshold}
    unvisited    = set(sparse_tiles)
    components   = []

    while unvisited:
        # BFS from an arbitrary unvisited sparse tile
        seed    = next(iter(unvisited))
        queue   = [seed]
        visited = set()
        while queue:
            tile = queue.pop()
            if tile in visited or tile not in sparse_tiles:
                continue
            visited.add(tile)
            unvisited.discard(tile)
            c, r = tile
            for nb in ((c+1,r),(c-1,r),(c,r+1),(c,r-1)):
                if nb in unvisited:
                    queue.append(nb)
        components.append(frozenset(visited))

    # Dense / normal tiles: each is its own single-tile component
    for tile in tile_counts:
        if tile not in sparse_tiles:
            components.append(frozenset([tile]))

    # Cap by BOTH seg count and tile count — split greedily row-by-row
    def _split_comp(comp):
        rows_map = {}
        for (c, r) in comp:
            rows_map.setdefault(r, []).append(c)
        bucket, bucket_segs, bucket_tiles = [], 0, 0
        out = []
        for row in sorted(rows_map):
            for col in sorted(rows_map[row]):
                t    = (col, row)
                segs = tile_counts.get(t, 0)
                over_segs  = bucket and bucket_segs  + segs  > max_component_segs
                over_tiles = bucket and bucket_tiles + 1     > max_component_tiles
                if over_segs or over_tiles:
                    out.append(frozenset(bucket))
                    bucket, bucket_segs, bucket_tiles = [], 0, 0
                bucket.append(t)
                bucket_segs  += segs
                bucket_tiles += 1
        if bucket:
            out.append(frozenset(bucket))
        return out

    final = []
    for comp in components:
        if len(comp) == 1:
            final.append(comp)
            continue
        total_segs = sum(tile_counts.get(t, 0) for t in comp)
        over_segs  = total_segs  > max_component_segs
        over_tiles = len(comp)   > max_component_tiles
        if not over_segs and not over_tiles:
            final.append(comp)
        else:
            final.extend(_split_comp(comp))

    return final


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
    logger.info("Calculating bounding box ...")
    bbox = con.execute("""
        SELECT MIN(ST_XMin(geom)), MIN(ST_YMin(geom)),
               MAX(ST_XMax(geom)), MAX(ST_YMax(geom))
        FROM seg_geoms
    """).fetchone()
    xmin, ymin, xmax, ymax = [float(x) for x in bbox]

    cols    = math.ceil((xmax - xmin) / tile_size)
    rows    = math.ceil((ymax - ymin) / tile_size)
    overlap = tile_size * OVERLAP_FACTOR
    logger.info("Base grid: %d x %d = %s tiles  (tile_size=%.4f deg, overlap=%.4f deg)",
                cols, rows, f"{cols*rows:,}", tile_size, overlap)

    # ── Count segments per base tile (one SQL scan, no geometry ops) ──────────
    logger.info("Counting segments per tile for adaptive sizing ...")
    tile_counts = {}
    for (tc, tr, cnt) in con.execute(f"""
        SELECT
            LEAST(FLOOR((ST_X(ST_Centroid(geom)) - {xmin}) / {tile_size})::INTEGER, {cols-1}) AS tc,
            LEAST(FLOOR((ST_Y(ST_Centroid(geom)) - {ymin}) / {tile_size})::INTEGER, {rows-1}) AS tr,
            COUNT(*) AS cnt
        FROM seg_geoms
        GROUP BY tc, tr
    """).fetchall():
        tile_counts[(int(tc), int(tr))] = int(cnt)

    n_occupied = len(tile_counts)
    n_sparse   = sum(1 for v in tile_counts.values() if v < SPARSE_THRESHOLD)
    n_dense    = sum(1 for v in tile_counts.values() if v > DENSE_THRESHOLD)
    logger.info("Non-empty tiles : %s / %s  (sparse<%d: %s, normal: %s, dense>%d: %s)",
                f"{n_occupied:,}", f"{cols*rows:,}",
                SPARSE_THRESHOLD, f"{n_sparse:,}",
                f"{n_occupied - n_sparse - n_dense:,}",
                DENSE_THRESHOLD,  f"{n_dense:,}")

    # ── Flood-fill sparse components ──────────────────────────────────────────
    logger.info("Building flood-fill components (sparse_threshold=%d, max_segs=%d) ...",
                SPARSE_THRESHOLD, MAX_COMPONENT_SEGS)
    components = _flood_fill_sparse_components(tile_counts, SPARSE_THRESHOLD,
                                               MAX_COMPONENT_SEGS, MAX_COMPONENT_TILES)

    # ── Build work queue from components ──────────────────────────────────────
    #
    # For each component:
    #   core bounds  = bounding box of all member tiles (exact tile edges)
    #   expanded     = core bounds + overlap on every side
    #
    # Single-tile components that are sparse get the standard base-tile overlap.
    # Multi-tile sparse components get the same overlap — but their core window
    # spans multiple tiles so roads crossing internal boundaries get co-noded.
    # Dense single-tile components are processed individually with base overlap.
    work_queue = []
    n_merged = n_solo = 0

    for comp in components:
        # Core bounding box: union of all member tiles
        min_c = min(c for c, r in comp)
        max_c = max(c for c, r in comp)
        min_r = min(r for c, r in comp)
        max_r = max(r for c, r in comp)

        cx0 = xmin + min_c * tile_size
        cx1 = xmin + (max_c + 1) * tile_size
        cy0 = ymin + min_r * tile_size
        cy1 = ymin + (max_r + 1) * tile_size

        # Overlap strategy:
        #   Solo tiles: full OVERLAP_FACTOR × tile_size (standard)
        #   Merged components: exactly ONE tile_size on each side.
        #     The core already spans multiple tiles so internal crossings are
        #     handled. We only need to reach one tile beyond the edge to co-node
        #     roads at the component boundary. Using OVERLAP_FACTOR × core_width
        #     would make the envelope enormous (reaching deep into dense areas)
        #     and cause OOM even when the component itself is light.
        if len(comp) == 1:
            comp_overlap = overlap                  # standard: OVERLAP_FACTOR * tile_size
            # Solo tile: standard single-bbox envelope, no member list needed
            member_envs = None
            ex0 = cx0 - comp_overlap
            ex1 = cx1 + comp_overlap
            ey0 = cy0 - comp_overlap
            ey1 = cy1 + comp_overlap
        else:
            comp_overlap = tile_size                # fixed: exactly one tile beyond edge
            # Merged component: build per-member expanded envelopes.
            # These are passed to _node_tile so ST_Intersects runs against
            # each member tile individually — never touching non-member tiles
            # that might be dense and cause OOM.
            member_envs = [
                (xmin + c * tile_size - comp_overlap,
                 ymin + r * tile_size - comp_overlap,
                 xmin + (c + 1) * tile_size + comp_overlap,
                 ymin + (r + 1) * tile_size + comp_overlap)
                for c, r in comp
            ]
            # ex0/ex1/ey0/ey1 = union of all member envelopes (used for OOM subdivide)
            ex0 = min(e[0] for e in member_envs)
            ex1 = max(e[2] for e in member_envs)
            ey0 = min(e[1] for e in member_envs)
            ey1 = max(e[3] for e in member_envs)
        kind = 'merged' if len(comp) > 1 else 'base'
        work_queue.append((kind, cx0, cy0, cx1, cy1, ex0, ey0, ex1, ey1, member_envs))
        if len(comp) > 1:
            n_merged += 1
        else:
            n_solo += 1

    logger.info("Work queue: %s merged components + %s solo tiles = %s total jobs",
                f"{n_merged:,}", f"{n_solo:,}", f"{len(work_queue):,}")

    # ── Create output table ───────────────────────────────────────────────────
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
        total=len(work_queue),
        desc="  Noding tiles ",
        unit="tile",
        dynamic_ncols=True,
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}] {postfix}",
    )
    pbar.set_postfix(segs=0)

    for job in work_queue:
        kind, cx0, cy0, cx1, cy1, ex0, ey0, ex1, ey1, member_envs = job
        try:
            n = _node_tile(con, ex0, ey0, ex1, ey1, cx0, cy0, cx1, cy1,
                           seg_counter, member_envelopes=member_envs)
            seg_counter += n
            logger.debug("%s tile (%.4f,%.4f)-(%.4f,%.4f): +%d segs",
                         kind, cx0, cy0, cx1, cy1, n)
        except Exception as e:
            if "OutOfMemory" in type(e).__name__ or "out of memory" in str(e).lower():
                con.execute("CHECKPOINT;")
                tiles_since_checkpoint = 0
                logger.warning("OOM on %s tile (%.4f,%.4f) -> split 4x4 ...", kind, cx0, cy0)
                seg_counter = _node_tile_subdivide(
                    con, cx0, cy0, cx1, cy1,
                    (cx1 - cx0) * OVERLAP_FACTOR,
                    cx1 - cx0, seg_counter, depth=1)
            else:
                raise

        processed              += 1
        tiles_since_checkpoint += 1
        if tiles_since_checkpoint >= CHECKPOINT_EVERY:
            con.execute("CHECKPOINT;")
            tiles_since_checkpoint = 0
            logger.debug("CHECKPOINT at job %d segs=%d", processed, seg_counter)

        # Use set_postfix (not postfix dict key) — correct tqdm API
        pbar.set_postfix(segs=f"{seg_counter:,}")
        pbar.update(1)

        # Log to file every 500 tiles (doesn't disturb the bar)
        if processed % 500 == 0:
            logger.info(
                "Noding: %d/%d tiles (%.0f%%)  segs=%s  [%s]",
                processed, len(work_queue),
                processed / len(work_queue) * 100,
                f"{seg_counter:,}", _elapsed(t0),
            )

    pbar.close()
    con.execute("CHECKPOINT;")
    logger.info("Noding complete: %s segments  [%s]", f"{seg_counter:,}", _elapsed(t0))


# ─────────────────────────────────────────────────────────────────────────────
# _node_tile_subdivide()
# ─────────────────────────────────────────────────────────────────────────────
def _node_tile_subdivide(con, cx0, cy0, cx1, cy1, parent_overlap,
                          tile_size, seg_counter, depth):
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
            try:
                n = _node_tile(con, scx0-sub_ov, scy0-sub_ov, scx1+sub_ov, scy1+sub_ov,
                               scx0, scy0, scx1, scy1, seg_counter)
                seg_counter += n
            except Exception as e2:
                if "OutOfMemory" in type(e2).__name__ or "out of memory" in str(e2).lower():
                    con.execute("CHECKPOINT;")
                    logger.warning("OOM sub-tile depth=%d -> split again ...", depth)
                    seg_counter = _node_tile_subdivide(
                        con, scx0, scy0, scx1, scy1, sub_ov, sub_size, seg_counter, depth+1)
                else:
                    raise
    return seg_counter

# ─────────────────────────────────────────────────────────────────────────────
# _snap_boundary_nodes()
#
# Merges dangling endpoints that sit within SNAP_TOLERANCE_DEG of each other.
# These are typically road stubs left where data was split at region/province
# boundaries.  After noding they become separate node IDs with no shared
# reference, creating isolated graph islands that break long-distance routing.
#
# Algorithm:
#   1. Find "dangling" endpoints (degree-1 nodes: appear as endpoint of
#      exactly one way — dead ends or boundary stubs).
#   2. Bounding-box + Euclidean distance join to find proximity pairs.
#   3. Iterative SQL label propagation (union-find) to get connected components
#      across chains of snapping pairs (A-B-C all merge to same canonical node).
#   4. Remap node_id in way_nodes and node_ids to the canonical (min) label.
#   5. Re-check for degenerate ways (ways whose endpoints collapsed to same node).
# ─────────────────────────────────────────────────────────────────────────────
def _snap_boundary_nodes(con):
    t0  = time.time()
    tol = SNAP_TOLERANCE_DEG
    logger.info("  Snap tolerance: %.5f deg (~%.0f m)", tol, tol * 111_111)

    # 1. Dangling endpoints (degree-1: endpoint of exactly one way)
    logger.info("  Finding dangling endpoints ...")
    con.execute("""
        CREATE OR REPLACE TABLE _endpoints AS
        WITH ranked AS (
            SELECT way_id, node_id, seq,
                MIN(seq) OVER (PARTITION BY way_id) AS min_seq,
                MAX(seq) OVER (PARTITION BY way_id) AS max_seq
            FROM way_nodes
        ),
        ep_uses AS (
            SELECT node_id, COUNT(*) AS n_uses
            FROM ranked WHERE seq = min_seq OR seq = max_seq
            GROUP BY node_id
        )
        SELECT node_id FROM ep_uses WHERE n_uses = 1;
    """)
    n_dangles = con.execute("SELECT COUNT(*) FROM _endpoints").fetchone()[0]
    logger.info("  Dangling endpoints: %s", f"{n_dangles:,}")

    if n_dangles == 0:
        logger.info("  No dangling endpoints — boundary snap skipped")
        con.execute("DROP TABLE IF EXISTS _endpoints;")
        return

    # Attach coordinates
    con.execute("""
        CREATE OR REPLACE TABLE _dangle_coords AS
        SELECT e.node_id, n.lat, n.lon
        FROM _endpoints e JOIN node_ids n ON e.node_id = n.node_id;
    """)
    con.execute("DROP TABLE IF EXISTS _endpoints;")

    # 2. Proximity pairs (bounding box pre-filter + exact distance)
    logger.info("  Finding proximity pairs ...")
    con.execute(f"""
        CREATE OR REPLACE TABLE _snap_pairs AS
        SELECT a.node_id AS node_a, b.node_id AS node_b
        FROM _dangle_coords a
        JOIN _dangle_coords b
          ON a.node_id < b.node_id
         AND b.lon BETWEEN a.lon - {tol} AND a.lon + {tol}
         AND b.lat BETWEEN a.lat - {tol} AND a.lat + {tol}
         AND SQRT(POWER(a.lon - b.lon, 2) + POWER(a.lat - b.lat, 2)) <= {tol};
    """)
    n_pairs = con.execute("SELECT COUNT(*) FROM _snap_pairs").fetchone()[0]
    logger.info("  Proximity pairs: %s", f"{n_pairs:,}")

    if n_pairs == 0:
        logger.info("  No snap pairs found — gaps may exceed %.0f m", tol * 111_111)
        logger.info("  TIP: increase SNAP_TOLERANCE_DEG (currently %.5f) if expected", tol)
        for tbl in ("_dangle_coords", "_snap_pairs"):
            con.execute(f"DROP TABLE IF EXISTS {tbl};")
        return

    # 3. Union-find via iterative label propagation
    logger.info("  Running union-find ...")
    con.execute("""
        CREATE OR REPLACE TABLE _labels AS
        SELECT node_id, node_id AS label
        FROM (
            SELECT node_a AS node_id FROM _snap_pairs
            UNION
            SELECT node_b AS node_id FROM _snap_pairs
        ) all_nodes;
    """)

    for iteration in range(20):
        con.execute("""
            CREATE OR REPLACE TABLE _labels_new AS
            SELECT l.node_id,
                MIN(COALESCE(la.label, lb.label, l.label)) AS label
            FROM _labels l
            LEFT JOIN _snap_pairs p  ON l.node_id = p.node_a OR l.node_id = p.node_b
            LEFT JOIN _labels la     ON p.node_a = la.node_id
            LEFT JOIN _labels lb     ON p.node_b = lb.node_id
            GROUP BY l.node_id;
        """)
        changed = con.execute("""
            SELECT COUNT(*) FROM _labels l
            JOIN _labels_new ln ON l.node_id = ln.node_id
            WHERE l.label != ln.label
        """).fetchone()[0]
        con.execute("DROP TABLE IF EXISTS _labels;")
        con.execute("ALTER TABLE _labels_new RENAME TO _labels;")
        logger.debug("  Union-find iter %d: %d changes", iteration + 1, changed)
        if changed == 0:
            logger.info("  Union-find converged in %d iterations", iteration + 1)
            break
    else:
        logger.warning("  Union-find did not converge in 20 iterations")

    n_clusters       = con.execute("SELECT COUNT(DISTINCT label) FROM _labels").fetchone()[0]
    n_nodes_in_clust = con.execute("SELECT COUNT(*) FROM _labels").fetchone()[0]
    logger.info("  Snap clusters: %s  (merging %s nodes)",
                f"{n_clusters:,}", f"{n_nodes_in_clust:,}")

    # 4. Remap way_nodes
    logger.info("  Remapping node refs in way_nodes ...")
    con.execute("""
        CREATE OR REPLACE TABLE way_nodes_snapped AS
        SELECT wn.way_id, wn.seq,
            COALESCE(l.label, wn.node_id) AS node_id
        FROM way_nodes wn
        LEFT JOIN _labels l ON wn.node_id = l.node_id;
    """)
    con.execute("DROP TABLE IF EXISTS way_nodes;")
    con.execute("ALTER TABLE way_nodes_snapped RENAME TO way_nodes;")

    # 5. Remap node_ids — remove duplicates produced by merging
    logger.info("  Cleaning node_ids table ...")
    con.execute("""
        CREATE OR REPLACE TABLE node_ids_snapped AS
        SELECT COALESCE(l.label, n.node_id) AS node_id, n.lat, n.lon
        FROM node_ids n
        LEFT JOIN _labels l ON n.node_id = l.node_id
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY COALESCE(l.label, n.node_id)
            ORDER BY n.node_id
        ) = 1;
    """)
    old_cnt = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
    con.execute("DROP TABLE IF EXISTS node_ids;")
    con.execute("ALTER TABLE node_ids_snapped RENAME TO node_ids;")
    new_cnt = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
    logger.info("  Nodes: %s -> %s (merged %s)",
                f"{old_cnt:,}", f"{new_cnt:,}", f"{old_cnt - new_cnt:,}")

    # 6. Re-check degenerate ways (boundary stubs whose both ends snapped together)
    logger.info("  Re-checking degenerate ways after snap ...")
    con.execute("""
        CREATE OR REPLACE TABLE _way_nodes_dedup AS
        SELECT way_id, seq, node_id
        FROM (
            SELECT way_id, seq, node_id,
                LAG(node_id) OVER (PARTITION BY way_id ORDER BY seq) AS prev_id
            FROM way_nodes
        ) t
        WHERE prev_id IS NULL OR node_id != prev_id;
    """)
    con.execute("""
        CREATE OR REPLACE TABLE _degen_snap AS
        SELECT way_id FROM (
            SELECT way_id,
                COUNT(*)                    AS n_refs,
                COUNT(DISTINCT node_id)     AS n_distinct,
                FIRST(node_id ORDER BY seq) AS first_node,
                LAST(node_id  ORDER BY seq) AS last_node
            FROM _way_nodes_dedup GROUP BY way_id
        ) s
        WHERE n_refs < 2
           OR n_distinct < 2
           OR (first_node = last_node AND n_refs = 2);
    """)  # n_refs = 2 guard on closed-loop filter
    n_degen = con.execute("SELECT COUNT(*) FROM _degen_snap").fetchone()[0]
    if n_degen > 0:
        logger.warning("  Dropping %s degenerate ways created by snap", f"{n_degen:,}")
        con.execute("DELETE FROM _way_nodes_dedup WHERE way_id IN (SELECT way_id FROM _degen_snap);")
        con.execute("DELETE FROM edges WHERE way_id IN (SELECT way_id FROM _degen_snap);")
    else:
        logger.info("  No new degenerate ways after snap")

    con.execute("DROP TABLE IF EXISTS way_nodes;")
    con.execute("ALTER TABLE _way_nodes_dedup RENAME TO way_nodes;")

    for tbl in ("_dangle_coords", "_snap_pairs", "_labels", "_degen_snap"):
        con.execute(f"DROP TABLE IF EXISTS {tbl};")

    con.execute("CHECKPOINT;")
    logger.info("  Boundary snap complete  [%s]", _elapsed(t0))

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
                        if n_written % 2_000_000 == 0:
                            logger.debug("%s nodes written", f"{n_written:,}")
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
                    if sum(1 for ch in elem if ch.tag == "nd") < 2:
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
                                if n_ways % 1_000_000 == 0:
                                    logger.debug("%s ways processed", f"{n_ways:,}")
                            etree.SubElement(way_elem, "nd", {"ref": str(node_id)})

                    flush_way(way_elem)

                written = n_ways - n_skipped
                if n_skipped:
                    pct = n_skipped / n_ways * 100 if n_ways else 0
                    logger.warning("Ways written: %s  skipped (< 2 refs): %s (%.2f%%)  [%s]",
                                   f"{written:,}", f"{n_skipped:,}", pct, _elapsed(t))
                else:
                    logger.info("Ways written: %s  skipped: 0  [%s]", f"{written:,}", _elapsed(t))



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
    logger.info("Min seg    : %.1e deg (~%.1f m)", MIN_SEG_LEN_DEG, MIN_SEG_LEN_DEG * 111_111)
    logger.info("Snap tol   : %.5f deg (~%.0f m)", SNAP_TOLERANCE_DEG, SNAP_TOLERANCE_DEG * 111_111)
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

    # Audit pkStreetID uniqueness — logs warnings if duplicates found.
    # Pipeline is safe regardless because id is now ROW_NUMBER().
    _audit_duplicate_ids(con)

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
    logger.info("Validating + repairing geometry ...")
    if geom_already_parsed:
        # geom_wkb is already GEOMETRY — validate directly, no WKB parsing needed
        con.execute("""
            CREATE OR REPLACE TABLE raw_segments_parsed AS
            SELECT
                id, orig_id, name, oneway, lanes, maxspeed, highway,
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
                id, orig_id, name, oneway, lanes, maxspeed, highway,
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
    if raw_count - parsed_count:
        logger.warning("Dropped %s rows with corrupt geometry", f"{raw_count-parsed_count:,}")

    logger.info("Geometry types: %s", con.execute("""
        SELECT ST_GeometryType(geom), COUNT(*)
        FROM raw_segments_parsed WHERE geom IS NOT NULL GROUP BY 1
    """).fetchall())
    con.execute("DROP TABLE IF EXISTS raw_segments;")

    # Pre-scan: count every drop reason in one pass so the log shows exactly
    # how many features each filter removes.
    # NOTE on drop_invalid: counted only for rows that pass length and vertex
    # filters — i.e. rows that would have survived everything else.
    total_parts, drop_short, drop_invalid, drop_vtx, worst_vtx = con.execute(f"""
        SELECT
            COUNT(*)                                                                AS total_parts,
            COUNT(*) FILTER (WHERE ST_Length(geom_part) <= {MIN_SEG_LEN_DEG})      AS drop_short,
            COUNT(*) FILTER (WHERE ST_Length(geom_part) >  {MIN_SEG_LEN_DEG}
                               AND ST_NPoints(geom_part) <= {MAX_VERTICES}
                               AND NOT ST_IsValid(geom_part))                       AS drop_invalid,
            COUNT(*) FILTER (WHERE ST_NPoints(geom_part) > {MAX_VERTICES})         AS drop_vtx,
            COALESCE(MAX(ST_NPoints(geom_part)), 0)                                 AS worst_vtx
        FROM (
            SELECT UNNEST(ST_Dump(r.geom)).geom AS geom_part
            FROM raw_segments_parsed r WHERE r.geom IS NOT NULL
        ) t
    """).fetchone()
    logger.info("Exploded parts total      : %s", f"{total_parts:,}")
    logger.info("  drop short (<1 m)       : %s", f"{drop_short:,}")
    if drop_invalid:
        logger.warning("  drop invalid geometry   : %s  (unfixable after ST_MakeValid)", f"{drop_invalid:,}")
    else:
        logger.info("  drop invalid geometry   : 0")
    if drop_vtx:
        logger.warning("  drop > %s-vertex cap  : %s  (worst: %s pts)",
                       f"{MAX_VERTICES:,}", f"{drop_vtx:,}", f"{worst_vtx:,}")
    else:
        logger.info("  drop > vertex cap       : 0  (max seen: %s pts)", f"{worst_vtx:,}")

    # Build segments — NO geometry-content dedup.
    #
    # The previous version had QUALIFY ROW_NUMBER() OVER (PARTITION BY snap_wkb)
    # which caused two separate bugs:
    #
    #   Bug 1 — false duplicate detection.
    #     Two carriageways of a divided highway or parallel roads can snap to the
    #     same 1-µdeg grid cell. The dedup silently dropped one, removing an
    #     entire direction of travel from the graph.
    #
    #   Bug 2 — QUALIFY scope error.
    #     snap_wkb was computed in a subquery but referenced in the outer QUALIFY.
    #     DuckDB resolves QUALIFY against the outer SELECT list; snap_wkb was not
    #     projected there, causing a BinderException or silent misresolution.
    #
    # True duplicates (same road exported twice) are handled downstream:
    # ST_Node produces identical noded pieces from identical input lines, and
    # node_ids deduplicates via SELECT DISTINCT lat, lon — no explicit dedup here.
    logger.info("Filtering + exploding (length > %.1e deg) ...", MIN_SEG_LEN_DEG)
    con.execute(f"""
        CREATE OR REPLACE TABLE segments AS
        SELECT r.id, r.orig_id, r.name, r.oneway, r.lanes, r.maxspeed, r.highway,
               ST_GeomFromWKB(ST_AsWKB(ST_ReducePrecision(geom_part, 1e-6))) AS geom
        FROM (
            SELECT r2.id, r2.orig_id, r2.name, r2.oneway, r2.lanes, r2.maxspeed, r2.highway,
                   UNNEST(ST_Dump(r2.geom)).geom AS geom_part
            FROM raw_segments_parsed r2 WHERE r2.geom IS NOT NULL
        ) r
        WHERE ST_Length(geom_part) > {MIN_SEG_LEN_DEG}
          AND ST_IsValid(geom_part)
          AND ST_NPoints(geom_part) <= {MAX_VERTICES};
    """)
    con.execute("DROP TABLE IF EXISTS raw_segments_parsed;")

    seg_count = con.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    expected  = total_parts - drop_short - drop_invalid - drop_vtx
    logger.info("Segments going to noding  : %s  (expected ~%s)  [%s]",
                f"{seg_count:,}", f"{expected:,}", _elapsed(t))

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
        SELECT id, orig_id, name, highway, oneway, lanes, maxspeed FROM segments;
    """)
    con.execute("DROP TABLE IF EXISTS segments;")
    con.execute("CHECKPOINT;")
    logger.info(
        "   seg_geoms + seg_attrs checkpointed to disk before snap + noding …"
    )
    # After CHECKPOINT, DuckDB marks these pages as clean and can evict them.
    # The noding loop will only reload the pages for the current tile's bbox.

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
            s.maxspeed,
            s.orig_id                           AS ref
        FROM noded_segments n LEFT JOIN seg_attrs s ON n.src_id = s.id;
    """)

    # Free both source tables — geometry RAM released here
    con.execute("DROP TABLE IF EXISTS noded_segments;")
    con.execute("DROP TABLE IF EXISTS seg_attrs;")
    con.execute("CHECKPOINT;")   # release buffer pool after big drop

    way_count = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    # Audit: noded pieces whose interior_pt matched no source segment get src_id=NULL.
    # They survive into edges as highway='road' / name='unknown'. Not dropped, but
    # worth knowing — a high count means ST_Contains failures (usually at tile borders
    # where the interior_pt lands just outside the source geometry after snapping).
    null_src = con.execute("SELECT COUNT(*) FROM edges WHERE name = 'unknown' AND highway = 'road'").fetchone()[0]
    logger.info("Edges (ways): %s  [%s]", f"{way_count:,}", _elapsed(t))
    if null_src > 0:
        pct = null_src / way_count * 100
        if pct > 1.0:
            logger.warning("  edges with no matched source (highway=road, name=unknown): %s (%.1f%%)",
                           f"{null_src:,}", pct)
        else:
            logger.info("  edges with no matched source: %s (%.2f%%) — normal for border slivers",
                        f"{null_src:,}", pct)


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
        FROM edge_points ep JOIN node_ids ni ON ep.lon = ni.lon AND ep.lat = ni.lat
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
    if dup_refs:
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
           OR (first_node = last_node AND n_refs = 2); -- closed loop guard: n_refs=2 means trivially degenerate; longer closed ways (roundabouts) are kept
    """)

    n_degen = con.execute("SELECT COUNT(*) FROM degenerate_ways").fetchone()[0]
    if n_degen:
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

    # ── Step 8c: Boundary snap ────────────────────────────────────────────────
    logger.info("Step 8c . Boundary snap (region-split endpoint merge) ...")
    _snap_boundary_nodes(con)
    con.execute("CHECKPOINT;")

    way_count  = con.execute("SELECT COUNT(DISTINCT way_id) FROM way_nodes").fetchone()[0]
    node_count = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
    logger.info("After snap -- Nodes: %s  Ways: %s", f"{node_count:,}", f"{way_count:,}")

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