import duckdb

def transform_with_duckdb(input_file, output_file):
    # 1. Connect and install the spatial extension
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")

    # 2. Define the SQL query with your logic
    # We use CASE statements to handle the "highway", "junction", and "surface" logic
    query = f"""
    COPY (
        SELECT 
            pkStreetID AS id,
            ArabicName AS "name:ar",
            EnglishNam AS "name",
            Strar AS "name:ar1",
            Stren AS "name:en1",
            Width AS width,
            CAST(COALESCE(NoOfLane, 1) AS VARCHAR) AS lanes,
            CAST(SpeedLimit AS VARCHAR) AS maxspeed,
            
            -- Oneway Logic
            CASE WHEN StreetCent = 1 THEN 'yes' ELSE 'no' END AS oneway,
            
            -- Junction Logic
            CASE WHEN StreetFOWI = 1 THEN 'roundabout' ELSE NULL END AS junction,
            
            -- Access Logic
            CASE  WHEN StreetStat = 3 THEN 'no' ELSE 'yes' END AS access,
            -- Surface logic: 
            CASE  
                WHEN StreetStat = 2 THEN 'gravel' 
                WHEN StreetStat = 3 THEN 'dirt'
                ELSE 'asphalt' 
            END AS surface,

            -- Highway Logic
            CASE 
                -- Check for Link Roads (FOWID 3 or 4)
                WHEN StreetFOWI IN (3, 4) THEN
                    CASE 
                        WHEN Subtype = 1 THEN 'trunk_link'
                        WHEN Subtype = 2 THEN 'primary_link'
                        WHEN Subtype = 3 THEN 'secondary_link'
                        ELSE 'tertiary_link'
                    END
                -- Standard Highway Mapping
                ELSE
                    CASE 
                        WHEN Subtype = 1 THEN 'trunk'
                        WHEN Subtype = 2 THEN 'primary'
                        WHEN Subtype = 3 THEN (CASE WHEN NoOfLane > 3 THEN 'primary' ELSE 'secondary' END)
                        WHEN Subtype = 4 THEN (CASE WHEN NoOfLane > 3 THEN 'secondary' ELSE 'tertiary' END)
                        WHEN Subtype = 5 THEN 'tertiary'
                        WHEN Subtype = 6 THEN 'residential'
                        WHEN Subtype = 7 THEN 'footway'
                        ELSE 'road'
                    END
            END AS highway,
            
            -- Keep the geometry column
            geom
            
        FROM st_read('{input_file}')
    ) TO '{output_file}' WITH (FORMAT GDAL, DRIVER 'GeoJSON');
    """

    # 3. Execute
    print(f"🚀 Starting DuckDB transformation for {input_file}...")
    con.execute(query)
    print(f"✅ Finished! Saved to {output_file}")

# Run it
transform_with_duckdb("data/streets_riyadh.geojson", "data/streets_riyadh_valhalla_duckdb.geojson")