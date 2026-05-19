"""
partition.py — split a GeoParquet file into overlapping geographic shards.

Each shard covers a core grid cell plus an overlap band so that roads
crossing cell boundaries are fully noded within at least one shard.
"""

import os
import duckdb


def _detect_geom_expr(con: duckdb.DuckDBPyConnection, parquet_path: str) -> str:
    """
    Return the SQL expression that produces a GEOMETRY value from the
    'geometry' column in the given Parquet file.

    GeoParquet stores geometry as GEOMETRY('EPSG:4326') — already parsed.
    Plain WKB Parquet stores it as BLOB — needs ST_GeomFromWKB().
    """
    row = con.execute(
        f"SELECT typeof(geometry) FROM read_parquet('{parquet_path}') LIMIT 1"
    ).fetchone()
    if row is None:
        return "geometry"
    type_str = row[0].upper()
    if "BLOB" in type_str:
        return "ST_GeomFromWKB(geometry)"
    return "geometry"   # already GEOMETRY


def partition_parquet(
    input_parquet: str,
    shard_dir: str,
    n_rows: int,
    n_cols: int,
    overlap_factor: float = 0.15,
) -> list[dict]:
    """
    Read input_parquet once, compute its bounding box, write R×C Parquet shards.

    Returns a list of shard descriptors:
      { path, row, col, cx0, cy0, cx1, cy1,   ← core cell bounds
        ex0, ey0, ex1, ey1,                    ← expanded bounds (with overlap)
        count }
    """
    os.makedirs(shard_dir, exist_ok=True)
    print(f"\n📦  Partitioning → {n_rows}×{n_cols} = {n_rows * n_cols} shards …")

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")

    geom_expr = _detect_geom_expr(con, input_parquet)
    print(f"   Geometry column: {geom_expr}")

    # ── Bounding box ────────────────────────────────────────────────────────────
    # Use envelope accessors — work for any geometry type (POINT, LINESTRING, …)
    bbox = con.execute(f"""
        SELECT
            MIN(ST_XMin({geom_expr})) AS xmin,
            MIN(ST_YMin({geom_expr})) AS ymin,
            MAX(ST_XMax({geom_expr})) AS xmax,
            MAX(ST_YMax({geom_expr})) AS ymax
        FROM read_parquet('{input_parquet}')
        WHERE geometry IS NOT NULL
    """).fetchone()
    xmin, ymin, xmax, ymax = (float(v) for v in bbox)

    cell_w = (xmax - xmin) / n_cols
    cell_h = (ymax - ymin) / n_rows
    ov_x   = cell_w * overlap_factor
    ov_y   = cell_h * overlap_factor

    print(f"   BBox: ({xmin:.4f},{ymin:.4f}) → ({xmax:.4f},{ymax:.4f})")
    print(f"   Cell: {cell_w:.4f}° × {cell_h:.4f}°  overlap: {ov_x:.4f}° × {ov_y:.4f}°")

    shards = []
    for r in range(n_rows):
        for c in range(n_cols):
            cx0 = xmin + c * cell_w;       cx1 = cx0 + cell_w
            cy0 = ymin + r * cell_h;       cy1 = cy0 + cell_h
            ex0 = max(xmin, cx0 - ov_x);  ex1 = min(xmax, cx1 + ov_x)
            ey0 = max(ymin, cy0 - ov_y);  ey1 = min(ymax, cy1 + ov_y)

            shard_path = os.path.join(shard_dir, f"shard_{r:02d}_{c:02d}.parquet")

            # ST_Intersects includes any road that crosses the expanded bbox —
            # correct for LINESTRING data where the road may straddle the edge.
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
                    "cx0": cx0, "cy0": cy0, "cx1": cx1, "cy1": cy1,
                    "ex0": ex0, "ey0": ey0, "ex1": ex1, "ey1": ey1,
                    "count": n,
                })
                print(f"   Shard ({r},{c}): {n:,} rows → {os.path.basename(shard_path)}")

    con.close()
    print(f"   Partitioning done.  {len(shards)} non-empty shards.")
    return shards


def reconstruct_shards_from_disk(shard_dir: str) -> list[dict]:
    """
    Rebuild the shard list from existing Parquet files on disk.
    Used by --resume mode to skip re-partitioning.
    """
    import glob as _glob
    existing = sorted(_glob.glob(os.path.join(shard_dir, "shard_*.parquet")))
    if not existing:
        return []

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")

    shards = []
    for p in existing:
        base   = os.path.basename(p)           # shard_RR_CC.parquet
        parts  = base.replace(".parquet", "").split("_")
        r, c   = int(parts[1]), int(parts[2])
        geom_e = _detect_geom_expr(con, p)
        bb     = con.execute(f"""
            SELECT MIN(ST_XMin({geom_e})), MIN(ST_YMin({geom_e})),
                   MAX(ST_XMax({geom_e})), MAX(ST_YMax({geom_e}))
            FROM read_parquet('{p}') WHERE geometry IS NOT NULL
        """).fetchone()
        cx0, cy0, cx1, cy1 = (float(v) for v in bb)
        n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{p}')").fetchone()[0]
        shards.append({
            "path": p, "row": r, "col": c,
            "cx0": cx0, "cy0": cy0, "cx1": cx1, "cy1": cy1,
            "ex0": cx0, "ey0": cy0, "ex1": cx1, "ey1": cy1,
            "count": n,
        })

    con.close()
    print(f"   --resume: found {len(shards)} existing shard Parquet files.")
    return shards
