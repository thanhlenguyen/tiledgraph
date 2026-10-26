"""
smoke_test.py — check every analytics endpoint against the planted test data.

Run AFTER generate_test_data.py, from any machine that can reach the API:

    python scripts/smoke_test.py                              # default http://localhost/api
    python scripts/smoke_test.py --base http://your-server/api
    python scripts/smoke_test.py --admin-token XXXX           # if ANALYTICS_ADMIN_TOKEN is set

Needs only: pip install httpx
Each check prints PASS / FAIL with the numbers it saw.
"""
import argparse
import math
import sys
import time

import httpx

BBOX = [46.51, 24.56, 46.85, 24.86]                      # same as generate_test_data.py
HOTSPOTS = [(46.640, 24.760), (46.735, 24.655)]          # planted failure hot spots

results = []


def check(name, ok, detail):
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")


def dist_m(a, b):
    return math.hypot((a[0] - b[0]) * 101_000, (a[1] - b[1]) * 111_000)


def centroid(geom):
    ring = geom["coordinates"][0]
    return (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))


def call(client, path, body=None, headers=None):
    t0 = time.time()
    r = client.post(path, json=body or {}, headers=headers or {}) if body is not None else client.get(path)
    secs = time.time() - t0
    if r.status_code != 200:
        raise RuntimeError(f"{path} → HTTP {r.status_code}: {r.text[:200]}")
    return r.json(), secs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost/api")
    ap.add_argument("--admin-token", default=None)
    args = ap.parse_args()
    c = httpx.Client(base_url=args.base, timeout=300)
    area = {"bbox": BBOX}

    print(f"\nTesting {args.base}\n")
    print("1. Data loaded")
    s, _ = call(c, "/analytics/status")
    check("demand points", s["demand_points"] > 0, f"{s['demand_points']:,} rows, {s['demand_missing_h3']:,} missing H3")
    check("deliveries", s["deliveries"] > 0, f"{s['deliveries']:,} rows, {s['deliveries_missing_h3']:,} missing H3")

    print("\n2. Heatmaps (Phase 3)")
    d, t = call(c, "/analytics/hexbin", {"area": area})
    check("demand heatmap", d["summary"]["cells"] > 100, f"{d['summary']['cells']} hexagons in {t:.1f}s")
    d, t = call(c, "/analytics/hexbin", {"area": area, "dataset": "deliveries", "metric": "failure_rate", "days": 90})
    check("failure-rate heatmap", d["summary"]["cells"] > 100, f"{d['summary']['cells']} hexagons in {t:.1f}s")

    print("\n3. Coverage (Phase 3) — needs Valhalla and hospitals in topology.places")
    try:
        d, t = call(c, "/analytics/coverage", {"area": area, "facility_type": "hospital", "minutes": 15})
        s = d["summary"]
        check("coverage", s["facilities_considered"] > 0 and 0 <= s["coverage_pct"] <= 100,
              f"{s['coverage_pct']}% within 15 min, {s['facilities_considered']} hospitals, {t:.1f}s")
    except RuntimeError as e:
        check("coverage", False, str(e))

    print("\n4. Hot spots (Phase 3) — both planted hot spots should be found")
    d, t = call(c, "/analytics/hotspots", {"area": area, "days": 90})
    hot = [centroid(f["geometry"]) for f in d["layers"][0]["geojson"]["features"] if f["properties"]["z"] >= 1.96]
    for i, h in enumerate(HOTSPOTS, 1):
        n = sum(1 for p in hot if dist_m(p, h) < 1500)
        check(f"planted hot spot {i}", n > 0, f"{n} significant hexagons nearby")
    inside = sum(1 for p in hot if any(dist_m(p, h) < 1500 for h in HOTSPOTS))
    check("few false alarms", inside >= 0.6 * max(len(hot), 1), f"{inside} of {len(hot)} hot hexagons are in planted zones")
    reason = (d["summary"]["failure_reasons_in_hotspots"] or [{}])[0].get("reason")
    check("main reason", reason == "address_not_found", f"top reason = {reason}")
    d, t = call(c, "/analytics/hotspots", {"area": area, "days": 90, "method": "dbscan", "eps_m": 200, "min_points": 15})
    check("DBSCAN clusters", d["summary"]["clusters"] > 0, f"{d['summary']['clusters']} clusters in {t:.1f}s")

    print("\n5. Delivery-point learning (Phase 5) — gate offsets 40-150 m were planted")
    hdr = {"X-Admin-Token": args.admin_token} if args.admin_token else {}
    d, t = call(c, "/analytics/delivery_points/learn", {}, headers=hdr)
    s = d["summary"]
    check("learning", s["addresses"] > 0, f"{s['addresses']:,} addresses learned in {t:.1f}s")
    check("normal addresses stay put", s["median_offset_m"] < 15, f"median offset {s['median_offset_m']:.1f} m (GPS noise)")
    share = s["over_30m"] / max(s["addresses"], 1)
    check("gate offsets found", 0.08 <= share <= 0.25, f"{s['over_30m']:,} addresses moved > 30 m ({share:.0%}, ~15% planted)")
    d, _ = call(c, "/analytics/delivery_points/offsets", {"area": area})
    med = d["summary"]["median_offset_m"]
    check("corrections list", 40 <= med <= 150, f"{d['summary']['addresses']} to review, median {med} m")

    print("\n6. Site selection (Phase 5) — needs Valhalla")
    try:
        d, t = call(c, "/analytics/site_selection", {"area": area, "n_sites": 3, "minutes": 10, "resolution": 7,
                                                     "existing_facility_type": "hospital"})
        s = d["summary"]
        gains = [x["added_demand"] for x in s["sites"]]
        check("sites returned", len(s["sites"]) == 3, f"{len(s['sites'])} sites in {t:.1f}s")
        check("coverage improves", s["coverage_after_pct"] > s["coverage_before_pct"],
              f"{s['coverage_before_pct']}% → {s['coverage_after_pct']}%")
        check("ranked by gain", gains == sorted(gains, reverse=True), f"added demand per site {gains}")
    except RuntimeError as e:
        check("site selection", False, str(e))

    print("\n7. Agent can see the analytics tools")
    try:
        r = c.get("/agent/health").json()
        missing = {"coverage_analysis", "find_hotspots", "suggest_sites"} - set(r.get("tools", []))
        check("agent tools", not missing, f"provider {r.get('provider')}, missing: {sorted(missing) or 'none'}")
    except Exception as e:
        check("agent tools", False, str(e))

    print(f"\n{sum(results)} of {len(results)} checks passed.")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
