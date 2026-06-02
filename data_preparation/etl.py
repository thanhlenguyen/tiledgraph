import polars as pl
from sqlalchemy import create_engine
import urllib
import pyodbc
import duckdb

server = '17.10.200.16'
username = 'na_dev'
password = """nadev123"""
db_name = "na_dev_sql_geo_db"
db_creds = ("DRIVER={ODBC Driver 17 for SQL Server};" 
    + f"SERVER={server};" 
    + "TrustServerCertificate=yes;" 
    + f"DATABASE={db_name};" 
    + f"UID={username};" 
    + f"PWD={password}")
conn_string = urllib.parse.quote_plus(db_creds)
engine = create_engine("mssql+pyodbc:///?odbc_connect={}".format(conn_string))
pyodbc_connect = pyodbc.connect(db_creds, autocommit=True)
db_cursor = pyodbc_connect.cursor()

table_schema = "sde"
table_name = "gis_verticaladdress_unit"
exclude_cols = ["EndDate", "GEOMETRY", "GDB_GEOMATTR_DATA"]
filter_cols = ""
query = f"""
    select 
        column_name, data_type, numeric_precision, numeric_scale
    from information_schema.columns c
    where table_name = '{table_name}'
    and table_schema = '{table_schema}'
    and column_name not in ({', '.join(f"'{c}'" for c in exclude_cols)})
    {filter_cols}
"""
df = pl.read_database(query = query, connection = engine)
adjusted_cols = []
datetime_cols = []
for row in df.iter_rows(named=True):
    if row['data_type'] == 'uniqueidentifier':
        adjusted_cols.append(f"CASE WHEN {row['column_name']} is null then null else CONCAT('{{', CAST({row['column_name']} AS VARCHAR(100)), '}}') END AS {row['column_name']}")
    elif row['data_type'] == 'numeric' and row['numeric_precision'] in (38, 8) and row['numeric_scale'] in (8, 4) and not str(row['column_name']).lower().startswith('legacyfk'):
        adjusted_cols.append(f"CAST({row['column_name']} AS FLOAT) AS {row['column_name']}")
    elif str(row['column_name']).lower().startswith('legacyfk'):
        adjusted_cols.append(f"CAST({row['column_name']} AS BIGINT) AS {row['column_name']}")
    elif row['data_type'] == 'datetime2':
        datetime_cols.append(row['column_name'])
        adjusted_cols.append(f"{row['column_name']}")
    elif row['column_name'] == 'GEOMETRY_WKB':
        pass
    else:
        adjusted_cols.append(f"{row['column_name']}")
adjusted_cols = ', '.join(adjusted_cols)
original_cols = ', '.join([col for col in df["column_name"].to_list() if col not in datetime_cols])
selected_cols = {"original_cols": original_cols, "adjusted_cols": adjusted_cols, "datetime_cols": datetime_cols}


df = pl.read_database(
    query = f"""
        select
            {selected_cols['adjusted_cols']}
            ,GEOMETRY_WKB
        from {table_schema}.{table_name}
    """
    ,connection=engine, 
)



con = duckdb.connect()
con.execute("INSTALL spatial; LOAD spatial;")
con.execute("LOAD spatial;")


con.register("df_data", df)
cols = [col for col in df.columns if col not in ['GEOMETRY_WKB']]
fields = [col for col in cols if col not in selected_cols['datetime_cols']]
fields.extend(f'epoch_ms({c}) AS {c}' for c in selected_cols['datetime_cols'])
fields.extend(['epoch_ms(StartDate) as Datestamp'])

convert_query = f"""
    COPY (
        with src_data as (
            SELECT 
                {', '.join(cols)}
                ,ST_GeomFromWKB(GEOMETRY_WKB) AS geometry
            FROM df_data
        )
        select
            {', '.join(fields)}
            ,geometry
        from src_data
    ) TO  {table_name}.geojson
    WITH (FORMAT GDAL, DRIVER 'GeoJSON', SRS 'EPSG:4326')"""
# print(convert_query)
con.execute(convert_query)
con.close()