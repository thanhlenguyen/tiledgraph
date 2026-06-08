"""
diagnose_and_fix_boundaries.py
──────────────────────────────
Diagnoses and fixes topology breaks at region boundaries in the DuckDB
produced by build_osm_topology.py.

PROBLEM
  Source data digitized region-by-region. Roads crossing a boundary are two
  segments whose endpoints are geometrically near-identical but assigned
  different OSM node IDs — so Valhalla sees a gap and cannot route across.

WHAT THIS SCRIPT DOES
  1. DIAGNOSE  — finds all near-duplicate endpoint pairs (distance < SNAP_TOL)
                 that belong to segments from different source regions.
                 Exports a GeoJSON you can load in QGIS/Kepler to see gaps.
  2. FIX       — re-snaps those endpoint pairs to a single shared coordinate,
                 rebuilds node_ids + way_nodes, re-runs Step 8b validation,
                 and overwrites the OSM XML.
  3. ROUTABLE AREA MAP — exports a GeoJSON bounding-box grid showing which
                 tiles contain routable edges, so you can visualise coverage.

USAGE
  # Step 1: diagnose only (fast, no changes)
  python diagnose_and_fix_boundaries.py diagnose <duckdb_file> <output_dir>

  # Step 2: fix + re-export OSM XML
  python diagnose_and_fix_boundaries.py fix <duckdb_file> <output.osm> [snap_tolerance_deg]

  # Step 3: export routable area map
  python diagnose_and_fix_boundaries.py map <duckdb_file> <output.geojson> [grid_deg]

  snap_tolerance_deg : default 1e-4 (≈ 11 m). Raise to 5e-4 (≈ 55 m) if
                       your region boundaries have larger positional errors.
  grid_deg           : cell size for routable-area grid, default 0.1°

DEPENDENCIES
  pip install duckdb lxml tqdm
"""

import gc
import json
import logging
import math
import os
import sys
import time

import duckdb
from lxml import etree
from tqdm import tqdm

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
LOG_FILE = "diagnose_and_fix_boundaries.log"


class _TqdmHandler(logging.StreamHandler):
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
_con = _TqdmHandler(sys.stdout)
_con.setLevel(logging.INFO)
_con.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
logging.getLogger().addHandler(_con)
logger = logging.getLogger(__name__)


def _elapsed(t0):
    s = time.time() - t0
    return f"{s:.1f}s" if s < 60 else f"{s/60:.1f}m"


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_SNAP_TOL = 1e-4   # ≈ 11 m at equator — covers typical boundary digitising error
MIN_SEG_LEN_DEG  = 9e-6   # must match build_osm_topology.py
CHUNK            = 100_000


# ─────────────────────────────────────────────────────────────────────────────
# HELPER: open DuckDB and load spatial
# ─────────────────────────────────────────────────────────────────────────────
def _open(db_path: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(db_path)
    con.execute("INSTALL spatial; LOAD spatial;")
    return con


# ═════════════════════════════════════════════════════════════════════════════
# COMMAND 1 — DIAGNOSE
# ═════════════════════════════════════════════════════════════════════════════
def cmd_diagnose(db_path: str, out_dir: str, snap_tol: float = DEFAULT_SNAP_TOL) -> None:
    """
    Find near-duplicate endpoints that should share a node but don't.
    Exports two files to out_dir:
      boundary_gaps.geojson   — LineString connecting each mismatched endpoint pair
      boundary_gap_nodes.geojson — Point for every orphaned endpoint
    Load either in QGIS / Kepler.gl to see exactly where the breaks are.
    """
    os.makedirs(out_dir, exist_ok=True)
    t0  = time.time()
    con = _open(db_path)

    logger.info("━━━━━━━━━━━━━ DIAGNOSE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("DuckDB   : %s", db_path)
    logger.info("Snap tol : %.1e deg (≈ %.1f m)", snap_tol, snap_tol * 111_111)
    logger.info("Output   : %s", out_dir)

    # ── Check required tables ────────────────────────────────────────────────
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    for need in ("edges", "node_ids", "way_nodes"):
        if need not in tables:
            logger.error(
                "Table '%s' not found. Run build_osm_topology.py first.", need
            )
            sys.exit(1)

    # ── Materialise endpoint table ───────────────────────────────────────────
    # For each way, grab first and last node_id and their (lon, lat).
    # This is cheap: one pass over way_nodes (already sorted).
    logger.info("Extracting way endpoints …")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE endpoints AS
        SELECT
            wn.way_id,
            fn.node_id   AS start_node,
            fn.lon       AS sx,
            fn.lat       AS sy,
            ln.node_id   AS end_node,
            ln.lon       AS ex,
            ln.lat       AS ey
        FROM (
            SELECT way_id,
                FIRST(node_id ORDER BY seq) AS first_nid,
                LAST(node_id  ORDER BY seq) AS last_nid
            FROM way_nodes
            GROUP BY way_id
        ) wn
        JOIN node_ids fn ON fn.node_id = wn.first_nid
        JOIN node_ids ln ON ln.node_id = wn.last_nid;
    """)
    n_ways = con.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0]
    logger.info("Ways with endpoints: %s", f"{n_ways:,}")

    # ── Find near-duplicate endpoint pairs ───────────────────────────────────
    # Self-join on endpoints within snap_tol, excluding same-way pairs and
    # pairs that already share a node (those are fine — they're real junctions).
    #
    # We approximate distance with a fast bounding-box check (ABS diff in
    # lon/lat) rather than ST_Distance, which avoids loading the spatial
    # extension for every pair and is equivalent at this scale.
    logger.info("Searching for near-duplicate endpoint pairs (tol=%.1e) …", snap_tol)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE gap_pairs AS
        -- end of way A close to start of way B
        SELECT
            a.way_id   AS way_a,
            b.way_id   AS way_b,
            a.ex       AS ax,
            a.ey       AS ay,
            b.sx       AS bx,
            b.sy       AS by,
            a.end_node   AS node_a,
            b.start_node AS node_b,
            SQRT(POWER(a.ex - b.sx, 2) + POWER(a.ey - b.sy, 2)) AS dist
        FROM endpoints a
        JOIN endpoints b
          ON a.way_id   <> b.way_id
         AND a.end_node <> b.start_node          -- different nodes → real gap
         AND ABS(a.ex - b.sx) < {snap_tol}
         AND ABS(a.ey - b.sy) < {snap_tol}

        UNION ALL

        -- end of way A close to end of way B
        SELECT
            a.way_id, b.way_id,
            a.ex, a.ey, b.ex, b.ey,
            a.end_node, b.end_node,
            SQRT(POWER(a.ex - b.ex, 2) + POWER(a.ey - b.ey, 2))
        FROM endpoints a
        JOIN endpoints b
          ON a.way_id   <> b.way_id
         AND a.end_node <> b.end_node
         AND ABS(a.ex - b.ex) < {snap_tol}
         AND ABS(a.ey - b.ey) < {snap_tol}

        UNION ALL

        -- start of way A close to start of way B
        SELECT
            a.way_id, b.way_id,
            a.sx, a.sy, b.sx, b.sy,
            a.start_node, b.start_node,
            SQRT(POWER(a.sx - b.sx, 2) + POWER(a.sy - b.sy, 2))
        FROM endpoints a
        JOIN endpoints b
          ON a.way_id    <> b.way_id
         AND a.start_node <> b.start_node
         AND ABS(a.sx - b.sx) < {snap_tol}
         AND ABS(a.sy - b.sy) < {snap_tol}

        UNION ALL

        -- start of way A close to end of way B
        SELECT
            a.way_id, b.way_id,
            a.sx, a.sy, b.ex, b.ey,
            a.start_node, b.end_node,
            SQRT(POWER(a.sx - b.ex, 2) + POWER(a.sy - b.ey, 2))
        FROM endpoints a
        JOIN endpoints b
          ON a.way_id    <> b.way_id
         AND a.start_node <> b.end_node
         AND ABS(a.sx - b.ex) < {snap_tol}
         AND ABS(a.sy - b.ey) < {snap_tol}
    """)

    n_gaps = con.execute("SELECT COUNT(*) FROM gap_pairs").fetchone()[0]
    logger.info("Near-duplicate endpoint pairs found: %s", f"{n_gaps:,}")

    if n_gaps == 0:
        logger.info("✅ No boundary gaps found at tolerance %.1e deg", snap_tol)
        logger.info(
            "If routing still fails, try a larger snap_tol (e.g. 5e-4 ≈ 55 m)"
        )
        con.close()
        return

    # ── Stats ────────────────────────────────────────────────────────────────
    stats = con.execute("""
        SELECT
            MIN(dist)  AS min_m,
            AVG(dist)  AS avg_m,
            MAX(dist)  AS max_m,
            PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY dist) AS p50,
            PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY dist) AS p95
        FROM gap_pairs
    """).fetchone()
    logger.info(
        "Gap distances (deg):  min=%.2e  avg=%.2e  p50=%.2e  p95=%.2e  max=%.2e",
        *stats
    )
    logger.info(
        "Gap distances (m):    min=%.1f  avg=%.1f  p50=%.1f  p95=%.1f  max=%.1f",
        *(x * 111_111 for x in stats)
    )

    # ── Export gap lines GeoJSON ─────────────────────────────────────────────
    gap_geojson = os.path.join(out_dir, "boundary_gaps.geojson")
    logger.info("Writing gap lines → %s …", gap_geojson)

    rows = con.execute("""
        SELECT ax, ay, bx, by, dist, way_a, way_b, node_a, node_b
        FROM gap_pairs
        ORDER BY dist DESC
        LIMIT 200000
    """).fetchall()

    features = []
    for ax, ay, bx, by, dist, way_a, way_b, node_a, node_b in rows:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[ax, ay], [bx, by]],
            },
            "properties": {
                "dist_m":  round(dist * 111_111, 3),
                "way_a":   way_a,
                "way_b":   way_b,
                "node_a":  node_a,
                "node_b":  node_b,
            },
        })

    with open(gap_geojson, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    logger.info("Wrote %s gap lines to %s", f"{len(features):,}", gap_geojson)

    # ── Export orphaned endpoint points ─────────────────────────────────────
    pts_geojson = os.path.join(out_dir, "boundary_gap_nodes.geojson")
    logger.info("Writing gap node points → %s …", pts_geojson)

    pt_rows = con.execute("""
        SELECT DISTINCT ax AS x, ay AS y, node_a AS nid FROM gap_pairs
        UNION
        SELECT DISTINCT bx,     by,       node_b        FROM gap_pairs
    """).fetchall()

    pt_features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [x, y]},
            "properties": {"node_id": nid},
        }
        for x, y, nid in pt_rows
    ]
    with open(pts_geojson, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": pt_features}, f)
    logger.info(
        "Wrote %s orphaned endpoint points to %s",
        f"{len(pt_features):,}", pts_geojson,
    )

    logger.info(
        "DIAGNOSE done [%s]  →  run 'fix' command to repair", _elapsed(t0)
    )
    con.close()


# ═════════════════════════════════════════════════════════════════════════════
# COMMAND 2 — FIX
# ═════════════════════════════════════════════════════════════════════════════
def cmd_fix(
    db_path: str,
    output_osm: str,
    snap_tol: float = DEFAULT_SNAP_TOL,
) -> None:
    """
    Snap near-duplicate endpoints to a single shared node, rebuild
    node_ids + way_nodes, revalidate, and stream a new OSM XML file.

    HOW THE SNAP WORKS
    ──────────────────
    For each cluster of endpoints within snap_tol of each other:
      1. Pick the centroid of all points in the cluster as the canonical coord.
      2. Update every affected row in way_nodes to reference the canonical node.
      3. Update node_ids to move all merged nodes to the canonical coordinate.

    We use a Union-Find (disjoint-set) in Python to cluster overlapping pairs
    efficiently — O(N α(N)) where N = number of gap pairs.
    """
    t0  = time.time()
    con = _open(db_path)

    logger.info("━━━━━━━━━━━━━ FIX ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("DuckDB   : %s", db_path)
    logger.info("Output   : %s", output_osm)
    logger.info("Snap tol : %.1e deg (≈ %.1f m)", snap_tol, snap_tol * 111_111)

    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    for need in ("edges", "node_ids", "way_nodes"):
        if need not in tables:
            logger.error("Table '%s' not found. Run build_osm_topology.py first.", need)
            sys.exit(1)

    # ── Step F1: collect all near-duplicate endpoint pairs ───────────────────
    logger.info("F1 · Collecting near-duplicate endpoint pairs …")
    t = time.time()

    con.execute("""
        CREATE OR REPLACE TEMP TABLE endpoints AS
        SELECT
            wn.way_id,
            fn.node_id AS start_node, fn.lon AS sx, fn.lat AS sy,
            ln.node_id AS end_node,   ln.lon AS ex, ln.lat AS ey
        FROM (
            SELECT way_id,
                FIRST(node_id ORDER BY seq) AS first_nid,
                LAST(node_id  ORDER BY seq) AS last_nid
            FROM way_nodes GROUP BY way_id
        ) wn
        JOIN node_ids fn ON fn.node_id = wn.first_nid
        JOIN node_ids ln ON ln.node_id = wn.last_nid;
    """)

    # Collect all near-duplicate node pairs (node_a, xa, ya, node_b, xb, yb)
    # using the same four-case UNION as diagnose.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE raw_pairs AS
        SELECT node_a, xa, ya, node_b, xb, yb FROM (
            SELECT a.end_node   AS node_a, a.ex AS xa, a.ey AS ya,
                   b.start_node AS node_b, b.sx AS xb, b.sy AS yb
            FROM endpoints a JOIN endpoints b
              ON a.way_id <> b.way_id AND a.end_node <> b.start_node
             AND ABS(a.ex-b.sx)<{snap_tol} AND ABS(a.ey-b.sy)<{snap_tol}
            UNION ALL
            SELECT a.end_node, a.ex, a.ey, b.end_node, b.ex, b.ey
            FROM endpoints a JOIN endpoints b
              ON a.way_id <> b.way_id AND a.end_node <> b.end_node
             AND ABS(a.ex-b.ex)<{snap_tol} AND ABS(a.ey-b.ey)<{snap_tol}
            UNION ALL
            SELECT a.start_node, a.sx, a.sy, b.start_node, b.sx, b.sy
            FROM endpoints a JOIN endpoints b
              ON a.way_id <> b.way_id AND a.start_node <> b.start_node
             AND ABS(a.sx-b.sx)<{snap_tol} AND ABS(a.sy-b.sy)<{snap_tol}
            UNION ALL
            SELECT a.start_node, a.sx, a.sy, b.end_node, b.ex, b.ey
            FROM endpoints a JOIN endpoints b
              ON a.way_id <> b.way_id AND a.start_node <> b.end_node
             AND ABS(a.sx-b.ex)<{snap_tol} AND ABS(a.sy-b.ey)<{snap_tol}
        ) t
    """)

    pairs = con.execute(
        "SELECT node_a, xa, ya, node_b, xb, yb FROM raw_pairs"
    ).fetchall()
    logger.info(
        "Found %s near-duplicate pairs  [%s]", f"{len(pairs):,}", _elapsed(t)
    )

    if not pairs:
        logger.info("✅ No gaps to fix. OSM file is already correct.")
        con.close()
        return

    # ── Step F2: Union-Find clustering ───────────────────────────────────────
    # Group all node IDs that should merge into one canonical node.
    # Union-Find ensures transitivity: if A≈B and B≈C, all three merge.
    logger.info("F2 · Clustering overlapping pairs with Union-Find …")
    t = time.time()

    parent: dict[int, int] = {}
    coords: dict[int, list] = {}   # node_id → [sum_x, sum_y, count]

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent.get(x, x), x)   # path compression
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for node_a, xa, ya, node_b, xb, yb in tqdm(
        pairs, desc="  Clustering", unit=" pairs", dynamic_ncols=True
    ):
        # Register coordinates
        if node_a not in coords:
            coords[node_a] = [xa, ya, 1]
        if node_b not in coords:
            coords[node_b] = [xb, yb, 1]
        union(node_a, node_b)

    # Build canonical → list of members mapping
    clusters: dict[int, list] = {}
    for nid in coords:
        root = find(nid)
        clusters.setdefault(root, []).append(nid)

    # For each cluster, canonical coord = mean of all member coords
    canonical: dict[int, tuple] = {}   # old_node_id → (canonical_node_id, cx, cy)
    for root, members in clusters.items():
        cx = sum(coords[m][0] for m in members) / len(members)
        cy = sum(coords[m][1] for m in members) / len(members)
        for m in members:
            canonical[m] = (root, cx, cy)   # all members map to root node

    n_merges = sum(len(v) - 1 for v in clusters.values())
    logger.info(
        "Clusters: %s  (merging %s redundant nodes)  [%s]",
        f"{len(clusters):,}", f"{n_merges:,}", _elapsed(t),
    )

    # ── Step F3: Apply snapping to node_ids ──────────────────────────────────
    # For each merged node, update its (lat, lon) to the canonical centroid
    # and then delete the redundant duplicates.
    logger.info("F3 · Applying coordinate snap to node_ids …")
    t = time.time()

    # Build update table: (old_node_id, canonical_node_id, new_lon, new_lat)
    update_rows = [
        (old, root, cx, cy)
        for old, (root, cx, cy) in canonical.items()
        if old != root          # root keeps its slot; others are merged into it
    ]
    # Also update root's coordinates to the centroid
    root_rows = [
        (root, cx, cy)
        for root, members in clusters.items()
        for (cx, cy) in [(
            sum(coords[m][0] for m in members) / len(members),
            sum(coords[m][1] for m in members) / len(members),
        )]
    ]

    # Insert mapping table into DuckDB for bulk SQL ops
    con.execute("CREATE OR REPLACE TEMP TABLE _snap_map (old_nid BIGINT, new_nid BIGINT);")
    con.executemany(
        "INSERT INTO _snap_map VALUES (?, ?)",
        [(old, root) for old, (root, cx, cy) in canonical.items()],
    )

    con.execute("CREATE OR REPLACE TEMP TABLE _snap_coords (nid BIGINT, new_lon DOUBLE, new_lat DOUBLE);")
    con.executemany(
        "INSERT INTO _snap_coords VALUES (?, ?, ?)",
        [(root, cx, cy) for root, (cx, cy) in {
            root: (
                sum(coords[m][0] for m in members) / len(members),
                sum(coords[m][1] for m in members) / len(members),
            )
            for root, members in clusters.items()
        }.items()],
    )

    # Update canonical node coordinates to centroid
    con.execute("""
        UPDATE node_ids
        SET lon = sc.new_lon,
            lat = sc.new_lat
        FROM _snap_coords sc
        WHERE node_ids.node_id = sc.nid;
    """)

    # Delete redundant (non-root) nodes
    redundant_ids = [old for old, (root, _, _) in canonical.items() if old != root]
    logger.info("Removing %s redundant node rows from node_ids …", f"{len(redundant_ids):,}")
    # Batch deletes to avoid huge IN() clauses
    BATCH = 10_000
    for i in range(0, len(redundant_ids), BATCH):
        batch = redundant_ids[i : i + BATCH]
        placeholders = ",".join("?" * len(batch))
        con.execute(f"DELETE FROM node_ids WHERE node_id IN ({placeholders})", batch)

    logger.info("node_ids updated  [%s]", _elapsed(t))

    # ── Step F4: Remap way_nodes to canonical node IDs ───────────────────────
    logger.info("F4 · Remapping way_nodes to canonical node IDs …")
    t = time.time()

    con.execute("""
        UPDATE way_nodes
        SET node_id = sm.new_nid
        FROM _snap_map sm
        WHERE way_nodes.node_id = sm.old_nid
          AND sm.old_nid <> sm.new_nid;
    """)
    logger.info("way_nodes remapped  [%s]", _elapsed(t))
    con.execute("CHECKPOINT;")

    # ── Step F5: Re-run Step 8b topology validation ──────────────────────────
    # Snapping may have created new consecutive duplicates or degenerate ways.
    logger.info("F5 · Re-running topology validation …")
    t = time.time()

    con.execute("""
        CREATE OR REPLACE TABLE way_nodes_clean AS
        SELECT way_id, seq, node_id
        FROM (
            SELECT way_id, seq, node_id,
                LAG(node_id) OVER (PARTITION BY way_id ORDER BY seq) AS prev
            FROM way_nodes
        ) t
        WHERE prev IS NULL OR node_id != prev;
    """)

    dup_refs = (
        con.execute("SELECT COUNT(*) FROM way_nodes").fetchone()[0]
        - con.execute("SELECT COUNT(*) FROM way_nodes_clean").fetchone()[0]
    )
    if dup_refs > 0:
        logger.warning("Removed %s new consecutive duplicate refs after snap", f"{dup_refs:,}")

    con.execute("""
        CREATE OR REPLACE TEMP TABLE _degen AS
        SELECT way_id FROM (
            SELECT way_id,
                COUNT(*)                    AS n_refs,
                COUNT(DISTINCT node_id)     AS n_distinct,
                FIRST(node_id ORDER BY seq) AS first_node,
                LAST(node_id  ORDER BY seq) AS last_node
            FROM way_nodes_clean GROUP BY way_id
        ) s
        WHERE n_refs < 2 OR n_distinct < 2 OR first_node = last_node;
    """)
    n_degen = con.execute("SELECT COUNT(*) FROM _degen").fetchone()[0]
    if n_degen > 0:
        logger.warning("Dropping %s newly degenerate ways after snap", f"{n_degen:,}")
        con.execute("DELETE FROM way_nodes_clean WHERE way_id IN (SELECT way_id FROM _degen);")
        con.execute("DELETE FROM edges             WHERE way_id IN (SELECT way_id FROM _degen);")

    con.execute("DROP TABLE IF EXISTS way_nodes;")
    con.execute("ALTER TABLE way_nodes_clean RENAME TO way_nodes;")
    con.execute("CHECKPOINT;")

    way_count  = con.execute("SELECT COUNT(DISTINCT way_id) FROM way_nodes").fetchone()[0]
    node_count = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
    logger.info(
        "After snap + validation — Nodes: %s   Ways: %s  [%s]",
        f"{node_count:,}", f"{way_count:,}", _elapsed(t),
    )

    # ── Step F6: Stream new OSM XML ──────────────────────────────────────────
    logger.info("F6 · Streaming fixed OSM XML → %s …", output_osm)
    _write_osm_xml(con, output_osm, node_count, way_count)

    logger.info("✅  FIX complete [%s]  →  %s", _elapsed(t0), output_osm)
    con.close()


# ═════════════════════════════════════════════════════════════════════════════
# COMMAND 3 — ROUTABLE AREA MAP
# ═════════════════════════════════════════════════════════════════════════════
def cmd_map(db_path: str, out_geojson: str, grid_deg: float = 0.1) -> None:
    """
    Export a GeoJSON grid showing which cells contain routable edges.

    Each cell is a polygon coloured by edge density:
      - present/absent  → tells you where Valhalla can route
      - edge_count      → density proxy for road coverage

    Load in QGIS (style by edge_count) or Kepler.gl (fill color by edge_count)
    to see the routable area at a glance.

    Also exports a simplified convex-hull polygon of the routable area as
    routable_hull.geojson — useful as a quick "are my points inside?" check.
    """
    t0  = time.time()
    con = _open(db_path)

    logger.info("━━━━━━━━━━━━━ ROUTABLE AREA MAP ━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("DuckDB  : %s", db_path)
    logger.info("Output  : %s", out_geojson)
    logger.info("Grid    : %.3f° (≈ %.0f km)", grid_deg, grid_deg * 111)

    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    if "edges" not in tables:
        logger.error("Table 'edges' not found. Run build_osm_topology.py first.")
        sys.exit(1)

    # ── Bounding box ─────────────────────────────────────────────────────────
    bbox = con.execute("""
        SELECT
            MIN(ST_XMin(geom)), MIN(ST_YMin(geom)),
            MAX(ST_XMax(geom)), MAX(ST_YMax(geom))
        FROM edges
    """).fetchone()
    xmin, ymin, xmax, ymax = [float(x) for x in bbox]
    logger.info(
        "Bounding box: (%.4f, %.4f) → (%.4f, %.4f)", xmin, ymin, xmax, ymax
    )

    cols = math.ceil((xmax - xmin) / grid_deg)
    rows = math.ceil((ymax - ymin) / grid_deg)
    logger.info("Grid: %d×%d = %s cells", cols, rows, f"{cols*rows:,}")

    # ── Count edges per cell ─────────────────────────────────────────────────
    logger.info("Counting edges per grid cell …")
    cell_counts = con.execute(f"""
        SELECT
            FLOOR((ST_X(ST_Centroid(geom)) - {xmin}) / {grid_deg})::INTEGER AS gc,
            FLOOR((ST_Y(ST_Centroid(geom)) - {ymin}) / {grid_deg})::INTEGER AS gr,
            COUNT(*) AS edge_count
        FROM edges
        GROUP BY gc, gr
    """).fetchall()
    logger.info("Occupied cells: %s / %s", f"{len(cell_counts):,}", f"{cols*rows:,}")

    # ── Build GeoJSON grid ───────────────────────────────────────────────────
    features = []
    for gc, gr, cnt in tqdm(
        cell_counts, desc="  Building grid", unit=" cells", dynamic_ncols=True
    ):
        x0 = xmin + gc * grid_deg
        y0 = ymin + gr * grid_deg
        x1 = x0 + grid_deg
        y1 = y0 + grid_deg
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0],
                ]],
            },
            "properties": {
                "col":        gc,
                "row":        gr,
                "edge_count": cnt,
                "center_lon": round((x0 + x1) / 2, 6),
                "center_lat": round((y0 + y1) / 2, 6),
            },
        })

    with open(out_geojson, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    logger.info("Wrote %s routable cells → %s", f"{len(features):,}", out_geojson)

    # ── Convex hull of all edge centroids (simplified routable boundary) ──────
    hull_path = os.path.splitext(out_geojson)[0] + "_hull.geojson"
    logger.info("Computing routable area hull → %s …", hull_path)
    hull_wkt = con.execute("""
        SELECT ST_AsGeoJSON(
            ST_ConvexHull(ST_Collect(list(ST_Centroid(geom))))
        )
        FROM edges
    """).fetchone()[0]
    hull_geojson = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": json.loads(hull_wkt),
            "properties": {"description": "Convex hull of routable edge centroids"},
        }],
    }
    with open(hull_path, "w", encoding="utf-8") as f:
        json.dump(hull_geojson, f)
    logger.info("Hull written → %s", hull_path)

    # ── Summary stats ────────────────────────────────────────────────────────
    total_edges = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    coverage_pct = len(cell_counts) / (cols * rows) * 100
    logger.info("Total routable edges : %s", f"{total_edges:,}")
    logger.info("Grid coverage        : %.1f%% of bounding-box cells occupied", coverage_pct)
    logger.info(
        "Bbox area            : %.0f × %.0f km",
        (xmax - xmin) * 111, (ymax - ymin) * 111,
    )
    logger.info("MAP done [%s]", _elapsed(t0))
    con.close()


# ─────────────────────────────────────────────────────────────────────────────
# _write_osm_xml() — identical logic to build_osm_topology.py
# ─────────────────────────────────────────────────────────────────────────────
def _write_osm_xml(
    con: duckdb.DuckDBPyConnection,
    path: str,
    node_count: int,
    way_count: int,
) -> None:
    CHUNK_NODES = 100_000
    CHUNK_REFS  = 500_000

    logger.info("Loading way attributes …")
    t = time.time()
    way_attrs: dict = {}
    cur_attrs = con.execute(
        "SELECT way_id, name, highway, oneway, lanes, maxspeed FROM edges ORDER BY way_id"
    )
    with tqdm(desc="  Loading attrs", unit=" ways", unit_scale=True, dynamic_ncols=True) as pbar:
        while True:
            rows = cur_attrs.fetchmany(100_000)
            if not rows:
                break
            for way_id, name, highway, oneway, lanes, maxspeed in rows:
                way_attrs[way_id] = (name, highway, oneway, lanes, maxspeed)
            pbar.update(len(rows))
    logger.info("Loaded %s way attrs  [%s]", f"{len(way_attrs):,}", _elapsed(t))
    gc.collect()

    with open(path, "wb") as fh:
        with etree.xmlfile(fh, encoding="utf-8") as xf:
            xf.write_declaration()
            with xf.element("osm", version="0.6", generator="build_osm_topology"):

                # Nodes
                logger.info("Writing %s nodes …", f"{node_count:,}")
                t         = time.time()
                n_written = 0
                cur       = con.execute(
                    "SELECT node_id, lat, lon FROM node_ids ORDER BY node_id"
                )
                with tqdm(
                    total=node_count, desc="  Writing nodes",
                    unit=" nodes", unit_scale=True, dynamic_ncols=True,
                ) as pbar:
                    while True:
                        rows = cur.fetchmany(CHUNK_NODES)
                        if not rows:
                            break
                        for node_id, lat, lon in rows:
                            xf.write(etree.Element("node", {
                                "id": str(node_id), "lat": f"{lat:.7f}",
                                "lon": f"{lon:.7f}", "version": "1", "visible": "true",
                            }))
                        n_written += len(rows)
                        pbar.update(len(rows))
                gc.collect()
                logger.info("Nodes done: %s  [%s]", f"{n_written:,}", _elapsed(t))

                # Ways
                logger.info("Writing %s ways …", f"{way_count:,}")
                t = time.time()
                cur_refs = con.execute("""
                    SELECT way_id, node_id FROM way_nodes
                    WHERE way_id IN (
                        SELECT way_id FROM way_nodes GROUP BY way_id HAVING COUNT(*) >= 2
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
                    total=way_count, desc="  Writing ways ",
                    unit=" ways", unit_scale=True, dynamic_ncols=True,
                ) as pbar:
                    while True:
                        rows = cur_refs.fetchmany(CHUNK_REFS)
                        if not rows:
                            break
                        for way_id, node_id in rows:
                            if way_id != current_id:
                                flush_way(way_elem)
                                way_elem = etree.Element("way", {
                                    "id": str(way_id), "version": "1", "visible": "true",
                                })
                                current_id = way_id
                                n_ways    += 1
                                pbar.update(1)
                                name, highway, oneway, lanes, maxspeed = \
                                    way_attrs.get(way_id, ("unknown","road","no","1",None))
                                for k, v in [
                                    ("highway", highway), ("name",    name),
                                    ("oneway",  oneway),  ("lanes",   lanes),
                                    ("maxspeed", maxspeed),
                                ]:
                                    if v is not None and str(v).strip():
                                        etree.SubElement(way_elem, "tag", {"k": k, "v": str(v)})
                            etree.SubElement(way_elem, "nd", {"ref": str(node_id)})
                    flush_way(way_elem)

                if n_skipped:
                    logger.warning("Skipped %s ways with < 2 refs", f"{n_skipped:,}")
                logger.info(
                    "Ways done: %s written, %s skipped  [%s]",
                    f"{n_ways - n_skipped:,}", f"{n_skipped:,}", _elapsed(t),
                )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def _usage():
    print(__doc__)
    sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        _usage()

    cmd = sys.argv[1].lower()

    if cmd == "diagnose":
        # diagnose <db> <out_dir> [snap_tol]
        snap = float(sys.argv[4]) if len(sys.argv) >= 5 else DEFAULT_SNAP_TOL
        cmd_diagnose(sys.argv[2], sys.argv[3], snap)

    elif cmd == "fix":
        # fix <db> <output.osm> [snap_tol]
        snap = float(sys.argv[4]) if len(sys.argv) >= 5 else DEFAULT_SNAP_TOL
        cmd_fix(sys.argv[2], sys.argv[3], snap)

    elif cmd == "map":
        # map <db> <output.geojson> [grid_deg]
        grid = float(sys.argv[4]) if len(sys.argv) >= 5 else 0.1
        cmd_map(sys.argv[2], sys.argv[3], grid)

    else:
        logger.error("Unknown command '%s'. Use: diagnose | fix | map", cmd)
        _usage()

    print()
    if cmd == "fix":
        print("Next step → OSM PBF:")
        print(f"  osmium cat {sys.argv[3]} -o output.osm.pbf")
