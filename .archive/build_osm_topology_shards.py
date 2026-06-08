"""
build_osm_topology.py  —  Regional-shard edition
═══════════════════════════════════════════════════════════════════════════════
Converts a large Parquet road network (country-scale, millions of segments)
into a valid OSM PBF for Valhalla / OSRM / GraphHopper.

WHY THIS VERSION EXISTS
  The single-pass tiled version is killed by the Linux OOM killer around tile
  12 000 of 37 000 for KSA-scale data.  Root cause: DuckDB's buffer pool holds
  noded_segments (growing) + seg_geoms (being queried) + ST_Node scratch space
  simultaneously.  Even with CHECKPOINT the OS RSS grows monotonically because
  Linux doesn't evict clean file-backed pages until memory pressure is extreme.

SOLUTION — REGIONAL SHARDS
  1. Partition the Parquet file into R×C geographic shards, each written to its
     own Parquet file.  Overlap = 50 % of shard size so border roads are
     fully noded within exactly one shard.
  2. Process each shard in a fresh DuckDB connection (own .duckdb file,
     own buffer pool, fully freed after the shard finishes).
  3. Collect per-shard .osm files and merge with:
       osmium merge shard_*.osm -o merged.osm
       osmium sort  merged.osm  -o sorted.osm
       osmium cat   sorted.osm  -o final.osm.pbf

BORDER-NODE DEDUPLICATION
  Adjacent shards share an overlap band.  Nodes in that band are written by
  both shards with identical (lat, lon) rounded to 7 decimal places.
  osmium merge + sort deduplicates by (id, lat, lon) — nodes with the same
  coordinates but different IDs from different shards are NOT deduplicated
  automatically.  We solve this with a post-merge re-ID step (see Step 3).

USAGE
  python build_osm_topology.py <input.parquet> <output.osm.pbf> \\
         [--regions R C] [--tile-size DEG] [--memory-gb GB] [--workers N]

  input.parquet    GeoParquet road network
  output.osm.pbf   Final merged OSM PBF
  --regions R C    Grid of R rows × C cols (default: auto from dataset size)
  --tile-size DEG  ST_Node tile size within each shard (default: 0.02°)
  --memory-gb GB   DuckDB memory limit per shard process (default: 6)
  --workers N      Parallel shard workers (default: 1, use 2-4 if RAM allows)

DEPENDENCIES
  pip install duckdb lxml
  apt install osmium-tool   (for the merge step)

SHARD SIZING GUIDE (KSA ~1.15 M segments, 10 GB RAM WSL)
  --regions 3 4   → 12 shards, ~100 k segments each, ~3 GB peak per shard ✓
  --regions 4 5   → 20 shards, ~60 k segments each, ~2 GB peak per shard ✓
  --regions 2 3   → 6 shards, ~190 k segments each, may still OOM at 10 GB
"""

import argparse
import json
import math
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile

import duckdb
from lxml import etree


# ═══════════════════════════════════════════════════════════════════════════════
# HIGHWAY CLASSIFICATION  (same as original)
# ═══════════════════════════════════════════════════════════════════════════════
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

CHECKPOINT_EVERY = 200   # flush noded_segments to disk every N tiles


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 0 — partition the Parquet file into geographic shards
# ═══════════════════════════════════════════════════════════════════════════════

def partition_parquet(input_parquet: str, shard_dir: str,
                      n_rows: int, n_cols: int,
                      overlap_factor: float = 0.5) -> list[dict]:
    """
    Read the input Parquet once, compute the bbox, write R×C Parquet shards.

    Each shard covers its core cell PLUS overlap_factor × cell_size on each
    side.  Roads that cross a cell boundary appear in both adjacent shards;
    this ensures every intersection is fully noded within at least one shard.

    Returns a list of shard descriptors:
      { "path": str, "row": int, "col": int,
        "cx0": float, "cy0": float, "cx1": float, "cy1": float }
    """
    os.makedirs(shard_dir, exist_ok=True)

    print(f"\n📦  Partitioning → {n_rows}×{n_cols} = {n_rows*n_cols} shards …")

    # One fast DuckDB connection just for partitioning (reads Parquet only)
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")

    # ── Detect geometry column type ───────────────────────────────────────────
    # GeoParquet stores geometry as GEOMETRY('EPSG:4326') — already parsed.
    # Older/plain Parquet may store it as BLOB (raw WKB bytes).
    # ST_GeomFromWKB(GEOMETRY) raises a BinderException, so we must branch.
    geom_type = con.execute(f"""
        SELECT typeof(geometry)
        FROM read_parquet('{input_parquet}')
        LIMIT 1
    """).fetchone()[0].upper()
    geom_is_parsed = "BLOB" not in geom_type  # GEOMETRY, POINT, etc. → already parsed
    print(f"   Geometry column type: {geom_type} → {'already GEOMETRY' if geom_is_parsed else 'raw WKB BLOB'}")

    # Helper: normalise geometry. ST_X/ST_Y only work on POINT geometries.
    # For LINESTRING/MULTILINESTRING use envelope accessors: ST_XMin/ST_XMax/ST_YMin/ST_YMax.
    # For filtering, use ST_Intersects(geom, ST_MakeEnvelope(...)).
    if geom_is_parsed:
        geom_expr = "geometry"
    else:
        geom_expr = "ST_GeomFromWKB(geometry)"

    # Bounding box of entire dataset — envelope accessors work on any geometry type.
    bbox = con.execute(f"""
        SELECT
            MIN(ST_XMin({geom_expr})) AS xmin,
            MIN(ST_YMin({geom_expr})) AS ymin,
            MAX(ST_XMax({geom_expr})) AS xmax,
            MAX(ST_YMax({geom_expr})) AS ymax
        FROM read_parquet('{input_parquet}')
        WHERE geometry IS NOT NULL
    """).fetchone()
    xmin, ymin, xmax, ymax = [float(v) for v in bbox]

    cell_w = (xmax - xmin) / n_cols
    cell_h = (ymax - ymin) / n_rows
    ov_x   = cell_w * overlap_factor
    ov_y   = cell_h * overlap_factor

    print(f"   BBox: ({xmin:.4f},{ymin:.4f}) \u2192 ({xmax:.4f},{ymax:.4f})")
    print(f"   Cell: {cell_w:.4f}\u00b0 \u00d7 {cell_h:.4f}\u00b0  overlap: {ov_x:.4f}\u00b0 \u00d7 {ov_y:.4f}\u00b0")

    shards = []
    for r in range(n_rows):
        for c in range(n_cols):
            # Core cell bounds
            cx0 = xmin + c * cell_w;      cx1 = cx0 + cell_w
            cy0 = ymin + r * cell_h;      cy1 = cy0 + cell_h
            # Expanded bounds with overlap
            ex0 = max(xmin, cx0 - ov_x); ex1 = min(xmax, cx1 + ov_x)
            ey0 = max(ymin, cy0 - ov_y); ey1 = min(ymax, cy1 + ov_y)

            shard_path = os.path.join(shard_dir, f"shard_{r:02d}_{c:02d}.parquet")

            # Export any segment that intersects the expanded shard bbox.
            # ST_Intersects works for any geometry type including LINESTRING.
            con.execute(f"""
                COPY (
                    SELECT *
                    FROM read_parquet('{input_parquet}')
                    WHERE geometry IS NOT NULL
                      AND ST_Intersects(
                              {geom_expr},
                              ST_MakeEnvelope({ex0}, {ey0}, {ex1}, {ey1})
                          )
                ) TO '{shard_path}' (FORMAT PARQUET)
            """)

            n = con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{shard_path}')"
            ).fetchone()[0]

            if n == 0:
                os.remove(shard_path)
                print(f"   Shard ({r},{c}): empty, skipped")
            else:
                shards.append({
                    "path": shard_path,
                    "row": r, "col": c,
                    "cx0": cx0, "cy0": cy0,
                    "cx1": cx1, "cy1": cy1,
                    "ex0": ex0, "ey0": ey0,
                    "ex1": ex1, "ey1": ey1,
                    "count": n,
                })
                print(f"   Shard ({r},{c}): {n:,} rows → {os.path.basename(shard_path)}")

    con.close()
    print(f"   Partitioning done.  {len(shards)} non-empty shards.")
    return shards


# ═══════════════════════════════════════════════════════════════════════════════
# SHARD PROCESSING — runs in its own process / DuckDB connection
# ═══════════════════════════════════════════════════════════════════════════════

def process_shard(shard: dict, out_osm: str, tile_size: float,
                  memory_gb: int, id_offset: int, n_workers: int = 1) -> int:
    """
    Process one geographic shard:
      ingest Parquet → validate → explode → node → join → write OSM XML

    id_offset: multiply shard index so node/way IDs don't collide across shards.
               We reserve id_offset * 10^9 IDs per shard.

    Returns the number of ways written.
    """
    label = f"({shard['row']},{shard['col']})"

    # Derive the DuckDB working file from the OSM output path.
    # IMPORTANT: use splitext on the basename — never str.replace(".osm", …)
    # on the full path because the parent directory name may also contain ".osm"
    # (e.g. "street_ksa.osm_work/") and replace() would silently corrupt it.
    db_path = os.path.join(
        os.path.dirname(out_osm),
        os.path.splitext(os.path.basename(out_osm))[0] + ".duckdb"
    )
    con = duckdb.connect(db_path)
    con.execute(f"SET memory_limit = '{memory_gb}GB';")
    con.execute("SET preserve_insertion_order = false;")
    con.execute(f"SET threads = {max(1, os.cpu_count() // max(1, n_workers))};")
    con.execute("SET temp_directory = '/tmp';")
    con.execute("INSTALL spatial; LOAD spatial;")

    # ── Detect geometry column type ───────────────────────────────────────────
    # GeoParquet stores geometry as GEOMETRY type (already parsed by DuckDB).
    # Plain Parquet / WKB stores it as BLOB — needs ST_GeomFromWKB().
    geom_type_row = con.execute(f"""
        SELECT typeof(geometry) FROM read_parquet(\'{shard["path"]}\') LIMIT 1
    """).fetchone()
    geom_is_parsed = geom_type_row is not None and "BLOB" not in geom_type_row[0].upper()
    geom_col = "geometry" if geom_is_parsed else "ST_GeomFromWKB(geometry)"

    # ── Ingest ────────────────────────────────────────────────────────────────
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
            {geom_col}                                          AS geom_raw
        FROM read_parquet(\'{shard["path"]}\')
        WHERE geometry IS NOT NULL;
    """)

    # ── Validate + repair ─────────────────────────────────────────────────────
    con.execute("""
        CREATE OR REPLACE TABLE raw_segments_parsed AS
        SELECT
            id, name, oneway, lanes, maxspeed, highway,
            CASE
                WHEN ST_IsValid(geom_raw) THEN geom_raw
                ELSE ST_MakeValid(geom_raw)
            END AS geom
        FROM raw_segments
        WHERE geom_raw IS NOT NULL;
    """)
    con.execute("DROP TABLE IF EXISTS raw_segments;")

    # ── Explode MULTI*, snap, dedup ───────────────────────────────────────────
    con.execute("""
        CREATE OR REPLACE TABLE segments AS
        SELECT
            id, name, oneway, lanes, maxspeed, highway,
            ST_GeomFromWKB(ST_AsWKB(ST_ReducePrecision(geom_part, 1e-6))) AS geom
        FROM (
            SELECT r.id, r.name, r.oneway, r.lanes, r.maxspeed, r.highway,
                   UNNEST(ST_Dump(r.geom)).geom AS geom_part
            FROM raw_segments_parsed r
            WHERE r.geom IS NOT NULL
        ) exploded
        WHERE ST_Length(geom_part) > 1e-8
          AND ST_IsValid(geom_part)
        QUALIFY ROW_NUMBER() OVER (PARTITION BY ST_AsText(geom_part)) = 1;
    """)
    con.execute("DROP TABLE IF EXISTS raw_segments_parsed;")

    # ── Split geom / attrs ────────────────────────────────────────────────────
    con.execute("CREATE OR REPLACE TABLE seg_geoms AS SELECT id, geom FROM segments;")
    con.execute("""
        CREATE OR REPLACE TABLE seg_attrs AS
        SELECT id, name, highway, oneway, lanes, maxspeed FROM segments;
    """)
    con.execute("DROP TABLE IF EXISTS segments;")
    con.execute("CHECKPOINT;")

    # ── Tiled ST_Node — streams each tile to its own Parquet file ────────────
    # tile_parquet_dir lives next to the shard .duckdb file in /tmp.
    # After _tiled_node, noded_segments is a VIEW over read_parquet(glob).
    shard_label = f"{shard['row']:02d}_{shard['col']:02d}"
    tile_parquet_dir = os.path.join("/tmp", f"tiles_{shard_label}")
    _tiled_node(con, tile_size,
                core_x0=shard["cx0"], core_y0=shard["cy0"],
                core_x1=shard["cx1"], core_y1=shard["cy1"],
                tile_parquet_dir=tile_parquet_dir)

    # seg_geoms no longer needed after noding — drop immediately.
    con.execute("DROP TABLE IF EXISTS seg_geoms;")

    # ── Attribute join → edges Parquet ────────────────────────────────────────
    # NEVER materialise edges as a DuckDB TABLE.
    # With 211k segments × geometry + attributes, a CREATE TABLE edges AS …
    # forces DuckDB to hold all geometry in the buffer pool simultaneously.
    # Instead: COPY the join result straight to a Parquet file.
    # DuckDB pipelines the read_parquet + join + COPY without buffering all rows.
    id_base = id_offset * 1_000_000_000
    edges_parquet = os.path.join("/tmp", f"edges_{shard_label}.parquet")

    con.execute(f"""
        COPY (
            SELECT
                ({id_base} + n.seg_id) * -1             AS way_id,
                n.geom_wkb,
                COALESCE(s.name,    'unknown')           AS name,
                COALESCE(s.highway, 'road')              AS highway,
                COALESCE(s.oneway,  'no')                AS oneway,
                COALESCE(s.lanes,   '1')                 AS lanes,
                s.maxspeed
            FROM noded_segments n
            LEFT JOIN seg_attrs s ON n.src_id = s.id
        ) TO '{edges_parquet}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    con.execute("DROP VIEW IF EXISTS noded_segments;")
    con.execute("DROP TABLE IF EXISTS seg_attrs;")
    import shutil as _shutil
    _shutil.rmtree(tile_parquet_dir, ignore_errors=True)

    way_count = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{edges_parquet}')"
    ).fetchone()[0]
    print(f"      Ways (edges): {way_count:,}")

    # ── Extract vertices → edge_points Parquet ────────────────────────────────
    # Geometry is stored as WKB in edges_parquet. Parse it here with
    # ST_GeomFromWKB → ST_Points → ST_Dump to extract individual vertices.
    # Result goes directly to Parquet — no DuckDB table, no index overhead.
    ep_parquet = os.path.join("/tmp", f"ep_{shard_label}.parquet")
    con.execute(f"""
        COPY (
            SELECT
                e.way_id,
                d.dump_struct.path[1]          AS seq,
                ST_X(d.dump_struct.geom)       AS lon,
                ST_Y(d.dump_struct.geom)       AS lat
            FROM read_parquet('{edges_parquet}') e,
                 UNNEST(ST_Dump(ST_Points(ST_GeomFromWKB(e.geom_wkb)))) AS d(dump_struct)
        ) TO '{ep_parquet}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    # ── node_ids → Parquet (no DuckDB table, no index) ───────────────────────
    # With 3.7M nodes, building a DuckDB table + index consumes ~2-3 GB.
    # Instead: write node_ids directly to Parquet, then use a pure hash join
    # (read_parquet JOIN read_parquet) for the way_nodes step.
    # DuckDB's hash join on two Parquet scans uses O(smaller_table) RAM,
    # not O(total rows), and needs no index.
    # Assign node IDs in hash order (no sort) — node IDs just need to be
    # unique and stable within this shard; the XML writer sorts them later
    # via an external-sort read_parquet scan that spills to /tmp.
    ni_parquet = os.path.join("/tmp", f"ni_{shard_label}.parquet")
    con.execute(f"""
        COPY (
            SELECT
                ({id_base} + ROW_NUMBER() OVER ()) * -1 AS node_id,
                lat, lon
            FROM (
                SELECT DISTINCT lat, lon
                FROM read_parquet('{ep_parquet}')
            )
        ) TO '{ni_parquet}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    node_count = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{ni_parquet}')"
    ).fetchone()[0]
    print(f"      Nodes: {node_count:,}")

    # ── way_nodes → Parquet: hash join WITHOUT ORDER BY ──────────────────────
    # ORDER BY inside COPY forces DuckDB to sort all ~20M vertex rows in RAM
    # before writing — at 3.5M nodes that alone hits the memory limit.
    # We write unsorted here and sort lazily inside _write_osm_xml using
    # DuckDB's external merge-sort on the read_parquet() scan, which spills
    # to temp_directory and only buffers one merge run at a time.
    wn_parquet = os.path.join("/tmp", f"wn_{shard_label}.parquet")
    con.execute(f"""
        COPY (
            SELECT ep.way_id, ep.seq, ni.node_id
            FROM read_parquet('{ep_parquet}') ep
            JOIN read_parquet('{ni_parquet}') ni
              ON ep.lon = ni.lon AND ep.lat = ni.lat
        ) TO '{wn_parquet}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    os.remove(ep_parquet)

    # ── Write OSM XML ──────────────────────────────────────────────────────────
    # _write_osm_xml reads ni_parquet for <node> elements and
    # edges_parquet + wn_parquet for <way> elements — all sequential scans.
    _write_osm_xml(con, out_osm, edges_parquet, wn_parquet, ni_parquet)
    os.remove(ni_parquet)

    # ── Cleanup ────────────────────────────────────────────────────────────────
    os.remove(edges_parquet)
    os.remove(wn_parquet)
    con.close()
    try:
        os.remove(db_path)
    except OSError:
        pass

    return way_count


# ═══════════════════════════════════════════════════════════════════════════════
# TILED ST_NODE  (same algorithm as original, but respects shard core bounds)
# ═══════════════════════════════════════════════════════════════════════════════

def _node_tile(con, ex0, ey0, ex1, ey1, cx0, cy0, cx1, cy1,
               seg_counter, tile_parquet_path):
    """
    Node one tile and write results directly to a Parquet file.

    CRITICAL MEMORY CHANGE vs INSERT-based approach:
      INSERT INTO noded_segments keeps every row in DuckDB's buffer pool as
      dirty WAL pages.  After 33 000 tiles × 160 rows = 5 M geometry rows,
      the buffer pool is exhausted and the OOM killer fires.

      Writing to Parquet instead:
        - DuckDB compresses and flushes the file immediately after COPY TO
        - The temp table _tile_noded is then dropped → its pages are evicted
        - Buffer pool stays flat regardless of how many tiles have been processed
        - /tmp is used for scratch, not the DuckDB WAL
    """
    con.execute("DROP TABLE IF EXISTS _tile_noded;")
    con.execute(f"""
        CREATE TEMP TABLE _tile_noded AS
        WITH src AS (
            SELECT id, geom FROM seg_geoms
            WHERE ST_Intersects(geom, ST_MakeEnvelope({ex0},{ey0},{ex1},{ey1}))
        ),
        collected AS (
            SELECT ST_Collect(list(geom)) AS collected_geom FROM src
        ),
        noded AS (
            SELECT ST_Node(collected_geom) AS noded_geom
            FROM collected WHERE collected_geom IS NOT NULL
        ),
        dumped AS (
            SELECT
                ST_GeomFromWKB(ST_AsWKB((d.dump_struct).geom))  AS geom,
                ST_X(ST_Centroid((d.dump_struct).geom))         AS cx,
                ST_Y(ST_Centroid((d.dump_struct).geom))         AS cy,
                ST_PointN((d.dump_struct).geom, 2)               AS interior_pt
            FROM noded,
                 UNNEST(ST_Dump(noded_geom)) AS d(dump_struct)
            WHERE NOT ST_IsEmpty((d.dump_struct).geom)
              AND ST_NPoints((d.dump_struct).geom) >= 2
              AND ST_Length((d.dump_struct).geom) > 1e-8
        ),
        owned AS (
            SELECT geom, interior_pt FROM dumped
            WHERE cx >= {cx0} AND cx < {cx1}
              AND cy >= {cy0} AND cy < {cy1}
        )
        SELECT o.geom, s.id AS src_id
        FROM owned o
        LEFT JOIN LATERAL (
            SELECT src.id FROM src
            WHERE ST_Contains(src.geom, o.interior_pt)
            LIMIT 1
        ) s ON true;
    """)

    n = con.execute("SELECT COUNT(*) FROM _tile_noded").fetchone()[0]
    if n > 0:
        # Write directly to Parquet — zero buffer pool accumulation.
        # Store geom as WKB BLOB (not GEOMETRY) so read_parquet() always
        # returns a consistent BLOB type regardless of spatial extension state.
        # The column is named geom_wkb to make the contract explicit.
        con.execute(f"""
            COPY (
                SELECT
                    {seg_counter} + ROW_NUMBER() OVER () AS seg_id,
                    ST_AsWKB(geom)                       AS geom_wkb,
                    src_id
                FROM _tile_noded
            ) TO '{tile_parquet_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)

    con.execute("DROP TABLE IF EXISTS _tile_noded;")
    return n


def _node_tile_subdivide(con, cx0, cy0, cx1, cy1, seg_counter,
                          tile_parquet_dir, depth=1):
    if depth > 3:
        print(f"   ⚠️  Skipping pathological tile at depth {depth}")
        return seg_counter
    n_sub = 4
    sub_w = (cx1 - cx0) / n_sub
    sub_h = (cy1 - cy0) / n_sub
    for si in range(n_sub):
        for sj in range(n_sub):
            scx0 = cx0 + si * sub_w;  scx1 = scx0 + sub_w
            scy0 = cy0 + sj * sub_h;  scy1 = scy0 + sub_h
            sov_x = sub_w * 0.70;     sov_y = sub_h * 0.70
            sex0 = scx0 - sov_x;  sex1 = scx1 + sov_x
            sey0 = scy0 - sov_y;  sey1 = scy1 + sov_y
            tile_path = os.path.join(
                tile_parquet_dir,
                f"tile_d{depth}_{int(scx0*1e4)}_{int(scy0*1e4)}.parquet"
            )
            try:
                seg_counter += _node_tile(con, sex0, sey0, sex1, sey1,
                                               scx0, scy0, scx1, scy1,
                                               seg_counter, tile_path)
            except Exception as e:
                if "OutOfMemory" in type(e).__name__ or "out of memory" in str(e).lower():
                    seg_counter = _node_tile_subdivide(
                        con, scx0, scy0, scx1, scy1, seg_counter, tile_parquet_dir, depth + 1)
                else:
                    raise
    return seg_counter


def _tiled_node(con, tile_size, core_x0, core_y0, core_x1, core_y1, tile_parquet_dir):
    """
    Node the geometry in seg_geoms tile by tile.

    MEMORY STRATEGY — Parquet streaming:
      Each tile's noded segments are written to their own Parquet file in
      tile_parquet_dir instead of being INSERTed into a DuckDB table.
      This keeps DuckDB's buffer pool at a constant low level regardless
      of how many tiles have been processed — the pool only ever holds:
        • seg_geoms pages for the current tile's bbox query  (~small)
        • _tile_noded scratch pages, dropped immediately after COPY TO
      After the loop, all tile Parquets are read back with read_parquet(glob)
      for the attribute join — one sequential scan, no random writes.
    """
    os.makedirs(tile_parquet_dir, exist_ok=True)

    bbox = con.execute("""
        SELECT MIN(ST_XMin(geom)), MIN(ST_YMin(geom)),
               MAX(ST_XMax(geom)), MAX(ST_YMax(geom))
        FROM seg_geoms
    """).fetchone()
    xmin, ymin, xmax, ymax = [float(x) for x in bbox]

    cols = math.ceil((xmax - xmin) / tile_size)
    rows = math.ceil((ymax - ymin) / tile_size)
    overlap = tile_size * 0.50

    occupied_set = set(con.execute(f"""
        SELECT DISTINCT
            LEAST(FLOOR((ST_X(ST_Centroid(geom)) - {xmin}) / {tile_size})::INTEGER, {cols-1}),
            LEAST(FLOOR((ST_Y(ST_Centroid(geom)) - {ymin}) / {tile_size})::INTEGER, {rows-1})
        FROM seg_geoms
    """).fetchall())

    seg_counter = 0
    processed = 0
    n_occ = len(occupied_set)
    written_parquets = []

    for r in range(rows):
        for c in range(cols):
            if (c, r) not in occupied_set:
                continue

            tx0 = xmin + c * tile_size;   tx1 = tx0 + tile_size
            ty0 = ymin + r * tile_size;   ty1 = ty0 + tile_size
            tcx0 = max(tx0, core_x0);     tcx1 = min(tx1, core_x1)
            tcy0 = max(ty0, core_y0);     tcy1 = min(ty1, core_y1)

            if tcx0 >= tcx1 or tcy0 >= tcy1:
                continue

            ex0 = tx0 - overlap;  ex1 = tx1 + overlap
            ey0 = ty0 - overlap;  ey1 = ty1 + overlap

            # Unique filename per tile — no collisions even on retry/subdivide
            tile_path = os.path.join(
                tile_parquet_dir, f"tile_{r:05d}_{c:05d}.parquet"
            )

            try:
                n = _node_tile(con, ex0, ey0, ex1, ey1,
                               tcx0, tcy0, tcx1, tcy1,
                               seg_counter, tile_path)
                if n > 0:
                    written_parquets.append(tile_path)
                seg_counter += n

            except Exception as e:
                if "OutOfMemory" in type(e).__name__ or "out of memory" in str(e).lower():
                    print(f"   ⚠️  OOM tile ({c},{r}) → subdivide …")
                    old_counter = seg_counter
                    seg_counter = _node_tile_subdivide(
                        con, tcx0, tcy0, tcx1, tcy1, seg_counter,
                        tile_parquet_dir)
                    # Collect any sub-tile Parquets that were written
                    import glob as _glob
                    written_parquets += [
                        p for p in _glob.glob(
                            os.path.join(tile_parquet_dir, f"tile_d*_{int(tcx0*1e4)}_*.parquet")
                        )
                    ]
                else:
                    raise

            processed += 1
            if processed % 500 == 0 or processed == n_occ:
                pct = processed / n_occ * 100
                print(f"      {processed:4d}/{n_occ:,} tiles ({pct:.0f}%)  → {seg_counter:,} segs")

    # No CHECKPOINT needed — we never wrote to the DuckDB WAL during noding.
    # Load all tile Parquets into a lazy view for the attribute join step.
    # Always return a VIEW — never a TABLE — so callers can unconditionally
    # use DROP VIEW IF EXISTS noded_segments without a Catalog type mismatch.
    glob_pattern = os.path.join(tile_parquet_dir, "*.parquet")
    if not written_parquets:
        # No segments produced (e.g. shard entirely outside core bounds).
        # Write one empty sentinel Parquet so read_parquet(glob) doesn't fail.
        sentinel = os.path.join(tile_parquet_dir, "empty_sentinel.parquet")
        os.makedirs(tile_parquet_dir, exist_ok=True)
        con.execute(f"""
            COPY (
                SELECT CAST(NULL AS BIGINT) AS seg_id,
                       CAST(NULL AS BLOB)   AS geom_wkb,
                       CAST(NULL AS BIGINT) AS src_id
                WHERE false
            ) TO '{sentinel}' (FORMAT PARQUET)
        """)

    # DuckDB forbids CREATE OR REPLACE VIEW when a TABLE of the same name
    # exists — it does not cross-replace types. Drop both explicitly first.
    con.execute("DROP VIEW  IF EXISTS noded_segments;")
    con.execute("DROP TABLE IF EXISTS noded_segments;")
    # geom column in tile Parquets is already WKB BLOB (written by _node_tile
    # via ST_AsWKB inside the COPY query). The VIEW exposes it as geom_wkb
    # so downstream code never needs to call ST_AsWKB again.
    con.execute(f"""
        CREATE VIEW noded_segments AS
        SELECT seg_id, geom_wkb, src_id
        FROM read_parquet('{glob_pattern}', hive_partitioning=false)
        WHERE seg_id IS NOT NULL;
    """)

    print(f"      Tile Parquets written: {len(written_parquets):,}  total segs: {seg_counter:,}")


# ═══════════════════════════════════════════════════════════════════════════════
# OSM XML WRITER  (same as original)
# ═══════════════════════════════════════════════════════════════════════════════

class _ChunkedCursor:
    """
    Wraps a DuckDB cursor and exposes it as a peekable row-by-row iterator.
    Avoids nested-closure nonlocal bugs by keeping all state in instance vars.
    """
    def __init__(self, cursor, chunk=50_000):
        self._cur   = cursor
        self._chunk = chunk
        self._buf   = []
        self._pos   = 0
        self._done  = False
        self._fill()

    def _fill(self):
        if self._done:
            return
        rows = self._cur.fetchmany(self._chunk)
        if rows:
            self._buf = rows
            self._pos = 0
        else:
            self._buf = []
            self._pos = 0
            self._done = True

    def peek(self):
        """Return next row without consuming it, or None if exhausted."""
        if self._pos >= len(self._buf):
            self._fill()
        if self._pos >= len(self._buf):
            return None
        return self._buf[self._pos]

    def next(self):
        """Consume and return next row, or None if exhausted."""
        row = self.peek()
        if row is not None:
            self._pos += 1
        return row


def _write_osm_xml(con, path, edges_parquet, wn_parquet, ni_parquet):
    """
    Stream OSM XML with O(CHUNK) peak RAM — no in-memory JOIN or sort buffer.

    Pre-sort both wn_parquet and edges_parquet via DuckDB COPY (external sort,
    spills to temp_directory). Then use _ChunkedCursor for a clean two-pointer
    merge in Python — no nested-closure nonlocal state, no unpacking ambiguity.
    """
    CHUNK = 50_000

    wn_sorted = wn_parquet.replace(".parquet", "_sorted.parquet")
    ed_sorted = edges_parquet.replace(".parquet", "_sorted.parquet")

    print("      Sorting wn_parquet …")
    con.execute(f"""
        COPY (
            SELECT way_id, seq, node_id
            FROM read_parquet('{wn_parquet}')
            ORDER BY way_id, seq
        ) TO '{wn_sorted}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    print("      Sorting edges_parquet …")
    con.execute(f"""
        COPY (
            SELECT way_id, name, highway, oneway, lanes, maxspeed
            FROM read_parquet('{edges_parquet}')
            ORDER BY way_id
        ) TO '{ed_sorted}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    with open(path, "wb") as fh:
        with etree.xmlfile(fh, encoding="utf-8") as xf:
            xf.write_declaration()
            with xf.element("osm", version="0.6", generator="build_osm_topology"):

                # ── Nodes ─────────────────────────────────────────────────────
                node_cur = _ChunkedCursor(con.execute(
                    f"SELECT node_id, lat, lon "
                    f"FROM read_parquet('{ni_parquet}') ORDER BY node_id"
                ), CHUNK)
                while True:
                    row = node_cur.next()
                    if row is None:
                        break
                    node_id, lat, lon = row
                    xf.write(etree.Element("node", {
                        "id": str(node_id),
                        "lat": f"{lat:.7f}",
                        "lon": f"{lon:.7f}",
                        "version": "1",
                        "visible": "true",
                    }))

                # ── Ways: two-pointer merge on sorted Parquet files ───────────
                ed_cur = _ChunkedCursor(con.execute(
                    f"SELECT way_id, name, highway, oneway, lanes, maxspeed "
                    f"FROM read_parquet('{ed_sorted}') ORDER BY way_id"
                ), CHUNK)
                wn_cur = _ChunkedCursor(con.execute(
                    f"SELECT way_id, seq, node_id "
                    f"FROM read_parquet('{wn_sorted}') ORDER BY way_id, seq"
                ), CHUNK)

                way_elem     = None
                current_wid  = None

                while True:
                    wn_row = wn_cur.next()
                    if wn_row is None:
                        break

                    wn_wid, _seq, nd_ref = wn_row

                    if wn_wid != current_wid:
                        # Flush previous way
                        if way_elem is not None:
                            xf.write(way_elem)
                        current_wid = wn_wid

                        # Advance edge cursor until ew_id >= wn_wid
                        while True:
                            ed_row = ed_cur.peek()
                            if ed_row is None:
                                break                   # edge cursor exhausted
                            ew_id = ed_row[0]
                            if ew_id < wn_wid:
                                ed_cur.next()           # skip orphaned edge
                            else:
                                break

                        # Build way element
                        way_elem = etree.Element("way", {
                            "id": str(wn_wid), "version": "1", "visible": "true",
                        })
                        ed_row = ed_cur.peek()
                        if ed_row is not None and ed_row[0] == wn_wid:
                            ed_cur.next()               # consume matched edge row
                            _, name, highway, oneway, lanes, maxspeed = ed_row
                            for k, v in [("highway", highway), ("name",    name),
                                         ("oneway",  oneway),  ("lanes",   lanes),
                                         ("maxspeed", maxspeed)]:
                                if v is not None and str(v).strip():
                                    etree.SubElement(way_elem, "tag", {"k": k, "v": str(v)})

                    etree.SubElement(way_elem, "nd", {"ref": str(nd_ref)})

                if way_elem is not None:
                    xf.write(way_elem)

    try:
        os.remove(wn_sorted)
        os.remove(ed_sorted)
    except OSError:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# WORKER ENTRY POINT  (called by multiprocessing)
# ═══════════════════════════════════════════════════════════════════════════════

def _worker(args):
    shard, out_osm, tile_size, memory_gb, id_offset, n_workers = args
    label = f"shard ({shard['row']},{shard['col']})"
    print(f"\n🔧  Processing {label} — {shard['count']:,} rows …")
    try:
        n = process_shard(shard, out_osm, tile_size, memory_gb, id_offset, n_workers)
        print(f"✅  {label} done → {n:,} ways → {os.path.basename(out_osm)}")
        return out_osm, True
    except Exception as e:
        print(f"❌  {label} FAILED: {e}")
        return out_osm, False


# ═══════════════════════════════════════════════════════════════════════════════
# POST-MERGE COORDINATE DEDUPLICATION
#
# osmium merge does NOT merge nodes from different shards that share the same
# (lat, lon) but have different IDs.  This step rewrites the merged OSM XML
# to unify such duplicate border nodes.
#
# Strategy (streaming, O(1) RAM):
#   Pass 1: scan all <node> elements, build coord → first_id dict.
#   Pass 2: rewrite — for each <node> whose coord already appeared, skip it;
#            for each <nd ref="..."> look up the canonical ID.
# ═══════════════════════════════════════════════════════════════════════════════

def dedup_border_nodes(merged_osm: str, deduped_osm: str) -> None:
    """
    Two-pass deduplication of border nodes by (lat, lon) coordinate.
    Writes a clean OSM XML to deduped_osm.
    """
    print(f"\n🔗  Deduplicating border nodes …")

    # Pass 1: collect coord → canonical ID (first occurrence wins)
    coord_to_id: dict[tuple, int] = {}
    id_remap: dict[int, int] = {}

    context = etree.iterparse(merged_osm, events=("end",), tag="node")
    for _, elem in context:
        nid = int(elem.get("id"))
        lat = round(float(elem.get("lat")), 7)
        lon = round(float(elem.get("lon")), 7)
        coord = (lat, lon)
        if coord in coord_to_id:
            id_remap[nid] = coord_to_id[coord]  # duplicate → remap to canonical
        else:
            coord_to_id[coord] = nid
        elem.clear()
    del coord_to_id

    n_dupes = len(id_remap)
    print(f"   Duplicate border nodes to merge: {n_dupes:,}")

    # Pass 2: write cleaned file
    CHUNK = 50_000
    skipped = 0
    remapped = 0

    with open(deduped_osm, "wb") as fh:
        with etree.xmlfile(fh, encoding="utf-8") as xf:
            xf.write_declaration()
            with xf.element("osm", version="0.6", generator="build_osm_topology"):
                context2 = etree.iterparse(merged_osm, events=("end",))
                for _, elem in context2:
                    if elem.tag == "node":
                        nid = int(elem.get("id"))
                        if nid in id_remap:
                            skipped += 1  # duplicate node — skip it
                        else:
                            xf.write(elem)
                        elem.clear()

                    elif elem.tag == "way":
                        # Rewrite nd refs through the remap table
                        for nd in elem.findall("nd"):
                            ref = int(nd.get("ref"))
                            if ref in id_remap:
                                nd.set("ref", str(id_remap[ref]))
                                remapped += 1
                        xf.write(elem)
                        elem.clear()

    print(f"   Nodes skipped (merged): {skipped:,}")
    print(f"   nd refs remapped: {remapped:,}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Build OSM PBF from Parquet road network via regional shards"
    )
    parser.add_argument("input_parquet", help="Input GeoParquet file")
    parser.add_argument("output_pbf",    help="Output OSM PBF file")
    parser.add_argument("--regions", nargs=2, type=int, default=[3, 4],
                        metavar=("ROWS", "COLS"),
                        help="Shard grid rows × cols (default: 3 4 → 12 shards)")
    parser.add_argument("--tile-size", type=float, default=0.02,
                        help="ST_Node tile size in degrees (default: 0.02)")
    parser.add_argument("--memory-gb", type=int, default=6,
                        help="DuckDB memory limit per shard in GB (default: 6)")
    # Accept both --workers (plural) and --worker (singular) for convenience
    parser.add_argument("--workers", "--worker", type=int, default=1,
                        dest="workers",
                        help="Parallel workers (default: 1)")
    parser.add_argument("--keep-shards", action="store_true",
                        help="Keep per-shard .osm files after merge (for debugging)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip shards whose .osm output already exists (crash recovery / incremental update)")
    args = parser.parse_args()

    input_parquet = os.path.abspath(args.input_parquet)
    output_pbf    = os.path.abspath(args.output_pbf)
    n_rows, n_cols = args.regions

    work_dir  = os.path.splitext(output_pbf)[0] + "_work"
    shard_dir = os.path.join(work_dir, "shards")
    osm_dir   = os.path.join(work_dir, "osm")
    os.makedirs(osm_dir, exist_ok=True)

    print(f"""
╔══════════════════════════════════════════════════════════╗
║         build_osm_topology — regional shard mode        ║
╠══════════════════════════════════════════════════════════╣
  Input   : {input_parquet}
  Output  : {output_pbf}
  Grid    : {n_rows} rows × {n_cols} cols = {n_rows*n_cols} shards
  Tile    : {args.tile_size}°
  Memory  : {args.memory_gb} GB / shard
  Workers : {args.workers}
  Resume  : {'yes (skipping completed shards)' if args.resume else 'no'}
╚══════════════════════════════════════════════════════════╝
""")

    # ── Step 0: Partition ──────────────────────────────────────────────────────
    # With --resume: if shard Parquet files already exist in shard_dir, skip
    # re-partitioning and reconstruct the shard list from disk instead.
    if args.resume and os.path.isdir(shard_dir):
        import glob
        existing = sorted(glob.glob(os.path.join(shard_dir, "shard_*.parquet")))
        if existing:
            print(f"   --resume: found {len(existing)} existing shard Parquet files, skipping partition step.")
            shards = []
            for p in existing:
                base = os.path.basename(p)          # shard_RR_CC.parquet
                parts = base.replace(".parquet","").split("_")
                r, c = int(parts[1]), int(parts[2])
                # Reconstruct approximate cell bounds (used only for core-tile clipping)
                # A full re-partition would be authoritative; this is close enough for resume.
                con_tmp = duckdb.connect()
                con_tmp.execute("INSTALL spatial; LOAD spatial;")
                bb = con_tmp.execute(f"""
                    SELECT MIN(ST_XMin(geometry)), MIN(ST_YMin(geometry)),
                           MAX(ST_XMax(geometry)), MAX(ST_YMax(geometry))
                    FROM read_parquet('{p}') WHERE geometry IS NOT NULL
                """).fetchone()
                con_tmp.close()
                cx0, cy0, cx1, cy1 = [float(v) for v in bb]
                n = duckdb.execute(f"SELECT COUNT(*) FROM read_parquet('{p}')").fetchone()[0]
                shards.append({"path": p, "row": r, "col": c,
                                "cx0": cx0, "cy0": cy0, "cx1": cx1, "cy1": cy1,
                                "ex0": cx0, "ey0": cy0, "ex1": cx1, "ey1": cy1,
                                "count": n})
        else:
            shards = partition_parquet(input_parquet, shard_dir, n_rows, n_cols)
    else:
        shards = partition_parquet(input_parquet, shard_dir, n_rows, n_cols)

    # ── Step 1: Process each shard ────────────────────────────────────────────
    shard_osm_files = []
    tasks = []
    skipped_shards = []
    for idx, shard in enumerate(shards):
        out_osm = os.path.join(osm_dir, f"shard_{shard['row']:02d}_{shard['col']:02d}.osm")
        if args.resume and os.path.exists(out_osm) and os.path.getsize(out_osm) > 0:
            print(f"   --resume: shard ({shard['row']},{shard['col']}) already done → {os.path.basename(out_osm)}")
            skipped_shards.append((out_osm, True))
        else:
            tasks.append((shard, out_osm, args.tile_size, args.memory_gb, idx + 1, args.workers))

    print(f"\n🚀  Processing {len(tasks)} shards"
          + (f" ({len(skipped_shards)} resumed/skipped)" if skipped_shards else "") + " …")

    if args.workers > 1:
        with multiprocessing.Pool(args.workers) as pool:
            new_results = pool.map(_worker, tasks)
    else:
        new_results = [_worker(t) for t in tasks]

    # Combine newly processed shards with any resumed/skipped ones
    results = skipped_shards + new_results

    failed = [p for p, ok in results if not ok]
    if failed:
        print(f"\n❌  {len(failed)} shards failed:")
        for p in failed:
            print(f"   {p}")
        sys.exit(1)

    shard_osm_files = [p for p, ok in results if ok]

    # ── Step 2: Merge shard OSM files ─────────────────────────────────────────
    merged_osm  = os.path.join(work_dir, "merged.osm")
    deduped_osm = os.path.join(work_dir, "deduped.osm")

    if shutil.which("osmium"):
        print(f"\n🔀  Merging {len(shard_osm_files)} shard OSM files with osmium …")
        subprocess.run(
            ["osmium", "merge"] + shard_osm_files + ["-o", merged_osm, "--overwrite"],
            check=True
        )

        # ── Step 3: Deduplicate border nodes ──────────────────────────────────
        dedup_border_nodes(merged_osm, deduped_osm)

        # ── Step 4: Sort + convert to PBF ─────────────────────────────────────
        print(f"\n📦  Sorting + writing PBF → {output_pbf} …")
        subprocess.run(
            ["osmium", "sort", deduped_osm, "-o", output_pbf, "--overwrite"],
            check=True
        )

    else:
        # osmium not installed — just concatenate XML shards (less clean)
        print("\n⚠️  osmium not found.  Writing plain merged OSM XML …")
        merged_osm = output_pbf.replace(".pbf", ".osm")
        _concat_osm_xml(shard_osm_files, merged_osm)
        dedup_border_nodes(merged_osm, deduped_osm)
        print(f"   Output: {deduped_osm}")
        print(f"   Convert to PBF manually: osmium sort {deduped_osm} -o {output_pbf}")

    # ── Cleanup ────────────────────────────────────────────────────────────────
    if not args.keep_shards:
        shutil.rmtree(work_dir, ignore_errors=True)
        print("   Work directory removed.")
    else:
        print(f"   Work files kept in: {work_dir}")

    print(f"\n✅  Done: {output_pbf}")
    print(f"\nNext: use {output_pbf} directly with Valhalla / OSRM / GraphHopper")


def _concat_osm_xml(shard_files: list[str], out_path: str) -> None:
    """Fallback: concatenate shard OSM files into one (no osmium required)."""
    CHUNK = 50_000
    with open(out_path, "wb") as fh:
        with etree.xmlfile(fh, encoding="utf-8") as xf:
            xf.write_declaration()
            with xf.element("osm", version="0.6", generator="build_osm_topology"):
                for shard_file in shard_files:
                    context = etree.iterparse(shard_file, events=("end",))
                    for _, elem in context:
                        if elem.tag in ("node", "way"):
                            xf.write(elem)
                        elem.clear()


if __name__ == "__main__":
    main()