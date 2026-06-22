# This only can run from Windows, because Windows environment has VPN, 
# If we want run from Ubuntu, we need to add [IP_ADDRESS_YOU_COPIED] non-prd-elastic.address.gov.sa to etc/hosts file
import json
import urllib3
from elasticsearch import Elasticsearch, helpers

# Suppress noisy insecure warnings (optional, for cleaner output)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

es = Elasticsearch(
    "https://non-prd-elastic.address.gov.sa",
    basic_auth=("lent2", ";]MM$6BCzCB76d8vK"),
    verify_certs=False,
    request_timeout=300
)

INDEX_NAME = "building_floors"
GEOJSON_FILE = "/mnt/d/Git/tiledgraph/buildingfloors.geojson"
# GEOJSON_FILE = r"D:\Git\mapserver\data\UnitsOnlyFlat.geojson"

# Load GeoJSON
with open(GEOJSON_FILE, "r", encoding="utf-8") as f:
    geojson_data = json.load(f)

def generate_actions():
    for feature in geojson_data.get("features", []):
        properties = feature.get("properties", {}) or {}
        geometry = feature.get("geometry")

        if not geometry:
            continue

        doc = {"properties": properties, "geometry": geometry}

        # Use UNIT_ID as _id (lowercased to match your normalizer) if it exists, otherwise let ES auto-generate
        # raw_id = properties.get("UNIT_ID")
        # doc_id = str(raw_id).strip().lower() if raw_id is not None else None

        yield {
            "_index": INDEX_NAME,
            # "_id": doc_id, #If UNIT_ID is missing, ES will auto-generate an ID
            "_source": doc
        }

print("Starting bulk import...")

try:
    # Use .options() to avoid DeprecationWarning + capture failures
    success, failed = helpers.bulk(
        es.options(request_timeout=180),
        generate_actions(),
        chunk_size=300,           # Smaller chunk = easier to debug
        raise_on_error=False,     # Important: do not raise, collect errors
        stats_only=False
    )

    print(f"✅ Bulk reported success: {success} operations")
    print(f"❌ Failed items: {len(failed) if failed else 0}")

    if failed:
        print("\n=== First 10 failed documents ===")
        for i, item in enumerate(failed[:10]):
            print(f"{i+1}: {item}")

except Exception as e:
    print(f"Critical error: {e}")