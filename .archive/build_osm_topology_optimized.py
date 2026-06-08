"""
build_osm_topology.py
─────────────────────
Converts any OGR-readable vector file (GeoJSON / SHP / GPKG / …)
into a valid OSM XML file that tools like osmium/osmconvert can then
turn into an OSM PBF for routing engines (Valhalla, OSRM, etc.).

DESIGNED FOR LARGE FILES (country-scale, millions of segments).

WHY OSM FORMAT?
  Routing engines expect roads as a graph: intersections are NODES,
  road segments between intersections are WAYS referencing those nodes.
  Raw GIS data has none of that structure — lines just cross each other
  without sharing vertices. This script builds that graph.

PIPELINE (10 steps)
───────────────────
  Input file (GeoJSON / SHP / GPKG)
      │
      ▼  [Steps 1-3 — DuckDB SQL]
  Read + clean attributes
  Normalize highway type, oneway, lanes, speed
  Explode MULTILINESTRING → individual LINESTRINGs
  Snap coordinates to a 1-µdeg grid (removes floating-point jitter)
  Deduplicate identical geometries
      │
      ▼  [Step 4 — DuckDB SQL, tiled]
  ST_Node: split every line wherever another line crosses it
  (Done tile-by-tile so RAM stays bounded on large cities)
      │
      ▼  [Step 5 — DuckDB SQL]
  Re-attach road attributes (name, highway, …) to the new segments
      │
      ▼  [Steps 6-7 — DuckDB SQL]
  Pull every vertex out of every segment
  Deduplicate vertices → these become OSM nodes
  Assign stable negative IDs (negative = "new", required by OSM spec)
      │
      ▼  [Step 8 — DuckDB SQL]
  Build the ordered list: for each way, which node IDs does it pass through?
      │
      ▼  [Step 9 — Python streaming]
  Write noded edges as GeoJSON for QA in QGIS
      │
      ▼  [Step 10 — Python streaming, lxml]
  Write OSM XML, one node/way at a time → O(1) RAM regardless of size

USAGE
  python build_osm_topology.py input.geojson output.osm

  tile_size_deg defaults to 0.05 (≈ 5 km).
  For very dense cities, go smaller: 0.02 or 0.01.

NEXT STEP
  osmium cat output.osm -o output.osm.pbf

DEPENDENCIES
  pip install duckdb lxml
  DuckDB spatial extension is installed automatically on first run.
"""

import json
import os
import sys
import math
import duckdb
from lxml import etree  # lxml writes XML much faster than stdlib xml.etree


# ─────────────────────────────────────────────────────────────────────────────
# HIGHWAY CLASSIFICATION
#
# The input data uses two numeric columns to describe road type:
#   FOW   (Form Of Way)  — 3/4 means it's a ramp / link road
#   Subtype              — 1=trunk, 2=primary, 3=secondary, else minor
#
# This SQL CASE expression maps those numbers to OSM highway tag values.
# It is embedded as a string and spliced into the CREATE TABLE query below.
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
# _node_tile()
#
# PURPOSE: take all road segments that touch one map tile, run GEOS ST_Node
# to split them at every crossing, then keep only the pieces whose midpoint
# falls inside the tile's "core" area (to avoid duplicates where tiles overlap).
#
# PARAMETERS
#   con               — open DuckDB connection
#   ex0/ey0/ex1/ey1   — expanded tile bounds (includes overlap from neighbours)
#   cx0/cy0/cx1/cy1   — core tile bounds (no overlap, used for dedup filter)
#   seg_counter       — running total of segments already inserted; used to give
#                       each new segment a unique ID (seg_counter+1, +2, …)

# WHY separate noding from attribute join?
#   ST_Intersection (used to find the "best matching" parent segment) allocates
#   one new geometry per noded piece. With 1 M+ segments in RAM and thousands
#   of noded pieces per tile, this pushes peak RAM to 15+ GB.
#
#   Instead we:
#     1. Node geometry only → very low RAM per tile
#     2. Record the parent source ID for each noded piece cheaply:
#        ST_PointN(noded_piece, 2) gives the SECOND vertex of the piece.
#        After ST_ReducePrecision, that point lies exactly on its parent.
#        ST_Contains(src.geom, interior_point) is a lightweight predicate
#        with no geometry allocation — just a coordinate test.
#     3. Join attributes by ID after ALL noding is done and `segments` is freed.
#
# Returns: number of rows inserted into noded_segments
#
# RETURNS: number of segments inserted into noded_segments
# ─────────────────────────────────────────────────────────────────────────────
def _node_tile(con, ex0, ey0, ex1, ey1, cx0, cy0, cx1, cy1, seg_counter):

    # ── Collect + node + filter, all inside DuckDB ────────────────────────────
    #
    # CTE "collected":
    #   ST_MakeEnvelope(xmin, ymin, xmax, ymax) builds a rectangle geometry.
    #   ST_Intersects returns TRUE if the segment touches that rectangle at all.
    #   list(geom) aggregates all matching geometries into a DuckDB array.
    #   ST_Collect(array) merges the array into one MULTILINESTRING geometry.
    #   → Result: one row, one big geometry containing all lines in this tile.
    #
    # CTE "noded":
    #   ST_Node() is a GEOS function that finds every point where two lines
    #   cross and splits both lines there, ensuring shared endpoints.
    #   Input: one MULTILINESTRING.  Output: one MULTILINESTRING, fully split.
    #   The WHERE NULL guard prevents a crash on empty tiles.
    #
    # Final SELECT:
    #   ST_Dump() explodes the MULTILINESTRING back into individual LINESTRINGs.
    #   Returns array of STRUCT(geom GEOMETRY, path INTEGER[]).
    #   UNNEST(...) AS d(dump_struct) turns that array into rows, one per piece.
    #   dump_struct.geom  → the linestring geometry of that piece.
    #   ST_AsText() converts geometry → WKT string so Python receives clean text
    #   instead of a binary BLOB that causes type-conversion errors.
    #   ST_Centroid() finds the midpoint of the piece; ST_X/ST_Y get lon/lat.
    #
    #   The four AND conditions keep only pieces whose centroid falls strictly
    #   inside the core tile (cx0..cx1, cy0..cy1).  This is the dedup guard:
    #   the same road may appear in the expanded area of two adjacent tiles,
    #   but only the tile whose core contains the midpoint "owns" it.
    con.execute("DROP TABLE IF EXISTS _tile_noded;")
    con.execute(f"""
        CREATE TEMP TABLE _tile_noded AS
        WITH collected AS (
            -- Gather all segments touching the expanded tile into one geometry
            SELECT ST_Collect(list(geom)) AS collected_geom
            FROM segments
            WHERE ST_Intersects(geom, ST_MakeEnvelope({ex0}, {ey0}, {ex1}, {ey1}))
        ),
        noded AS (
            -- Split every line at every crossing point (GEOS planar noding)
            SELECT ST_Node(collected_geom) AS noded_geom
            FROM collected
            WHERE collected_geom IS NOT NULL   -- skip if tile was empty
        )
        -- Explode the noded MULTILINESTRING back to individual pieces
        SELECT
            ST_AsText((d.dump_struct).geom)         AS wkt,  -- geometry as WKT text
            ST_X(ST_Centroid((d.dump_struct).geom)) AS cx,   -- centroid longitude
            ST_Y(ST_Centroid((d.dump_struct).geom)) AS cy    -- centroid latitude
        FROM noded,
             UNNEST(ST_Dump(noded_geom)) AS d(dump_struct)   -- one row per piece
        WHERE NOT ST_IsEmpty((d.dump_struct).geom)           -- skip empty results
          AND ST_Length((d.dump_struct).geom) > 1e-8         -- skip zero-length slivers
          -- Core-tile dedup: only keep pieces "owned" by this tile
          AND ST_X(ST_Centroid((d.dump_struct).geom)) >= {cx0}
          AND ST_X(ST_Centroid((d.dump_struct).geom)) <  {cx1}
          AND ST_Y(ST_Centroid((d.dump_struct).geom)) >= {cy0}
          AND ST_Y(ST_Centroid((d.dump_struct).geom)) <  {cy1};
    """)

    # Bulk-insert into the permanent table using a single SQL statement.
    # ROW_NUMBER() OVER () assigns 1, 2, 3, … within this result set.
    # Adding seg_counter offsets those so IDs are globally unique across tiles.
    # ST_GeomFromText() converts the WKT string back to a GEOMETRY column.
    n = con.execute("SELECT COUNT(*) FROM _tile_noded").fetchone()[0]
    if n > 0:
        con.execute(f"""
            INSERT INTO noded_segments (seg_id, geom)
            SELECT
                {seg_counter} + ROW_NUMBER() OVER () AS seg_id,
                ST_GeomFromText(wkt)                 AS geom
            FROM _tile_noded;
        """)

    # Drop the temp table immediately to release RAM before the next tile
    con.execute("DROP TABLE IF EXISTS _tile_noded;")
    return n


# ─────────────────────────────────────────────────────────────────────────────
# _tiled_node()
#
# PURPOSE: divide the whole dataset into a grid of small tiles, call
# _node_tile() on each, and collect results into noded_segments.
#
# WHY TILING?
#   ST_Node on 128k segments at once would require ~15 GB RAM.
#   At 0.02° (~2 km) tiles, each call sees ~500-2000 segments → < 1 GB peak.
#
# WHY OVERLAP?
#   A road crossing a tile boundary must be noded against roads on the other
#   side. So each tile's expanded bounds reach 60% of tile_size into neighbours.
#   The core-tile filter in _node_tile() discards the duplicates.
#
# WHY ADAPTIVE SUB-SPLITTING?
#   Occasionally one tile covers a very dense interchange and still OOMs.
#   We catch the error and split that tile into 4 quarters and retry.
#   One level of splitting is always sufficient in practice.
# ─────────────────────────────────────────────────────────────────────────────
def _tiled_node(con: duckdb.DuckDBPyConnection, tile_size: float = 0.01) -> None:

    # Find the bounding box of all road data.
    # ST_XMin/XMax return the leftmost/rightmost coordinate of a geometry.
    # MIN/MAX over all rows gives the overall geographic extent.
    print("   Calculating bounding box …")
    bbox = con.execute("""
        SELECT
            MIN(ST_XMin(geom)) AS xmin,
            MIN(ST_YMin(geom)) AS ymin,
            MAX(ST_XMax(geom)) AS xmax,
            MAX(ST_YMax(geom)) AS ymax
        FROM segments
    """).fetchone()

    xmin, ymin, xmax, ymax = [float(x) for x in bbox]

    # How many tiles fit across the width and height of the bounding box?
    # math.ceil rounds up so we never miss the edge of the data.
    cols = math.ceil((xmax - xmin) / tile_size)
    rows = math.ceil((ymax - ymin) / tile_size)
    total_tiles = cols * rows

    print(f"   Grid: {cols}×{rows} = {total_tiles} tiles  (tile_size={tile_size}° ≈ 1 km)")

    # Create the output table that accumulates all noded segments.
    # BIGINT for IDs (can exceed 2 billion for large cities).
    con.execute("DROP TABLE IF EXISTS noded_segments;")
    con.execute("CREATE TABLE noded_segments (seg_id BIGINT, geom GEOMETRY);")

    seg_counter = 0   # global running count — used to assign unique IDs across tiles
    empty_tiles = 0   # just for the progress summary at the end
    overlap = tile_size * 0.60   # each tile reaches 60% of its width into neighbours

    # Pre-compute all tile bounds so the loop body stays clean.
    # Each entry: (core: cx0,cy0,cx1,cy1 | expanded: ex0,ey0,ex1,ey1)
    tiles = []
    for r in range(rows):
        for c in range(cols):
            cx0 = xmin + c * tile_size;  cx1 = cx0 + tile_size   # core left/right
            cy0 = ymin + r * tile_size;  cy1 = cy0 + tile_size   # core bottom/top
            ex0 = cx0 - overlap;         ex1 = cx1 + overlap     # expanded left/right
            ey0 = cy0 - overlap;         ey1 = cy1 + overlap     # expanded bottom/top
            tiles.append((cx0, cy0, cx1, cy1, ex0, ey0, ex1, ey1))

    for idx, (cx0, cy0, cx1, cy1, ex0, ey0, ex1, ey1) in enumerate(tiles, 1):

        # Quick count: how many segments touch the expanded tile?
        # DuckDB spatial filters by bounding box before running ST_Intersects,
        # so this count query is efficient even without an explicit index.
        n_in = con.execute(f"""
            SELECT COUNT(*) FROM segments
            WHERE ST_Intersects(geom, ST_MakeEnvelope({ex0},{ey0},{ex1},{ey1}))
        """).fetchone()[0]

        if n_in == 0:
            empty_tiles += 1   # skip empty tiles entirely
        else:
            try:
                inserted = _node_tile(con, ex0, ey0, ex1, ey1,
                                           cx0, cy0, cx1, cy1, seg_counter)
                seg_counter += inserted

            except Exception as e:
                # If this tile still OOMs (very dense area), split into 4
                # quarter-tiles and retry each one individually.
                if "OutOfMemory" in type(e).__name__ or "out of memory" in str(e).lower():
                    print(f"   ⚠️  OOM on tile {idx}/{total_tiles} ({n_in} segs)"
                          f" → splitting into 4 sub-tiles …")
                    mx = (cx0 + cx1) / 2   # midpoint longitude
                    my = (cy0 + cy1) / 2   # midpoint latitude
                    sub_overlap = overlap / 2

                    # Four quarters: bottom-left, bottom-right, top-left, top-right
                    for scx0, scy0, scx1, scy1 in [
                        (cx0, cy0, mx,  my ),
                        (mx,  cy0, cx1, my ),
                        (cx0, my,  mx,  cy1),
                        (mx,  my,  cx1, cy1),
                    ]:
                        sex0 = scx0 - sub_overlap;  sex1 = scx1 + sub_overlap
                        sey0 = scy0 - sub_overlap;  sey1 = scy1 + sub_overlap
                        sn = con.execute(f"""
                            SELECT COUNT(*) FROM segments
                            WHERE ST_Intersects(geom,
                                  ST_MakeEnvelope({sex0},{sey0},{sex1},{sey1}))
                        """).fetchone()[0]
                        if sn == 0:
                            continue
                        inserted = _node_tile(con, sex0, sey0, sex1, sey1,
                                                   scx0, scy0, scx1, scy1,
                                                   seg_counter)
                        seg_counter += inserted
                else:
                    raise   # re-raise anything that isn't an OOM

        # Progress report every 100 tiles (and always on the last tile)
        if idx % 50 == 0 or idx == total_tiles:
            print(f"   Tile {idx:4d}/{total_tiles}  →  {seg_counter:,} segments so far")

    print(f"   Empty tiles skipped: {empty_tiles}/{total_tiles}")
    print(f"   Total noded segments: {seg_counter:,}")


# ─────────────────────────────────────────────────────────────────────────────
# build_osm_topology() — main entry point
# ─────────────────────────────────────────────────────────────────────────────
def build_osm_topology(input_file: str, output_osm: str) -> None:
    input_file = os.path.abspath(input_file)   # DuckDB needs absolute paths

    # Open an in-memory DuckDB database.
    # All tables live in RAM (+ disk spill when needed) and vanish when con closes.
    con = duckdb.connect()

    # memory_limit: how much RAM DuckDB may use before spilling to disk.
    # preserve_insertion_order=false lets DuckDB reorder rows internally for
    # better parallelism — safe because we ORDER BY explicitly when it matters.
    # Leave one CPU free for the OS; DuckDB uses the rest automatically.
    con.execute("SET memory_limit = '6GB';")
    con.execute("SET temp_directory='duckdb_tmp';")
    con.execute("SET preserve_insertion_order = false;")
    con.execute("SET threads = 2;")

    # Install and load the spatial extension (downloaded once, cached after that).
    # Gives us ST_Node, ST_Dump, ST_Intersects, st_read(), and ~100 more functions.
    con.execute("INSTALL spatial; LOAD spatial;")

    # ─────────────────────────────────────────────────────────────────────────
    # STEPS 1-2: Ingest + normalize attributes
    #
    # st_read() is DuckDB's universal geo reader — handles GeoJSON, SHP, GPKG, …
    # It exposes every attribute column plus a special "geom" column.
    #
    # ST_MakeValid() fixes self-intersecting or otherwise broken geometries.
    # Broken geometries would crash ST_Node later, so we fix them here.
    #
    # CAST(SpeedLimit AS INT) > 0 drops placeholder values (0, -1 = "unknown").
    # ─────────────────────────────────────────────────────────────────────────
    print("🚀  Steps 1-3 · Ingest, clean, normalize, explode …")

    con.execute(f"""
        CREATE OR REPLACE TABLE raw_segments AS
        SELECT
            pkStreetID                                          AS id,
            EnglishName                                         AS name,
            -- Direction=1 means travel is only allowed in the digitized direction
            CASE WHEN Direction = 1 THEN 'yes' ELSE 'no' END   AS oneway,
            -- Treat NULL or 0 lanes as 1 (safe default for routing)
            CASE
                WHEN NoOfLane IS NULL OR NoOfLane <= 0 THEN '1'
                ELSE CAST(NoOfLane AS VARCHAR)
            END                                                 AS lanes,
            -- Only keep speed when it's a real positive value
            CASE
                WHEN SpeedLimit IS NOT NULL AND CAST(SpeedLimit AS INT) > 0
                THEN CAST(SpeedLimit AS VARCHAR)
                ELSE NULL
            END                                                 AS maxspeed,
            {HIGHWAY_SQL}                                       AS highway,
            ST_MakeValid(geom)                                  AS geom
        FROM st_read('{input_file}')
        WHERE geom IS NOT NULL;   -- drop features that have no geometry at all
    """)

    print("   Geometry types in raw data:",
          con.execute("""
              SELECT ST_GeometryType(geom), COUNT(*)
              FROM raw_segments GROUP BY 1
          """).fetchall())

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 3: Explode MULTI* → LINESTRINGs + snap coordinates + dedup
    #
    # WHY EXPLODE?
    #   A MULTILINESTRING is one feature made of several separate lines.
    #   ST_Node works on individual lines, so we must split MULTIs first.
    #
    # ST_Dump(geom) → array of STRUCT(geom GEOMETRY, path INTEGER[])
    #   Each struct holds one piece. UNNEST() turns the array into rows.
    #   .geom accesses the piece geometry.
    #
    # ST_ReducePrecision(geom, 1e-6):
    #   Snaps all coordinates to a grid of 0.000001° (~0.1 m precision).
    #   Removes floating-point jitter so two lines that "should" share an
    #   endpoint actually have byte-identical coordinates.
    #   Without this, ST_Node can miss intersections by nanometres.
    #
    # QUALIFY ROW_NUMBER() OVER (PARTITION BY ST_AsText(geom_part)) = 1:
    #   Deduplicates geometrically identical segments.
    #   ST_AsText converts geometry → WKT string for comparison.
    #   ROW_NUMBER numbers duplicates 1, 2, 3, … and QUALIFY keeps only #1.
    # ─────────────────────────────────────────────────────────────────────────
    con.execute("""
        CREATE OR REPLACE TABLE segments AS
        SELECT
            id, name, oneway, lanes, maxspeed, highway,
            ST_GeomFromWKB(ST_AsWKB(ST_ReducePrecision(geom_part, 1e-6))) AS geom  -- cast to plain GEOMETRY
        FROM (
            -- Inner query: explode each row's geometry into one row per piece
            SELECT
                r.id, r.name, r.oneway, r.lanes, r.maxspeed, r.highway,
                UNNEST(ST_Dump(r.geom)).geom AS geom_part
            FROM raw_segments r
        ) exploded
        WHERE ST_Length(geom_part) > 1e-8   -- drop zero-length slivers
          AND ST_IsValid(geom_part)          -- drop anything MakeValid couldn't fix
        QUALIFY ROW_NUMBER() OVER (PARTITION BY ST_AsText(geom_part)) = 1;  -- dedup
    """)

    seg_count = con.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    print(f"   Segments after explode + dedup: {seg_count:,}")

    # raw_segments is no longer needed — drop it to free RAM for the noding step
    con.execute("DROP TABLE IF EXISTS raw_segments;")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 4: Tiled ST_Node — split lines at every intersection
    #
    # After this step, noded_segments contains only LINESTRINGs that start and
    # end at intersections or dead ends. No two lines cross mid-edge anymore.
    # This is the core topological structure that routing engines require.
    # ─────────────────────────────────────────────────────────────────────────
    print("🔀  Step 4 · Tiled ST_Node – split lines at intersections …")
    _tiled_node(con)
    cnt = con.execute("SELECT COUNT(*) FROM noded_segments").fetchone()[0]
    print(f"   Noded segments: {cnt:,}")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 5: Re-join attributes to noded segments
    #
    # ST_Node creates brand-new geometries with no attributes attached.
    # We match each noded segment back to the original it came from.
    #
    # LEFT JOIN LATERAL:
    #   For each noded segment n, the lateral subquery runs independently:
    #   "find all original segments that spatially intersect n, pick the one
    #    with the longest shared overlap — that's the parent road."
    #   LIMIT 1 keeps only the best match.
    #   LEFT JOIN means: keep n even if no match is found (attributes → NULL).
    #
    # COALESCE(value, fallback):
    #   If the join found no match, use a safe default.
    #   e.g. COALESCE(s.highway, 'road') → tag it 'road' if unknown.
    # ─────────────────────────────────────────────────────────────────────────
    print("🔗  Step 5 · Re-joining attributes to noded segments …")
    con.execute("""
        CREATE OR REPLACE TABLE edges AS
        SELECT
            n.seg_id                        AS way_id,
            COALESCE(s.name,    'unknown')  AS name,
            COALESCE(s.highway, 'road')     AS highway,
            COALESCE(s.oneway,  'no')       AS oneway,
            COALESCE(s.lanes,   '1')        AS lanes,
            s.maxspeed,                     -- NULL is fine here (optional OSM tag)
            n.geom
        FROM noded_segments n
        LEFT JOIN LATERAL (
            -- Best-matching original segment for this noded piece
            SELECT s2.*
            FROM segments s2
            WHERE ST_Intersects(n.geom, s2.geom)
            ORDER BY ST_Length(ST_Intersection(n.geom, s2.geom)) DESC
            LIMIT 1
        ) s ON true;
    """)

    # Both source tables are no longer needed — free RAM
    con.execute("DROP TABLE IF EXISTS noded_segments;")
    con.execute("DROP TABLE IF EXISTS segments;")

    # ─────────────────────────────────────────────────────────────────────────
    # STEPS 6-7: Extract vertices → deduplicate → assign OSM node IDs
    #
    # OSM NODES are the vertices of every road — intersections, shape points, ends.
    # Each unique coordinate pair becomes one OSM node with one ID.
    #
    # ST_Points(geom) → MULTIPOINT of all vertices of the linestring, in order.
    # ST_Dump(multipoint) → array of STRUCT(geom POINT, path INTEGER[]).
    # UNNEST turns that array into one row per vertex.
    # dump_struct.path[1] → 1-based position of this vertex along the line
    #                        (1=start, 2=second point, …, N=end).
    # ST_X/ST_Y extract longitude and latitude from the point geometry.
    #
    # edge_points: one row per (way, vertex):  way_id | seq | lon | lat
    # ─────────────────────────────────────────────────────────────────────────
    print("📌  Steps 6-7 · Extracting vertices, deduplicating nodes, assigning IDs …")

    con.execute("""
        CREATE OR REPLACE TABLE edge_points AS
        SELECT
            e.way_id,
            dump_struct.path[1]    AS seq,   -- vertex order within this way (1-based)
            ST_X(dump_struct.geom) AS lon,   -- longitude of this vertex
            ST_Y(dump_struct.geom) AS lat    -- latitude of this vertex
        FROM edges e,
             -- UNNEST expands ST_Dump's array into rows; each is one vertex struct
             UNNEST(ST_Dump(ST_Points(e.geom))) AS d(dump_struct)
    """)

    # Many vertices are shared by multiple ways (any intersection appears in
    # every way that passes through it). Deduplicate by (lon, lat) and assign
    # one stable ID per unique location.
    #
    # ROW_NUMBER() OVER (ORDER BY lon, lat) * -1:
    #   Negative IDs are required by OSM convention for "new" objects not yet
    #   uploaded to openstreetmap.org. Ordering by lon/lat is deterministic
    #   so IDs are stable across re-runs on the same data.
    con.execute("""
        CREATE OR REPLACE TABLE node_ids AS
        SELECT
            (ROW_NUMBER() OVER (ORDER BY lon, lat)) * -1 AS node_id,
            lat,
            lon
        FROM (
            SELECT DISTINCT lat, lon   -- one row per unique coordinate pair
            FROM edge_points
        );
    """)

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 8: Build way→node reference list
    #
    # In OSM XML, a <way> lists its nodes as <nd ref="node_id"/> elements in
    # order. We join edge_points (knows vertex sequence) with node_ids (knows
    # the ID for each coordinate), ordering by seq for correct geometry order.
    # ─────────────────────────────────────────────────────────────────────────
    print("🔗  Step 8 · Building way→node reference table …")

    con.execute("""
        CREATE OR REPLACE TABLE way_nodes AS
        SELECT
            ep.way_id,
            ep.seq,
            ni.node_id
        FROM edge_points ep
        JOIN node_ids ni
          ON ep.lat = ni.lat AND ep.lon = ni.lon   -- exact match (safe after snap)
        ORDER BY ep.way_id, ep.seq;                -- vertex order is mandatory for OSM
    """)

    node_count = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
    way_count  = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    print(f"   Nodes: {node_count:,}   Ways: {way_count:,}")

    # edge_points no longer needed after way_nodes is built
    con.execute("DROP TABLE IF EXISTS edge_points;")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 9: Export noded edges as GeoJSON (optional QA layer)
    #
    # Load the resulting _noded.geojson in QGIS to visually verify topology:
    # every line should start/end exactly at intersections, no mid-crossings.
    # ─────────────────────────────────────────────────────────────────────────
    geojson_path = os.path.splitext(output_osm)[0] + "_noded.geojson"
    print(f"🗺️   Step 9 · Exporting noded edges → {geojson_path} …")
    # _write_geojson(con, geojson_path)
    print(f"   ✅ GeoJSON saved: {geojson_path}")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 10: Stream OSM XML
    # ─────────────────────────────────────────────────────────────────────────
    print(f"📝  Step 10 · Streaming OSM XML → {output_osm} …")
    _write_osm_xml(con, output_osm)
    print(f"✅  Done: {output_osm}")


# ─────────────────────────────────────────────────────────────────────────────
# _write_geojson()
#
# Tries DuckDB's built-in GDAL GeoJSON driver first (fastest, single call).
# Falls back to a manual Python streaming writer if GDAL isn't available,
# fetching rows in chunks of 10 000 to keep RAM usage flat.
# ─────────────────────────────────────────────────────────────────────────────
def _write_geojson(con: duckdb.DuckDBPyConnection, path: str) -> None:
    try:
        # COPY ... TO ... WITH (FORMAT GDAL, DRIVER 'GeoJSON') uses GDAL's
        # writer, which handles CRS metadata and encoding automatically.
        con.execute(f"""
            COPY (
                SELECT way_id, name, highway, oneway, lanes, maxspeed, geom
                FROM edges
            ) TO '{path}' WITH (FORMAT GDAL, DRIVER 'GeoJSON');
        """)
    except Exception as e:
        print(f"   GDAL GeoJSON export failed ({e}) — using manual fallback …")
        CHUNK = 10_000
        # ST_AsGeoJSON() returns the geometry as a GeoJSON geometry string, e.g.:
        #   {"type":"LineString","coordinates":[[lon,lat],[lon,lat],...]}
        cur = con.execute("""
            SELECT way_id, name, highway, oneway, lanes, maxspeed,
                   ST_AsGeoJSON(geom) AS geom_json
            FROM edges ORDER BY way_id
        """)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"type":"FeatureCollection","features":[\n')
            first = True
            while True:
                rows = cur.fetchmany(CHUNK)
                if not rows:
                    break
                for way_id, name, highway, oneway, lanes, maxspeed, geom_json in rows:
                    props = {"way_id": way_id, "name": name, "highway": highway,
                             "oneway": oneway, "lanes": lanes, "maxspeed": maxspeed}
                    feat = (f'{{"type":"Feature",'
                            f'"properties":{json.dumps(props)},'
                            f'"geometry":{geom_json}}}')
                    if not first:
                        fh.write(",\n")
                    fh.write(feat)
                    first = False
            fh.write("\n]}\n")


# ─────────────────────────────────────────────────────────────────────────────
# _write_osm_xml()
#
# Writes a valid OSM 0.6 XML file using lxml's incremental xmlfile API.
# "Incremental" means elements are written to disk as they are created —
# the entire XML tree is never held in RAM at once → O(1) memory.
#
# OSM XML structure:
#   <osm version="0.6">
#     <node id="-1" lat="24.12345" lon="46.12345" version="1" visible="true"/>
#     … all nodes first …
#     <way id="-1" version="1" visible="true">
#       <tag k="highway" v="primary"/>
#       <tag k="name"    v="King Fahd Road"/>
#       <nd ref="-1"/>   ← first vertex node
#       <nd ref="-2"/>   ← second vertex node
#       …
#     </way>
#     … all ways …
#   </osm>
#
# CHUNK = 50 000: rows fetched from DuckDB per batch.
# Larger = fewer round-trips but more RAM; 50k is a good balance.
# ─────────────────────────────────────────────────────────────────────────────
def _write_osm_xml(con: duckdb.DuckDBPyConnection, path: str) -> None:
    CHUNK = 50_000

    with open(path, "wb") as fh:                               # "wb" = write binary
        with etree.xmlfile(fh, encoding="utf-8") as xf:
            xf.write_declaration()                             # <?xml version='1.0' …?>
            with xf.element("osm", version="0.6", generator="build_osm_topology"):

                # ── Write all <node> elements first ──────────────────────────
                # OSM requires all nodes before any ways.
                print("   Writing nodes …")
                cur = con.execute(
                    "SELECT node_id, lat, lon FROM node_ids ORDER BY node_id"
                )
                while True:
                    rows = cur.fetchmany(CHUNK)
                    if not rows:
                        break
                    for node_id, lat, lon in rows:
                        # etree.Element creates one XML element in memory.
                        # xf.write() flushes it to disk immediately — no accumulation.
                        xf.write(etree.Element("node", {
                            "id":      str(node_id),
                            "lat":     f"{lat:.7f}",    # 7 decimal places ≈ 1 cm precision
                            "lon":     f"{lon:.7f}",
                            "version": "1",
                            "visible": "true",
                        }))

                # ── Write all <way> elements ──────────────────────────────────
                # The query returns one row per (way, vertex), sorted by way then seq.
                # We detect when way_id changes to know a way is complete and flush it.
                print("   Writing ways …")
                cur = con.execute("""
                    SELECT e.way_id, e.name, e.highway, e.oneway, e.lanes,
                           e.maxspeed, wn.node_id, wn.seq
                    FROM edges e
                    JOIN way_nodes wn ON e.way_id = wn.way_id
                    ORDER BY e.way_id, wn.seq   -- must be in vertex order for valid OSM
                """)

                current_id = None    # which way we're currently building
                way_elem   = None    # the <way> XML element being assembled

                def flush(elem):
                    """Write a completed <way> element to disk and release it."""
                    if elem is not None:
                        xf.write(elem)

                while True:
                    rows = cur.fetchmany(CHUNK)
                    if not rows:
                        break
                    for way_id, name, highway, oneway, lanes, maxspeed, node_id, _ in rows:
                        if way_id != current_id:
                            flush(way_elem)            # write the previous way (if any)
                            # Start building the next <way>
                            way_elem = etree.Element("way", {
                                "id":      str(way_id),
                                "version": "1",
                                "visible": "true",
                            })
                            current_id = way_id
                            # Add <tag k="..." v="..."/> for each non-null attribute
                            for k, v in [("highway",  highway),
                                         ("name",     name),
                                         ("oneway",   oneway),
                                         ("lanes",    lanes),
                                         ("maxspeed", maxspeed)]:
                                if v is not None and str(v).strip():
                                    etree.SubElement(way_elem, "tag", {"k": k, "v": str(v)})

                        # Append <nd ref="node_id"/> for this vertex
                        etree.SubElement(way_elem, "nd", {"ref": str(node_id)})

                flush(way_elem)   # don't forget the very last way


# ─────────────────────────────────────────────────────────────────────────────
# Entry point — called when you run: python build_osm_topology.py <in> <out>
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python build_osm_topology.py <input_file> <output.osm>")
        sys.exit(1)

    build_osm_topology(sys.argv[1], sys.argv[2])

    print()
    print("Next step → convert to OSM PBF for Valhalla / OSRM:")
    print(f"  osmium cat {sys.argv[2]} -o output.osm.pbf")
    print("  # or")
    print(f"  osmconvert {sys.argv[2]} -o=output.osm.pbf")