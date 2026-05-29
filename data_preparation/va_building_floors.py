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

df = pl.read_database(
    query = f"""
        SELECT		
            b.OBJECTID,
            b.fkShortAddress,
            b.BuildingUseType,
            Building_Type,
            b.BuildingHeight,
            f.BuildingID,
            b.NoofFloors,
            f.FloorName,
            f.FloorUsage,
            b.FloorsBelowGround,
            b.FloorsAboveGround,
            f.NoOfUnits,
            b.Unit_Type,
            --b.Shape,
            b.Geometry_WKB
        FROM
            {table_schema}.{building} as b 
            Full Join {table_schema}.{floor} as f 
            ON b.fkShortAddress = f.fkShortAddress
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
            fkShortAddress,
            BuildingUseType,
            Building_Type,
            BuildingHeight,
            BuildingID,
            NoofFloors,
            FloorName,
            FloorUsage,
            FloorsBelowGround,
            FloorsAboveGround,
            NoOfUnits,
            Unit_Type,
            --Shape AS geometry 
            ST_GeomFromWKB(GEOMETRY_WKB) AS geometry
        FROM df_data
        )
        TO 'buildingfloors.geojson'
    WITH (FORMAT GDAL, DRIVER 'GeoJSON', SRS 'EPSG:4326')"""
# print(convert_query)
con.execute(convert_query)
con.close()

print("GeoJSON exported successfully.")
