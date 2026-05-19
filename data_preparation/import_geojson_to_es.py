import json
from elasticsearch import Elasticsearch, helpers

# Connect to ES (update host if not localhost)
es = Elasticsearch(["http://localhost:9200"])

# Load GeoJSON
with open("data/UnitsOnlyFlat.geojson", "r") as f:  # Replace with your file path
    geojson_data = json.load(f)

# Prepare bulk actions (one document per feature)
actions = []
for feature in geojson_data["features"]:
    doc = {
        "_index": "buildings_vertical",  # Your index name
        "_source": {
            **feature["properties"],  # Spread properties
            "geometry": feature["geometry"]  # GeoJSON geometry
        }
    }
    actions.append(doc)

# Bulk index
helpers.bulk(es, actions)
print("Import completed!")