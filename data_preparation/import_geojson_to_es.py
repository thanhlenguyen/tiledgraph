import json
import hashlib
from elasticsearch import Elasticsearch, helpers

# Connect to ES 
es = Elasticsearch(["http://localhost:9200/"])

<<<<<<< HEAD
geojson_file = "/mnt/d/Git/tiledgraph/buildingfloors.geojson"  # Path to your GeoJSON file
index = "building_floors"  # Target ES index name
=======
geojson_file = "/mnt/d/Git/mapserver/data/pois.geojson"  # Path to your GeoJSON file
index = "pois"  # Target ES index name
>>>>>>> agentic

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

