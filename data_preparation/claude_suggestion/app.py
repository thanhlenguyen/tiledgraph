"""
Geo-Routing API
FastAPI proxy/orchestration layer in front of Valhalla (routing) and VROOM (VRP optimisation).

Key design decisions
--------------------
* A single shared httpx.AsyncClient is created at startup and closed at shutdown.
  Never use `async with client` inside route handlers — that closes the client
  after the first request and breaks all subsequent calls.
* All upstream errors are surfaced with the original HTTP status code so the
  browser sees a meaningful code (e.g. 422 from Valhalla) rather than 500.
* /optimize_route enriches VROOM output with per-step Valhalla geometry so the
  front-end receives GeoJSON LineStrings it can draw directly.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
import httpx
import json
from typing import Any

# ---------------------------------------------------------------------------
# Lifespan — create / close the shared HTTP client
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    # One client for the whole process — connection pooling, keep-alive, etc.
    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
    yield
    await client.aclose()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Geo-Routing API",
    description="FastAPI proxy for Valhalla (tiled graph routing) + VROOM (VRP optimisation)",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Tighten in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# Docker service names (resolved inside the Docker network)
VALHALLA_URL = "http://valhalla:8002"
VROOM_URL    = "http://vroom:3000"

# Will be set in lifespan
client: httpx.AsyncClient = None  # type: ignore


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {"message": "Routing & Optimisation API is running", "services": ["Valhalla", "VROOM"]}


@app.get("/health")
async def health():
    """Liveness probe — just confirms this container is up."""
    return {"status": "healthy"}


@app.get("/health/valhalla")
async def health_valhalla():
    """Check that Valhalla is responding."""
    try:
        resp = await client.get(f"{VALHALLA_URL}/status")
        return {"status": "ok", "valhalla": resp.json()}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Valhalla unreachable: {exc}")


@app.get("/health/vroom")
async def health_vroom():
    """Check that VROOM is responding."""
    try:
        resp = await client.get(f"{VROOM_URL}/health")
        return {"status": "ok", "vroom": resp.json()}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"VROOM unreachable: {exc}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _forward(method: str, url: str, payload: Any) -> Any:
    """
    Forward a JSON payload to an upstream service and return the parsed response.
    Raises HTTPException with the upstream status code on failure.
    """
    try:
        resp = await client.request(method, url, json=payload)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Upstream request failed: {exc}")

    if resp.status_code != 200:
        # Try to forward a structured error message
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)

    return resp.json()


# ---------------------------------------------------------------------------
# Valhalla endpoints
# ---------------------------------------------------------------------------

@app.post("/route")
async def valhalla_route(request: Request):
    """
    Point-to-point (or multi-point) route using Valhalla.

    Example minimal payload:
    {
      "locations": [
        {"lon": 106.6297, "lat": 10.8231},
        {"lon": 106.6600, "lat": 10.7769}
      ],
      "costing": "auto",
      "directions_options": {"language": "en-US"}
    }
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    return await _forward("POST", f"{VALHALLA_URL}/route", payload)


@app.post("/matrix")
async def valhalla_matrix(request: Request):
    """
    Time/distance cost matrix between many sources and targets.

    Example payload:
    {
      "sources": [{"lon": 106.62, "lat": 10.82}],
      "targets": [{"lon": 106.66, "lat": 10.77}, {"lon": 106.70, "lat": 10.80}],
      "costing": "auto"
    }
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    return await _forward("POST", f"{VALHALLA_URL}/matrix", payload)


@app.post("/isochrone")
async def valhalla_isochrone(request: Request):
    """
    Generate reachability polygons (isochrones / service areas).

    Example payload:
    {
      "locations": [{"lon": 106.6297, "lat": 10.8231}],
      "costing": "auto",
      "contours": [{"time": 5}, {"time": 10}, {"time": 15}],
      "polygons": true
    }
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    return await _forward("POST", f"{VALHALLA_URL}/isochrone", payload)


# ---------------------------------------------------------------------------
# VROOM endpoints
# ---------------------------------------------------------------------------

@app.post("/optimize")
async def vroom_optimize(request: Request):
    """
    Vehicle Routing Problem (VRP) optimisation via VROOM.
    VROOM uses Valhalla for travel-time calculations internally.

    The geometry key in the response is an encoded polyline (precision 6).
    Set  "options": {"g": true}  in the payload to request geometry,
    or use /optimize_route which enriches results with GeoJSON automatically.

    Minimal example payload:
    {
      "vehicles": [{"id": 1, "start": [106.629, 10.823], "end": [106.629, 10.823], "profile": "car"}],
      "jobs": [
        {"id": 1, "location": [106.660, 10.776]},
        {"id": 2, "location": [106.700, 10.800]}
      ],
      "options": {"g": true}
    }
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Always request geometry so the front-end can draw the route
    if "options" not in payload:
        payload["options"] = {}
    payload["options"]["g"] = True

    return await _forward("POST", VROOM_URL, payload)


@app.post("/optimize_route")
async def optimize_and_get_geometry(request: Request):
    """
    Optimise with VROOM, then decode the encoded-polyline geometry into
    GeoJSON so the front-end can render it directly with MapLibre.

    Response shape:
    {
      "vroom": { ...original VROOM response... },
      "geojson": {
        "type": "FeatureCollection",
        "features": [
          {
            "type": "Feature",
            "geometry": { "type": "LineString", "coordinates": [[lng,lat], ...] },
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

    # Build GeoJSON FeatureCollection from each vehicle's route geometry
    features = []
    for route in vroom_result.get("routes", []):
        geometry_encoded = route.get("geometry")
        if geometry_encoded:
            coordinates = _decode_polyline6(geometry_encoded)
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": coordinates
                },
                "properties": {
                    "vehicle_id": route.get("vehicle"),
                    "duration":   route.get("duration"),
                    "distance":   route.get("distance"),
                    "steps":      route.get("steps", [])
                }
            })

    return {
        "vroom": vroom_result,
        "geojson": {
            "type": "FeatureCollection",
            "features": features
        }
    }


# ---------------------------------------------------------------------------
# Polyline decoder (precision 6 — used by Valhalla / VROOM)
# ---------------------------------------------------------------------------

def _decode_polyline6(encoded: str) -> list[list[float]]:
    """
    Decode a Google-style encoded polyline with precision 6.
    Returns [[lng, lat], ...] (MapLibre / GeoJSON order).
    """
    coordinates = []
    index = 0
    lat = 0
    lng = 0
    length = len(encoded)

    while index < length:
        # Latitude
        result, shift = 0, 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlat = ~(result >> 1) if (result & 1) else (result >> 1)
        lat += dlat

        # Longitude
        result, shift = 0, 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlng = ~(result >> 1) if (result & 1) else (result >> 1)
        lng += dlng

        # Precision 6: divide by 1e6
        coordinates.append([lng / 1e6, lat / 1e6])

    return coordinates


# ---------------------------------------------------------------------------
# Entry point (for `python app.py` during development)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=5000, log_level="info", reload=False)
