<<<<<<< HEAD
=======
# =============================================================================
# This tool will read geodata table "building" and full join with table "floor" geodatabase
# then export to geojson file (after that convert to pmtiles or mbtiles files)
# =============================================================================
>>>>>>> agentic
import polars as pl
from sqlalchemy import create_engine
import urllib
import pyodbc
import duckdb

server = '10.50.2.14'
username = 'gis_dev'
password = """#13e4fWMB7|3"""
db_name = "na_dev_sql_geo_db"
db_creds = ("DRIVER={ODBC Driver 18 for SQL Server};" 
    + f"SERVER={server};" 
    + "TrustServerCertificate=yes;" 
    + f"DATABASE={db_name};" 
    + f"UID={username};" 
    + f"PWD={password}")
conn_string = urllib.parse.quote_plus(db_creds)
engine = create_engine("mssql+pyodbc:///?odbc_connect={}".format(conn_string))
# pyodbc_connect = pyodbc.connect(db_creds, autocommit=True)
# db_cursor = pyodbc_connect.cursor()

table_schema = "sde"
building = "dim_building"
floor = "dim_floor"

<<<<<<< HEAD
=======
print("reading data ......")
>>>>>>> agentic
df = pl.read_database(
    query = f"""
        SELECT		
            b.OBJECTID,
            b.fkShortAddress,
            b.BuildingUseType,
            b.Building_Type,
            b.BuildingHeight,
            b.NoofFloors,
            b.FloorsBelowGround,
            b.FloorsAboveGround,
            b.Unit_Type,
            --b.Shape,
            b.Geometry_WKB,
            f.BuildingID,
            f.FloorName,
            f.FloorID,
            f.FloorUsage,
            f.NoOfUnits,
            CASE 
                WHEN f.FloorID ='G' THEN 0 
                WHEN TRY_CAST(f.FloorID AS INT) IS NOT NULL THEN CAST(f.FloorID AS INT) 
                ELSE -1 
            END AS FloorNumber
        FROM
            {table_schema}.{building} as b 
            Full Join {table_schema}.{floor} as f 
            ON b.fkShortAddress = f.fkShortAddress
<<<<<<< HEAD
=======

        WHERE 
            b.BuildingUseType <> 'Residential';
>>>>>>> agentic
    """
    ,connection=engine, 
)



con = duckdb.connect()
con.execute("INSTALL spatial;")
con.execute("LOAD spatial;")


con.register("df_data", df)

convert_query = f"""
    COPY (
        SELECT		
            OBJECTID,
            fkShortAddress as ShortAddress,
            CASE 
                WHEN Building_Type == 1 THEN CONCAT(fkShortAddress,'-',FloorID)
                ELSE CONCAT(fkShortAddress,'-',BuildingID,FloorID)
            END AS VerticalAddress,    
            BuildingUseType,
            Building_Type,
            CAST(
                CASE 
<<<<<<< HEAD
                    WHEN CAST(BuildingUseType AS INT) = 2000 THEN FloorsAboveGround * 3.5 
=======
                    WHEN CAST(BuildingUseType AS INT) = 2000 THEN FloorsAboveGround * 3
>>>>>>> agentic
                    ELSE FloorsAboveGround * 5
                END 
                AS FLOAT)
            AS BuildingHeight,
            BuildingID,
            NoofFloors,
            FloorsBelowGround,
            FloorsAboveGround,
            FloorName,
            FloorID,
            FloorNumber,
            FloorUsage,
            NoOfUnits,
            Unit_Type,
            --Shape AS geometry 
            ST_GeomFromWKB(GEOMETRY_WKB) AS geometry
        FROM df_data
        )
        TO 'buildingfloors.geojson'
    WITH (FORMAT GDAL, DRIVER 'GeoJSON', SRS 'EPSG:4326')"""
<<<<<<< HEAD
# print(convert_query)
=======

print("converting data...")
>>>>>>> agentic
con.execute(convert_query)
con.close()

print("GeoJSON exported successfully.")
print("Convert to tiles layer: ")
print("tippecanoe -z16 -Z14 --layer BuildingFloor -o building_floors.pmtiles -f -pc buildingfloors.geojson")