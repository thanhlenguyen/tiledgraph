"""
generate_test_data.py — synthetic Riyadh data with KNOWN patterns, so you can
check that each analysis finds what was planted.

    docker compose exec routing-api python scripts/generate_test_data.py
    docker compose exec routing-api python scripts/generate_test_data.py --cleanup   # remove it again

What gets planted (printed at the end too):
  • 20,000 addresses in 25 neighbourhoods across ~30 × 30 km of Riyadh
  • ~60 days of delivery attempts, 6% normal failure rate
  • 2 FAILURE HOT SPOTS (1 km radius) with ~35% failures, mostly 'address_not_found'
  • 15% of addresses have a GATE OFFSET: drivers always stop 40-150 m away from
    the official pin (same direction every time) → delivery-point learning must find them
  • GPS noise ~8 m, plus 3% wild GPS readings that the learning must ignore

All test rows are tagged (source_id / external_id start with 'TEST-'), so
--cleanup removes only them. Your real data is never touched.
"""
import argparse
import math
import os
import random
from datetime import datetime, timedelta, timezone

import h3
import psycopg2
from psycopg2.extras import execute_values

CENTER = (46.68, 24.71)          # Riyadh (lon, lat)
HALF_SPAN_KM = 15
HOTSPOTS = [(46.640, 24.760), (46.735, 24.655)]   # planted failure hot spots (lon, lat)
HOTSPOT_RADIUS_M = 1000


def connect():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "postgis"), dbname=os.getenv("POSTGRES_DB", "geodb"),
        user=os.getenv("POSTGRES_USER"), password=os.getenv("POSTGRES_PASSWORD"),
        port=int(os.getenv("POSTGRES_PORT", "5432")))


def move(lon, lat, east_m, north_m):
    return (lon + east_m / (111_320 * math.cos(math.radians(lat))), lat + north_m / 111_320)


def dist_m(a, b):
    dx = (a[0] - b[0]) * 111_320 * math.cos(math.radians(a[1]))
    dy = (a[1] - b[1]) * 111_320
    return math.hypot(dx, dy)


def h3s(lon, lat):
    return tuple(h3.latlng_to_cell(lat, lon, r) for r in (7, 8, 9))


def generate(conn, n_addresses: int, days: int, seed: int):
    rnd = random.Random(seed)
    span = HALF_SPAN_KM * 1000
    hoods = [move(*CENTER, rnd.uniform(-span, span), rnd.uniform(-span, span)) for _ in range(25)]
    hoods += HOTSPOTS                     # make sure hot spots have addresses

    # ---- addresses -----------------------------------------------------------
    addrs = []
    for i in range(n_addresses):
        hx = rnd.choice(hoods)
        lon, lat = move(*hx, rnd.gauss(0, 1200), rnd.gauss(0, 1200))
        weight = rnd.choice([1] * 8 + [2, 4])            # some apartment buildings
        addrs.append((f"TEST-{i}", f"TST{i:05d}", weight, lon, lat))

    with conn.cursor() as cur:
        rows = execute_values(cur, """
            INSERT INTO analytics.demand_points (source_id, short_address, weight, geom, h3_r7, h3_r8, h3_r9)
            SELECT v.sid, v.sa, v.w, ST_SetSRID(ST_MakePoint(v.lon, v.lat), 4326), v.r7, v.r8, v.r9
            FROM (VALUES %s) AS v(sid, sa, w, lon, lat, r7, r8, r9)
            ON CONFLICT (source_id) WHERE source_id IS NOT NULL DO NOTHING
            RETURNING id, ST_X(geom), ST_Y(geom)""",
            [(s, sa, w, lon, lat, *h3s(lon, lat)) for s, sa, w, lon, lat in addrs],
            template="(%s, %s, %s::real, %s::float8, %s::float8, %s, %s, %s)", fetch=True, page_size=5000)
    conn.commit()
    print(f"Inserted {len(rows):,} addresses")

    # ---- deliveries ----------------------------------------------------------
    now = datetime.now(timezone.utc)
    deliveries, gate_addresses = [], 0
    for addr_id, lon, lat in rows:
        if rnd.random() > 0.45:
            continue                                      # not every address orders
        in_hot = any(dist_m((lon, lat), h) < HOTSPOT_RADIUS_M for h in HOTSPOTS)
        has_gate = rnd.random() < 0.15
        gate = None
        if has_gate:
            gate_addresses += 1
            ang, d = rnd.uniform(0, 2 * math.pi), rnd.uniform(40, 150)
            gate = (math.cos(ang) * d, math.sin(ang) * d)
        for _ in range(rnd.randint(1, 8)):
            t = now - timedelta(days=rnd.uniform(0, days), hours=rnd.uniform(0, 10))
            p_fail = 0.35 if in_hot else 0.06
            failed = rnd.random() < p_fail
            if failed:
                reason = (rnd.choices(["address_not_found", "no_access", "customer_absent"], [6, 3, 1])[0]
                          if in_hot else rnd.choices(["customer_absent", "address_not_found", "refused"], [7, 2, 1])[0])
                drop = None
            else:
                reason = None
                ex, ny = gate if gate else (0, 0)
                if rnd.random() < 0.03:                   # wild GPS reading
                    ex, ny = rnd.uniform(-400, 400), rnd.uniform(-400, 400)
                drop = move(lon, lat, ex + rnd.gauss(0, 8), ny + rnd.gauss(0, 8))
            deliveries.append((f"TEST-D{len(deliveries)}", addr_id, lon, lat,
                               drop[0] if drop else None, drop[1] if drop else None,
                               "failed" if failed else "delivered", reason, t,
                               f"TEST-V{rnd.randint(1, 40)}", *h3s(lon, lat)))

    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO analytics.deliveries (external_id, address_id, geom, drop_geom, status, failure_reason,
                                              attempted_at, vehicle_id, h3_r7, h3_r8, h3_r9)
            SELECT v.eid, v.aid, ST_SetSRID(ST_MakePoint(v.lon, v.lat), 4326),
                   CASE WHEN v.dlon IS NULL THEN NULL ELSE ST_SetSRID(ST_MakePoint(v.dlon, v.dlat), 4326) END,
                   v.st, v.reason, v.t, v.veh, v.r7, v.r8, v.r9
            FROM (VALUES %s) AS v(eid, aid, lon, lat, dlon, dlat, st, reason, t, veh, r7, r8, r9)""",
            deliveries,
            template="(%s, %s::bigint, %s::float8, %s::float8, %s::float8, %s::float8, %s, %s, %s::timestamptz, %s, %s, %s, %s)",
            page_size=5000)
        cur.execute("ANALYZE analytics.demand_points; ANALYZE analytics.deliveries;")
    conn.commit()
    failed = sum(1 for d in deliveries if d[6] == "failed")
    print(f"Inserted {len(deliveries):,} delivery attempts ({failed / len(deliveries) * 100:.1f}% failed)")
    print("\nPLANTED PATTERNS — check that the analyses find these:")
    for i, (lon, lat) in enumerate(HOTSPOTS, 1):
        print(f"  Hot spot {i}: lon {lon}, lat {lat} (1 km radius, ~35% failures, mostly address_not_found)")
    print(f"  Gate offsets: {gate_addresses:,} addresses, drivers stop 40-150 m from the pin")
    print(f"\nTest area bbox for the API: [{CENTER[0] - 0.17:.2f}, {CENTER[1] - 0.15:.2f}, "
          f"{CENTER[0] + 0.17:.2f}, {CENTER[1] + 0.15:.2f}]")


def cleanup(conn):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM analytics.deliveries WHERE external_id LIKE 'TEST-%'")
        d = cur.rowcount
        cur.execute("DELETE FROM analytics.demand_points WHERE source_id LIKE 'TEST-%'")
        a = cur.rowcount
    conn.commit()
    print(f"Removed {a:,} test addresses and {d:,} test deliveries (learned points cascade).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--addresses", type=int, default=20_000)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--cleanup", action="store_true", help="remove all TEST- rows and exit")
    args = ap.parse_args()
    with connect() as c:
        cleanup(c) if args.cleanup else generate(c, args.addresses, args.days, args.seed)
