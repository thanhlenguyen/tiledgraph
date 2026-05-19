"""
noding.py — tiled ST_Node that streams each tile's output directly to Parquet.

MEMORY STRATEGY
  Classical approach:  INSERT every tile into noded_segments (DuckDB table)
                       → WAL pages accumulate → OOM at ~12 000 tiles.

  This approach:       COPY each tile to its own Parquet file in tile_dir.
                       DuckDB compresses and flushes immediately; the temp
                       table _tile_noded is dropped right after → buffer pool
                       stays flat regardless of tile count.

  After the loop:      all tile Parquets are exposed as a lazy VIEW via
                       read_parquet(glob) for the attribute-join step.
"""

import math
import os

import duckdb

# Print tile progress every N tiles processed
TILE_LOG_EVERY = 500


# ─────────────────────────────────────────────────────────────────────────────
# Single-tile noding
# ─────────────────────────────────────────────────────────────────────────────

def _node_tile(
    con: duckdb.DuckDBPyConnection,
    ex0: float, ey0: float, ex1: float, ey1: float,   # expanded bbox for ST_Node
    cx0: float, cy0: float, cx1: float, cy1: float,   # core bbox (dedup guard)
    seg_counter: int,
    tile_parquet_path: str,
) -> int:
    """
    Node one tile and write results as WKB BLOB to tile_parquet_path.

    Returns the number of segments written (0 if tile is empty).

    COLUMN SCHEMA written to Parquet:
      seg_id   BIGINT   — globally unique ID (seg_counter offset)
      geom_wkb BLOB     — WKB bytes (NOT GEOMETRY) so read_parquet() always
                          returns a consistent BLOB type across all tile files
      src_id   BIGINT   — FK → seg_attrs.id for attribute join
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
                ST_GeomFromWKB(ST_AsWKB(d.dump_struct.geom)) AS geom,
                ST_X(ST_Centroid(d.dump_struct.geom))         AS cx,
                ST_Y(ST_Centroid(d.dump_struct.geom))         AS cy,
                ST_PointN(d.dump_struct.geom, 2)              AS interior_pt
            FROM noded,
                 UNNEST(ST_Dump(noded_geom)) AS d(dump_struct)
            WHERE NOT ST_IsEmpty(d.dump_struct.geom)
              AND ST_NPoints(d.dump_struct.geom) >= 2
              AND ST_Length(d.dump_struct.geom) > 1e-8
        ),
        owned AS (
            -- Keep only segments whose centroid is inside the core bbox.
            -- This prevents the same segment appearing in two adjacent tiles.
            SELECT geom, interior_pt FROM dumped
            WHERE cx >= {cx0} AND cx < {cx1}
              AND cy >= {cy0} AND cy < {cy1}
        )
        SELECT
            o.geom,
            s.id AS src_id
        FROM owned o
        LEFT JOIN LATERAL (
            SELECT src.id FROM src
            WHERE ST_Contains(src.geom, o.interior_pt)
            LIMIT 1
        ) s ON true;
    """)

    n = con.execute("SELECT COUNT(*) FROM _tile_noded").fetchone()[0]

    if n > 0:
        # Write as WKB BLOB (not GEOMETRY) — type is consistent across ALL tile
        # Parquets including the empty sentinel, so read_parquet(glob) infers
        # a single stable BLOB type for the geom_wkb column.
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


# ─────────────────────────────────────────────────────────────────────────────
# OOM subdivision
# ─────────────────────────────────────────────────────────────────────────────

def _node_tile_subdivide(
    con: duckdb.DuckDBPyConnection,
    cx0: float, cy0: float, cx1: float, cy1: float,
    seg_counter: int,
    tile_parquet_dir: str,
    depth: int = 1,
) -> int:
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
            ov_x = sub_w * 0.70;      ov_y = sub_h * 0.70
            sex0 = scx0 - ov_x;       sex1 = scx1 + ov_x
            sey0 = scy0 - ov_y;       sey1 = scy1 + ov_y

            tile_path = os.path.join(
                tile_parquet_dir,
                f"tile_d{depth}_{int(scx0 * 1e4):08d}_{int(scy0 * 1e4):08d}.parquet",
            )
            try:
                seg_counter += _node_tile(
                    con, sex0, sey0, sex1, sey1,
                    scx0, scy0, scx1, scy1,
                    seg_counter, tile_path,
                )
            except Exception as e:
                if "OutOfMemory" in type(e).__name__ or "out of memory" in str(e).lower():
                    seg_counter = _node_tile_subdivide(
                        con, scx0, scy0, scx1, scy1,
                        seg_counter, tile_parquet_dir, depth + 1,
                    )
                else:
                    raise

    return seg_counter


# ─────────────────────────────────────────────────────────────────────────────
# Tiled noding loop
# ─────────────────────────────────────────────────────────────────────────────

def tiled_node(
    con: duckdb.DuckDBPyConnection,
    tile_size: float,
    core_x0: float, core_y0: float,
    core_x1: float, core_y1: float,
    tile_parquet_dir: str,
) -> None:
    """
    Node all geometry in seg_geoms tile by tile, writing each tile's result
    to its own Parquet file in tile_parquet_dir.

    After this function returns, a VIEW named noded_segments is created over
    read_parquet(tile_parquet_dir/*.parquet) exposing columns:
      seg_id BIGINT, geom_wkb BLOB, src_id BIGINT
    """
    os.makedirs(tile_parquet_dir, exist_ok=True)

    bbox = con.execute("""
        SELECT MIN(ST_XMin(geom)), MIN(ST_YMin(geom)),
               MAX(ST_XMax(geom)), MAX(ST_YMax(geom))
        FROM seg_geoms
    """).fetchone()
    xmin, ymin, xmax, ymax = (float(x) for x in bbox)

    cols    = math.ceil((xmax - xmin) / tile_size)
    rows    = math.ceil((ymax - ymin) / tile_size)
    overlap = tile_size * 0.50

    # One query to find all occupied (col, row) cells — avoids N×M COUNT queries
    occupied = set(con.execute(f"""
        SELECT DISTINCT
            LEAST(FLOOR((ST_X(ST_Centroid(geom)) - {xmin}) / {tile_size})::INTEGER, {cols - 1}),
            LEAST(FLOOR((ST_Y(ST_Centroid(geom)) - {ymin}) / {tile_size})::INTEGER, {rows - 1})
        FROM seg_geoms
    """).fetchall())

    seg_counter = 0
    processed   = 0
    n_occ       = len(occupied)
    print(f"      Grid: {cols}×{rows}, occupied: {n_occ:,} tiles")

    for r in range(rows):
        for c in range(cols):
            if (c, r) not in occupied:
                continue

            tx0 = xmin + c * tile_size;    tx1 = tx0 + tile_size
            ty0 = ymin + r * tile_size;    ty1 = ty0 + tile_size

            # Intersect tile with shard's core cell — segments outside the core
            # are produced by the adjacent shard, preventing duplicates
            tcx0 = max(tx0, core_x0);  tcx1 = min(tx1, core_x1)
            tcy0 = max(ty0, core_y0);  tcy1 = min(ty1, core_y1)

            if tcx0 >= tcx1 or tcy0 >= tcy1:
                continue   # tile entirely outside shard core

            ex0 = tx0 - overlap;  ex1 = tx1 + overlap
            ey0 = ty0 - overlap;  ey1 = ty1 + overlap

            tile_path = os.path.join(tile_parquet_dir, f"tile_{r:05d}_{c:05d}.parquet")

            try:
                n = _node_tile(
                    con, ex0, ey0, ex1, ey1,
                    tcx0, tcy0, tcx1, tcy1,
                    seg_counter, tile_path,
                )
                seg_counter += n

            except Exception as e:
                if "OutOfMemory" in type(e).__name__ or "out of memory" in str(e).lower():
                    print(f"   ⚠️  OOM tile ({c},{r}) → subdivide …")
                    seg_counter = _node_tile_subdivide(
                        con, tcx0, tcy0, tcx1, tcy1,
                        seg_counter, tile_parquet_dir,
                    )
                else:
                    raise

            processed += 1
            if processed % TILE_LOG_EVERY == 0 or processed == n_occ:
                pct = processed / n_occ * 100
                print(f"      {processed:5d}/{n_occ:,} tiles ({pct:.0f}%)  → {seg_counter:,} segs")

    # Count actual Parquet files written (empty tiles produce no file)
    written = [
        f for f in os.listdir(tile_parquet_dir)
        if f.endswith(".parquet") and not f.startswith("empty_")
    ]
    print(f"      Tile Parquets written: {len(written):,}  total segs: {seg_counter:,}")

    # ── Create VIEW over tile Parquets ─────────────────────────────────────────
    glob_pattern = os.path.join(tile_parquet_dir, "*.parquet")

    # Always DROP both types before CREATE VIEW — DuckDB forbids replacing
    # a TABLE with CREATE OR REPLACE VIEW (and vice versa).
    con.execute("DROP VIEW  IF EXISTS noded_segments;")
    con.execute("DROP TABLE IF EXISTS noded_segments;")

    if not written:
        # No segments — write a typed empty sentinel so read_parquet(glob)
        # returns the correct schema (seg_id BIGINT, geom_wkb BLOB, src_id BIGINT)
        # without a GEOMETRY type mismatch.
        sentinel = os.path.join(tile_parquet_dir, "empty_sentinel.parquet")
        con.execute(f"""
            COPY (
                SELECT
                    CAST(NULL AS BIGINT) AS seg_id,
                    CAST(NULL AS BLOB)   AS geom_wkb,
                    CAST(NULL AS BIGINT) AS src_id
                WHERE false
            ) TO '{sentinel}' (FORMAT PARQUET)
        """)

    con.execute(f"""
        CREATE VIEW noded_segments AS
        SELECT seg_id, geom_wkb, src_id
        FROM read_parquet('{glob_pattern}', hive_partitioning=false)
        WHERE seg_id IS NOT NULL;
    """)
