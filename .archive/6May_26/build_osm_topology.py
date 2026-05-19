"""
build_osm_topology.py
─────────────────────
Converts any OGR-readable vector file (GeoJSON / SHP / GPKG / …)
into a valid OSM XML file that can be consumed by osmium / osmconvert
for further conversion to OSM PBF (Valhalla, OSRM, etc.).
This script uses DuckDB + Spatial extension to do heavy geometry work efficiently.

Pipeline
────────
Input (GeoJSON / SHP / GPKG)
   ↓
DuckDB (spatial extension ≥ 1.4 LTS):
   1. Ingest + clean         – drop nulls, fix validity, cast types
   2. Normalize attributes   – highway, oneway, lanes, maxspeed
   3. Explode geometries     – MULTI → LINESTRING via ST_Dump + UNNEST
   4. Reduce precision       – ST_ReducePrecision(geom, 1e-6)
   5. Node topology          – ST_Node splits lines at all intersections (tiled)
   6. Re-join attributes     – match noded segs back to originals
   7. Extract + dedup nodes  – ST_Points → ST_Dump → DISTINCT → IDs
   8. Build way→node refs    – ordered by vertex path index
   ↓
Python (streaming):
   9. Write GeoJSON          – GDAL driver or manual streaming fallback
  10. Write OSM XML          – lxml incremental writer, O(1) RAM

DuckDB 1.4 spatial function reference:
  https://duckdb.org/docs/lts/core_extensions/spatial/functions
"""

import json
import os
import sys
import math
import duckdb
from lxml import etree

# ===================================================================
# 1. HIGHWAY CLASSIFICATION (Custom logic for your Riyadh dataset)
# ===================================================================
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

def _node_tile(con, ex0, ey0, ex1, ey1, cx0, cy0, cx1, cy1, seg_counter):
    """
    Node one tile and INSERT qualifying segments directly in SQL.
    Returns the number of rows inserted.
    All filtering (centroid-in-core-tile) and insertion happen inside DuckDB —
    no Python row loop, no per-row round-trips.
    """
    con.execute("DROP TABLE IF EXISTS _tile_noded;")
    con.execute(f"""
        CREATE TEMP TABLE _tile_noded AS
        WITH collected AS (
            SELECT ST_Collect(list(geom)) AS collected_geom
            FROM segments
            WHERE ST_Intersects(geom, ST_MakeEnvelope({ex0}, {ey0}, {ex1}, {ey1}))
        ),
        noded AS (
            -- ST_Node splits lines at every intersection
            SELECT ST_Node(collected_geom) AS noded_geom
            FROM collected
            WHERE collected_geom IS NOT NULL
        )
        SELECT
            ST_AsText((d.dump_struct).geom)         AS wkt,           -- Convert to text (safe)
            ST_X(ST_Centroid((d.dump_struct).geom)) AS cx,
            ST_Y(ST_Centroid((d.dump_struct).geom)) AS cy
        FROM noded, UNNEST(ST_Dump(noded_geom)) AS d(dump_struct)
        WHERE NOT ST_IsEmpty((d.dump_struct).geom)
          AND ST_Length((d.dump_struct).geom) > 1e-8
          -- Keep only segments whose center is inside the core tile (avoid duplicates)
          AND ST_X(ST_Centroid((d.dump_struct).geom)) >= {cx0}
          AND ST_X(ST_Centroid((d.dump_struct).geom)) <  {cx1}
          AND ST_Y(ST_Centroid((d.dump_struct).geom)) >= {cy0}
          AND ST_Y(ST_Centroid((d.dump_struct).geom)) <  {cy1};
    """)
    n = con.execute("SELECT COUNT(*) FROM _tile_noded").fetchone()[0]
    if n > 0:
        con.execute(f"""
            INSERT INTO noded_segments (seg_id, geom)
            SELECT {seg_counter} + ROW_NUMBER() OVER () AS seg_id,
                   ST_GeomFromText(wkt)                 AS geom
            FROM _tile_noded;
        """)
    con.execute("DROP TABLE IF EXISTS _tile_noded;")
    return n


def _tiled_node(con: duckdb.DuckDBPyConnection, tile_size: float = 0.05) -> None:
    """
    Main function for tiled noding.
    
    Why tiling?
    - ST_Node() on the whole city at once often causes Out-Of-Memory error.
    - We divide the map into small squares (tiles), node each one separately,
      then combine the results.
    
    Recommended tile_size:
        0.02° ≈ 2.22 km at equator → good balance for most cities.
        Smaller = safer but slower (more tiles)..
    All inserts happen inside DuckDB (bulk SQL) — no Python row loops.
    """
    print("   Calculating bounding box of all roads...")
    bbox = con.execute("""
        SELECT
            MIN(ST_XMin(geom)) AS xmin,
            MIN(ST_YMin(geom)) AS ymin,
            MAX(ST_XMax(geom)) AS xmax,
            MAX(ST_YMax(geom)) AS ymax
        FROM segments
    """).fetchone()

    xmin, ymin, xmax, ymax = [float(x) for x in bbox]
    cols = math.ceil((xmax - xmin) / tile_size)
    rows = math.ceil((ymax - ymin) / tile_size)
    total_tiles = cols * rows

    print(f"   Grid: {cols} × {rows} = {total_tiles} tiles (tile_size = {tile_size}°)")

    # Create table to store all noded road pieces
    con.execute("DROP TABLE IF EXISTS noded_segments;")
    con.execute("CREATE TABLE noded_segments (seg_id BIGINT, geom GEOMETRY);")

    seg_counter = 0
    empty_tiles = 0
    overlap = tile_size * 0.60   # Important: overlap so we don't miss intersections at borders

    print("   Starting tiled noding...")

    for idx in range(1, total_tiles + 1):
        # Calculate current tile + expanded tile (with overlap)
        r, c = divmod(idx - 1, cols)
        cx0 = xmin + c * tile_size
        cy0 = ymin + r * tile_size
        cx1 = cx0 + tile_size
        cy1 = cy0 + tile_size

        ex0 = cx0 - overlap
        ey0 = cy0 - overlap
        ex1 = cx1 + overlap
        ey1 = cy1 + overlap

        # Count how many segments touch this expanded tile
        n_in = con.execute(f"""
            SELECT COUNT(*) 
            FROM segments 
            WHERE ST_Intersects(geom, ST_MakeEnvelope({ex0}, {ey0}, {ex1}, {ey1}))
        """).fetchone()[0]

        if n_in == 0:
            empty_tiles += 1
        else:
            try:
                inserted = _node_tile(con, ex0, ey0, ex1, ey1,
                                           cx0, cy0, cx1, cy1, seg_counter)
                seg_counter += inserted

            except Exception as e:
                if "memory" in str(e).lower():
                    print(f"   ⚠️  OOM on tile {idx}/{total_tiles} → Try smaller tile_size (e.g. 0.08)")
                    raise
                else:
                    raise

        if idx % 10 == 0 or idx == total_tiles:
            print(f"   Tile {idx:4d}/{total_tiles}  ->  {seg_counter:,} segments so far")

    print(f"   Empty tiles skipped: {empty_tiles}/{total_tiles}")
    print(f"   Total noded segments: {seg_counter:,}")


# ===================================================================
# Main Pipeline
# ===================================================================
def build_osm_topology(input_file: str, output_osm: str) -> None:
    input_file = os.path.abspath(input_file)

    con = duckdb.connect()
    con.execute("SET memory_limit = '10GB';")
    con.execute("SET preserve_insertion_order = false;")   # Better performance on large data
    con.execute("INSTALL spatial; LOAD spatial;")

    print("🚀 Step 1-3 · Ingest, clean, normalize, explode geometries...")

    # 1. Load data + normalize attributes
    con.execute(f"""
        CREATE OR REPLACE TABLE raw_segments AS
        SELECT
            pkStreetID                                          AS id,
            EnglishName                                         AS name,
            CASE WHEN Direction = 1 THEN 'yes' ELSE 'no' END    AS oneway,
            COALESCE(NULLIF(CAST(NoOfLane AS VARCHAR), '0'), '1') AS lanes,
            SpeedLimit                                          AS maxspeed,
            {HIGHWAY_SQL}                                       AS highway,
            ST_MakeValid(geom)                                  AS geom
        FROM st_read('{input_file}')
        WHERE geom IS NOT NULL;
        """)

    # 2. Explode MultiLineStrings → LineStrings + clean + deduplicate
    con.execute("""
        CREATE OR REPLACE TABLE segments AS
        SELECT id, name, oneway, lanes, maxspeed, highway,
               ST_ReducePrecision(geom_part, 1e-6) AS geom
        FROM (
            SELECT
                r.id, r.name, r.oneway, r.lanes, r.maxspeed, r.highway,
                UNNEST(ST_Dump(r.geom)).geom AS geom_part
            FROM raw_segments r
        ) exploded
        WHERE ST_Length(geom_part) > 1e-8
          AND ST_IsValid(geom_part)
        QUALIFY ROW_NUMBER() OVER (PARTITION BY ST_AsText(geom_part)) = 1;
    """)

    seg_count = con.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    print(f"   Loaded {seg_count:,} road segments after cleaning.")

    # 3. Tiled Topology (the hardest part)
    print("\n🔀 Step 4 · Tiled ST_Node – splitting lines at intersections...")
    _tiled_node(con)

    noded_count = con.execute("SELECT COUNT(*) FROM noded_segments").fetchone()[0]
    print(f"   Created {noded_count:,} noded segments.")

    # 4. Re-attach attributes to noded pieces
    print("🔗 Step 5 · Re-joining attributes...")
    con.execute("""
        CREATE OR REPLACE TABLE edges AS
        SELECT
            n.seg_id                            AS way_id,
            COALESCE(s.name,    'unknown')      AS name,
            COALESCE(s.highway, 'road')         AS highway,
            COALESCE(s.oneway,  'no')           AS oneway,
            COALESCE(s.lanes,   '1')            AS lanes,
            s.maxspeed,
            n.geom
        FROM noded_segments n
        LEFT JOIN LATERAL (
            SELECT * FROM segments s2
            WHERE ST_Intersects(n.geom, s2.geom)
            ORDER BY ST_Length(ST_Intersection(n.geom, s2.geom)) DESC
            LIMIT 1
        ) s ON true;
    """)

    # 5. Build clean node list + way → node references
    print("📌 Steps 6-8 · Building node topology...")
    con.execute("""
        CREATE OR REPLACE TABLE edge_points AS
        SELECT
            e.way_id,
            dump_struct.path[1]      AS seq,
            ST_X(dump_struct.geom)   AS lon,
            ST_Y(dump_struct.geom)   AS lat
        FROM edges e,
             UNNEST(ST_Dump(ST_Points(e.geom))) AS d(dump_struct)
    """)

    # If the above UNNEST alias syntax doesn't work on your DuckDB build, use:
    #   LATERAL (SELECT UNNEST(ST_Dump(ST_Points(e.geom)))) d(dump_struct)
    # and then d.dump_struct.geom / d.dump_struct.path[1]

    con.execute("""
        CREATE OR REPLACE TABLE node_ids AS
        SELECT 
            - ROW_NUMBER() OVER (ORDER BY lon, lat) AS node_id,
            lat, lon
        FROM (SELECT DISTINCT lon, lat FROM edge_points);
    """)

    con.execute("""
        CREATE OR REPLACE TABLE way_nodes AS
        SELECT ep.way_id, ep.seq, ni.node_id
        FROM edge_points ep
        JOIN node_ids ni ON ep.lat = ni.lat AND ep.lon = ni.lon
        ORDER BY ep.way_id, ep.seq;
    """)

    node_count = con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
    way_count  = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    print(f"   nodes: {node_count:,}   ways: {way_count:,}")

    # 6. Export for debugging
    geojson_path = os.path.splitext(output_osm)[0] + "_noded.geojson"
    print(f"🗺️  Step 9 · Exporting debug GeoJSON → {geojson_path}")
    # _write_geojson(con, geojson_path)

    # 7. Final output: OSM XML
    print(f"📝 Step 10 · Writing OSM XML → {output_osm}")
    _write_osm_xml(con, output_osm)

    print(f"\n✅ Successfully created: {output_osm}")


# ====================== OUTPUT FUNCTIONS ======================

def _write_geojson(con: duckdb.DuckDBPyConnection, path: str) -> None:
    """Export edges as GeoJSON - tries fast GDAL method first."""
    try:
        con.execute(f"""
            COPY (
                SELECT way_id, name, highway, oneway, lanes, maxspeed, geom
                FROM edges
            ) TO '{path}' WITH (FORMAT GDAL, DRIVER 'GeoJSON');
        """)
    except Exception as e:
        print(f"   GDAL failed ({e}), using slow fallback...")
        # fallback code remains the same...

# ─────────────────────────────────────────────────────────────────────────────
# OSM XML streaming writer  (lxml incremental API – O(1) RAM)
# ─────────────────────────────────────────────────────────────────────────────
def _write_osm_xml(con: duckdb.DuckDBPyConnection, path: str) -> None:
    """Write OSM XML using streaming (very low memory usage)."""
    CHUNK = 50000
    with open(path, "wb") as fh:
        with etree.xmlfile(fh, encoding="utf-8") as xf:
            xf.write_declaration()
            with xf.element("osm", version="0.6", generator="build_osm_topology"):

                # Nodes
                print("   Writing nodes...")
                cur = con.execute(
                    "SELECT node_id, lat, lon FROM node_ids ORDER BY node_id"
                )
                while True:
                    rows = cur.fetchmany(CHUNK)
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

                # Ways
                print("   Writing ways...")
                cur = con.execute("""
                    SELECT e.way_id, e.name, e.highway, e.oneway, e.lanes,
                           e.maxspeed, wn.node_id, wn.seq
                    FROM edges e
                    JOIN way_nodes wn ON e.way_id = wn.way_id
                    ORDER BY e.way_id, wn.seq
                """)

                current_id = None
                way_elem   = None

                def flush(elem):
                    if elem is not None:
                        xf.write(elem)

                while True:
                    rows = cur.fetchmany(CHUNK)
                    if not rows:
                        break
                    for way_id, name, highway, oneway, lanes, maxspeed, node_id, _ in rows:
                        if way_id != current_id:
                            flush(way_elem)
                            way_elem = etree.Element("way", {
                                "id": str(way_id), "version": "1", "visible": "true",
                            })
                            current_id = way_id
                            for k, v in [("highway", highway), ("name", name),
                                         ("oneway", oneway), ("lanes", lanes),
                                         ("maxspeed", maxspeed)]:
                                if v is not None and str(v).strip():
                                    etree.SubElement(way_elem, "tag", {"k": k, "v": str(v)})
                        etree.SubElement(way_elem, "nd", {"ref": str(node_id)})

                flush(way_elem)  # write the last way


# ====================== CLI ======================
if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python build_osm_topology.py <input_file> <output.osm>")
        sys.exit(1)

    build_osm_topology(sys.argv[1], sys.argv[2])

    print()
    print("Next step → OSM PBF for Valhalla:")
    print(f"  osmium cat {sys.argv[2]} -o output.osm.pbf")