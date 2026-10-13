import os
import typesense
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

# When running on the host (outside Docker), service names don't resolve.
# Set RUNNING_IN_DOCKER=1 if you ever run this script inside a container.
IN_DOCKER = os.getenv("RUNNING_IN_DOCKER") == "1"

PG_HOST = os.getenv("POSTGRES_HOST", "postgis") if IN_DOCKER else "localhost"
TS_HOST = os.getenv("TYPESENSE_HOST", "typesense") if IN_DOCKER else "localhost"

ts_client = typesense.Client({
    "nodes": [{
        "host": TS_HOST,
        "port": os.getenv("TYPESENSE_PORT", "8108"),
        "protocol": os.getenv("TYPESENSE_PROTOCOL", "http"),
    }],
    "api_key": os.getenv("TYPESENSE_API_KEY", "xyz"),
    "connection_timeout_seconds": 10,
})

def get_postgis_connection():
    return psycopg2.connect(
        host=PG_HOST,
        database=os.getenv("POSTGRES_DB", "geodb"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        connect_timeout=10,
    )
# ─────────────────────────────────────────────
# Import function
# ─────────────────────────────────────────────
def import_pois_from_postgis():
    print("Connecting to PostGIS...")
    conn = get_postgis_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT id, name, address, type, class,
            ST_Y(ST_Transform(ST_GeometryN(geom, 1), 4326)) AS lat, 
            ST_X(ST_Transform(ST_GeometryN(geom, 1), 4326)) AS lon
        FROM topology.places
        WHERE geom IS NOT NULL
    """)

    rows = cur.fetchall()
    print(f"Found {len(rows)} places in PostGIS")

    docs = []
    for row in rows:
        # Skip invalid coordinates
        if row["lat"] is None or row["lon"] is None:
            continue

        docs.append({
            "id": str(row["id"]),
            "name": row["name"] or "",
            "address": row["address"] or "",
            "type": (row["type"] or "").lower().strip(),
            "class": (row["class"] or "").lower().strip() if row.get("class") else "",
            "location": [float(row["lat"]), float(row["lon"])]   # Typesense expects [lat, lon]
        })

    cur.close()
    conn.close()

    if not docs:
        print("No documents to import.")
        return

    print(f"Importing {len(docs)} documents into Typesense...")

    # Import in batches of 200 (recommended)
    batch_size = 200
    for i in range(0, len(docs), batch_size):
        batch = docs[i:i + batch_size]
        result = ts_client.collections["pois"].documents.import_(
            batch,
            {"action": "upsert"}
        )
        success = sum(1 for r in result if r.get("success"))
        print(f"  Batch {i // batch_size + 1}: {success}/{len(batch)} success")

    print("Import finished.")


if __name__ == "__main__":
    import_pois_from_postgis()