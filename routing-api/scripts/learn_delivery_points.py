"""
learn_delivery_points.py — run delivery-point learning from the command line.

Same logic as the "Re-learn delivery points from GPS" button, but with no HTTP
time limits and with progress output. Use it for big data and for nightly runs.

    docker compose exec routing-api python scripts/learn_delivery_points.py
    docker compose exec routing-api python scripts/learn_delivery_points.py --batch-size 20000

Nightly at 02:00 (host crontab):
    0 2 * * * cd /path/to/project && docker compose exec -T routing-api python scripts/learn_delivery_points.py >> learn.log 2>&1
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # /app

import psycopg2

import app        # noqa: F401  — import app FIRST so analytics and agent load in the right order
import analytics


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-deliveries", type=int, default=3, help="successful deliveries needed per address")
    ap.add_argument("--inlier-radius-m", type=float, default=150, help="drops farther than this from the median are outliers")
    ap.add_argument("--max-offset-m", type=float, default=500, help="GPS farther than this from the pin is ignored")
    ap.add_argument("--batch-size", type=int, default=None, help=f"address ids per batch (default {analytics.LEARN_BATCH_SIZE})")
    ap.add_argument("--batch-timeout", default=None, help=f"time limit per batch (default {analytics.LEARN_BATCH_TIMEOUT})")
    a = ap.parse_args()
    if a.batch_size:
        analytics.LEARN_BATCH_SIZE = a.batch_size
    if a.batch_timeout:
        analytics.LEARN_BATCH_TIMEOUT = a.batch_timeout

    conn = psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "postgis"), dbname=os.getenv("POSTGRES_DB", "geodb"),
        user=os.getenv("POSTGRES_USER"), password=os.getenv("POSTGRES_PASSWORD"),
        port=int(os.getenv("POSTGRES_PORT", "5432")))
    t0 = time.time()

    def progress(i, n, learned):
        print(f"  batch {i}/{n}: {learned:,} addresses learned so far ({time.time() - t0:.1f}s)", flush=True)

    try:
        s = analytics.learn_sync(conn, {"max_offset": a.max_offset_m, "min_n": a.min_deliveries,
                                        "inlier": a.inlier_radius_m}, progress=progress)
    except analytics.AnalyticsError as exc:
        sys.exit(f"Stopped: {exc}")
    except psycopg2.Error as exc:
        sys.exit(f"Database error ({type(exc).__name__}): {(exc.pgerror or str(exc)).strip()}"
                 f"{analytics._db_error_tip(exc)}")
    finally:
        conn.close()

    print(f"\nDone in {time.time() - t0:.1f}s")
    print(f"  Addresses learned : {s['addresses']:,}")
    print(f"  Median offset     : {s['median_offset_m']:.1f} m")
    print(f"  Offset > 30 m     : {s['over_30m']:,}")
    print(f"  Offset > 100 m    : {s['over_100m']:,}")
    print(f"  Confident (≥ 0.5) : {s['confident']:,}")


if __name__ == "__main__":
    main()
