# GeoRouting Lab — System Workflow

## System Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│               Browser (MapLibre GL JS)               │
│   Route · VRP · Isochrone · Facility · Search        │
└───────────────┬─────────────────────┬───────────────┘
                │ /api/* and /es/*    │ tile requests
                ▼                     ▼
┌──────────────────────┐   ┌──────────────────────────┐
│   Nginx reverse      │   │   Martin  (port 3000)    │
│   proxy              │   │   vector tiles · styles  │
└───────────┬──────────┘   │   fonts · sprites        │
            │              └────────────┬─────────────┘
            │ /api/*                    │ auto-publish
            ▼                           │ topology schema
┌──────────────────────────────┐        │
│   FastAPI app  (port 5000)   │        ▼
│                              │   ┌─────────────────┐
│  POST /route_val             │   │   PostGIS DB    │
│  POST /optimize_route        │◄──►   topology.     │
│  POST /isochrone             │   │   places        │
│  GET  /nearest_facility_es   │   └─────────────────┘
│  GET  /nearest_facility_pg   │
│  GET  /health                │
└──┬──────┬──────┬──────┬──────┘
   │      │      │      │
   ▼      ▼      ▼      ▼
┌──────┐ ┌────┐ ┌──────┐ ┌───────────────┐
│Valha │ │VRO │ │Post  │ │Elasticsearch  │
│lla   │ │OM  │ │GIS   │ │pois index     │
│:8002 │ │:300│ │:5432 │ │:9200          │
└──────┘ └────┘ └──────┘ └───────────────┘
```

---

## Service Responsibilities

| Service | Role | Endpoints used |
|---|---|---|
| **Nginx** | Reverse proxy; routes `/api/*` → FastAPI, `/es/*` → Elasticsearch | — |
| **FastAPI** | Smart proxy + business logic layer | All `/api/*` |
| **Valhalla** | Turn-by-turn routing, travel-time matrix, isochrone polygons | `/route` `/sources_to_targets` `/isochrone` |
| **VROOM** | VRP / TSP stop-order optimisation | `POST /` |
| **PostGIS** | Spatial facility store (`topology.places`), also read by Martin | `ST_DWithin` + `<->` KNN |
| **Elasticsearch** | Facility POI index with `geo_point` for distance queries | `pois/_search` |
| **Martin** | Serves vector tiles, map styles, fonts, sprites | `/{source}/{z}/{x}/{y}` |

---

## Feature Flows

### 1. Route Comparison

```
Browser
  └─► POST /api/route_val  (×3 in parallel, different costing options)
        └─► Valhalla /route
              └─► encoded polyline6 geometry
                    └─► decoded in browser → 3 coloured lines on map
```

Three variants sent simultaneously via `Promise.allSettled`:
- **Fastest** — default costing
- **Avoid highways** — `use_highways: 0.05`
- **Shortest distance** — `shortest: true`

---

### 2. VRP / TSP Optimisation

```
Browser (user clicks stops, 2 s debounce)
  └─► POST /api/optimize_route
        ├─► VROOM  →  optimal stop order
        └─► Valhalla /route  (×N segments, in parallel)
              └─► per-segment polyline, coloured by origin stop
                    └─► drawn on map
```

VROOM returns the visit sequence; FastAPI then fetches road geometry for each leg from Valhalla and decodes the polyline6 shape into GeoJSON coordinates.

---

### 3. Isochrone

```
Browser
  └─► POST /api/isochrone
        └─► Valhalla /isochrone
              └─► GeoJSON polygon rings (one per time contour)
                    └─► fill + border layers added to map
```

Contour colours are stripped of `#` before sending (Valhalla requirement). Results are re-used on parameter change without a new map click.

---

### 4. Nearest Facility Search (3-step pipeline)

This is the most complex flow. A single user click triggers three sequential backend calls.

```
Browser  →  GET /api/nearest_facility_es  (or _pg)
              │   params: lon, lat, type, limit, radius
              │
              │  ── STEP 1: spatial candidates ──────────────────
              ├─► Elasticsearch  geo_distance filter + term filter
              │     sorted by crow-fly km, returns limit×3 hits
              │   — OR —
              ├─► PostGIS  ST_DWithin + KNN order (<->)
              │     returns limit×3 rows
              │
              │  ── STEP 2: re-rank by drive time ───────────────
              ├─► Valhalla /sources_to_targets  (matrix, 1 call)
              │     1 origin → N candidates
              │     sorts by travel_seconds, keeps top `limit`
              │
              │  ── STEP 3: fetch route geometry (parallel) ─────
              └─► asyncio.gather → Valhalla /route  ×N
                    one call per ranked facility
                    returns GeoJSON FeatureCollection per facility
                          │
                          ▼
                    JSON response to browser
                    facilities[] with:
                      - name, address, type
                      - travel_minutes, crow_distance_km
                      - route: FeatureCollection (per-maneuver segments)
```

> **Why `limit × 3` candidates?**  Crow-fly nearest ≠ drive-time nearest. A hospital 2 km away across a river may take longer to reach than one 3.5 km away on a direct road. Over-fetching then re-ranking with the Valhalla matrix ensures the truly closest facility by road always appears first.

---

### 5. Building Search (Elasticsearch direct)

```
Browser
  └─► POST /es/building_floors/_search  (via Nginx /es/* proxy)
        └─► multi_match query, fuzziness AUTO
              └─► hit list → user clicks result
                    └─► switch to BDF style → add fill-extrusion layer
                          └─► 3D highlight + popup on map
```

The browser queries Elasticsearch directly through the `/es/*` Nginx proxy — FastAPI is not involved. On result click, the BDF map style is activated (it contains indoor building layers) and the matched floor unit is rendered as a 3D extrusion.

---

### 6. Map Tiles (Martin, independent path)

```
Browser (MapLibre style load)
  └─► GET /styles/style.json  (from Martin /styles)
        └─► sources reference Martin tile URLs:
              http://martin:3000/topology.places/{z}/{x}/{y}
              http://martin:3000/street_centerline/{z}/{x}/{y}
              http://martin:3000/administrative/{z}/{x}/{y}
              … etc.

Martin reads from:
  ├─► PostGIS  (topology schema, auto-published as vector tiles)
  └─► PMTiles files  (/pmtiles volume mount)
```

Tile traffic bypasses FastAPI entirely. Martin auto-publishes every table in the `topology` schema as a separate tile source, which is how `topology.places` POIs appear on the map.

---

## Data Flow Summary

```
User action
    │
    ├── tile render ──────────────────────────► Martin ──► PostGIS / PMTiles
    │
    ├── building search ──────── Nginx /es/* ──► Elasticsearch
    │
    └── routing / facility ── Nginx /api/* ──► FastAPI
                                                   ├──► Valhalla  (routing, matrix, isochrone)
                                                   ├──► VROOM     (VRP/TSP optimisation)
                                                   ├──► PostGIS   (facility spatial query)
                                                   └──► Elasticsearch (facility geo search)
```
