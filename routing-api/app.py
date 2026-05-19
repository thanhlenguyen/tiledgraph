"""
GeoRouting Lab — app.py
FastAPI proxy/orchestration layer in front of:
  • Valhalla  (routing, matrix, isochrone)
  • VROOM     (VRP / TSP optimisation)

Key rules
---------
* Never use `async with client` inside handlers — the shared client must stay open.
* Surface upstream HTTP errors with their original status codes.
* /optimize_route handles both VRP (vehicle has start+end) and TSP (open tour, no end).
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
import httpx

# ---------------------------------------------------------------------------
# Lifespan — shared async HTTP client
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
    yield
    await client.aclose()

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="GeoRouting Lab API",
    description="Proxy for Valhalla routing + VROOM optimisation",
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

VALHALLA_URL = "http://valhalla:8002"
VROOM_URL    = "http://vroom:3000"

client: httpx.AsyncClient = None  # type: ignore


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------
async def _forward(method: str, url: str, payload) -> dict:
    """Forward a JSON payload to an upstream service. Raises HTTPException on failure."""
    try:
        resp = await client.request(method, url, json=payload)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Upstream unreachable: {exc}")

    if resp.status_code != 200:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)

    return resp.json()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/")
async def root():
    return {"message": "GeoRouting Lab API", "services": ["Valhalla", "VROOM"]}

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/health/valhalla")
async def health_valhalla():
    try:
        resp = await client.get(f"{VALHALLA_URL}/status")
        return {"status": "ok", "detail": resp.json()}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))

@app.get("/health/vroom")
async def health_vroom():
    try:
        resp = await client.get(f"{VROOM_URL}/health")
        return {"status": "ok", "detail": resp.json()}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ---------------------------------------------------------------------------
# Valhalla — Route
# ---------------------------------------------------------------------------
@app.post("/route")
async def valhalla_route(request: Request):
    """
    Point-to-point or multi-stop route.

    Minimal payload:
    {
      "locations": [{"lon": 106.63, "lat": 10.82}, {"lon": 106.70, "lat": 10.77}],
      "costing": "auto"
    }
    Supports costing_options for route alternatives (use_highways, shortest, etc.)
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    return await _forward("POST", f"{VALHALLA_URL}/route", payload)


# ---------------------------------------------------------------------------
# Valhalla — Matrix
# ---------------------------------------------------------------------------
@app.post("/matrix")
async def valhalla_matrix(request: Request):
    """Time/distance cost matrix."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    return await _forward("POST", f"{VALHALLA_URL}/matrix", payload)


# ---------------------------------------------------------------------------
# Valhalla — Isochrone
# ---------------------------------------------------------------------------
@app.post("/isochrone")
async def valhalla_isochrone(request: Request):
    """
    Reachability polygons (isochrones / service areas).

    Payload:
    {
      "locations": [{"lon": 106.63, "lat": 10.82}],
      "costing": "auto",
      "contours": [
        {"time": 5,  "color": "ff4444"},
        {"time": 10, "color": "ffaa00"},
        {"time": 15, "color": "22cc66"}
      ],
      "polygons": true
    }
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    return await _forward("POST", f"{VALHALLA_URL}/isochrone", payload)


# ---------------------------------------------------------------------------
# VROOM — Raw optimise
# ---------------------------------------------------------------------------
@app.post("/optimize")
async def vroom_optimize(request: Request):
    """
    Raw VROOM VRP/TSP. geometry flag injected server-side.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if "options" not in payload:
        payload["options"] = {}
    payload["options"]["g"] = True

    return await _forward("POST", VROOM_URL, payload)


# ---------------------------------------------------------------------------
# VROOM — Optimise + decode geometry → GeoJSON
# ---------------------------------------------------------------------------
@app.post("/optimize_route")
async def optimize_route(request: Request):
    """
    Optimise with VROOM then decode each route's encoded polyline into a
    GeoJSON FeatureCollection for direct MapLibre consumption.

    Supports both VRP (vehicle has start + end → returns to depot) and
    TSP / open tour (vehicle has start only → no return leg).

    Response:
    {
      "vroom":  { ...raw VROOM output... },
      "geojson": {
        "type": "FeatureCollection",
        "features": [
          {
            "type": "Feature",
            "geometry": { "type": "LineString", "coordinates": [[lng,lat],…] },
            "properties": { "vehicle_id": 1, "duration": 3600, "distance": 12000 }
          }
        ]
      }
    }
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Always request geometry
    if "options" not in payload:
        payload["options"] = {}
    payload["options"]["g"] = True

    vroom_result = await _forward("POST", VROOM_URL, payload)

    features = []
    for route in vroom_result.get("routes", []):
        encoded = route.get("geometry")
        if encoded:
            coords = _decode_polyline6(encoded)
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {
                    "vehicle_id": route.get("vehicle"),
                    "duration":   route.get("duration"),
                    "distance":   route.get("distance"),
                }
            })

    return {
        "vroom":  vroom_result,
        "geojson": {"type": "FeatureCollection", "features": features}
    }


# ---------------------------------------------------------------------------
# Polyline decoder — precision 6 (Valhalla / VROOM)
# Returns [[lng, lat], ...]
# ---------------------------------------------------------------------------
def _decode_polyline6(encoded: str) -> list:
    coords, index, lat, lng = [], 0, 0, 0
    length = len(encoded)
    while index < length:
        result, shift = 0, 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        lat += ~(result >> 1) if (result & 1) else (result >> 1)

        result, shift = 0, 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        lng += ~(result >> 1) if (result & 1) else (result >> 1)

        coords.append([lng / 1e6, lat / 1e6])
    return coords


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=5000, log_level="info")