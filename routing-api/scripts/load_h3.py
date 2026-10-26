"""
load_h3.py — fill the h3_r7 / h3_r8 / h3_r9 columns for rows that don't have them yet.

Run after importing real data (and again after each new import; it only touches
rows where h3_r8 IS NULL):

    docker compose exec routing-api python scripts/load_h3.py

HOW TO IMPORT YOUR REAL DATA FIRST (examples — change table/column names):

  -- National Address points → demand
  INSERT INTO analytics.demand_points (source_id, short_address, weight, geom)
  SELECT na.id::text, na.short_address, 1, ST_Transform(na.geom, 4326)
  FROM your_schema.national_address na
  ON CONFLICT (source_id) WHERE source_id IS NOT NULL DO NOTHING;

  -- Delivery attempts (one row per attempt) from your delivery system
  INSERT INTO analytics.deliveries
         (external_id, address_id, geom, drop_geom, status, failure_reason, attempted_at, vehicle_id)
  SELECT s.attempt_id, a.id, a.geom,
         ST_SetSRID(ST_MakePoint(s.pod_lon, s.pod_lat), 4326),
         CASE WHEN s.result = 'DELIVERED' THEN 'delivered' ELSE 'failed' END,
         s.failure_reason, s.attempt_time, s.vehicle_id
  FROM your_schema.shipment_attempts s
  JOIN analytics.demand_points a ON a.source_id = s.national_address_id::text;
"""
import os
import sys
import time

import h3
import psycopg2
from psycopg2.extras import execute_values

BATCH = 50_000


def connect():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "postgis"), dbname=os.getenv("POSTGRES_DB", "geodb"),
        user=os.getenv("POSTGRES_USER"), password=os.getenv("POSTGRES_PASSWORD"),
        port=int(os.getenv("POSTGRES_PORT", "5432")))


def fill(conn, table: str) -> int:
    done = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(f"SELECT id, ST_X(geom), ST_Y(geom) FROM analytics.{table} "
                        f"WHERE h3_r8 IS NULL LIMIT {BATCH}")
            rows = cur.fetchall()
            if not rows:
                return done
            values = [(i, h3.latlng_to_cell(lat, lon, 7), h3.latlng_to_cell(lat, lon, 8),
                       h3.latlng_to_cell(lat, lon, 9)) for i, lon, lat in rows]
            execute_values(cur, f"""
                UPDATE analytics.{table} AS t SET h3_r7 = v.r7, h3_r8 = v.r8, h3_r9 = v.r9
                FROM (VALUES %s) AS v(id, r7, r8, r9) WHERE t.id = v.id""", values)
        conn.commit()
        done += len(rows)
        print(f"  {table}: {done:,} rows updated", flush=True)


if __name__ == "__main__":
    t0 = time.time()
    with connect() as conn:
        for table in ("demand_points", "deliveries"):
            n = fill(conn, table)
            print(f"{table}: {n:,} rows filled")
        with conn.cursor() as cur:
            cur.execute("ANALYZE analytics.demand_points; ANALYZE analytics.deliveries;")
    print(f"Done in {time.time() - t0:.1f}s")
    sys.exit(0)
