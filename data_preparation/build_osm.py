"""
Valhalla-safe road network converter
══════════════════════════════════════════════════════════════════
PURPOSE
  Reads any vector road dataset (GeoParquet / GeoJSON / SHP / GPKG / …)
  and writes a valid OSM XML file that can be converted to OSM PBF format
  for use with routing engines such as Valhalla, OSRM, or GraphHopper.

HOW IT WORKS  (high level)
  Every road feature (LineString) becomes an OSM <way>.
  Every vertex of every road becomes an OSM <node>.
  Two roads that share a vertex get the SAME node ID, so routing engines
  know they are connected.  We call this "topological correctness".

  The pipeline has 8 steps:

    Step 1  Read the source file into DuckDB
    Step 2  Validate / repair geometries, explode MultiLineStrings
    Step 3  Extract all vertices, round coordinates, deduplicate → node table
    Step 4  Build way_nodes  (ordered vertex list per road)
    Step 5  Build edges      (road attributes: name, highway type, …)
    Step 6  Initial topology check  (remove artefacts from rounding)
    Step 7a Node-to-node snapping   (merge nodes that are very close together)
    Step 7b Point-to-edge snapping  (connect roads that almost-but-not-quite meet)
    Step 8  Write OSM XML

CONNECTIVITY FIXES  (Steps 7a and 7b)
  Real-world datasets often have small gaps between roads that should be
  connected.  We fix these in two passes:

  7a  Node-to-node snap  (two sub-passes)
        Pass 1 — ALL road endpoints, tight gap (≈ 2 m):
                  Merges endpoints that are nearly identical but differ due
                  to floating-point rounding or tiny digitising errors.
        Pass 2 — DANGLING endpoints only, wider gap (≈ 11 m):
                  A "dangling" endpoint is the start or end of a road that
                  does not connect to any other road.  The wider tolerance
                  catches region-boundary offsets and roundabout spoke tips.

  7b  Point-to-edge snap  (two sub-steps, runs after 7a)
        Step A — Snap dangling endpoint to nearest node within ≈ 1 m.
                  Uses square bounding-box search (fast; at 1 m the corner
                  error is only 0.29 m which does not matter in practice).
        Step B — If still dangling: find the nearest road segment within
                  ≈ 17 m, project the endpoint perpendicularly onto that
                  segment, insert a new shared node, and split the segment.
                  Fixes T-junctions and roundabout spokes.

USAGE
  python build_osm_topology.py <input_file> <output.osm> [memory_gb]

  input_file  : path to .parquet, .gpkg, .geojson, or .shp
                Parquet is fastest because DuckDB reads it natively.
                Convert first with:  ogr2ogr -f Parquet out.parquet in.gpkg
  output.osm  : path where the OSM XML file will be written
  memory_gb   : (optional) RAM limit for DuckDB.  Default = 8.
                Set to about 60 % of your available RAM  (run `free -h`).

NEXT STEP  (convert to PBF for routing engines)
  osmium cat output.osm -o output.osm.pbf

DEPENDENCIES
  pip install duckdb lxml tqdm
"""

# ─────────────────────────────────────────────────────────────────────────────
# STANDARD LIBRARY IMPORTS
# ─────────────────────────────────────────────────────────────────────────────
import gc            # Garbage collector — used to free RAM after large queries
import os            # File paths, CPU count, environment helpers
import sys           # Command-line arguments, exit codes
import time          # Wall-clock timing for progress messages
import logging       # Structured log messages (to file + console)
from datetime import datetime  # Timestamp for the log filename

# ─────────────────────────────────────────────────────────────────────────────
# THIRD-PARTY IMPORTS
# Install with:  pip install duckdb lxml tqdm
# ─────────────────────────────────────────────────────────────────────────────
import duckdb        # In-process analytical SQL engine — handles all geometry
                     # work and on-disk spill when RAM is exhausted
from lxml import etree  # Fast C-based XML writer — streams OSM XML to disk
from tqdm import tqdm   # Terminal progress bars


# ─────────────────────────────────────────────────────────────────────────────
# LOGGING SETUP
#
# Two handlers:
#   • FileHandler  (DEBUG+) — full timestamped trace in build_osm_topology.log
#   • TqdmLoggingHandler (INFO+) — routes through tqdm.write() so progress
#     bars are never clobbered by a stray logger.info() call
# ─────────────────────────────────────────────────────────────────────────────

class _TqdmHandler(logging.StreamHandler):
    """
    A custom logging handler that writes through tqdm.write() instead of
    directly to stdout/stderr.  Without this, a plain print() or
    logging.StreamHandler would overwrite the tqdm progress bar on the
    current line, making it jump or display garbage.
    """
    def emit(self, record):
        try:
            tqdm.write(self.format(record))
        except Exception:
            self.handleError(record)

# Build a timestamped log filename so each run creates a fresh log file.
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_FILE  = f"build_osm_topology_{timestamp}.log"

# Root logger: send DEBUG+ to file (verbose), INFO+ to console (quiet).
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")],
)

# Console handler: show only INFO+ through tqdm.
_console = _TqdmHandler(sys.stdout)
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
logging.getLogger().addHandler(_console)
logger = logging.getLogger(__name__)


def _elapsed(t0: float) -> str:
    """
    Return a human-readable elapsed-time string from a time.time() snapshot.
    Examples: '4.3s', '2.1m'
    """
    s = time.time() - t0
    return f"{s:.1f}s" if s < 60 else f"{s/60:.1f}m"


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 — TUNEABLE CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════
#
# All distance tolerances are in DEGREES of latitude/longitude.
# Rough conversion:  1 degree ≈ 111,111 metres at the equator.
# So 0.00001° ≈ 1.11 m,  0.0001° ≈ 11.1 m,  0.001° ≈ 111 m.

# ── Geometry precision ────────────────────────────────────────────────────────
# Number of decimal places to keep when rounding vertex coordinates.
# 7 digits ≈ 1.1 cm precision at the equator — same default as ogr2osm.
# Two vertices that round to the same value become ONE shared OSM node,
# which is what creates the road-network topology.
# Increase to 8 for sub-centimetre survey data.
# Decrease to 6 (≈ 11 cm) if your source has many near-duplicate vertices
# that you want to automatically merge.
ROUNDING_DIGITS = 7

# Maximum number of vertices a single road segment may have.
# Segments with more than this are almost always data artefacts (e.g. a road
# that was accidentally duplicated thousands of times).  They slow everything
# down enormously and are dropped with a warning.
MAX_VERTICES = 10_000

# ── Node-to-node snapping tolerances (Step 7a) ────────────────────────────────
# Pass 1: applies to ALL road endpoints.
# Catches tiny floating-point drift that survives coordinate rounding.
# At 2 m the square/circle difference is only 0.6 m — irrelevant.
SNAP_TOL_TIGHT_DEG = 0.00002    # ≈  2 m

# Pass 2: applies only to DANGLING endpoints (roads that connect to nothing).
# Wider because region-boundary offsets and roundabout gaps can be larger.
SNAP_TOL_WIDE_DEG  = 0.00005    # ≈ 6 m

# ── Point-to-edge snapping tolerances (Step 7b) ───────────────────────────────
# Step A: snap dangling endpoint → nearest node (any node, not just endpoints).
# We use a square bounding-box search here.  At 1 m the worst-case corner
# error is only 0.29 m, which is harmless.  Keeping square avoids the
# extra SQRT call and is measurably faster on large datasets.
ENDPOINT_SNAP_TOL_DEG = 0.00002   # ≈  2.2 m  (square bbox, intentional)

# Step B: snap dangling endpoint → nearest road SEGMENT (by projection).
# Larger than node-to-node because the gap may be to the middle of a segment,
# not to any existing node.  Circle check matters here because at 17 m the
# corner error would be 5 m — large enough to snap to the wrong segment.
EDGE_SNAP_TOL_DEG = 0.00005        # ≈ 6 m  (circle check kept)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3 — HIGHWAY CLASSIFICATION SQL
# ═════════════════════════════════════════════════════════════════════════════
#
# Maps the input data's numeric columns to OSM highway tag values.
#
#   FOW (Form Of Way):
#     3 = slip road / on-ramp
#     4 = roundabout exit / off-ramp
#   Subtype (road class):
#     1 = trunk   (national expressway / dual carriageway)
#     2 = primary (main inter-city road)
#     3 = secondary (regional road)
#     anything else → generic 'road'
#
# The CASE expression is embedded verbatim in SQL so DuckDB evaluates it
# once per row during the initial ingest — no Python loop needed.
# ─────────────────────────────────────────────────────────────────────────────
HIGHWAY_SQL = """
CASE
    WHEN FOW IN (3,4) AND Subtype = 1 THEN 'trunk_link'
    WHEN FOW IN (3,4) AND Subtype = 2 THEN 'primary_link'
    WHEN FOW IN (3,4) AND Subtype = 3 THEN 'secondary_link'
    WHEN FOW IN (3,4)                  THEN 'tertiary_link'
    WHEN Subtype = 1                   THEN 'trunk'
    WHEN Subtype = 2                   THEN 'primary'
    ELSE                                    'road'
END
"""


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4 — INPUT INGESTION  (Step 1)
# ═════════════════════════════════════════════════════════════════════════════

def _read_input(con: duckdb.DuckDBPyConnection, input_file: str) -> bool:
    """
    Read the source road dataset into a DuckDB table called raw_segments.

    Columns produced:
        id         — surrogate integer primary key  (ROW_NUMBER, always unique)
        orig_id    — original pkStreetID from source (may have duplicates)
        name       — road name (EnglishName)
        oneway     — 'yes' if Direction=1, else 'no'
        lanes      — number of lanes as text, '1' if missing/zero
        maxspeed   — speed limit as text, NULL if missing/zero
        highway    — OSM highway tag value (from HIGHWAY_SQL)
        geom_wkb   — road geometry as WKB bytes (parsed later in Step 2)

    WHY TWO PATHS?
    Parquet  — DuckDB reads GeoParquet natively without GDAL.  The geometry
               column is already WKB bytes; we pass geom_wkb straight through.
               Returns True so Step 2 knows to treat geom_wkb as GEOMETRY.
    Other    — st_read() calls GDAL, which can segfault on corrupt geometries
               if we let GEOS parse them immediately.  We store raw WKB bytes
               via ST_AsWKB() and defer parsing to Step 2 where try_cast()
               can catch bad WKB safely.
               Returns False so Step 2 knows to call ST_GeomFromWKB() first.

    Returns
    -------
    True  if the geometry column is already a parsed GEOMETRY object
          (Parquet path — DuckDB auto-parses GeoParquet geometry).
    False if the geometry column is a raw BLOB (GDAL path — needs
          ST_GeomFromWKB in Step 2).
    """
    is_parquet = input_file.lower().endswith(".parquet")

    if is_parquet:
        logger.info("   Input format: Parquet (native DuckDB reader, no GDAL)")
        con.execute(f"""
            CREATE OR REPLACE TABLE raw_segments AS
            SELECT
                -- Globally unique surrogate key replaces pkStreetID as PK.
                -- ROW_NUMBER() never produces duplicates even when pkStreetID does.
                ROW_NUMBER() OVER ()                                AS id,

                pkStreetID                                          AS orig_id,
                EnglishName                                         AS name,

                -- Direction = 1 means the road is one-way in the digitised direction.
                CASE WHEN Direction = 1 THEN 'yes' ELSE 'no' END   AS oneway,

                -- Guard against zero/NULL lane counts which would break routing.
                CASE
                    WHEN NoOfLane IS NULL OR NoOfLane <= 0 THEN '1'
                    ELSE CAST(NoOfLane AS VARCHAR)
                END                                                 AS lanes,

                -- Speed limit: keep NULL if unknown so routing engines can
                -- fall back to their own defaults rather than getting '0'.
                CASE
                    WHEN SpeedLimit IS NOT NULL AND CAST(SpeedLimit AS INT) > 0
                    THEN CAST(SpeedLimit AS VARCHAR)
                    ELSE NULL
                END                                                 AS maxspeed,

                -- Computed highway tag from FOW + Subtype (see HIGHWAY_SQL above).
                {HIGHWAY_SQL}                                       AS highway,

                -- GeoParquet stores geometry as WKB bytes — carry through as-is.
                geom                                            AS geom_wkb

            FROM read_parquet('{input_file}')
            WHERE geom IS NOT NULL AND fkEmirateID = 4;
        """)
        return True   # geometry column already holds parsed GEOMETRY objects

    else:
        logger.info("   Input format: GDAL (st_read)")
        # Phase A: store geometry as raw WKB bytes — safe against bad geometries.
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

                -- ST_AsWKB converts the geometry to raw bytes.
                -- GEOS is never called here — just byte extraction.
                ST_AsWKB(geom)                                      AS geom_wkb

            FROM st_read('{input_file}')
            WHERE geom IS NOT NULL;
        """)
        return False  # geometry is raw WKB bytes; Step 2 must call ST_GeomFromWKB


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5 — SHARED HELPER FUNCTIONS
# ═════════════════════════════════════════════════════════════════════════════
#
# These small functions are reused in multiple steps.  Keeping them here
# avoids copy-pasting the same SQL logic in several places.

# ─────────────────────────────────────────────────────────────────────────────
# HELPER A — build a table of "dangling" endpoints
#
# A dangling endpoint is the first or last node of a way that is NOT shared
# with any other way.  These are the connectivity gaps we want to close.
#
#                  Way A ─────●─────●─────●
#                                         ↑ dangling (not connected to Way B)
#                  Way B          ●─────●─────●
# ─────────────────────────────────────────────────────────────────────────────
def _build_dangle_table(con, table_name: str, include_coords: bool = True) -> int:
    """
    Create (or replace) a table called `table_name` containing all dangling
    endpoints: nodes that appear as the first OR last vertex of exactly one way.

    Find every road endpoint (first or last vertex) that belongs to exactly
    ONE way.  These are "dangling" endpoints — roads that connect to nothing.

    A well-connected road network has very few dangling endpoints.  Every
    genuine cul-de-sac or dead-end produces one, but a large number usually
    means small gaps in the data that our snapping steps need to fix.

    Parameters
    ----------
    con           : active DuckDB connection
    table_name    : nname to give the output DuckDB table
    include_coords : if True, also join lat/lon from node_ids (needed for
                     distance calculations in snapping).  Set False when you
                     only need the node_id list and want to save join cost.

    Returns the count of unique dangling nodes found.

    HOW IT WORKS
    ────────────
    1. For each row in way_nodes, compute the min and max seq for that way.
       The min-seq row is the first vertex (start of road).
       The max-seq row is the last vertex  (end of road).
    2. Count how many times each node_id appears as a start or end.
       If a node appears only ONCE it is the endpoint of exactly one road
       → it is dangling.
       If it appears TWICE or more it is shared between roads → connected.

    Returns
    -------
    Count of distinct dangling node IDs.
    """
    # Only add lat/lon columns if the caller needs them for geometry work.
    coord_cols = ", n.lat, n.lon" if include_coords else ""
    coord_join = "JOIN node_ids n ON e.node_id = n.node_id" if include_coords else ""

    con.execute(f"""
        CREATE OR REPLACE TABLE {table_name} AS
        WITH eps AS (
            -- Label every node in way_nodes with its way's min and max seq numbers.
            -- A node at seq=min_seq is the first node of the way (start endpoint).
            -- A node at seq=max_seq is the last node of the way (end endpoint).
            SELECT way_id, node_id, seq,
                MIN(seq) OVER (PARTITION BY way_id) AS min_seq,
                MAX(seq) OVER (PARTITION BY way_id) AS max_seq
            FROM way_nodes
        ),
        ep_count AS (
            -- Count how many times each endpoint node appears across ALL ways.
            -- A node that appears only once is dangling (it belongs to one way only).
            -- A node that appears twice or more is shared (it connects two ways).
            SELECT node_id, COUNT(*) AS n
            FROM eps
            WHERE seq = min_seq OR seq = max_seq   -- keep only first/last rows
            GROUP BY node_id
        )
        -- Keep only nodes that appear as an endpoint of exactly ONE way
        SELECT e.way_id, e.node_id
               {coord_cols}
        FROM eps e
        JOIN ep_count ec ON e.node_id = ec.node_id
        {coord_join}
        WHERE ec.n = 1                              -- only nodes that appear once
          AND (e.seq = e.min_seq OR e.seq = e.max_seq);
    """)
    return con.execute(
        f"SELECT COUNT(DISTINCT node_id) FROM {table_name}"
    ).fetchone()[0]


# ─────────────────────────────────────────────────────────────────────────────
# HELPER B — Union-Find label propagation (graph component merging)
#
# Problem: we have a list of "these two nodes should be merged" pairs.
# Challenge: merging is transitive — if A→B and B→C then A, B, C all merge.
# Solution: label propagation (iterative union-find).
#
# How it works
# ────────────
# 1. Assign each node its own ID as its initial label.
# 2. In each iteration, every node takes the MINIMUM label of all its
#    neighbours (via the pairs table).
# 3. Repeat until no label changes (convergence = all components labelled).
# 4. All nodes with the same final label belong to one component.
#    The canonical node for that component = the one with the minimum ID.
# ─────────────────────────────────────────────────────────────────────────────
def _union_find(con, pairs_table: str, max_iter: int = 30) -> int:
    """
    Run iterative label propagation on `pairs_table` (columns: node_a, node_b).
    Produces a table called _labels (columns: node_id, label).
    Given a table of (node_a, node_b) pairs — nodes that should be merged —
    compute the connected components so that transitive chains are handled.

    EXAMPLE
    -------
    Pairs: (A, B) and (B, C).
    A direct merge would give A→B and B→C separately.
    Union-find realises A, B, and C are all connected and gives them all
    the same label (the smallest ID, e.g. A) so they collapse to one node.

    ALGORITHM  (label propagation)
    ─────────────────────────────
    1. Start: every node gets its own ID as its label.
    2. Each iteration: for every node, look at all its neighbours (from
       the pairs table) and take the MINIMUM label seen.
    3. Repeat until no labels change (convergence).

    After convergence, all nodes in the same component share the same label.
    We then remap every node_id to its label (see _apply_labels).

    Parameters
    ----------
    con          : active DuckDB connection
    pairs_table  : name of a table with columns (node_a, node_b) —
                   each row means "these two nodes should be merged"
    max_iter     : safety cap; 30 is enough for typical road networks

    Returns
    -------
    Total number of nodes that will be involved in some merge.
    """
    # Initialise: each node is its own component (label = itself).
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
        # Each node picks the minimum label among itself and all its direct
        # neighbours.  COALESCE handles nodes that appear on only one side
        # of the join (i.e. they have no pair in this direction).
        con.execute(f"""
            CREATE OR REPLACE TABLE _labels_new AS
            SELECT
                l.node_id,
                MIN(COALESCE(la.label, lb.label, l.label)) AS label
            FROM _labels l
            -- Find all pairs that involve this node (either as node_a or node_b)
            LEFT JOIN {pairs_table} p  ON l.node_id = p.node_a
                                       OR l.node_id = p.node_b
            -- Look up the current label of the other node in each pair
            LEFT JOIN _labels la       ON p.node_a = la.node_id
            LEFT JOIN _labels lb       ON p.node_b = lb.node_id
            GROUP BY l.node_id;
        """)

        # Count how many nodes changed their label in this iteration.
        # When changed == 0 the algorithm has converged.
        changed = con.execute("""
            SELECT COUNT(*)
            FROM _labels l
            JOIN _labels_new ln ON l.node_id = ln.node_id
            WHERE l.label != ln.label
        """).fetchone()[0]

        # Swap the new label table into place.
        con.execute("DROP TABLE IF EXISTS _labels;")
        con.execute("ALTER TABLE _labels_new RENAME TO _labels;")
        logger.debug("    union-find iter %d: %d changes", i + 1, changed)

        if changed == 0:
            logger.info("    union-find converged in %d iterations", i + 1)
            break
    else:
        logger.warning("    union-find did not converge in %d iterations - result may be incomplete", max_iter)

    return con.execute("SELECT COUNT(*) FROM _labels").fetchone()[0]


# ─────────────────────────────────────────────────────────────────────────────
# HELPER C — apply union-find labels to way_nodes and node_ids
#
# After _union_find() produces _labels, this function physically rewrites
# both tables so every reference to a "slave" node ID is replaced with the
# canonical "master" (minimum) label for that component.
# ─────────────────────────────────────────────────────────────────────────────
def _apply_labels(con) -> tuple[int, int]:
    """
    Remap node IDs in way_nodes and node_ids using the _labels table.
    After union-find has computed _labels (node_id → label), apply the
    remapping to both tables that reference node IDs:

      way_nodes  — replace every merged node_id with its canonical label
      node_ids   — delete the duplicate rows (keep only the canonical one)

    Returns (old_node_count, new_node_count) so the caller can log how many
    nodes were removed.

    WHY COALESCE?
    Not every node in way_nodes is in _labels (only the ones involved in
    a snap pair are).  COALESCE(label, original_id) keeps unmapped nodes
    unchanged.

    Returns
    -------
    (old_node_count, new_node_count)
    The difference is the number of nodes that were merged away.
    """
    # Rewrite way_nodes — replace node_id with its canonical label.
    # COALESCE: if a node has no label entry (wasn't part of any pair)
    # keep its original ID unchanged.
    con.execute("""
        CREATE OR REPLACE TABLE _wn_remapped AS
        SELECT
            wn.way_id,
            wn.seq,
            COALESCE(l.label, wn.node_id) AS node_id   -- keep original if not in _labels
        FROM way_nodes wn
        LEFT JOIN _labels l ON wn.node_id = l.node_id;
    """)
    con.execute("DROP TABLE IF EXISTS way_nodes;")
    con.execute("ALTER TABLE _wn_remapped RENAME TO way_nodes;")

    # Rewrite node_ids — deduplicate rows that now share the same label.
    # QUALIFY with ROW_NUMBER() keeps exactly one row per canonical label,
    # choosing the row with the lowest original node_id (ORDER BY n.node_id).
    con.execute("""
        CREATE OR REPLACE TABLE _ni_remapped AS
        SELECT
            COALESCE(l.label, n.node_id) AS node_id,
            n.lat,
            n.lon
        FROM node_ids n
        LEFT JOIN _labels l ON n.node_id = l.node_id
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY COALESCE(l.label, n.node_id)
            ORDER BY n.node_id   -- keep the row with the smallest (canonical) ID
        ) = 1;
    """)
    old_cnt = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
    con.execute("DROP TABLE IF EXISTS node_ids;")
    con.execute("ALTER TABLE _ni_remapped RENAME TO node_ids;")
    new_cnt = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]

    return old_cnt, new_cnt


# ─────────────────────────────────────────────────────────────────────────────
# HELPER D — remove consecutive duplicate node refs and degenerate ways
#
# After merging nodes, a way might contain runs like [A, A, B] (same node
# twice in a row) or collapse to [A, A] (only two refs, both the same node).
# These are invalid in OSM and confuse routing engines.
# ─────────────────────────────────────────────────────────────────────────────
def _clean_way_nodes(con, caller: str = "") -> tuple[int, int]:
    """
    After any operation that modifies node IDs, two kinds of artefacts can
    appear in way_nodes that would produce invalid OSM output:

    1. CONSECUTIVE DUPLICATE NODE REFS
       If rounding or snapping causes two adjacent vertices to get the same
       node ID, the way has a zero-length segment.  OSM validators reject this.
       We remove all but the first of any run of equal consecutive node IDs.
       Remove consecutive duplicate node refs (artefacts from rounding/merging).
       Example:  [1, 2, 2, 3]  →  [1, 2, 3]

    2. DEGENERATE WAYS
       A way that ends up with fewer than 2 distinct nodes is invalid.
       We also reject ways where the only two nodes are the same (a spike).
       Both the way_nodes rows AND the edges row are deleted.
       Drop degenerate ways (fewer than 2 nodes, or a closed loop with 2 refs).
       Example:  [5, 5]  →  way deleted entirely

    Parameters
    ----------
    con    : active DuckDB connection
    caller : string label for debug logging (e.g. "step-A")

    Returns
    -------
    (dup_refs_removed, degen_ways_dropped)  for logging.
    """
    # Step 1: remove consecutive duplicate refs using LAG window function.
    # LAG(node_id) gives the previous node_id in the same way (ordered by seq).
    # We keep a row only if it differs from its predecessor (or is the first row).
    con.execute("""
        CREATE OR REPLACE TABLE _wn_clean AS
        SELECT way_id, seq, node_id
        FROM (
            SELECT
                way_id,
                seq,
                node_id,
                LAG(node_id) OVER (PARTITION BY way_id ORDER BY seq) AS prev_id
            FROM way_nodes
        ) t
        WHERE prev_id IS NULL        -- first node of the way — always keep
           OR node_id != prev_id;    -- different from the previous — keep
    """)

    # Count how many refs were removed.
    dup_refs = (
        con.execute("SELECT COUNT(*) FROM way_nodes").fetchone()[0]
        - con.execute("SELECT COUNT(*) FROM _wn_clean").fetchone()[0]
    )

    # Step 2: find ways that are now degenerate.
    #   n < 2  → fewer than two node refs → not a valid LineString
    #   nd < 2 → all refs point to the same node → zero-length way
    #   fn == ln AND n == 2 → both refs are the same node (closed 2-node loop)
    con.execute("""
        CREATE OR REPLACE TABLE _degen AS
        SELECT way_id
        FROM (
            SELECT
                way_id,
                COUNT(*)                    AS n,         -- total node refs
                COUNT(DISTINCT node_id)     AS nd,        -- distinct nodes
                FIRST(node_id ORDER BY seq) AS fn,        -- first node
                LAST(node_id  ORDER BY seq) AS ln         -- last node
            FROM _wn_clean
            GROUP BY way_id
        ) s
        WHERE n  < 2              -- fewer than 2 refs → invalid OSM way
           OR nd < 2              -- all refs are the same node → point, not line
           OR (fn = ln AND n = 2) -- only two refs and they are the same node
        ;
    """)
    n_degen = con.execute("SELECT COUNT(*) FROM _degen").fetchone()[0]

    if n_degen > 0:
        # Remove degenerate ways from both the node-ref table and the edges table.
        con.execute(
            "DELETE FROM _wn_clean WHERE way_id IN (SELECT way_id FROM _degen);"
        )
        con.execute(
            "DELETE FROM edges WHERE way_id IN (SELECT way_id FROM _degen);"
        )

    # Replace way_nodes with the cleaned version
    con.execute("DROP TABLE IF EXISTS way_nodes;")
    con.execute("ALTER TABLE _wn_clean RENAME TO way_nodes;")
    con.execute("DROP TABLE IF EXISTS _degen;")

    return dup_refs, n_degen


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6 — STEP 7a: NODE-TO-NODE SNAPPING
# ═════════════════════════════════════════════════════════════════════════════
#
# Problem: two road endpoints that SHOULD share a node have slightly different
# coordinates — either because the source data was edited in different tools,
# or because floating-point rounding shifts the vertex by a tiny amount.
#
#    Way A ────────────────●  (lon: 55.2712340, lat: 25.1983210)
#    Way B  ●──────────────   (lon: 55.2712342, lat: 25.1983211)  ← 0.2 mm off
#
# Fix: if two endpoints are within tolerance, merge them into one node.
#
# ─────────────────────────────────────────────────────────────────────────────
def _snap_node_to_node(con):
    """
    Merge road endpoints that are very close together but not identical.

    This fixes the most common connectivity gap: two roads that are supposed
    to meet but whose shared point has slightly different coordinates in the
    source data (e.g. 55.123456_1 vs 55.123456_2 after rounding).

    TWO PASSES
    ──────────
    Pass 1  ALL endpoints, tight tolerance (≈ 2 m)
            Every road has two endpoints (first and last vertex).
            We compare all endpoints against each other and merge pairs
            that are within SNAP_TOL_TIGHT_DEG.
            This catches float-drift that survives coordinate rounding.

    Pass 2  DANGLING endpoints only, wide tolerance (≈ 11 m)
            After Pass 1, some endpoints are still unconnected (dangling).
            We run again with a larger tolerance but only for dangling nodes.
            This catches larger offsets at region boundaries and roundabout
            spoke tips that missed their ring node.

    Both passes use the same inner logic (_one_pass), then a final
    _clean_way_nodes() call removes any artefacts the merges created.
    """
    t0 = time.time()

    def _one_pass(tol: float, dangling_only: bool, label: str):
        """
        Run one snap pass.

        Parameters
        ----------
        tol           : distance threshold in degrees
        dangling_only : if True, only consider dangling endpoints as candidates;
                        if False, consider ALL road endpoints
        label         : short string used in log messages (e.g. "pass1-all-ep")
        """
        # ── Build candidate set ───────────────────────────────────────────────
        if dangling_only:
            # Re-use the helper — builds _cands with dangling nodes only
            n_cands = _build_dangle_table(con, "_cands")
        else:
            # All endpoints: first and last vertex of every way
            con.execute("""
                CREATE OR REPLACE TABLE _cands AS
                WITH eps AS (
                    SELECT
                        way_id, node_id, seq,
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

        # ── Find close pairs ──────────────────────────────────────────────────
        # Self-join: compare every candidate against every other candidate.
        # "a.node_id < b.node_id" ensures we get each pair only once (A,B not B,A).
        # Square bounding-box pre-filter is fast; exact distance picks the winner.
        con.execute(f"""
            CREATE OR REPLACE TABLE _pairs AS
            SELECT a.node_id AS node_a, b.node_id AS node_b
            FROM _cands a
            JOIN _cands b
              ON a.node_id < b.node_id   -- avoid duplicate (A,B) and (B,A) pairs
             AND b.lon BETWEEN a.lon - {tol} AND a.lon + {tol}   -- bbox X pre-filter
             AND b.lat BETWEEN a.lat - {tol} AND a.lat + {tol}   -- bbox Y pre-filter
             AND SQRT(POWER(a.lon - b.lon, 2) + POWER(a.lat - b.lat, 2)) <= {tol};
                                         -- exact Euclidean distance check
        """)
        n_pairs = con.execute("SELECT COUNT(*) FROM _pairs").fetchone()[0]
        logger.info("  [%s] snap pairs found: %s", label, f"{n_pairs:,}")
        con.execute("DROP TABLE IF EXISTS _cands;")

        if n_pairs == 0:
            con.execute("DROP TABLE IF EXISTS _pairs;")
            return

        # ── Merge connected groups ────────────────────────────────────────────
        n_in = _union_find(con, "_pairs")
        n_clusters = con.execute(
            "SELECT COUNT(DISTINCT label) FROM _labels"
        ).fetchone()[0]
        logger.info(
            "  [%s] merging %s nodes → %s clusters",
            label, f"{n_in:,}", f"{n_clusters:,}"
        )

        old_cnt, new_cnt = _apply_labels(con)
        logger.info(
            "  [%s] nodes: %s → %s  (merged %s)",
            label, f"{old_cnt:,}", f"{new_cnt:,}", f"{old_cnt - new_cnt:,}"
        )
        con.execute("DROP TABLE IF EXISTS _pairs; DROP TABLE IF EXISTS _labels;")

    # Run the two passes in order
    _one_pass(SNAP_TOL_TIGHT_DEG, dangling_only=False, label="pass1-all-ep")
    _one_pass(SNAP_TOL_WIDE_DEG,  dangling_only=True,  label="pass2-dangle")

    # Clean up any artefacts the merges introduced
    dup_refs, n_degen = _clean_way_nodes(con, "node-to-node snap")
    if dup_refs:
        logger.warning("  Removed %s consecutive duplicate node refs", f"{dup_refs:,}")
    if n_degen:
        logger.warning("  Dropped %s degenerate ways", f"{n_degen:,}")

    con.execute("CHECKPOINT;")
    logger.info("  Node-to-node snap complete  [%s]", _elapsed(t0))


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7 — STEP 7b: POINT-TO-EDGE SNAPPING
# ═════════════════════════════════════════════════════════════════════════════
#
# Problem: a dangling endpoint P lies BESIDE (not at the end of) another road.
# The endpoint-to-endpoint snap cannot fix this because the nearest matching
# node on the other road is too far away.
#
#         Way A  ────────────────────────────────
#         Way B            ● P  (dangling, should connect to Way A)
#
# Fix: project P perpendicularly onto the nearest segment of Way A,
#      insert a new node at the projection point, split Way A there,
#      and remap P to the new shared node.
#
#         Way A  ──────────●──────────────────────   ← split here
#         Way B            ● P  (now connected)
#
# This is the standard T-intersection fix used by routing preprocessors.
# ─────────────────────────────────────────────────────────────────────────────
def _snap_point_to_edge(con):
    """
    Fix remaining dangling endpoints that node-to-node snapping could not fix.

    Node-to-node snapping only works when two endpoints are close to EACH OTHER.
    But sometimes road A ends beside road B but nowhere near any of road B's
    existing nodes — for example, a side street that T-junctions into a main
    road, or a roundabout spoke whose tip falls between two ring nodes.

    In these cases we need to:
      1. Find the nearest SEGMENT of road B (not just its endpoints).
      2. Calculate the point on that segment closest to the dangling endpoint.
      3. Insert a new node there, split road B at that point, and make the
         dangling endpoint point to the new shared node.

    TWO STEPS
    ─────────
    Step A — Fast node search (≈ 1 m square box)
      Before doing expensive geometry math, check whether any node already
      exists very close to the dangling endpoint.  If yes, snap directly.
      We search ALL nodes (not just endpoints) because an interior node of
      road B may be the right connection point.
      Square bounding-box only — at 1 m the corner error is 0.29 m, harmless.

    Step B — Segment projection (≈ 17 m, circle check)
      For endpoints still dangling after Step A, project them onto the nearest
      road segment within EDGE_SNAP_TOL_DEG.
      We use the segment's full bounding box (not just its start node) so we
      catch segments that pass THROUGH the search area even if both their
      endpoints are outside it.
      Circle check (exact distance) is kept here because at 17 m the corner
      error would be 5 m — large enough to snap to the wrong segment.
      
      For each dangling endpoint P still unresolved after Step A:
        1. Find candidate road segments whose bounding box overlaps P ± tol.
        2. Project P onto each candidate segment (parametric formula).
        3. Keep only projections that land inside the segment (t ∈ 0.001–0.999).
        4. Pick the closest valid projection.
        5. Insert a new node at the rounded projection point.
        6. Split the segment so the new node is shared.
        7. Remap P → new node.
    """
    t0 = time.time()
    r  = ROUNDING_DIGITS  # coordinate rounding precision (decimal digits)

    # ── Collect all dangling endpoints (will be refreshed after Step A) ───────
    n_dangles = _build_dangle_table(con, "_dangle_all")
    logger.info("  Dangling endpoints (before edge snap): %s", f"{n_dangles:,}")

    if n_dangles == 0:
        # Nothing to do — all endpoints already connected.
        con.execute("DROP TABLE IF EXISTS _dangle_all;")
        logger.info("  No dangling endpoints — skipped  [%s]", _elapsed(t0))
        return

    # =========================================================================
    # STEP A — Snap dangling endpoint to nearest node within ≈ 1 m
    # =========================================================================
    #
    # SQUARE BOUNDING BOX (intentional — see module constants for explanation)
    #
    # For each dangling endpoint P we search all nodes within a square of
    # side 2×ENDPOINT_SNAP_TOL_DEG centred on P.  We exclude:
    #   • P itself  (node_id != d.node_id)
    #   • Nodes belonging to the same way as P  (would create a self-loop)
    # We then pick the single nearest node with QUALIFY ROW_NUMBER() = 1.
    #
    # Union-find handles transitive chains: if A snaps to B and B snaps to C,
    # all three end up with the same label.
    # =========================================================================
    tol_ep = ENDPOINT_SNAP_TOL_DEG
    logger.info(
        "  [Step A] dangling → nearest node  bbox=%.6f° (~%.1f m)",
        tol_ep, tol_ep * 111_111
    )

    # Build a pool of ALL way endpoints (first/last node of every way).
    # We search this pool for nodes close to each dangling endpoint.
    # Using ALL endpoints (not just dangles) lets a dangle snap onto a
    # well-connected mid-network node that is not itself dangling.
    logger.info("  [Step A] building ALL endpoint pool …")
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
    n_all_ep = con.execute("SELECT COUNT(*) FROM _all_ep").fetchone()[0]
    logger.info("  [Step A] total endpoint pool: %s nodes", f"{n_all_ep:,}")

    # ── Step A: find close pairs (dangle → any endpoint within tol_ep) ────────
    # We do this in Python chunks so we can show a progress bar.
    # Each chunk loads BATCH_SIZE dangling endpoints, runs the proximity join
    # for just those rows, and accumulates the results.
    BATCH_SIZE = 50_000   # number of dangling nodes per chunk

    # Fetch all dangling endpoints into Python memory for chunked processing.
    # Fields: (node_id, lat, lon)
    dangle_rows = con.execute(
        "SELECT node_id, lat, lon FROM _dangle_all ORDER BY node_id"
    ).fetchall()
    total_dangles = len(dangle_rows)

    # Staging table: we'll accumulate pairs from all chunks here.
    con.execute("CREATE OR REPLACE TABLE _ep_pairs (node_a BIGINT, node_b BIGINT);")

    logger.info("  [Step A] scanning %s dangling endpoints in batches of %s …",
                f"{total_dangles:,}", f"{BATCH_SIZE:,}")

    with tqdm(
        total=total_dangles,
        desc="  [Step A] ep→ep snap",
        unit=" nodes",
        unit_scale=True,
        dynamic_ncols=True,
    ) as pbar:
        # Process dangling endpoints in batches so that the bounding-box
        # join (which can be O(n²) in the worst case) only has to scan
        # BATCH_SIZE rows at a time instead of the full dangle set.
        for batch_start in range(0, total_dangles, BATCH_SIZE):
            batch = dangle_rows[batch_start : batch_start + BATCH_SIZE]

            # Build a temporary VALUES table for this batch so DuckDB can
            # join it against _all_ep without reading the whole dangle table.
            values_sql = ", ".join(
                f"({nid}, {lat}, {lon})" for nid, lat, lon in batch
            )

            con.execute(f"""
                INSERT INTO _ep_pairs
                SELECT d.node_id AS node_a, ep.node_id AS node_b
                FROM (VALUES {values_sql}) AS d(node_id, lat, lon)
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
            pbar.update(len(batch))

    n_ep_pairs = con.execute("SELECT COUNT(*) FROM _ep_pairs").fetchone()[0]
    logger.info("  [Step A] pairs found: %s", f"{n_ep_pairs:,}")

    con.execute("DROP TABLE IF EXISTS _all_ep;")

    if n_ep_pairs > 0:
        # Merge found pairs using union-find and apply the remapping.
        n_in = _union_find(con, "_ep_pairs")
        n_clusters = con.execute(
            "SELECT COUNT(DISTINCT label) FROM _labels"
            ).fetchone()[0]
        logger.info("  [Step A] merging %s nodes → %s clusters", f"{n_in:,}", f"{n_clusters:,}")

        old_cnt, new_cnt = _apply_labels(con)
        logger.info("  [Step A] nodes: %s → %s (merged %s)",
                    f"{old_cnt:,}", f"{new_cnt:,}", f"{old_cnt - new_cnt:,}")
        con.execute("DROP TABLE IF EXISTS _labels;")

    con.execute("DROP TABLE IF EXISTS _ep_pairs;")

    # Clean up artefacts created by Step A merges.
    dup_refs, n_degen = _clean_way_nodes(con, "step-A")
    if dup_refs:
        logger.warning("  [Step A] removed %s duplicate node refs", f"{dup_refs:,}")
    if n_degen:
        logger.warning("  [Step A] dropped %s degenerate ways", f"{n_degen:,}")


    # =========================================================================
    # STEP B — Project dangling endpoint onto nearest road segment
    # =========================================================================
    #
    # PERPENDICULAR PROJECTION EXPLAINED
    # ───────────────────────────────────
    # Given:
    #   P = the dangling endpoint  (lon, lat)
    #   A = start of a road segment  (a_x, a_y)
    #   B = end   of a road segment  (b_x, b_y)
    #
    # The foot of the perpendicular from P to line AB is:
    #   t = dot(P-A, B-A) / |B-A|²          ← parametric position along AB
    #   Q = A + t*(B-A)                      ← foot point coordinates
    #
    # t is between 0 and 1 when Q lies strictly inside the segment (not beyond
    # either endpoint).  We use 0.001 and 0.999 to avoid landing right at the
    # existing endpoints (those cases are handled by Step A and node-to-node).
    #
    # After finding Q, we:
    #   1. Round Q's coordinates to ROUNDING_DIGITS decimal places.
    #   2. Insert Q as a new node in node_ids (if it doesn't already exist).
    #   3. Split road B's way_nodes by inserting Q between A and the next node.
    #   4. Remap P's node_id → Q's node_id so P and Q are the same node.
    # =========================================================================

    # Refresh dangling list — some may have been resolved by Step A
    tol_edge = EDGE_SNAP_TOL_DEG
    logger.info("  [Step B] endpoint→edge  tol=%.5f° (~%.0f m)",
                tol_edge, tol_edge * 111_111)

    # Rebuild the dangling table — Step A may have resolved some endpoints.
    con.execute("DROP TABLE IF EXISTS _dangle_all;")
    n_dangles = _build_dangle_table(con, "_dangle_all")
    logger.info("  [Step B] remaining dangling endpoints: %s", f"{n_dangles:,}")

    if n_dangles == 0:
        con.execute("DROP TABLE IF EXISTS _dangle_all;")
        logger.info("  [Step B] all endpoints connected after Step A  [%s]", _elapsed(t0))
        return

    # ── B1. Build a segment index ─────────────────────────────────────────────
    # One row per consecutive node pair in every way.
    # We store the segment's bounding box (min/max of its two endpoint coords)
    # so the join in B2 can use a simple range check to find candidate segments.
    # This is much faster than computing the distance to every segment.
    logger.info("  [Step B] building segment index ...")
    con.execute("""
        CREATE OR REPLACE TABLE _segs AS
        SELECT
            wn.way_id,
            wn.seq          AS seq_a,    -- sequence number of the first node
            wn.node_id      AS node_a,   -- first node of the segment
            na.lon          AS a_x,      -- its longitude
            na.lat          AS a_y,      -- its latitude
            wn2.node_id     AS node_b,   -- second node of the segment
            nb.lon          AS b_x,
            nb.lat          AS b_y,
            -- Segment bounding box — used for the fast range filter in B2
            LEAST   (na.lon, nb.lon) AS seg_min_x,
            GREATEST(na.lon, nb.lon) AS seg_max_x,
            LEAST   (na.lat, nb.lat) AS seg_min_y,
            GREATEST(na.lat, nb.lat) AS seg_max_y
        FROM way_nodes wn
        -- Self-join to get the NEXT node in the same way (seq + 1)
        JOIN way_nodes wn2
          ON wn.way_id = wn2.way_id
         AND wn2.seq   = wn.seq + 1
        JOIN node_ids na ON wn.node_id  = na.node_id
        JOIN node_ids nb ON wn2.node_id = nb.node_id
        -- Skip zero-length segments (both endpoints identical after rounding)
        WHERE (nb.lon - na.lon) * (nb.lon - na.lon)
            + (nb.lat - na.lat) * (nb.lat - na.lat) > 1e-18;
    """)
    logger.info(
        "  [Step B] segments indexed: %s",
        f"{con.execute('SELECT COUNT(*) FROM _segs').fetchone()[0]:,}"
    )

    # ── B2. Project each dangling point onto candidate segments ───────────────
    # The WITH chain breaks the projection into readable named steps:
    #   candidates  — bbox filter: which segments are near enough to bother?
    #   projected   — compute dot products and squared length
    #   with_t      — compute parameter t
    #   with_foot   — compute foot point Q; filter t to (0.001, 0.999)
    #   final SELECT — compute exact distance, round Q, apply distance filter
    # This is the most compute-intensive part of the pipeline.
    # We use a chunked approach to show a progress bar.
    #
    # PARAMETRIC PROJECTION FORMULA
    # ──────────────────────────────
    # Given: segment A→B and point P (the dangling endpoint).
    #
    #   dx = B.x - A.x,   dy = B.y - A.y
    #   t  = dot(P-A, B-A) / |B-A|²
    #      = [(P.x-A.x)*dx + (P.y-A.y)*dy] / (dx²+dy²)
    #
    #   Q  = A + t*(B-A)   ← foot of perpendicular from P onto line A-B
    #   d  = |P - Q|       ← perpendicular distance
    #
    # t=0 → Q is at A,  t=1 → Q is at B.
    # We only keep projections with t ∈ (0.001, 0.999) so the foot lands
    # strictly INSIDE the segment, not at (or beyond) the endpoints.
    # Endpoint cases are already handled by Step A above.

    logger.info("  [Step B] projecting dangling endpoints onto segments …")

    # Fetch dangling endpoints into Python for batched processing.
    dangle_rows = con.execute(
        "SELECT node_id, way_id, lat, lon FROM _dangle_all ORDER BY node_id"
    ).fetchall()
    total_dangles = len(dangle_rows)

    # Staging table for projection results.
    con.execute("""
        CREATE OR REPLACE TABLE _proj (
            dangle_node BIGINT,
            dangle_way  BIGINT,
            target_way  BIGINT,
            seq_a       BIGINT,
            node_a      BIGINT,
            node_b      BIGINT,
            proj_lon    DOUBLE,
            proj_lat    DOUBLE,
            dist        DOUBLE
        );
    """)

    PROJ_BATCH = 10_000   # smaller batch for projection — heavier SQL per row

    with tqdm(
        total=total_dangles,
        desc="  [Step B] projecting",
        unit=" nodes",
        unit_scale=True,
        dynamic_ncols=True,
    ) as pbar:
        for batch_start in range(0, total_dangles, PROJ_BATCH):
            batch = dangle_rows[batch_start : batch_start + PROJ_BATCH]

            # Build VALUES literal for this batch.
            # Each row: (node_id, way_id, lat, lon)
            values_sql = ", ".join(
                f"({nid}, {wid}, {lat}, {lon})" for nid, wid, lat, lon in batch
            )

            # All arithmetic happens inside SQL — no Python loop over rows.
            con.execute(f"""
                INSERT INTO _proj
                WITH candidates AS (
                    -- Fast bbox filter: keep only segments whose bbox overlaps the
                    -- search box around the dangling point P.
                    -- Using the segment's bbox (not just its start point) means we
                    -- catch segments that PASS THROUGH the area even if both their
                    -- endpoints are outside it — which is the key fix over the old code.
                    SELECT
                        d.node_id  AS dangle_node,
                        d.way_id   AS dangle_way,
                        s.way_id   AS target_way,
                        s.seq_a,
                        s.node_a,
                        s.node_b,
                        s.a_x, s.a_y,
                        s.b_x, s.b_y,
                        d.lon      AS p_x,   -- dangling point longitude
                        d.lat      AS p_y    -- dangling point latitude  
                    FROM (VALUES {values_sql}) AS d(node_id, way_id, lat, lon)
                    JOIN _segs s
                      ON s.way_id   != d.way_id    -- never snap a road onto itself
             -- Segment bbox must overlap the tolerance box around P
                     AND s.seg_max_x >= d.lon - {tol_edge}
                     AND s.seg_min_x <= d.lon + {tol_edge}
                     AND s.seg_max_y >= d.lat - {tol_edge}
                     AND s.seg_min_y <= d.lat + {tol_edge}
                ),
                projected AS (
                    -- Compute the dot product numerator and the squared segment length.
                    -- These are the two ingredients needed to calculate t.
                    SELECT *,
                        -- dot(P-A, B-A) = (px-ax)*(bx-ax) + (py-ay)*(by-ay)
                        (p_x - a_x)*(b_x - a_x) + (p_y - a_y)*(b_y - a_y) AS dot_num,
                        (b_x - a_x)*(b_x - a_x) + (b_y - a_y)*(b_y - a_y) AS len2
                    FROM candidates
                ),
                with_t AS (
                    -- t = dot_num / len2  gives the normalised position along AB.
                    -- t=0 → point A,  t=1 → point B,  t=0.5 → midpoint.        
                    SELECT *, dot_num / len2 AS t
                    FROM projected
                    WHERE len2 > 1e-18    -- guard against degenerate segments
                ),
                with_foot AS (
                    -- Q = A + t*(B-A) is the foot of the perpendicular from P to AB.
                    -- We only keep projections where Q is STRICTLY inside the segment.
                    SELECT *,
                        a_x + t*(b_x - a_x) AS q_x,   -- foot longitude
                        a_y + t*(b_y - a_y) AS q_y    -- foot latitude
                    FROM with_t
                    -- Keep only projections that land INSIDE the segment.
                    WHERE t BETWEEN 0.001 AND 0.999
                    -- t=0.001 and t=0.999 keep Q away from the existing endpoints,
                    -- which are already handled by node-to-node snapping and Step A.
                )
        -- Final selection: compute exact perpendicular distance, round foot
        -- coordinates, and apply the distance tolerance filter.
                SELECT
                    dangle_node,
                    dangle_way,
                    target_way,
                    seq_a,
                    node_a,
                    node_b,
                    ROUND(q_x, {r})::DOUBLE AS proj_lon,   -- rounded foot longitude
                    ROUND(q_y, {r})::DOUBLE AS proj_lat,   -- rounded foot latitude
                    -- Exact perpendicular distance P → Q  (circle check for accuracy)
                    SQRT(POWER(p_x - q_x, 2) + POWER(p_y - q_y, 2)) AS dist
                FROM with_foot
                -- Only keep projections within the snap tolerance.
                WHERE SQRT(POWER(p_x - q_x, 2) + POWER(p_y - q_y, 2)) <= {tol_edge};
            """)
            pbar.update(len(batch))

    # ── B3. Keep only the nearest projection per dangling endpoint ────────────
    # A dangling endpoint may have multiple candidate segments within tolerance.
    # We want only the closest one.
    con.execute("""
        CREATE OR REPLACE TABLE _best AS
        SELECT *
        FROM _proj
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY dangle_node   -- one result per dangling endpoint
            ORDER BY dist              -- nearest segment wins
        ) = 1;
    """)
    n_snaps = con.execute("SELECT COUNT(*) FROM _best").fetchone()[0]
    logger.info("  [Step B] projection matches: %s", f"{n_snaps:,}")

    # Clean up large intermediate tables immediately to free disk/RAM.
    for tbl in ("_proj", "_segs", "_dangle_all"):
        con.execute(f"DROP TABLE IF EXISTS {tbl};")

    if n_snaps == 0:
        con.execute("DROP TABLE IF EXISTS _best;")
        logger.info("  [Step B] no projection matches  [%s]", _elapsed(t0))
        return

    # ── B4. Insert new nodes at projection coordinates ────────────────────────
    # New node IDs must be more negative than all existing IDs so they don't
    # collide with any existing node.
    min_id = con.execute("SELECT MIN(node_id) FROM node_ids").fetchone()[0]

    con.execute(f"""
        CREATE OR REPLACE TABLE _new_nodes AS
        SELECT
            -- Count down from min_id - 1 so new IDs are unique and negative.
            ({min_id} - ROW_NUMBER() OVER (ORDER BY dangle_node)) AS new_node_id,
            proj_lat  AS lat,
            proj_lon  AS lon,
            dangle_node,
            target_way,
            seq_a,      -- seq of the segment's first node (for split in B5)
            node_a,
            node_b
        FROM _best;
    """)

    # Insert only if no node already exists at these exact rounded coordinates.
    # (Two dangling points may project to the same spot; the second one would
    # find the node already there from the first insertion.)
    con.execute("""
        INSERT INTO node_ids (node_id, lat, lon)
        SELECT nn.new_node_id, nn.lat, nn.lon
        FROM _new_nodes nn
        LEFT JOIN node_ids ex ON ex.lat = nn.lat AND ex.lon = nn.lon
        WHERE ex.node_id IS NULL;   -- skip if a node already exists here
    """)

    # If the projection rounded to an existing node, reuse its ID instead
    # of the freshly generated new_node_id.
    con.execute("""
        CREATE OR REPLACE TABLE _resolved AS
        SELECT
            nn.dangle_node,
            nn.target_way,
            nn.seq_a,
            nn.node_a,
            nn.node_b,
            -- Prefer an existing node at these coords; fall back to new_node_id.
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
    # For each projection, the target segment [A, B] must become [A, NEW, B].
    # We do this by injecting a fractional seq value (seq_a + 0.5) and
    # renumbering with ROW_NUMBER() afterwards.
    #
    # TECHNIQUE: fractional sequence numbers
    #   The existing nodes have integer seq values (1, 2, 3, …).
    #   We assign the new node seq = seq_a + 0.5, which sorts between seq_a
    #   and seq_a + 1.  After the UNION ALL we renumber with ROW_NUMBER() to
    #   restore clean integer seq values.
    logger.info("  [Step B] splitting %s segments …", f"{n_snaps:,}")

    with tqdm(
        total=n_snaps,
        desc="  [Step B] splitting ways",
        unit=" segs",
        unit_scale=True,
        dynamic_ncols=True,
    ) as pbar:
        con.execute("""
            CREATE OR REPLACE TABLE _affected_ways AS
            SELECT DISTINCT target_way AS way_id FROM _resolved;
        """)

        # Build expanded node list: original nodes UNION new injected nodes.
        # The fractional seq value (seq_a + 0.5) places NEW between A and B.
        con.execute("""
            CREATE OR REPLACE TABLE _wn_expanded AS
            -- All original node refs of affected ways (integer seq values).
            SELECT wn.way_id, wn.seq::DOUBLE AS seq_f, wn.node_id
            FROM way_nodes wn
            WHERE wn.way_id IN (SELECT way_id FROM _affected_ways)

            UNION ALL

            -- Injected projection nodes — fractional seq places them mid-segment.
            SELECT r.target_way, r.seq_a + 0.5, r.shared_node
            FROM _resolved r;
        """)

        # Renumber seqs as integers (1, 2, 3, …) ordered by the fractional values.
        con.execute("""
            CREATE OR REPLACE TABLE _wn_rebuilt AS
            SELECT way_id,
                   -- Re-assign clean integer sequence numbers after the injection
                   ROW_NUMBER() OVER (PARTITION BY way_id ORDER BY seq_f) AS seq,
                   node_id
            FROM _wn_expanded;
        """)

        # Combine: unaffected ways unchanged, affected ways rebuilt.
        con.execute("""
            CREATE OR REPLACE TABLE _wn_new AS

        -- Ways that were NOT split — leave completely unchanged
            SELECT way_id, seq, node_id
            FROM way_nodes
            WHERE way_id NOT IN (SELECT way_id FROM _affected_ways)

            UNION ALL

            -- Ways that were split — use the rebuilt version
            SELECT way_id, seq, node_id FROM _wn_rebuilt;
        """)
        con.execute("DROP TABLE IF EXISTS way_nodes;")
        con.execute("ALTER TABLE _wn_new RENAME TO way_nodes;")

        # Update progress bar: one tick per split performed.
        pbar.update(n_snaps)

    # ── B6. Remap dangling endpoint → shared node ─────────────────────────────
    # The original dangling node still exists in way_nodes; replace it with
    # the shared_node we just inserted/found so the two ways now meet.
    logger.info("  [Step B] remapping dangling endpoints …")
    con.execute("""
        CREATE OR REPLACE TABLE _wn_remapped AS
        SELECT wn.way_id, wn.seq,
               COALESCE(r.shared_node, wn.node_id) AS node_id
        FROM way_nodes wn
        LEFT JOIN _resolved r ON wn.node_id = r.dangle_node;
    """)
    con.execute("DROP TABLE IF EXISTS way_nodes;")
    con.execute("ALTER TABLE _wn_remapped RENAME TO way_nodes;")

    # Final cleanup of any artefacts.
    for tbl in ("_resolved", "_affected_ways", "_wn_expanded", "_wn_rebuilt"):
        con.execute(f"DROP TABLE IF EXISTS {tbl};")

    # Final artefact cleanup
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


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 8 — STEP 8: WRITE OSM XML
# ═════════════════════════════════════════════════════════════════════════════
    # OSM XML FORMAT
    # ──────────────
    # <osm version="0.6">
    #     <!-- All nodes first -->
    #     <node id="-1" lat="25.123" lon="55.456" version="1" visible="true"/>
    #     …
    #     <!-- Then all ways -->
    #     <way id="-1" version="1" visible="true">
    #         <tag k="highway" v="primary"/>
    #         <tag k="name" v="Sheikh Zayed Road"/>
    #         <nd ref="-1"/>   ← references node IDs in order
    #         <nd ref="-2"/>
    #     </way>
    #     …
    # </osm>
    
    # MEMORY STRATEGY  (O(1) RAM regardless of dataset size)
    # ──────────────────────────────────────────────────────
    # We use lxml's xmlfile context manager which flushes each element to disk
    # immediately after writing it, so RAM usage stays constant.
    
    # Way attributes (name, highway, …) are pre-loaded into a Python dict
    # (way_attrs) keyed by way_id.  This dict is small compared to way_nodes.
    
    # Way node references are streamed from DuckDB in chunks of CHUNK_REFS rows.
    # We advance through the sorted stream, building one <way> element at a time
    # and writing it to disk before starting the next.

    # DESIGN: two independent DuckDB cursors, merged in Python.
    # Cursor A (attrs): one row per way — tag attributes only (no geometry).
    # Cursor B (refs) : one row per (way, vertex) — way_id + node_id + seq.

    # The two cursors advance in lockstep:
    # - When B's way_id matches the current way → append <nd> child.
    # - When B's way_id changes → flush current <way>, start next.
    # - Cursor A provides tag values for each new way_id.

    # This avoids a JOIN over 100M+ rows and keeps Python RAM to:
    # one <way> element at a time + one chunk of refs (~8 MB for 500k rows).
# ─────────────────────────────────────────────────────────────────────────────
def _write_osm_xml(
    con: duckdb.DuckDBPyConnection,
    path: str,
    node_count: int,
    way_count: int,
) -> None:
    """
    Write the final OSM XML file by streaming nodes and ways from DuckDB.

    Parameters
    ----------
    con        : active DuckDB connection
    path       : output file path (e.g. "output.osm")
    node_count : expected total nodes (for the progress bar)
    way_count  : expected total ways (for the progress bar)
    """
    # How many rows to fetch from DuckDB per Python batch.
    # Larger = fewer round-trips but more peak Python list RAM.
    CHUNK_NODES = 100_000  # ~100k nodes per fetch ≈ negligible RAM
    CHUNK_REFS  = 500_000  # ~500k (way_id, node_id) pairs ≈ ~8 MB

    # ── Pre-load way attributes ───────────────────────────────────────────────
    # We load ALL way attributes into a Python dict BEFORE opening the XML
    # file.  This avoids needing a second DuckDB cursor open simultaneously,
    # which would complicate the streaming loop.
    # Size estimate: 6.2M ways × ~100 bytes ≈ 620 MB — acceptable.
    # If RAM is very tight, this could be changed to a lookup cursor instead.
    logger.info("Loading way attributes into memory …")
    t = time.time()
    way_attrs: dict = {}    # way_id → (name, highway, oneway, lanes, maxspeed)

    cur_attrs = con.execute(
        "SELECT way_id, name, highway, oneway, lanes, maxspeed "
        "FROM edges ORDER BY way_id"
    )
    with tqdm(
        desc="  attrs", unit=" ways", unit_scale=True, dynamic_ncols=True
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
    gc.collect()  # Free the cursor result memory before opening the XML file.

    with open(path, "wb") as fh:
        # etree.xmlfile is an incremental XML writer — it writes each element
        # to disk immediately rather than building the whole tree in RAM.
        with etree.xmlfile(fh, encoding="utf-8") as xf:
            xf.write_declaration()  # <?xml version='1.0' encoding='utf-8'?>

            with xf.element("osm", version="0.6", generator="build_osm_topology"):

                # ── Write nodes ───────────────────────────────────────────────
                # OSM requires all <node> elements to appear before any <way>.
                # We stream them in chunks from DuckDB, writing each one and
                # releasing it immediately — O(chunk) RAM at any point.
                logger.info("Writing %s nodes …", f"{node_count:,}")
                t         = time.time()
                n_written = 0
                cur = con.execute(
                    "SELECT node_id, lat, lon FROM node_ids ORDER BY node_id"
                )
                with tqdm(total=node_count, desc="  Writing nodes", unit=" nodes", unit_scale=True, dynamic_ncols=True) as pbar:
                    while True:
                        rows = cur.fetchmany(CHUNK_NODES)
                        if not rows:
                            break
                        for node_id, lat, lon in rows:
                            # etree.Element creates one XML element in memory,
                            # xf.write() serialises it to disk, then it's freed.
                            xf.write(etree.Element("node", {
                                "id":      str(node_id),
                                "lat":     f"{lat:.7f}",   # 7 decimal places matches ROUNDING_DIGITS
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

                # ── Write ways ────────────────────────────────────────────────
                # Stream (way_id, node_id) pairs from way_nodes, ordered by
                # way_id then seq so all refs for one way arrive consecutively.
                # When the way_id changes, flush the current <way> and start a new one.
                logger.info("Writing %s ways …", f"{way_count:,}")
                t = time.time()
                cur_refs = con.execute("""
                    SELECT way_id, node_id
                    FROM way_nodes
                    WHERE way_id IN (
                        -- Guard: only write ways that have at least 2 node refs.
                        SELECT way_id FROM way_nodes
                        GROUP BY way_id HAVING COUNT(*) >= 2
                    )
                    ORDER BY way_id, seq
                """)

                current_id = None   # way_id of the <way> element being built
                way_elem   = None   # the in-progress lxml Element
                n_ways     = 0      # count of ways started
                n_skipped  = 0      # count of ways dropped (< 2 <nd> children)

                def flush_way(elem):
                    """Write the current <way> element to disk, or skip if degenerate."""
                    nonlocal n_skipped
                    if elem is None:
                        return
                    # Count <nd> children — a valid way needs at least 2.
                    if sum(1 for ch in elem if ch.tag == "nd") < 2:
                        n_skipped += 1
                        return
                    xf.write(elem)

                with tqdm( total=way_count, desc="  Writing ways ", unit=" ways", unit_scale=True, dynamic_ncols=True) as pbar:
                    while True:
                        rows = cur_refs.fetchmany(CHUNK_REFS)
                        if not rows:
                            break

                        for way_id, node_id in rows:
                            if way_id != current_id:
                                # New way_id — flush the previous way and start a new one.
                                flush_way(way_elem)
                                # Create the new <way> element
                                way_elem = etree.Element("way", {
                                    "id":      str(way_id),
                                    "version": "1",
                                    "visible": "true",
                                })
                                current_id = way_id
                                n_ways    += 1
                                pbar.update(1)

                                # Add OSM tag children from the pre-loaded attrs dict.
                                name, highway, oneway, lanes, maxspeed = way_attrs.get(
                                    way_id, ("unknown", "road", "no", "1", None)
                                )
                                for k, v in [
                                    ("highway",  highway),
                                    ("name",     name),
                                    ("oneway",   oneway),
                                    ("lanes",    lanes),
                                    ("maxspeed", maxspeed),
                                ]:
                                    # Only write the tag if the value is non-empty
                                    if v is not None and str(v).strip():
                                        etree.SubElement(way_elem, "tag", {"k": k, "v": str(v)})

                                if n_ways % 1_000_000 == 0:
                                    logger.debug("%s ways processed", f"{n_ways:,}")

                            # Add a node-reference child to the current <way>.
                            etree.SubElement(way_elem, "nd", {"ref": str(node_id)})

                    # Flush the last way (loop ends before it is flushed normally).
                    flush_way(way_elem)

                written = n_ways - n_skipped
                if n_skipped:
                    pct = n_skipped / n_ways * 100 if n_ways else 0
                    logger.warning(
                        "Ways written: %s  skipped (< 2 refs): %s (%.2f%%)  [%s]",
                        f"{written:,}", f"{n_skipped:,}", pct, _elapsed(t),
                    )
                else:
                    logger.info("Ways written: %s  skipped: 0  [%s]",
                                f"{written:,}", _elapsed(t))

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 9 — MAIN PIPELINE
# ═════════════════════════════════════════════════════════════════════════════
#
# Calls each step in sequence, logging counts and timings between steps.
# All heavy work happens inside DuckDB; Python only orchestrates.
# ─────────────────────────────────────────────────────────────────────────────
def build_osm_topology(input_file: str, output_osm: str, memory_gb: int = 8) -> None:
    """
    Full pipeline: source vector file → OSM XML.

    Parameters
    ----------
    input_file : path to the source road dataset
    output_osm : path where the OSM XML file should be written
    memory_gb  : RAM limit for DuckDB in gigabytes (default 8)
    """
    pipeline_t0    = time.time()
    original_input = input_file
    input_file     = os.path.abspath(input_file)
    output_osm     = os.path.abspath(output_osm)

    # ── WSL path handling ─────────────────────────────────────────────────────
    # Files under /mnt/c/ (Windows drive mounts) are ~5-10× slower than native
    # Linux filesystem paths in WSL.  If a copy exists in ~/tiledgraph/data/
    # we prefer it automatically.
    home_data_dir = os.path.expanduser("~/tiledgraph/data")
    if "/mnt/" in input_file.lower():
        basename  = os.path.basename(input_file)
        wsl_input = os.path.join(home_data_dir, basename)
        if os.path.exists(wsl_input):
            logger.info("Using fast WSL copy: %s", wsl_input)
            input_file = wsl_input
        else:
            logger.warning(
                "Input file is on a Windows drive (slow in WSL). "
                "For better performance, copy it first:\n"
                "  cp \"%s\" ~/tiledgraph/data/",
                original_input,
            )

    # DuckDB uses a persistent file so it can spill to disk when RAM fills up.
    # We put it in /tmp which is fast and on the Linux filesystem.
    db_name = os.path.splitext(os.path.basename(output_osm))[0] + ".duckdb"
    db_path = os.path.join("/tmp", db_name)

    # ── Print configuration summary ───────────────────────────────────────────
    logger.info("━" * 60)
    logger.info("Input         → %s", input_file)
    logger.info("Output        → %s", output_osm)
    logger.info("DuckDB file   → %s  (deleted on success)", db_path)
    logger.info("RAM limit     : %d GB", memory_gb)
    logger.info("Coord rounding: %d digits  (~%.1f cm at equator)",
                ROUNDING_DIGITS, 10 ** (7 - ROUNDING_DIGITS) * 1.1)
    logger.info("Snap N→N tight: %.5f° (~%.0f m)", SNAP_TOL_TIGHT_DEG,
                SNAP_TOL_TIGHT_DEG * 111_111)
    logger.info("Snap N→N wide : %.5f° (~%.0f m)", SNAP_TOL_WIDE_DEG,
                SNAP_TOL_WIDE_DEG  * 111_111)
    logger.info("Snap P→node   : %.6f° (~%.1f m — square box)",
                ENDPOINT_SNAP_TOL_DEG, ENDPOINT_SNAP_TOL_DEG * 111_111)
    logger.info("Snap P→edge   : %.5f° (~%.0f m — circle check)",
                EDGE_SNAP_TOL_DEG, EDGE_SNAP_TOL_DEG * 111_111)
    logger.info("Log file      → %s", os.path.abspath(LOG_FILE))
    logger.info("━" * 60)

    # ── Open DuckDB connection ────────────────────────────────────────────────
    # Persistent .duckdb file: survives crashes so you can inspect tables
    # with:  duckdb /tmp/<name>.duckdb
    # DuckDB spills to this file automatically when RAM limit is exceeded.
    con = duckdb.connect(db_path)

    # memory_limit: DuckDB's soft cap.  When exceeded, DuckDB spills to disk
    # instead of crashing.  Set to ~60 % of physical RAM so the OS and other
    # processes keep their headroom.
    con.execute(f"SET memory_limit = '{memory_gb}GB';")

    # preserve_insertion_order = false allows DuckDB to reorder rows for speed.
    # We ORDER BY explicitly when order matters, so this is safe.
    con.execute("SET preserve_insertion_order = false;")

    # Leave 2 CPU cores for the OS; DuckDB uses the rest for parallel queries.
    con.execute(f"SET threads = {max(1, os.cpu_count() - 2)};")

    # Spill temporary data to /tmp (fast Linux filesystem, not Windows drive)
    con.execute("SET temp_directory = '/tmp';")

    # Load the spatial extension (provides ST_* functions for geometry)
    con.execute("INSTALL spatial; LOAD spatial;")


    # ─────────────────────────────────────────────────────────────────────────
    # STEP 1 — INGEST SOURCE DATA
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("━━  Step 1 · Ingest source data ...")
    t = time.time()

    # Read the file; returns True if geometry is already parsed, False if raw WKB
    geom_parsed = _read_input(con, input_file)
    raw_count   = con.execute("SELECT COUNT(*) FROM raw_segments").fetchone()[0]
    logger.info("Raw rows ingested: %s  [%s]", f"{raw_count:,}", _elapsed(t))

    # Warn if the original ID column has duplicates (safe because we use ROW_NUMBER)
    duped = con.execute(
        "SELECT COUNT(*) - COUNT(DISTINCT orig_id) FROM raw_segments"
    ).fetchone()[0]
    if duped:
        logger.warning(
            "pkStreetID has %s duplicate values — surrogate id (ROW_NUMBER) "
            "used as primary key, safe to continue", f"{duped:,}"
        )
    else:
        logger.info("pkStreetID is unique across all rows")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 2 — VALIDATE AND EXPLODE GEOMETRIES
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("🔧  Step 2 · Validate, repair, explode geometries ...")
    t = time.time()

    # Repair any invalid geometries (self-intersections, etc.)
    # For Parquet: geom_wkb is already a GEOMETRY column.
    # For GDAL: geom_wkb is raw WKB bytes; we parse with ST_GeomFromWKB first.
    if geom_parsed:
        # Parquet path: geometry is already a GEOMETRY object — just validate.
        con.execute("""
            CREATE OR REPLACE TABLE segs_valid AS
            SELECT id, orig_id, name, oneway, lanes, maxspeed, highway,
                CASE
                    WHEN ST_IsValid(geom_wkb) THEN geom_wkb
                    ELSE ST_MakeValid(geom_wkb)    -- fix invalid geometry
                END AS geom
            FROM raw_segments
            WHERE geom_wkb IS NOT NULL;
        """)
    else:
        # GDAL path: parse the raw WKB bytes first, then validate.
        # try_cast returns NULL instead of crashing on corrupt WKB bytes
        con.execute("""
            CREATE OR REPLACE TABLE segs_valid AS
            SELECT id, orig_id, name, oneway, lanes, maxspeed, highway,
                CASE
                    WHEN ST_IsValid(ST_GeomFromWKB(geom_wkb))
                    THEN ST_GeomFromWKB(geom_wkb)
                    ELSE ST_MakeValid(ST_GeomFromWKB(geom_wkb))
                END AS geom
            FROM raw_segments
            WHERE try_cast(geom_wkb AS BLOB) IS NOT NULL;   -- skip unreadable WKB
        """)
    con.execute("DROP TABLE raw_segments;")   # free space

    valid_count  = con.execute(
        "SELECT COUNT(*) FROM segs_valid WHERE geom IS NOT NULL"
    ).fetchone()[0]
    dropped_geom = raw_count - valid_count
    if dropped_geom:
        logger.warning(
            "Dropped %s rows with corrupt or null geometry", f"{dropped_geom:,}"
        )

    # Explode MultiLineString → individual LineString parts.
    # A single source feature can contain multiple disconnected lines;
    # ST_Dump splits them into separate rows so each becomes its own OSM way.
    con.execute("""
        CREATE OR REPLACE TABLE segments AS
        SELECT
            s.id, s.orig_id, s.name, s.oneway, s.lanes, s.maxspeed, s.highway,
            UNNEST(ST_Dump(s.geom)).geom AS part_geom   -- one row per part
        FROM segs_valid s
        WHERE s.geom IS NOT NULL;
    """)

    total_parts  = con.execute("SELECT COUNT(*) FROM segments").fetchone()[0]

    drop_vtx     = con.execute(f"""
        SELECT COUNT(*) FROM segments WHERE ST_NPoints(part_geom) > {MAX_VERTICES}
    """).fetchone()[0]
    drop_invalid = con.execute(f"""
        SELECT COUNT(*) FROM segments
        WHERE ST_NPoints(part_geom) <= {MAX_VERTICES} AND NOT ST_IsValid(part_geom)
    """).fetchone()[0]

    # Keep only clean, reasonable-length LineStrings
    con.execute(f"""
        CREATE OR REPLACE TABLE segments_clean AS
        SELECT id, orig_id, name, oneway, lanes, maxspeed, highway,
               part_geom AS geom
        FROM segments
        WHERE ST_NPoints(part_geom) >= 2              -- need at least 2 vertices
          AND ST_NPoints(part_geom) <= {MAX_VERTICES} -- reject data artefacts
          AND ST_IsValid(part_geom);                  -- geometry must be valid
    """)
    con.execute("DROP TABLE segments; DROP TABLE segs_valid;")

    seg_count = con.execute("SELECT COUNT(*) FROM segments_clean").fetchone()[0]
    logger.info(
        "Parts: %s total → %s kept  (dropped: vtx_cap=%s  invalid=%s)  [%s]",
        f"{total_parts:,}", f"{seg_count:,}",
        f"{drop_vtx:,}", f"{drop_invalid:,}", _elapsed(t)
    )
    logger.info(
        "Geometry types: %s",
        con.execute(
            "SELECT ST_GeometryType(geom), COUNT(*) FROM segments_clean GROUP BY 1"
        ).fetchall()
    )

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 3 — EXTRACT VERTICES → NODE TABLE
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("📌  Step 3 · Extract vertices, round, deduplicate nodes …")
    t = time.time()
    r = ROUNDING_DIGITS

    # ST_Points returns all vertices of a geometry as a MultiPoint.
    # ST_Dump then explodes that into individual Point rows.
    # ROW_NUMBER() gives each vertex a sequence number within its way —
    # this preserves the vertex order, which matters for road direction.
    #
    # WHY A CTE?  The UNNEST(ST_Dump(...)) alias used to be written as
    # "UNNEST(...) AS d(geom, path)" which caused a DuckDB parser error
    # because "by" is a reserved keyword.  The CTE form avoids that.
    con.execute(f"""
        CREATE OR REPLACE TABLE all_vertices AS
        WITH pts AS (
            SELECT
                s.id AS way_id,
                UNNEST(ST_Dump(ST_Points(s.geom))) AS pt   -- pt is a struct: pt.geom
            FROM segments_clean s
        )
        SELECT
            way_id,
            -- Sequence number within the way — preserves vertex order
            ROW_NUMBER() OVER (PARTITION BY way_id ORDER BY (SELECT NULL)) AS seq,
            ROUND(ST_X(pt.geom), {r})::DOUBLE AS lon,   -- rounded longitude
            ROUND(ST_Y(pt.geom), {r})::DOUBLE AS lat    -- rounded latitude
        FROM pts;
    """)
    con.execute("CHECKPOINT;")

    total_verts = con.execute("SELECT COUNT(*) FROM all_vertices").fetchone()[0]

    # Deduplicate: two vertices with the same rounded (lon, lat) become ONE node.
    # Negative IDs signal "this is new data" to OSM tools (not imported from OSM).
    # ORDER BY lon, lat gives a deterministic ID assignment.
    con.execute("""
        CREATE OR REPLACE TABLE node_ids AS
        SELECT (ROW_NUMBER() OVER (ORDER BY lon, lat)) * -1 AS node_id, lat, lon
        FROM (SELECT DISTINCT lat, lon FROM all_vertices);
    """)

    # Index for fast lookups in Step 4 (join on lon, lat)
    con.execute("CREATE INDEX ni_lonlat ON node_ids (lon, lat);")

    node_count = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
    shared_pct = (1 - node_count / total_verts) * 100 if total_verts else 0
    logger.info(
        "Vertices: %s total → %s unique nodes  (%.1f%% shared between roads)  [%s]",
        f"{total_verts:,}", f"{node_count:,}", shared_pct, _elapsed(t)
    )


    # ─────────────────────────────────────────────────────────────────────────
    # STEP 4 — BUILD WAY_NODES TABLE
    # ─────────────────────────────────────────────────────────────────────────
    # Join every vertex back to its node_id.
    # The result is: for each way, the ordered list of node IDs that form it.
    #
    # way_nodes is the core join table: (way_id, seq, node_id).
    # Every row = "the node at position seq in way way_id is node node_id".
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("🔗  Step 4 · Build way_nodes …")
    t = time.time()
    con.execute("""
        CREATE OR REPLACE TABLE way_nodes AS
        SELECT v.way_id, v.seq, n.node_id
        FROM all_vertices v JOIN node_ids n ON v.lon = n.lon AND v.lat = n.lat
        ORDER BY v.way_id, v.seq;
    """)
    con.execute("DROP TABLE all_vertices;")   # no longer needed
    con.execute("CHECKPOINT;")
    logger.info("Ways (raw): %s  [%s]",
                f"{con.execute('SELECT COUNT(DISTINCT way_id) FROM way_nodes').fetchone()[0]:,}",
                _elapsed(t))

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 5 — BUILD EDGES TABLE  (road attributes)
    # ─────────────────────────────────────────────────────────────────────────
    # edges holds one row per road with its OSM tag values.
    # COALESCE fills in safe defaults for any NULL attribute values.
    #
    # One row per way with the OSM tag values.
    # Separate from way_nodes so we can join them in the XML writer.
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("🔗  Step 5 · Build edges (road attributes) …")
    t = time.time()
    con.execute("""
        CREATE OR REPLACE TABLE edges AS
        SELECT
            id                           AS way_id,
            COALESCE(name,    'unknown') AS name,       -- road name
            COALESCE(highway, 'road')    AS highway,    -- OSM highway type
            COALESCE(oneway,  'no')      AS oneway,     -- one-way flag
            COALESCE(lanes,   '1')       AS lanes,      -- number of lanes
            maxspeed,                                   -- speed limit (may be NULL)
            orig_id                      AS ref         -- original source ID
        FROM segments_clean;
    """)
    con.execute("DROP TABLE segments_clean;")
    con.execute("CHECKPOINT;")
    logger.info("Edges: %s  [%s]",
                f"{con.execute('SELECT COUNT(*) FROM edges').fetchone()[0]:,}",
                _elapsed(t))

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 6 — INITIAL TOPOLOGY VALIDATION
    # ─────────────────────────────────────────────────────────────────────────
    # Coordinate rounding in Step 3 can collapse adjacent vertices to the same
    # node, creating consecutive duplicates or degenerate ways.
    # Clean these up before the snapping steps run.
    #
    # Before snapping, clean up any degenerate ways that were created by the
    # geometry explosion step (e.g. a MULTI* that contained a zero-length part).
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("━━  Step 6 · Topology validation …")
    t = time.time()
    dup_refs, n_degen = _clean_way_nodes(con, "initial validation")
    if dup_refs:
        logger.warning("Removed %s consecutive duplicate node refs", f"{dup_refs:,}")
    if n_degen:
        logger.warning("Dropped %s degenerate ways (fewer than 2 distinct nodes)", f"{n_degen:,}")
    con.execute("CHECKPOINT;")

    node_count = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
    way_count  = con.execute("SELECT COUNT(DISTINCT way_id) FROM way_nodes").fetchone()[0]
    logger.info("After validation — nodes: %s  ways: %s  [%s]",
                f"{node_count:,}", f"{way_count:,}", _elapsed(t))

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 7a — NODE-TO-NODE SNAPPING
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("━━  Step 7a · Node-to-node snap …")
    _snap_node_to_node(con)
    node_count = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
    way_count  = con.execute("SELECT COUNT(DISTINCT way_id) FROM way_nodes").fetchone()[0]
    logger.info("After node-snap — nodes: %s  ways: %s",
                f"{node_count:,}", f"{way_count:,}")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 7b — POINT-TO-EDGE SNAPPING
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("━━  Step 7b · Point-to-edge snap …")
    _snap_point_to_edge(con)
    node_count = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
    way_count  = con.execute("SELECT COUNT(DISTINCT way_id) FROM way_nodes").fetchone()[0]
    logger.info("After edge-snap — nodes: %s  ways: %s",
                f"{node_count:,}", f"{way_count:,}")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 8 — WRITE OSM XML to disk
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("━━  Step 8 · Writing OSM XML → %s …", output_osm)
    _write_osm_xml(con, output_osm, node_count, way_count)

    con.close()

    # Optionally delete the working DuckDB file after a successful run.
    # Uncomment the line below if you do not need to inspect the intermediate
    # tables after the run finishes.
    # os.remove(db_path)

    logger.info("━" * 60)
    logger.info("Pipeline complete in %s", _elapsed(pipeline_t0))
    logger.info("Output OSM   → %s", output_osm)
    logger.info("Next step    → osmium cat %s -o output.osm.pbf", output_osm)
    logger.info("━" * 60)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 10 — COMMAND-LINE ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Validate command-line arguments
    if len(sys.argv) not in (3, 4):
        print()
        print("Usage:")
        print("  python build_osm_topology.py <input> <output.osm> [memory_gb]")
        print()
        print("Arguments:")
        print("  input       Path to the source road dataset.")
        print("              Supported formats: .parquet (fastest), .gpkg,")
        print("              .geojson, .shp")
        print("              Convert to Parquet first for best performance:")
        print("                ogr2ogr -f Parquet roads.parquet roads.gpkg")
        print()
        print("  output.osm  Path where the OSM XML file will be written.")
        print()
        print("  memory_gb   (optional) RAM limit for DuckDB in GB.")
        print("              Default: 8.  Set to ~60 % of available RAM.")
        print("              Check available RAM with:  free -h")
        print()
        print("Next step after this script:")
        print("  osmium cat output.osm -o output.osm.pbf")
        print()
        sys.exit(1)

    memory_gb = int(sys.argv[3]) if len(sys.argv) == 4 else 8
    build_osm_topology(sys.argv[1], sys.argv[2], memory_gb)

    print()
    print("Done!  Convert to PBF for routing engines:")
    print(f"  osmium cat {sys.argv[2]} -o output.osm.pbf")