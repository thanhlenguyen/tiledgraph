# INSERT statements or use pgAdmin/ogr2ogr to import data ...
1. Prepare data
- Prepare GeoJSON, shapefiles, or CSV with geometry (e.g., latitude/longitude or WKT)
- Save data in your project directory, e.g., /mnt/d/Git/mapserver/data/
2. Enable PostGIS Extension
Ensure the PostGIS extension is enabled in your database for geospatial functionality.

Connect to the database:
```bash
psql -h localhost -U le -d geodb -W
```
Enable PostGIS if needed:
```sql
CREATE EXTENSION IF NOT EXISTS postgis;  
```  
3. Import GeoJSON or Other Formats Using ogr2ogr
install ogr2ogr (part of GDAL): 
```bash
sudo apt install gdal-bin
```
- Import GeoJSON (most of time we use this task, we don't neet to configure anything in postgres):

Use ogr2ogr to load into PostGIS:
```bash
ogr2ogr -f PostgreSQL PG:"host=localhost dbname=$POSTGRES_DB user=$POSTGRES_USER password=$POSTGRES_PASSWORD" \  # Destination
  /data/Units.geojson \  # Source
  -nln indoor.units_raw \  # New Layer name
  -lco GEOMETRY_NAME=geom \   # Layer create option
  -lco FID=gid \              # Layer create option
  -lco LAUNDER=YES \        # Layer create option (lower case for field name)
  -t_srs EPSG:4326 \
  -nlt MULTIPOLYGON \    # New layer type
  -overwrite

# Specific case - Import pois.geojson to topology.places
ogr2ogr -f PostgreSQL PG:"host=localhost user=le password=123456 dbname=geodb" -nlt PROMOTE_TO_MULTI -overwrite -lco GEOMETRY_NAME=geom -lco FID=id -lco LAUNDER=YES -t_srs EPSG:4326 -progress \
-nln topology.places  /mnt/d/Git/mapserver/data/pois.geojson

```
Options:
```bash
-nln topology.places: Places table in topology schema.
-nlt PROMOTE_TO_MULTI: Ensures multi-geometry support.
-lco GEOMETRY_NAME=geom: Names geometry column geom (matches Martin’s default).
```
- Import Shapefile (Similar command):
```bash
ogr2ogr -f PostgreSQL PG:"host=localhost user=le password=123456 dbname=geodb" \
-nln topology.places -nlt PROMOTE_TO_MULTI -lco GEOMETRY_NAME=geom \
/mnt/d/Git/mapserver/data/sample.shp
```

# Index for ElasticSearch

## building_unit.geojson
```json
PUT /building_units
{
  "settings": {
    "analysis": {
      "normalizer": {
        "lowercase_normalizer": {
          "type": "custom",
          "filter": ["lowercase"]
          // Optional but very useful for Vietnamese addresses: "asciifolding"  → turns "Lê" → "Le", "Đ" → "D", etc.
        }
      }
    }
  },
  "mappings": {
    "dynamic": "true",              // ← change to "strict" later when stable
    "properties": {
      "UNIT_ID": { "type": "keyword", "normalizer": "lowercase_normalizer"},           // ← this is the key, Exact match for IDs
      "USE_TYPE": { "type": "keyword", "normalizer": "lowercase_normalizer" },
      "NAME": { "type": "text" },        // Full-text search
      "NAME_LONG": { "type": "text" },
      "LEVEL_ID": { "type": "keyword", "normalizer": "lowercase_normalizer" },
      "HEIGHT": { "type": "float" },
      "LabelNames": { "type": "text" },
      "UnitAddres": { "type": "keyword", "normalizer": "lowercase_normalizer" },
      "Sequance": { "type": "keyword", "normalizer": "lowercase_normalizer" },
      "Base": { "type": "float" },
      "geometry": { "type": "geo_shape" }  // For MultiPolygon spatial data
    }
  }
}

```json
// Count number imported records
GET /building_units/_count

// Show data structure
GET /building_units/_mapping

// Searching
GET /building_units/_search
{
  "query": {
    "match_all": {}
  },
  "size": 5                       // show only first 5 documents (default is 10)
}

// Remove old one 

DELETE building_units
```
Reindex:
```json
POST _reindex
{
  "source": {
    "index": "building_units"
  },
  "dest": {
    "index": "building_units_v2"
  }
}
```

Alias new one with old name incase we don't want to change the code of app

```json
POST _aliases
{
  "actions": [
    { "add": { "index": "building_units_v2", "alias": "building_units" } }
  ]
}
```


## pois
```json
PUT /pois
{
  "settings": {
    "analysis": {
      "normalizer": {
        "lowercase_normalizer": {
          "type": "custom",
          "filter": ["lowercase"]
        }
      }
    }
  },    
  "mappings": {
    "dynamic": "true",              // ← change to "strict" later when stable
    "properties": {
      "id": { "type": "interger" }, // exact match, no full-text needed (codes like "12345 67890") 
      "name": { "type": "keyword", "normalizer": "lowercase_normalizer" },
      "address": { "type": "text" },
      "type": { "type": "keyword", "normalizer": "lowercase_normalizer" },
      "class": { "type": "keyword", "normalizer": "lowercase_normalizer"  },
      "geometry": { "type": "geo_point" }
    }
  }
}
```
## building_floors
```json
PUT /building_floors
{
  "settings": {
    "analysis": {
      "normalizer": {
        "lowercase_normalizer": {
          "type": "custom",
          "filter": ["lowercase"]
        }
      }
    }
  },    
  "mappings": {
    "dynamic": "true",              // ← change to "strict" later when stable
    "properties": {
      "OBJECTID" : {"type": "integer" },
      "ShortAddress" : {"type": "keyword", "normalizer": "lowercase_normalizer" },
      "VerticalAddress" : {"type": "keyword", "normalizer": "lowercase_normalizer" },
      "BuildingUseType" : {"type": "text" },
      "Building_Type" : {"type": "integer" },
      "BuildingHeight" : {"type": "float" },
      "BuildingID" : {"type": "text" },
      "NoofFloors" : {"type": "integer" },
      "FloorsBelowGround" : {"type": "integer" },
      "FloorsAboveGround" : {"type": "integer" },
      "FloorName" : {"type": "text" },
      "FloorID" : {"type": "text" },
      "FloorNumber" : {"type": "integer" },
      "FloorUsage" : {"type": "text" },
      "NoOfUnits" : {"type": "integer" },
      "Unit_Type" : {"type": "integer" },
      "geometry": { "type": "geo_shape" }
    }
  }
}
```

# Insert data to ElasticSearch
## Execute code
```bash
vsol@LeNguyen:/mnt/d/Git/tiledgraph$ source env/bin/activate
(env) vsol@LeNguyen:/mnt/d/Git/tiledgraph$ python data_preparation/import_geojson_to_es.py
```
## Python code
```python

import json
import hashlib
from elasticsearch import Elasticsearch, helpers

# Connect to ES 
es = Elasticsearch(["http://localhost:9200/"])

geojson_file = "/mnt/d/Git/mapserver/data/pois.geojson"  # Path to your GeoJSON file
index = "pois"  # Target ES index name

# Load GeoJSON
with open(geojson_file, "r", encoding="utf-8") as f:  # Replace with your file path
    geojson_data = json.load(f)

# Prepare bulk actions (one document per feature)
actions = []
for feature in geojson_data["features"]:
    properties = feature.get("properties", {})
    id = str(hashlib.md5(str(properties).encode()).hexdigest())
    doc = {
        "_index": index,  # Your index name
        "_id": id,
        "_source": {
            "properties": properties,  # Spread properties
            "geometry": feature.get("geometry")  # GeoJSON geometry
        }
    }
    actions.append(doc)

# Bulk index
print(f"Importing {len(actions)} documents to Elasticsearch...")
helpers.bulk(es, actions)
print("Import completed!")
```