"""
shard_processor.py — process one geographic shard end-to-end.

Pipeline for each shard:
  1. Ingest shard Parquet → validate geometry → explode MULTI* → snap → dedup
  2. Split into seg_geoms (geometry only) + seg_attrs (attributes only)
  3. Tiled ST_Node  → tile Parquets in /tmp/tiles_RR_CC/
  4. Attribute join → edges Parquet
  5. Vertex extraction → edge_points Parquet
  6. Node deduplication → node_ids (ni) Parquet
  7. Way→node reference list → way_nodes (wn) Parquet
  8. Write OSM XML

ALL large intermediate data lives in /tmp as Parquet files — never in DuckDB
tables — so the DuckDB buffer pool stays flat throughout.
"""

import os
import shutil

import duckdb

from noding import tiled_node
from xml_writer import write_osm_xml

# Maps input numeric columns to OSM highway tag values.
# FOW (Form Of Way): 3/4 = ramp/link.  Subtype: 1=trunk, 2=primary, 3=secondary.
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

def process_shard(
    shard:      dict,
    out_osm:    str,
    tile_size:  float,
    memory_gb:  int,
    id_offset:  int,
    n_workers:  int = 1,
) -> int:
    """
    Process one geographic shard and write its OSM XML to out_osm.

    id_offset: shard index (1-based) used to namespace node/way IDs so they
               don't collide across shards before the osmium merge step.
               IDs are: (id_offset * 1_000_000_000 + local_id) * -1

    Returns the number of ways written.
    """
    shard_label = f"{shard['row']:02d}_{shard['col']:02d}"

    # ── DuckDB connection ──────────────────────────────────────────────────────
    # Use a per-shard .duckdb file in the same directory as out_osm.
    # NEVER use str.replace(".osm", ".duckdb") on the full path — the parent
    # directory name may also contain ".osm" and replace() would corrupt it.
    db_path = os.path.join(
        os.path.dirname(out_osm),
        os.path.splitext(os.path.basename(out_osm))[0] + ".duckdb",
    )

    con = duckdb.connect(db_path)
    con.execute(f"SET memory_limit = '{memory_gb}GB';")
    con.execute("SET preserve_insertion_order = false;")
    con.execute(f"SET threads = {max(1, os.cpu_count() // max(1, n_workers))};")
    con.execute("SET temp_directory = '/tmp';")
    con.execute("INSTALL spatial; LOAD spatial;")

    # ── Step 1: Ingest ─────────────────────────────────────────────────────────
    # Detect geometry column type: GeoParquet = GEOMETRY (already parsed),
    # plain WKB Parquet = BLOB (needs ST_GeomFromWKB).
    shard_path    = shard["path"]
    geom_type_row = con.execute(
        f"SELECT typeof(geometry) FROM read_parquet('{shard_path}') LIMIT 1"
    ).fetchone()
    geom_is_parsed = geom_type_row is not None and "BLOB" not in geom_type_row[0].upper()
    geom_col = "geometry" if geom_is_parsed else "ST_GeomFromWKB(geometry)"

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
        FROM read_parquet('{shard_path}')
        WHERE geometry IS NOT NULL;
    """)

    # ── Step 2: Validate + repair geometry ────────────────────────────────────
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

    # ── Step 3: Explode MULTI*, snap to 1 µdeg grid, dedup ───────────────────
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

    # ── Step 4: Split geometry / attributes ───────────────────────────────────
    # seg_geoms holds only (id, geom) — queried during noding.
    # seg_attrs holds only scalar attributes — joined after noding.
    # Keeping them separate halves the buffer pool pressure during the noding loop.
    con.execute("CREATE OR REPLACE TABLE seg_geoms AS SELECT id, geom FROM segments;")
    con.execute("""
        CREATE OR REPLACE TABLE seg_attrs AS
        SELECT id, name, highway, oneway, lanes, maxspeed FROM segments;
    """)
    con.execute("DROP TABLE IF EXISTS segments;")
    con.execute("CHECKPOINT;")   # flush seg_geoms + seg_attrs to disk

    # ── Step 5: Tiled ST_Node ─────────────────────────────────────────────────
    tile_parquet_dir = os.path.join("/tmp", f"tiles_{shard_label}")
    # Remove any stale tile Parquets from a previous (crashed) run
    shutil.rmtree(tile_parquet_dir, ignore_errors=True)

    tiled_node(
        con, tile_size,
        core_x0=shard["cx0"], core_y0=shard["cy0"],
        core_x1=shard["cx1"], core_y1=shard["cy1"],
        tile_parquet_dir=tile_parquet_dir,
    )

    # seg_geoms no longer needed — drop to free buffer pool pages
    con.execute("DROP TABLE IF EXISTS seg_geoms;")

    # ── Step 6: Attribute join → edges Parquet ────────────────────────────────
    # COPY (join) TO Parquet — DuckDB pipelines the join without buffering all
    # rows in memory. geom_wkb is already a WKB BLOB from the tile Parquets.
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

    con.execute("DROP VIEW  IF EXISTS noded_segments;")
    con.execute("DROP TABLE IF EXISTS seg_attrs;")
    shutil.rmtree(tile_parquet_dir, ignore_errors=True)

    way_count = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{edges_parquet}')"
    ).fetchone()[0]
    print(f"      Ways (edges): {way_count:,}")

    # ── Step 7: Extract vertices → edge_points Parquet ────────────────────────
    # Parse geom_wkb (BLOB) → GEOMETRY → ST_Points → ST_Dump → individual vertices.
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

    # ── Step 8: Deduplicate vertices → node_ids (ni) Parquet ─────────────────
    # ROW_NUMBER() OVER () — no ORDER BY, no sort — IDs are arbitrary but unique.
    # The XML writer sorts ni_parquet by node_id via external sort.
    ni_parquet = os.path.join("/tmp", f"ni_{shard_label}.parquet")
    con.execute(f"""
        COPY (
            SELECT
                ({id_base} + ROW_NUMBER() OVER ()) * -1 AS node_id,
                lat, lon
            FROM (SELECT DISTINCT lat, lon FROM read_parquet('{ep_parquet}'))
        ) TO '{ni_parquet}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    node_count = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{ni_parquet}')"
    ).fetchone()[0]
    print(f"      Nodes: {node_count:,}")

    # ── Step 9: Way→node reference list → wn Parquet ─────────────────────────
    # Hash join: ni_parquet (smaller) is the build side, ep_parquet is the probe.
    # No ORDER BY here — sorting is deferred to the XML writer's pre-sort step
    # where DuckDB can spill to /tmp without competing with the join.
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

    # ── Step 10: Write OSM XML ────────────────────────────────────────────────
    write_osm_xml(con, out_osm, edges_parquet, wn_parquet, ni_parquet)

    # ── Cleanup ───────────────────────────────────────────────────────────────
    for p in (edges_parquet, wn_parquet, ni_parquet):
        try:
            os.remove(p)
        except OSError:
            pass

    con.close()
    try:
        os.remove(db_path)
    except OSError:
        pass

    return way_count
