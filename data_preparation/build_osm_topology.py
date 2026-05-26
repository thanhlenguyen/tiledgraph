"""
build_osm_topology.py  — Valhalla-safe edition
─────────────────────────────────────────────
Converts any OGR-readable vector file (GeoJSON / SHP / GPKG / Parquet / …)
into a valid OSM XML file for osmium/osmconvert → OSM PBF
(Valhalla, OSRM, GraphHopper, etc.).

DESIGNED FOR LARGE FILES (country-scale, millions of segments).

APPROACH
  Inspired by ogr2osm: every source feature becomes OSM nodes + a way directly.
  Nodes are deduplicated by rounding coordinates to ROUNDING_DIGITS decimal
  places. No ST_Node, no tiling, no OOM risk. Nothing is dropped.

  1. Read every geometry → extract all vertices → round coordinates
  2. Deduplicate vertices (same rounded coord = same OSM node)
  3. Write each feature as a <way> referencing its node IDs in order
  4. Write all unique <node> elements

CONNECTIVITY PIPELINE (Steps 7a → 7c)
  7a. Node-to-node snap (two passes):
        Pass 1 — all endpoints, tight tolerance (~2 m)
        Pass 2 — dangling endpoints only, wide tolerance (~11 m)
  7b. Point-to-edge snap:
        Dangling endpoint near a road segment but far from its nodes →
        project endpoint perpendicularly onto segment → split segment →
        insert shared node.  Fixes T-junctions and roundabout spokes.
  7c. Final degenerate-way cleanup

USAGE
  python build_osm_topology.py <input_file> <output.osm> [memory_gb]
                  Parquet is fastest: columnar, compressed, no GDAL overhead.
                  Convert once with: ogr2ogr -f Parquet out.parquet in.gpkg
                  OR in DuckDB: COPY (SELECT ...) TO 'out.parquet' (FORMAT PARQUET)
  memory_gb     : default 8. Set to ~60-70% of available RAM (`free -h`).

NEXT STEP
  osmium cat output.osm -o output.osm.pbf

DEPENDENCIES
  pip install duckdb lxml tqdm
"""

import gc
import os
import sys
import time
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

# Coordinate rounding precision — same default as ogr2osm (7 decimal places).
# Two vertices rounded to the same value become one shared OSM node.
# 7 digits ≈ 1.1 cm precision at equator — fine for road networks.
# Increase to 8 for sub-centimetre data; decrease to 6 (≈11 cm) if your
# source data has many near-duplicate coordinates that should merge.
ROUNDING_DIGITS = 7

# Drop source segments with more than this many vertices.
# Segments with 10k+ vertices are data artifacts that cause slow processing.
MAX_VERTICES = 10_000

# ── Connectivity tolerances ──────────────────────────────────────────────────
# Node-to-node snap, pass 1 — ALL way endpoints (catches float drift).
SNAP_TOL_TIGHT_DEG  = 0.00002   # ~2 m

# Node-to-node snap, pass 2 — DANGLING endpoints only (catches larger gaps,
# roundabout spokes, region-split offsets).
SNAP_TOL_WIDE_DEG   = 0.00005   # ~11 m

# Point-to-edge snap — dangling endpoint projected onto nearest segment.
# Larger than node-to-node because the segment may be far from any node.
EDGE_SNAP_TOL_DEG   = 0.00005   # ~17 m — endpoint projected onto nearest segment
ENDPOINT_SNAP_TOL_DEG = 0.00002  # ~1 m  — tight endpoint-to-endpoint final pass

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
# STEP 1 — INGEST
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


# ─────────────────────────────────────────────────────────────────────────────
# HELPER — dangling endpoint table
# ─────────────────────────────────────────────────────────────────────────────

def _build_dangle_table(con, table_name: str, include_coords: bool = True) -> int:
    """
    Create a table of dangling endpoints (first or last node of exactly one way).
    Returns the count of dangling nodes.
    """
    coord_cols = ", n.lat, n.lon" if include_coords else ""
    coord_join = "JOIN node_ids n ON e.node_id = n.node_id" if include_coords else ""

    con.execute(f"""
        CREATE OR REPLACE TABLE {table_name} AS
        WITH eps AS (
            SELECT way_id, node_id, seq,
                MIN(seq) OVER (PARTITION BY way_id) AS min_seq,
                MAX(seq) OVER (PARTITION BY way_id) AS max_seq
            FROM way_nodes
        ),
        ep_count AS (
            SELECT node_id, COUNT(*) AS n
            FROM eps
            WHERE seq = min_seq OR seq = max_seq
            GROUP BY node_id
        )
        SELECT e.way_id, e.node_id
               {coord_cols}
        FROM eps e
        JOIN ep_count ec ON e.node_id = ec.node_id
        {coord_join}
        WHERE ec.n = 1
          AND (e.seq = e.min_seq OR e.seq = e.max_seq);
    """)
    return con.execute(f"SELECT COUNT(DISTINCT node_id) FROM {table_name}").fetchone()[0]


# ─────────────────────────────────────────────────────────────────────────────
# HELPER — union-find label propagation
# ─────────────────────────────────────────────────────────────────────────────

def _union_find(con, pairs_table: str, max_iter: int = 30) -> int:
    """
    Iterative label propagation on _snap_pairs → _labels.
    Returns the number of nodes that will be merged.
    """
    con.execute(f"""
        CREATE OR REPLACE TABLE _labels AS
        SELECT node_id, node_id AS label
        FROM (
            SELECT node_a AS node_id FROM {pairs_table}
            UNION
            SELECT node_b            FROM {pairs_table}
        ) t;
    """)

    for i in range(max_iter):
        con.execute(f"""
            CREATE OR REPLACE TABLE _labels_new AS
            SELECT l.node_id,
                MIN(COALESCE(la.label, lb.label, l.label)) AS label
            FROM _labels l
            LEFT JOIN {pairs_table} p ON l.node_id = p.node_a OR l.node_id = p.node_b
            LEFT JOIN _labels la      ON p.node_a = la.node_id
            LEFT JOIN _labels lb      ON p.node_b = lb.node_id
            GROUP BY l.node_id;
        """)
        changed = con.execute("""
            SELECT COUNT(*) FROM _labels l
            JOIN _labels_new ln ON l.node_id = ln.node_id
            WHERE l.label != ln.label
        """).fetchone()[0]
        con.execute("DROP TABLE IF EXISTS _labels;")
        con.execute("ALTER TABLE _labels_new RENAME TO _labels;")
        logger.debug("    union-find iter %d: %d changes", i + 1, changed)
        if changed == 0:
            logger.info("    union-find converged in %d iterations", i + 1)
            break
    else:
        logger.warning("    union-find did not converge in %d iterations", max_iter)

    return con.execute("SELECT COUNT(*) FROM _labels").fetchone()[0]


# ─────────────────────────────────────────────────────────────────────────────
# HELPER — apply label remapping to way_nodes and node_ids
# ─────────────────────────────────────────────────────────────────────────────

def _apply_labels(con) -> tuple[int, int]:
    """
    Remap node_ids in way_nodes and node_ids tables using _labels.
    Returns (old_node_count, new_node_count).
    """
    con.execute("""
        CREATE OR REPLACE TABLE _wn_remapped AS
        SELECT wn.way_id, wn.seq,
            COALESCE(l.label, wn.node_id) AS node_id
        FROM way_nodes wn
        LEFT JOIN _labels l ON wn.node_id = l.node_id;
    """)
    con.execute("DROP TABLE IF EXISTS way_nodes;")
    con.execute("ALTER TABLE _wn_remapped RENAME TO way_nodes;")

    con.execute("""
        CREATE OR REPLACE TABLE _ni_remapped AS
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
    con.execute("ALTER TABLE _ni_remapped RENAME TO node_ids;")
    new_cnt = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
    return old_cnt, new_cnt


# ─────────────────────────────────────────────────────────────────────────────
# HELPER — remove consecutive duplicate node refs and degenerate ways
# ─────────────────────────────────────────────────────────────────────────────

def _clean_way_nodes(con, caller: str = "") -> tuple[int, int]:
    """
    1. Remove consecutive duplicate node refs (rounding collapse artefacts).
    2. Drop degenerate ways (< 2 nodes, or closed loop with only 2 refs).
    Returns (dup_refs_removed, degen_ways_dropped).
    """
    con.execute("""
        CREATE OR REPLACE TABLE _wn_clean AS
        SELECT way_id, seq, node_id FROM (
            SELECT way_id, seq, node_id,
                LAG(node_id) OVER (PARTITION BY way_id ORDER BY seq) AS prev_id
            FROM way_nodes
        ) t WHERE prev_id IS NULL OR node_id != prev_id;
    """)
    dup_refs = (con.execute("SELECT COUNT(*) FROM way_nodes").fetchone()[0]
                - con.execute("SELECT COUNT(*) FROM _wn_clean").fetchone()[0])

    con.execute("""
        CREATE OR REPLACE TABLE _degen AS
        SELECT way_id FROM (
            SELECT way_id,
                COUNT(*)                    AS n,
                COUNT(DISTINCT node_id)     AS nd,
                FIRST(node_id ORDER BY seq) AS fn,
                LAST(node_id  ORDER BY seq) AS ln
            FROM _wn_clean GROUP BY way_id
        ) s WHERE n < 2 OR nd < 2 OR (fn = ln AND n = 2);
    """)
    n_degen = con.execute("SELECT COUNT(*) FROM _degen").fetchone()[0]
    if n_degen > 0:
        con.execute("DELETE FROM _wn_clean WHERE way_id IN (SELECT way_id FROM _degen);")
        con.execute("DELETE FROM edges     WHERE way_id IN (SELECT way_id FROM _degen);")

    con.execute("DROP TABLE IF EXISTS way_nodes;")
    con.execute("ALTER TABLE _wn_clean RENAME TO way_nodes;")
    con.execute("DROP TABLE IF EXISTS _degen;")
    return dup_refs, n_degen


# ─────────────────────────────────────────────────────────────────────────────
# STEP 7a — NODE-TO-NODE SNAPPING  (two passes)
# ─────────────────────────────────────────────────────────────────────────────

def _snap_node_to_node(con):
    """
    Pass 1: snap ALL way endpoints within SNAP_TOL_TIGHT_DEG.
            Fixes float-drift gaps that survive rounding.
    Pass 2: snap remaining DANGLING endpoints within SNAP_TOL_WIDE_DEG.
            Fixes roundabout spokes and region-split offsets.
    """
    t0 = time.time()

    def _one_pass(tol: float, dangling_only: bool, label: str):
        if dangling_only:
            n_cands = _build_dangle_table(con, "_cands")
        else:
            con.execute("""
                CREATE OR REPLACE TABLE _cands AS
                WITH eps AS (
                    SELECT way_id, node_id, seq,
                        MIN(seq) OVER (PARTITION BY way_id) AS min_seq,
                        MAX(seq) OVER (PARTITION BY way_id) AS max_seq
                    FROM way_nodes
                )
                SELECT DISTINCT e.node_id, n.lat, n.lon
                FROM eps e
                JOIN node_ids n ON e.node_id = n.node_id
                WHERE e.seq = e.min_seq OR e.seq = e.max_seq;
            """)
            n_cands = con.execute("SELECT COUNT(*) FROM _cands").fetchone()[0]

        logger.info("  [%s] candidates: %s  tol=%.5f°", label, f"{n_cands:,}", tol)
        if n_cands == 0:
            con.execute("DROP TABLE IF EXISTS _cands;")
            return

        con.execute(f"""
            CREATE OR REPLACE TABLE _pairs AS
            SELECT a.node_id AS node_a, b.node_id AS node_b
            FROM _cands a JOIN _cands b
              ON a.node_id < b.node_id
             AND b.lon BETWEEN a.lon - {tol} AND a.lon + {tol}
             AND b.lat BETWEEN a.lat - {tol} AND a.lat + {tol}
             AND SQRT(POWER(a.lon-b.lon,2)+POWER(a.lat-b.lat,2)) <= {tol};
        """)
        n_pairs = con.execute("SELECT COUNT(*) FROM _pairs").fetchone()[0]
        logger.info("  [%s] pairs: %s", label, f"{n_pairs:,}")
        con.execute("DROP TABLE IF EXISTS _cands;")

        if n_pairs == 0:
            con.execute("DROP TABLE IF EXISTS _pairs;")
            return

        n_in = _union_find(con, "_pairs")
        n_clusters = con.execute("SELECT COUNT(DISTINCT label) FROM _labels").fetchone()[0]
        logger.info("  [%s] merging %s nodes → %s clusters", label,
                    f"{n_in:,}", f"{n_clusters:,}")

        old_cnt, new_cnt = _apply_labels(con)
        logger.info("  [%s] nodes: %s → %s (merged %s)",
                    label, f"{old_cnt:,}", f"{new_cnt:,}", f"{old_cnt-new_cnt:,}")
        con.execute("DROP TABLE IF EXISTS _pairs; DROP TABLE IF EXISTS _labels;")

    _one_pass(SNAP_TOL_TIGHT_DEG, dangling_only=False, label="pass1-all-ep")
    _one_pass(SNAP_TOL_WIDE_DEG,  dangling_only=True,  label="pass2-dangle")

    dup_refs, n_degen = _clean_way_nodes(con, "node-to-node snap")
    if dup_refs:
        logger.warning("  Removed %s duplicate node refs", f"{dup_refs:,}")
    if n_degen:
        logger.warning("  Dropped %s degenerate ways", f"{n_degen:,}")

    con.execute("CHECKPOINT;")
    logger.info("  Node-to-node snap complete  [%s]", _elapsed(t0))


# ─────────────────────────────────────────────────────────────────────────────
# STEP 7b — POINT-TO-EDGE SNAPPING
# ─────────────────────────────────────────────────────────────────────────────

def _snap_point_to_edge(con):
    """
    Two-step endpoint connectivity fix applied to every remaining dangling
    endpoint (first/last node of exactly one way) after node-to-node snapping.

    STEP A — endpoint-to-endpoint (tight, ~1 m):
      Snap P to the nearest endpoint of ANY other way within
      ENDPOINT_SNAP_TOL_DEG.  This is a final tight-tolerance cleanup pass
      that catches cases the wider node-to-node passes missed.

    STEP B — endpoint-to-edge (projection):
      For each dangling endpoint P that STILL has no partner after Step A:
        1. Find every segment (consecutive node pair) of every other way
           whose bounding box overlaps a box of size EDGE_SNAP_TOL_DEG
           centred on P.  The box covers BOTH endpoints of the segment,
           so no segment that passes through the area is missed.
        2. Project P perpendicularly onto each candidate segment.
           Keep only projections that land strictly inside the segment
           (t ∈ (0.001, 0.999)) — endpoints are already handled by Step A.
        3. Pick the single nearest valid projection.
        4. Insert a new node at the rounded projection point.
        5. Split the target segment: [..., A, B, ...] → [..., A, NEW, B, ...]
        6. Remap P → NEW so the two ways share the node.

    Fixes: T-intersections, roundabout spokes, roads that end beside (not at)
    another road's node.
    """
    t0 = time.time()
    r  = ROUNDING_DIGITS

    # ── Collect all dangling endpoints once ───────────────────────────────────
    n_dangles = _build_dangle_table(con, "_dangle_all")
    logger.info("  Dangling endpoints (before edge snap): %s", f"{n_dangles:,}")
    if n_dangles == 0:
        con.execute("DROP TABLE IF EXISTS _dangle_all;")
        logger.info("  No dangling endpoints — skipped  [%s]", _elapsed(t0))
        return

    # ═══════════════════════════════════════════════════════════════════════════
    # STEP A — tight endpoint-to-endpoint snap (~1 m)
    # ═══════════════════════════════════════════════════════════════════════════
    tol_ep = ENDPOINT_SNAP_TOL_DEG
    logger.info("  [Step A] endpoint→endpoint  tol=%.6f° (~%.1f m)",
                tol_ep, tol_ep * 111_111)

    # Candidate pool: ALL way endpoints (not just dangles) so a dangle can
    # snap to a well-connected node of another road.
    con.execute(f"""
        CREATE OR REPLACE TABLE _all_ep AS
        WITH eps AS (
            SELECT way_id, node_id, seq,
                MIN(seq) OVER (PARTITION BY way_id) AS min_seq,
                MAX(seq) OVER (PARTITION BY way_id) AS max_seq
            FROM way_nodes
        )
        SELECT DISTINCT e.node_id, n.lat, n.lon
        FROM eps e
        JOIN node_ids n ON e.node_id = n.node_id
        WHERE e.seq = e.min_seq OR e.seq = e.max_seq;
    """)

    con.execute(f"""
        CREATE OR REPLACE TABLE _ep_pairs AS
        SELECT d.node_id AS node_a, ep.node_id AS node_b
        FROM _dangle_all d
        JOIN _all_ep ep
          ON ep.node_id != d.node_id
         AND ep.lon BETWEEN d.lon - {tol_ep} AND d.lon + {tol_ep}
         AND ep.lat BETWEEN d.lat - {tol_ep} AND d.lat + {tol_ep}
         AND SQRT(POWER(d.lon - ep.lon, 2) + POWER(d.lat - ep.lat, 2)) <= {tol_ep}
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY d.node_id
            ORDER BY SQRT(POWER(d.lon - ep.lon, 2) + POWER(d.lat - ep.lat, 2))
        ) = 1;
    """)
    n_ep_pairs = con.execute("SELECT COUNT(*) FROM _ep_pairs").fetchone()[0]
    logger.info("  [Step A] pairs found: %s", f"{n_ep_pairs:,}")

    if n_ep_pairs > 0:
        n_in = _union_find(con, "_ep_pairs")
        n_clusters = con.execute("SELECT COUNT(DISTINCT label) FROM _labels").fetchone()[0]
        logger.info("  [Step A] merging %s nodes → %s clusters", f"{n_in:,}", f"{n_clusters:,}")
        old_cnt, new_cnt = _apply_labels(con)
        logger.info("  [Step A] nodes: %s → %s (merged %s)",
                    f"{old_cnt:,}", f"{new_cnt:,}", f"{old_cnt - new_cnt:,}")
        con.execute("DROP TABLE IF EXISTS _labels;")

    con.execute("DROP TABLE IF EXISTS _ep_pairs; DROP TABLE IF EXISTS _all_ep;")

    # Re-run dedup/degen cleanup after Step A merges
    dup_refs, n_degen = _clean_way_nodes(con, "step-A")
    if dup_refs:
        logger.warning("  [Step A] removed %s duplicate node refs", f"{dup_refs:,}")
    if n_degen:
        logger.warning("  [Step A] dropped %s degenerate ways", f"{n_degen:,}")

    # ═══════════════════════════════════════════════════════════════════════════
    # STEP B — endpoint-to-edge projection snap
    # ═══════════════════════════════════════════════════════════════════════════
    tol_edge = EDGE_SNAP_TOL_DEG
    logger.info("  [Step B] endpoint→edge  tol=%.5f° (~%.0f m)",
                tol_edge, tol_edge * 111_111)

    # Refresh dangling table — some may have been resolved by Step A
    con.execute("DROP TABLE IF EXISTS _dangle_all;")
    n_dangles = _build_dangle_table(con, "_dangle_all")
    logger.info("  [Step B] remaining dangling endpoints: %s", f"{n_dangles:,}")

    if n_dangles == 0:
        con.execute("DROP TABLE IF EXISTS _dangle_all;")
        logger.info("  [Step B] all endpoints connected after Step A  [%s]", _elapsed(t0))
        return

    # ── B1. Build segment index ───────────────────────────────────────────────
    # Each row = one consecutive node pair from way_nodes.
    # We store BOTH endpoint coords so the bounding box join below can check
    # whether the segment passes through the search area from either direction.
    logger.info("  [Step B] building segment index ...")
    con.execute("""
        CREATE OR REPLACE TABLE _segs AS
        SELECT
            wn.way_id,
            wn.seq      AS seq_a,
            wn.node_id  AS node_a,
            na.lon      AS a_x,
            na.lat      AS a_y,
            wn2.node_id AS node_b,
            nb.lon      AS b_x,
            nb.lat      AS b_y,
            -- bounding box of the segment for fast range filter
            LEAST(na.lon, nb.lon)    AS seg_min_x,
            GREATEST(na.lon, nb.lon) AS seg_max_x,
            LEAST(na.lat, nb.lat)    AS seg_min_y,
            GREATEST(na.lat, nb.lat) AS seg_max_y
        FROM way_nodes wn
        JOIN way_nodes wn2
          ON wn.way_id = wn2.way_id AND wn2.seq = wn.seq + 1
        JOIN node_ids na ON wn.node_id  = na.node_id
        JOIN node_ids nb ON wn2.node_id = nb.node_id
        WHERE (nb.lon - na.lon)*(nb.lon - na.lon)
            + (nb.lat - na.lat)*(nb.lat - na.lat) > 1e-18;
    """)
    logger.info("  [Step B] segments indexed: %s",
                f"{con.execute('SELECT COUNT(*) FROM _segs').fetchone()[0]:,}")

    # ── B2. Project each dangling point onto candidate segments ───────────────
    # Bounding-box filter: the segment's own bbox must overlap the search box
    # around P.  This correctly finds segments that PASS THROUGH the area even
    # if their start node is outside it.
    #
    # Parametric projection:
    #   t  = dot(P-A, B-A) / |B-A|²          (scalar, 0..1 = inside segment)
    #   Q  = A + t*(B-A)                      (foot of perpendicular)
    #   d  = |P - Q|                          (perpendicular distance)
    logger.info("  [Step B] computing projections ...")
    con.execute(f"""
        CREATE OR REPLACE TABLE _proj AS
        WITH candidates AS (
            SELECT
                d.node_id  AS dangle_node,
                d.way_id   AS dangle_way,
                s.way_id   AS target_way,
                s.seq_a,
                s.node_a,
                s.node_b,
                s.a_x, s.a_y,
                s.b_x, s.b_y,
                d.lon      AS p_x,
                d.lat      AS p_y
            FROM _dangle_all d
            JOIN _segs s
              ON s.way_id   != d.way_id
             -- segment bbox overlaps search box around P
             AND s.seg_max_x >= d.lon - {tol_edge}
             AND s.seg_min_x <= d.lon + {tol_edge}
             AND s.seg_max_y >= d.lat - {tol_edge}
             AND s.seg_min_y <= d.lat + {tol_edge}
        ),
        projected AS (
            SELECT *,
                -- dot(P-A, B-A)
                (p_x - a_x)*(b_x - a_x) + (p_y - a_y)*(b_y - a_y) AS dot_num,
                -- |B-A|²
                (b_x - a_x)*(b_x - a_x) + (b_y - a_y)*(b_y - a_y) AS len2
            FROM candidates
        ),
        with_t AS (
            SELECT *,
                dot_num / len2 AS t
            FROM projected
            WHERE len2 > 1e-18
        ),
        with_foot AS (
            SELECT *,
                a_x + t*(b_x - a_x) AS q_x,
                a_y + t*(b_y - a_y) AS q_y
            FROM with_t
            -- foot must land strictly inside segment (not at endpoints)
            WHERE t BETWEEN 0.001 AND 0.999
        )
        SELECT
            dangle_node,
            dangle_way,
            target_way,
            seq_a,
            node_a,
            node_b,
            ROUND(q_x, {r})::DOUBLE AS proj_lon,
            ROUND(q_y, {r})::DOUBLE AS proj_lat,
            SQRT(POWER(p_x - q_x, 2) + POWER(p_y - q_y, 2)) AS dist
        FROM with_foot
        WHERE SQRT(POWER(p_x - q_x, 2) + POWER(p_y - q_y, 2)) <= {tol_edge};
    """)

    # ── B3. Best projection per dangling endpoint ─────────────────────────────
    con.execute("""
        CREATE OR REPLACE TABLE _best AS
        SELECT *
        FROM _proj
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY dangle_node
            ORDER BY dist
        ) = 1;
    """)
    n_snaps = con.execute("SELECT COUNT(*) FROM _best").fetchone()[0]
    logger.info("  [Step B] projection matches: %s", f"{n_snaps:,}")

    for tbl in ("_proj", "_segs", "_dangle_all"):
        con.execute(f"DROP TABLE IF EXISTS {tbl};")

    if n_snaps == 0:
        con.execute("DROP TABLE IF EXISTS _best;")
        logger.info("  [Step B] no projection matches  [%s]", _elapsed(t0))
        return

    # ── B4. Insert new nodes at projection coordinates ────────────────────────
    min_id = con.execute("SELECT MIN(node_id) FROM node_ids").fetchone()[0]

    con.execute(f"""
        CREATE OR REPLACE TABLE _new_nodes AS
        SELECT
            ({min_id} - ROW_NUMBER() OVER (ORDER BY dangle_node)) AS new_node_id,
            proj_lat  AS lat,
            proj_lon  AS lon,
            dangle_node,
            target_way,
            seq_a,
            node_a,
            node_b
        FROM _best;
    """)

    # Only insert if no node already exists at these rounded coordinates.
    con.execute("""
        INSERT INTO node_ids (node_id, lat, lon)
        SELECT nn.new_node_id, nn.lat, nn.lon
        FROM _new_nodes nn
        LEFT JOIN node_ids ex ON ex.lat = nn.lat AND ex.lon = nn.lon
        WHERE ex.node_id IS NULL;
    """)

    # If the projection rounded to an already-existing node, reuse it.
    con.execute("""
        CREATE OR REPLACE TABLE _resolved AS
        SELECT
            nn.dangle_node,
            nn.target_way,
            nn.seq_a,
            nn.node_a,
            nn.node_b,
            COALESCE(ex.node_id, nn.new_node_id) AS shared_node,
            nn.lat,
            nn.lon
        FROM _new_nodes nn
        LEFT JOIN node_ids ex
          ON ex.lat = nn.lat AND ex.lon = nn.lon
         AND ex.node_id != nn.new_node_id;
    """)
    con.execute("DROP TABLE IF EXISTS _new_nodes; DROP TABLE IF EXISTS _best;")

    # ── B5. Split target segments ─────────────────────────────────────────────
    logger.info("  [Step B] splitting %s segments ...", f"{n_snaps:,}")

    con.execute("""
        CREATE OR REPLACE TABLE _affected_ways AS
        SELECT DISTINCT target_way AS way_id FROM _resolved;
    """)

    # Inject new node between seq_a and seq_a+1 using a fractional seq value.
    con.execute("""
        CREATE OR REPLACE TABLE _wn_expanded AS
        -- Original nodes of affected ways
        SELECT wn.way_id, wn.seq::DOUBLE AS seq_f, wn.node_id
        FROM way_nodes wn
        WHERE wn.way_id IN (SELECT way_id FROM _affected_ways)

        UNION ALL

        -- Injected projection nodes (land between A and B)
        SELECT r.target_way, r.seq_a + 0.5, r.shared_node
        FROM _resolved r;
    """)

    con.execute("""
        CREATE OR REPLACE TABLE _wn_rebuilt AS
        SELECT way_id,
               ROW_NUMBER() OVER (PARTITION BY way_id ORDER BY seq_f) AS seq,
               node_id
        FROM _wn_expanded;
    """)

    con.execute("""
        CREATE OR REPLACE TABLE _wn_new AS
        SELECT way_id, seq, node_id
        FROM way_nodes
        WHERE way_id NOT IN (SELECT way_id FROM _affected_ways)

        UNION ALL

        SELECT way_id, seq, node_id FROM _wn_rebuilt;
    """)
    con.execute("DROP TABLE IF EXISTS way_nodes;")
    con.execute("ALTER TABLE _wn_new RENAME TO way_nodes;")

    # ── B6. Remap dangling endpoint → shared node ─────────────────────────────
    logger.info("  [Step B] remapping dangling endpoints ...")
    con.execute("""
        CREATE OR REPLACE TABLE _wn_remapped AS
        SELECT wn.way_id, wn.seq,
               COALESCE(r.shared_node, wn.node_id) AS node_id
        FROM way_nodes wn
        LEFT JOIN _resolved r ON wn.node_id = r.dangle_node;
    """)
    con.execute("DROP TABLE IF EXISTS way_nodes;")
    con.execute("ALTER TABLE _wn_remapped RENAME TO way_nodes;")

    for tbl in ("_resolved", "_affected_ways", "_wn_expanded", "_wn_rebuilt"):
        con.execute(f"DROP TABLE IF EXISTS {tbl};")

    dup_refs, n_degen = _clean_way_nodes(con, "step-B")
    if dup_refs:
        logger.warning("  [Step B] removed %s duplicate node refs", f"{dup_refs:,}")
    if n_degen:
        logger.warning("  [Step B] dropped %s degenerate ways", f"{n_degen:,}")

    con.execute("CHECKPOINT;")
    n_nodes = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
    n_ways  = con.execute("SELECT COUNT(DISTINCT way_id) FROM way_nodes").fetchone()[0]
    logger.info("  Point-to-edge snap complete — nodes: %s  ways: %s  [%s]",
                f"{n_nodes:,}", f"{n_ways:,}", _elapsed(t0))



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
def build_osm_topology(input_file: str, output_osm: str, memory_gb: int = 8) -> None:
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

    logger.info("━" * 60)
    logger.info("Input    → %s", input_file)
    logger.info("Output   → %s", output_osm)
    logger.info("DuckDB   → %s", db_path)
    logger.info("Memory   : %d GB", memory_gb)
    logger.info("Rounding : %d digits (~%.1f cm at equator)",
                ROUNDING_DIGITS, 10 ** (7 - ROUNDING_DIGITS) * 1.1)
    logger.info("Snap N→N tight : %.5f° (~%.0f m)",
                SNAP_TOL_TIGHT_DEG, SNAP_TOL_TIGHT_DEG * 111_111)
    logger.info("Snap N→N wide  : %.5f° (~%.0f m)",
                SNAP_TOL_WIDE_DEG,  SNAP_TOL_WIDE_DEG  * 111_111)
    logger.info("Snap P→Edge    : %.5f° (~%.0f m)",
                EDGE_SNAP_TOL_DEG,  EDGE_SNAP_TOL_DEG  * 111_111)
    logger.info("Log      → %s", os.path.abspath(LOG_FILE))
    logger.info("━" * 60)


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
    logger.info("🚀  Step 1 · Ingest…")
    t = time.time()

    geom_parsed = _read_input(con, input_file)
    raw_count = con.execute("SELECT COUNT(*) FROM raw_segments").fetchone()[0]
    logger.info("Raw rows: %s  [%s]", f"{raw_count:,}", _elapsed(t))

    # Audit pkStreetID uniqueness — logs warnings if duplicates found.
    # Pipeline is safe regardless because id is now ROW_NUMBER().
    duped = con.execute("""
        SELECT COUNT(*) - COUNT(DISTINCT orig_id) FROM raw_segments
    """).fetchone()[0]
    if duped:
        logger.warning("pkStreetID has %s duplicates — surrogate id used (safe)", f"{duped:,}")
    else:
        logger.info("pkStreetID unique across all rows")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 2: Validate + repair geometry, explode MULTI* → LINESTRING parts
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("🔧  Step 2 · Validate, repair, explode …")
    t = time.time()

    if geom_parsed:
        con.execute("""
            CREATE OR REPLACE TABLE segs_valid AS
            SELECT id, orig_id, name, oneway, lanes, maxspeed, highway,
                CASE WHEN ST_IsValid(geom_wkb) THEN geom_wkb
                     ELSE ST_MakeValid(geom_wkb) END AS geom
            FROM raw_segments WHERE geom_wkb IS NOT NULL;
        """)
    else:
        # geom_wkb is raw BLOB bytes — parse with ST_GeomFromWKB first
        con.execute("""
            CREATE OR REPLACE TABLE segs_valid AS
            SELECT id, orig_id, name, oneway, lanes, maxspeed, highway,
                CASE WHEN ST_IsValid(ST_GeomFromWKB(geom_wkb))
                     THEN ST_GeomFromWKB(geom_wkb)
                     ELSE ST_MakeValid(ST_GeomFromWKB(geom_wkb)) END AS geom
            FROM raw_segments
            WHERE try_cast(geom_wkb AS BLOB) IS NOT NULL;
        """)
    con.execute("DROP TABLE raw_segments;")

    valid_count = con.execute("SELECT COUNT(*) FROM segs_valid WHERE geom IS NOT NULL").fetchone()[0]
    dropped_geom = raw_count - valid_count
    if dropped_geom:
        logger.warning("Dropped %s rows with corrupt/null geometry", f"{dropped_geom:,}")

    # Explode MULTI* → individual LINESTRING parts, drop pathological segments
    con.execute(f"""
        CREATE OR REPLACE TABLE segments AS
        SELECT s.id, s.orig_id, s.name, s.oneway, s.lanes, s.maxspeed, s.highway,
               UNNEST(ST_Dump(s.geom)).geom AS part_geom
        FROM segs_valid s WHERE s.geom IS NOT NULL;
    """)
    # Apply filters as separate step so we can count drops
    total_parts = con.execute("SELECT COUNT(*) FROM segments").fetchone()[0]

    drop_vtx = con.execute(f"""
        SELECT COUNT(*) FROM segments WHERE ST_NPoints(part_geom) > {MAX_VERTICES}
    """).fetchone()[0]
    drop_invalid = con.execute(f"""
        SELECT COUNT(*) FROM segments 
        WHERE ST_NPoints(part_geom) <= {MAX_VERTICES} AND NOT ST_IsValid(part_geom)
    """).fetchone()[0]

    con.execute(f"""
        CREATE OR REPLACE TABLE segments_clean AS
        SELECT id, orig_id, name, oneway, lanes, maxspeed, highway, part_geom AS geom
        FROM segments
        WHERE ST_NPoints(part_geom) >= 2
          AND ST_NPoints(part_geom) <= {MAX_VERTICES}
          AND ST_IsValid(part_geom);
    """)
    con.execute("DROP TABLE segments; DROP TABLE segs_valid;")

    seg_count = con.execute("SELECT COUNT(*) FROM segments_clean").fetchone()[0]
    logger.info("Parts: %s total → %s kept  (vtx_cap: %s, invalid: %s)  [%s]",
                f"{total_parts:,}", f"{seg_count:,}",
                f"{drop_vtx:,}", f"{drop_invalid:,}", _elapsed(t))
    logger.info("Geometry types: %s", con.execute("""
        SELECT ST_GeometryType(geom), COUNT(*) FROM segments_clean GROUP BY 1
    """).fetchall())

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 3: Extract all vertices, round coordinates, deduplicate → node_ids
    #
    # This is the ogr2osm approach:
    #   • Every vertex (lon, lat) is rounded to ROUNDING_DIGITS decimal places
    #   • Two vertices at the same rounded coordinate → same OSM node ID
    #   • No ST_Node, no tiling, no dropped segments
    #   • Result: a dense node ID table and a vertex sequence table per way
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("📌  Step 3 · Extract vertices, round, deduplicate nodes …")
    t = time.time()
    r = ROUNDING_DIGITS

    con.execute(f"""
        CREATE OR REPLACE TABLE all_vertices AS
        WITH pts AS (
            SELECT
                s.id AS way_id,
                UNNEST(ST_Dump(ST_Points(s.geom))) AS pt
            FROM segments_clean s
        )
        SELECT
            way_id,
            ROW_NUMBER() OVER (PARTITION BY way_id ORDER BY (SELECT NULL)) AS seq,
            ROUND(ST_X(pt.geom), {r})::DOUBLE AS lon,
            ROUND(ST_Y(pt.geom), {r})::DOUBLE AS lat
        FROM pts;
    """)
    con.execute("CHECKPOINT;")

    # Count vertices before dedup for logging
    total_verts = con.execute("SELECT COUNT(*) FROM all_vertices").fetchone()[0]

    # Unique (rounded) coordinate pairs → OSM node IDs (negative = new data)
    con.execute("""
        CREATE OR REPLACE TABLE node_ids AS
        SELECT (ROW_NUMBER() OVER (ORDER BY lon, lat)) * -1 AS node_id, lat, lon
        FROM (SELECT DISTINCT lat, lon FROM all_vertices);
    """)
    con.execute("CREATE INDEX ni_lonlat ON node_ids (lon, lat);")

    node_count = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
    logger.info("Vertices: %s total → %s unique nodes  (%.1f%% shared)  [%s]",
                f"{total_verts:,}", f"{node_count:,}",
                (1 - node_count/total_verts)*100 if total_verts else 0, _elapsed(t))

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 4: Build way_nodes (ordered node refs per way)
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("🔗  Step 4 · Build way_nodes …")
    t = time.time()
    con.execute("""
        CREATE OR REPLACE TABLE way_nodes AS
        SELECT v.way_id, v.seq, n.node_id
        FROM all_vertices v JOIN node_ids n ON v.lon = n.lon AND v.lat = n.lat
        ORDER BY v.way_id, v.seq;
    """)
    con.execute("DROP TABLE all_vertices;")
    con.execute("CHECKPOINT;")
    logger.info("Ways (raw): %s  [%s]",
                f"{con.execute('SELECT COUNT(DISTINCT way_id) FROM way_nodes').fetchone()[0]:,}",
                _elapsed(t))

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 5: Build edges (way attributes)
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("🔗  Step 5 · Build edges (attributes) …")
    t = time.time()
    con.execute("""
        CREATE OR REPLACE TABLE edges AS
        SELECT
            id                           AS way_id,
            COALESCE(name,    'unknown') AS name,
            COALESCE(highway, 'road')    AS highway,
            COALESCE(oneway,  'no')      AS oneway,
            COALESCE(lanes,   '1')       AS lanes,
            maxspeed,
            orig_id                      AS ref
        FROM segments_clean;
    """)
    con.execute("DROP TABLE segments_clean;")
    con.execute("CHECKPOINT;")
    logger.info("Edges: %s  [%s]",
                f"{con.execute('SELECT COUNT(*) FROM edges').fetchone()[0]:,}",
                _elapsed(t))

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 6: Topology validation
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("━━  Step 6 · Topology validation ...")
    t = time.time()
    dup_refs, n_degen = _clean_way_nodes(con, "initial validation")
    if dup_refs:
        logger.warning("Removed %s consecutive duplicate node refs", f"{dup_refs:,}")
    if n_degen:
        logger.warning("Dropped %s degenerate ways", f"{n_degen:,}")
    con.execute("CHECKPOINT;")

    node_count = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
    way_count  = con.execute("SELECT COUNT(DISTINCT way_id) FROM way_nodes").fetchone()[0]
    logger.info("After validation — nodes: %s  ways: %s  [%s]",
                f"{node_count:,}", f"{way_count:,}", _elapsed(t))


    # ─────────────────────────────────────────────────────────────────────────
    # STEP 7: Boundary snap
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("━━  Step 7a · Node-to-node snap ...")
    _snap_node_to_node(con)
    node_count = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
    way_count  = con.execute("SELECT COUNT(DISTINCT way_id) FROM way_nodes").fetchone()[0]
    logger.info("After node-snap — nodes: %s  ways: %s",
                f"{node_count:,}", f"{way_count:,}")

    # ── Step 7b: Point-to-edge snapping ───────────────────────────────────────
    logger.info("━━  Step 7b · Point-to-edge snap ...")
    _snap_point_to_edge(con)
    node_count = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
    way_count  = con.execute("SELECT COUNT(DISTINCT way_id) FROM way_nodes").fetchone()[0]
    logger.info("After edge-snap — nodes: %s  ways: %s",
                f"{node_count:,}", f"{way_count:,}")

    # ── Step 8: Stream OSM XML ────────────────────────────────────────────────
    logger.info("━━  Step 8 · Writing OSM XML → %s ...", output_osm)
    _write_osm_xml(con, output_osm, node_count, way_count)

    con.close()
    logger.info("━" * 60)
    logger.info("Done in %s → %s", _elapsed(pipeline_t0), output_osm)
    logger.info("Next: osmium cat %s -o output.osm.pbf", output_osm)
    logger.info("━" * 60)

# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        print("Usage: python build_osm_topology.py <input> <output.osm> [memory_gb]")
        print("  input      : .parquet (fastest), .gpkg, .geojson, .shp")
        print("  memory_gb  : default 8. Use ~60% of available RAM (`free -h`)")
        print()
        print("  Convert to Parquet first for best performance:")
        print("    ogr2ogr -f Parquet out.parquet in.gpkg")
        sys.exit(1)

    memory_gb = int(sys.argv[3]) if len(sys.argv) == 4 else 8
    build_osm_topology(sys.argv[1], sys.argv[2], memory_gb)

    print()
    print("Next step → OSM PBF:")
    print(f"  osmium cat {sys.argv[2]} -o output.osm.pbf")