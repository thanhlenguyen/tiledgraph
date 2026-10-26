"""
GeoRouting Lab — agent.py
=========================
The "agentic layer". A user types a question in plain Arabic or English
("nearest hospital to my point", "15-minute drive area from here"), and an
AI model (Claude) decides which GIS functions to call, calls them, and writes
a short answer. The map then draws the results.

HOW IT FITS INTO YOUR SYSTEM
────────────────────────────
  Browser (agent.js)
     │  POST /api/agent/chat   { messages, context }
     ▼
  Nginx  ──►  FastAPI (app.py + this router)
                 │
                 ├─► Claude API        "Which tool should I use?"
                 │
                 └─► YOUR EXISTING CODE (no HTTP hop, called in-process):
                        • Valhalla  /route, /isochrone      via core._forward
                        • VROOM     optimisation            via core._forward
                        • PostGIS   nearest facility        via core.nearest_facility_pg
                        • Elasticsearch address / POI search

THE AGENT LOOP (the whole idea in 4 lines)
──────────────────────────────────────────
  1. Send the conversation + the list of TOOLS to Claude.
  2. If Claude answers with "tool_use", run that tool and send the result back.
  3. Repeat until Claude answers with plain text (or MAX_STEPS is reached).
  4. Return { reply text, map layers } to the browser.

Nothing in app.py changes except two lines at the bottom that plug this in.
"""

# =============================================================================
# IMPORTS
# =============================================================================

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict, deque

import anthropic
from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, Field

import analytics     # Phase new tools: coverage, hotspots, site selection, …
import app as core   # your existing app.py — we reuse its client, helpers and endpoints

logger = logging.getLogger("georouting.agent")


# =============================================================================
# CONFIGURATION (override with environment variables in docker-compose)
# =============================================================================

# Which "brain" decides the tools:
#   anthropic → Claude API             (needs ANTHROPIC_API_KEY)
#   openai    → any OpenAI-compatible server: Ollama, vLLM, LM Studio … (free, self-hosted)
#   mock      → simple keyword rules, no AI at all (for testing the wiring and the UI)
# Default: anthropic if a key is set, otherwise mock.
AGENT_PROVIDER = os.getenv("AGENT_PROVIDER",
                           "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "mock").lower()
AGENT_MODEL    = os.getenv("AGENT_MODEL") or {"anthropic": "claude-sonnet-5-5",
                                              "openai": "qwen2.5:7b"}.get(AGENT_PROVIDER, "rules")
LLM_BASE_URL   = os.getenv("AGENT_BASE_URL", "http://ollama:11434/v1")   # for provider "openai"
LLM_API_KEY    = os.getenv("AGENT_API_KEY", "ollama")                    # Ollama ignores it
DEBUG_TOOLS    = os.getenv("AGENT_DEBUG_TOOLS", "1") == "1"   # /agent/tool/{name}; set 0 in production
MAX_STEPS         = int(os.getenv("AGENT_MAX_STEPS", "8"))     # max tool rounds per question
MAX_HISTORY       = 20                                          # chat messages kept per request
MAX_MESSAGE_CHARS = 2000                                        # reject very long inputs
RATE_LIMIT_PER_MIN = int(os.getenv("AGENT_RATE_LIMIT_PER_MIN", "20"))  # per client IP
LOG_FILE          = os.getenv("AGENT_LOG_FILE", "agent_log.jsonl")     # one JSON line per question

# Elasticsearch indices used by your Building Search (same as scripts.js)
ES_ADDRESS_INDICES = {
    "building_floors": ["properties.VerticalAddress^3", "properties.ShortAddress^1.8"],
    "building_units":  ["properties.UnitAddres^3", "properties.LabelNames^1.5", "properties.NAME"],
}

# Same palettes as scripts.js so agent results look like the rest of the app
ISO_COLORS     = ["#22d98a", "#3b9eff", "#f5a623", "#f43f5e"]
ROUTE_COLOR    = "#3b9eff"
VEHICLE_COLORS = ["#f43f5e", "#22d3ee", "#facc15", "#818cf8", "#4ade80",
                  "#fb923c", "#e879f9", "#86efac", "#f9a8d4", "#fde68a"]
FACILITY_COLORS = {"hospital": "#ef4444", "fire station": "#f97316",
                   "police": "#8b5cf6", "clinic": "#10b981"}

router = APIRouter(prefix="/agent", tags=["Agent"])

_llm: anthropic.AsyncAnthropic | None = None


def _get_llm() -> anthropic.AsyncAnthropic:
    """Create the Claude client the first time it is needed."""
    global _llm
    if _llm is None:
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise HTTPException(503, "Agent is not configured: set ANTHROPIC_API_KEY.")
        _llm = anthropic.AsyncAnthropic()
    return _llm


# =============================================================================
# SYSTEM PROMPT — the agent's "job description"
# Change the wording here to change how the agent behaves. Test after edits.
# =============================================================================

SYSTEM_PROMPT = f"""You are the map assistant inside GeoRouting Lab, a GIS tool for Saudi Arabia
used by logistics, healthcare and government teams. You answer by calling tools, never by guessing.

Coordinates
- Every point is {{"lon": <longitude>, "lat": <latitude>}}. In Saudi Arabia lon is about 34-56 and lat about 16-33.
- Never invent coordinates. Get them from: the user's picked point, a tool result, or numbers the user typed.
- "here", "my point", "this location", "هنا", "النقطة" mean the picked point from the context.
  If there is no picked point, ask the user to press "Pick point" and click the map. Do not use the map center
  unless the user explicitly says "map center" or "the area I'm looking at".
- To find an address, building, floor or unit, use search_address. To find a named place, use search_places.
  If several results fit, list the top 2-3 and ask which one.

Tools and limits
- Facility types available: {", ".join(sorted(core._ALLOWED_FACILITY_TYPES))}. Search radius 1-20 km.
- Travel profiles: auto (car), bicycle, pedestrian. Default to auto.
- Isochrone: up to 4 time bands, each 1-60 minutes.
- optimize_stops: the FIRST stop is the start/depot. It returns a draft plan only; nothing is dispatched.
- If a request is outside these tools, say what you can do instead.

Area analytics (area_heatmap, coverage_analysis, find_hotspots, suggest_sites, delivery_point_corrections)
- These need an "area". If the user names none, use the current map view: {{"bbox": <Map bbox from context>}}.
  For "around my point" use {{"center": <picked point>, "radius_km": 3-10}}.
- If a tool says the area is too large, ask the user to zoom in.
- suggest_sites ranks candidate locations by travel time only. Always say that land, zoning and cost
  still need checking on site. Never present them as final decisions.
- Mention the time window (days) behind hotspot or failure numbers.

Answer style
- Reply in the user's language (Arabic or English).
- The map already shows the geometry, so do not list coordinates. Give the key numbers first
  (minutes, km, names), then at most 2 short sentences. Use a short numbered list only for rankings.
"""


# =============================================================================
# TOOL SCHEMAS — what Claude "sees". Good descriptions = good tool choices.
# =============================================================================

POINT = {
    "type": "object",
    "properties": {"lon": {"type": "number"}, "lat": {"type": "number"}},
    "required": ["lon", "lat"],
}
PROFILE = {"type": "string", "enum": ["auto", "bicycle", "pedestrian"], "default": "auto"}

TOOLS = [
    {
        "name": "search_address",
        "description": (
            "Search the National Address building data for a Vertical Address, Short Address, unit "
            "address, or unit name. Returns up to 5 matches with a representative point. "
            "Use dataset 'building_floors' for vertical/short addresses (default) and "
            "'building_units' for indoor units (SPL units, shop or office names)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query":   {"type": "string", "description": "Address text or code, Arabic or English"},
                "dataset": {"type": "string", "enum": list(ES_ADDRESS_INDICES), "default": "building_floors"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_places",
        "description": (
            "Find points of interest (hospitals, clinics, police, fire stations, and other places) by NAME. "
            "Optionally restrict to a facility type or to a radius around a point. "
            "Use this when the user names a specific place, e.g. 'King Faisal hospital'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name":          {"type": "string"},
                "facility_type": {"type": "string", "description": "Optional exact type, e.g. 'hospital'"},
                "near":          POINT,
                "radius_km":     {"type": "number", "default": 30},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_route",
        "description": (
            "Driving, cycling or walking route between two points (optionally via waypoints). "
            "Returns distance, travel time and the main turn instructions. "
            "preference: 'fastest' (default), 'avoid_highways', or 'shortest'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origin":      POINT,
                "destination": POINT,
                "via":         {"type": "array", "items": POINT, "maxItems": 10},
                "profile":     PROFILE,
                "preference":  {"type": "string", "enum": ["fastest", "avoid_highways", "shortest"],
                                "default": "fastest"},
            },
            "required": ["origin", "destination"],
        },
    },
    {
        "name": "compute_isochrone",
        "description": (
            "Area reachable from a point within given travel times (service area / catchment). "
            "Use for coverage, accessibility and 'how far can I get in N minutes' questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origin":  POINT,
                "minutes": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 60},
                            "minItems": 1, "maxItems": 4, "description": "e.g. [10, 20, 30]"},
                "profile": PROFILE,
            },
            "required": ["origin", "minutes"],
        },
    },
    {
        "name": "find_nearest_facility",
        "description": (
            "Nearest facilities of one type to a point, ranked by real DRIVING time (not straight line), "
            "with the driving route to each. Types: "
            + ", ".join(sorted(core._ALLOWED_FACILITY_TYPES)) + "."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origin":          POINT,
                "facility_type":   {"type": "string", "enum": sorted(core._ALLOWED_FACILITY_TYPES)},
                "limit":           {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
                "max_distance_km": {"type": "number", "minimum": 1, "maximum": 20, "default": 5},
            },
            "required": ["origin", "facility_type"],
        },
    },
    {
        "name": "optimize_stops",
        "description": (
            "Best visiting order for a list of stops (VROOM). The FIRST stop is the depot/start. "
            "mode 'vrp' returns to the depot at the end, 'tsp' ends at the last stop. "
            "vehicle_count > 1 splits the stops between several vehicles from the same depot. "
            "Produces a DRAFT plan only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "stops": {
                    "type": "array", "minItems": 2, "maxItems": 100,
                    "items": {
                        "type": "object",
                        "properties": {"lon": {"type": "number"}, "lat": {"type": "number"},
                                       "label": {"type": "string"}},
                        "required": ["lon", "lat"],
                    },
                },
                "mode":          {"type": "string", "enum": ["vrp", "tsp"], "default": "vrp"},
                "profile":       PROFILE,
                "vehicle_count": {"type": "integer", "minimum": 1, "maximum": 10, "default": 1},
            },
            "required": ["stops"],
        },
    },
]


# =============================================================================
# SMALL HELPERS
# =============================================================================

class ToolError(Exception):
    """Raised inside a tool for a problem the model should see and fix."""


def _pt(p: dict, name: str = "point") -> tuple[float, float]:
    """Read and validate a {lon, lat} dict. Returns (lon, lat)."""
    try:
        lon, lat = float(p["lon"]), float(p["lat"])
    except (KeyError, TypeError, ValueError):
        raise ToolError(f"{name} must be {{'lon': number, 'lat': number}}")
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ToolError(f"{name} is out of range. Did you swap lon and lat?")
    return lon, lat


def _fc(features: list) -> dict:
    return {"type": "FeatureCollection", "features": features}


def _point_feature(lon, lat, **props) -> dict:
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props}


def _centroid(geometry: dict) -> tuple[float, float] | None:
    """Average of all vertices — good enough to put a pin on a building or floor."""
    xs, ys = [], []

    def walk(c):
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            xs.append(c[0]); ys.append(c[1])
        elif isinstance(c, (list, tuple)):
            for x in c:
                walk(x)

    walk((geometry or {}).get("coordinates", []))
    return (round(sum(xs) / len(xs), 6), round(sum(ys) / len(ys), 6)) if xs else None


def _es_point(loc) -> tuple[float, float] | None:
    """Read an Elasticsearch geo field in any of its 3 formats (same logic as app.py)."""
    if isinstance(loc, str):
        a, b = loc.split(",")
        return float(b), float(a)
    if isinstance(loc, dict) and "coordinates" in loc:
        return float(loc["coordinates"][0]), float(loc["coordinates"][1])
    if isinstance(loc, dict) and "lat" in loc:
        return float(loc["lon"]), float(loc["lat"])
    return None


def _layer(kind: str, label: str, geojson: dict, color: str | None = None) -> dict:
    """
    One drawable thing for the map. kind is one of:
      'line'    → routes        (drawn as a MapLibre line layer)
      'polygon' → isochrones    (fill + outline)
      'points'  → pins          (HTML markers with a popup)
    """
    return {"kind": kind, "label": label, "color": color, "geojson": geojson}


# =============================================================================
# TOOL IMPLEMENTATIONS
# Each returns (summary_for_model, layers_for_map).
# Keep summaries SMALL: the model only needs names, numbers and coordinates.
# Big geometries go to the map, not to the model (saves cost and time).
# =============================================================================

async def tool_search_address(query: str, dataset: str = "building_floors"):
    if dataset not in ES_ADDRESS_INDICES:
        raise ToolError(f"dataset must be one of {list(ES_ADDRESS_INDICES)}")
    body = {
        "query": {"multi_match": {"query": query, "fields": ES_ADDRESS_INDICES[dataset],
                                  "type": "best_fields", "fuzziness": "AUTO"}},
        "size": 5,
        "_source": ["geometry", "properties"],
    }
    resp = await core.client.post(f"{core.ES_URL}/{dataset}/_search", json=body, timeout=15)
    if resp.status_code != 200:
        raise ToolError(f"Address search failed (HTTP {resp.status_code})")

    results, pins = [], []
    for i, hit in enumerate(resp.json().get("hits", {}).get("hits", []), start=1):
        src, props = hit["_source"], hit["_source"].get("properties", {})
        c = _centroid(src.get("geometry"))
        if not c:
            continue
        if dataset == "building_floors":
            label = props.get("VerticalAddress") or props.get("ShortAddress") or "—"
            info = {"vertical_address": props.get("VerticalAddress"),
                    "short_address": props.get("ShortAddress"),
                    "floor": props.get("FloorID"), "usage": props.get("FloorUsage")}
        else:
            label = props.get("UnitAddres") or props.get("LabelNames") or props.get("UNIT_ID") or "—"
            info = {"unit_id": props.get("UNIT_ID"), "name": props.get("LabelNames") or props.get("NAME"),
                    "unit_address": props.get("UnitAddres"), "use_type": props.get("USE_TYPE")}
        info.update({"rank": i, "lon": c[0], "lat": c[1], "match_score": round(hit.get("_score") or 0, 2)})
        results.append(info)
        pins.append(_point_feature(c[0], c[1], label=str(i), title=label))

    if not results:
        return {"count": 0, "message": f"No match for '{query}' in {dataset}."}, []
    return ({"count": len(results), "results": results},
            [_layer("points", f"Address: {query}", _fc(pins), "#a855f7")])


async def tool_search_places(name: str, facility_type: str | None = None,
                             near: dict | None = None, radius_km: float = 30):
    filters = []
    if facility_type:
        filters.append({"term": {"properties.type.keyword": facility_type.lower().strip()}})
    if near:
        lon, lat = _pt(near, "near")
        filters.append({"geo_distance": {"distance": f"{min(max(radius_km, 1), 200)}km",
                                         "geometry": {"lat": lat, "lon": lon}}})
    body = {
        "size": 5,
        "query": {"bool": {
            "must": [{"multi_match": {"query": name, "fuzziness": "AUTO",
                                      "fields": ["properties.name^2", "properties.name_ar^2",
                                                 "properties.address"]}}],
            "filter": filters,
        }},
        "_source": ["geometry", "properties"],
    }
    resp = await core.client.post(f"{core.ES_URL}/{core.ES_FACILITY_INDEX}/_search", json=body, timeout=15)
    if resp.status_code != 200:
        raise ToolError(f"Place search failed (HTTP {resp.status_code})")

    results, pins = [], []
    for i, hit in enumerate(resp.json().get("hits", {}).get("hits", []), start=1):
        props = hit["_source"].get("properties", {})
        p = _es_point(hit["_source"].get("geometry"))
        if not p:
            continue
        title = props.get("name") or props.get("name_ar") or "Unknown"
        results.append({"rank": i, "name": title, "type": props.get("type"),
                        "address": props.get("address") or props.get("address_ar"),
                        "lon": round(p[0], 6), "lat": round(p[1], 6)})
        pins.append(_point_feature(p[0], p[1], label=str(i), title=title))

    if not results:
        return {"count": 0, "message": f"No place named '{name}' found."}, []
    return ({"count": len(results), "results": results},
            [_layer("points", f"Places: {name}", _fc(pins), "#22d3ee")])


async def tool_get_route(origin: dict, destination: dict, via: list | None = None,
                         profile: str = "auto", preference: str = "fastest"):
    points = [_pt(origin, "origin"), *[_pt(v, "via") for v in (via or [])], _pt(destination, "destination")]

    # Same three variants as the Route tab in scripts.js
    options = {"fastest": {},
               "avoid_highways": {"use_highways": 0.05, "use_tolls": 0.0} if profile == "auto" else {},
               "shortest": {"shortest": True}}[preference]

    data = await core._forward("POST", f"{core.VALHALLA_URL}/route", {
        "locations": [{"lon": lo, "lat": la} for lo, la in points],
        "costing": profile,
        "costing_options": {profile: options},
        "units": "km",
        "directions_options": {"language": "en-US"},
    })
    trip = data.get("trip", {})
    coords, steps = [], []
    for leg in trip.get("legs", []):
        coords += core._parse_shape(leg)
        steps += [m.get("instruction") for m in leg.get("maneuvers", []) if m.get("instruction")]

    summary = trip.get("summary", {})
    result = {
        "distance_km": round(summary.get("length", 0), 1),
        "duration_min": round(summary.get("time", 0) / 60, 1),
        "profile": profile, "preference": preference,
        "main_instructions": steps[:8],   # first few turns are enough for the answer
    }
    line = _fc([{"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
                 "properties": {"color": ROUTE_COLOR}}])
    pins = _fc([_point_feature(*points[0], label="S", title="Start"),
                _point_feature(*points[-1], label="E", title="End")])
    return result, [_layer("line", f"Route ({preference})", line, ROUTE_COLOR),
                    _layer("points", "Route ends", pins, "#22d98a")]


async def tool_compute_isochrone(origin: dict, minutes: list, profile: str = "auto"):
    lon, lat = _pt(origin, "origin")
    bands = sorted({int(m) for m in minutes if 1 <= int(m) <= 60})[:4]
    if not bands:
        raise ToolError("minutes must contain values between 1 and 60")

    geojson = await core._forward("POST", f"{core.VALHALLA_URL}/isochrone", {
        "locations": [{"lon": lon, "lat": lat}],
        "costing": profile,
        "contours": [{"time": m, "color": ISO_COLORS[i].lstrip("#")} for i, m in enumerate(bands)],
        "polygons": True, "denoise": 0.5, "generalize": 50,
    })
    # Valhalla may return colors with or without '#'; MapLibre needs '#'
    for f in geojson.get("features", []):
        c = str(f.get("properties", {}).get("color", ""))
        if c and not c.startswith("#"):
            f["properties"]["color"] = "#" + c

    pin = _fc([_point_feature(lon, lat, label="◉", title="Origin")])
    return ({"bands_minutes": bands, "profile": profile, "polygons": len(geojson.get("features", []))},
            [_layer("polygon", f"Reachable in {', '.join(map(str, bands))} min", geojson),
             _layer("points", "Isochrone origin", pin, "#3b9eff")])


async def tool_find_nearest_facility(origin: dict, facility_type: str, limit: int = 3,
                                     max_distance_km: float = 5):
    lon, lat = _pt(origin, "origin")
    # Call your existing endpoint function directly (no extra HTTP request).
    # FastAPI's Query(ge=..., le=...) checks don't run on direct calls, so clamp here.
    data = await core.nearest_facility_pg(
        lon=lon, lat=lat, type=facility_type,
        limit=int(min(max(limit, 1), 10)),
        max_distance_km=float(min(max(max_distance_km, 1), 20)),
        routes=True,
    )
    facilities = data.get("facilities", [])
    if not facilities:
        return {"count": 0, "message": data.get("message", "None found."),
                "hint": "Try a larger max_distance_km (up to 20)."}, []

    color = FACILITY_COLORS.get(facility_type, "#ef4444")
    route_feats, pins, summary = [], [], []
    for f in facilities:
        rank = len(summary) + 1
        for feat in (f.get("route") or {}).get("features", []):
            feat["properties"]["facility_rank"] = rank
            route_feats.append(feat)
        pins.append(_point_feature(f["facility_lon"], f["facility_lat"], label=str(rank),
                                   title=f"{f['name']} · {f['travel_minutes']} min"))
        summary.append({"rank": rank, "name": f["name"], "address": f.get("address"),
                        "drive_minutes": f["travel_minutes"], "straight_line_km": float(f["crow_distance_km"]),
                        "lon": round(f["facility_lon"], 6), "lat": round(f["facility_lat"], 6)})

    pins.append(_point_feature(lon, lat, label="◉", title="Search origin"))
    return ({"facility_type": facility_type, "count": len(summary), "facilities": summary},
            [_layer("line", f"Routes to {facility_type}", _fc(route_feats), color),
             _layer("points", f"Nearest {facility_type}", _fc(pins), color)])


async def tool_optimize_stops(stops: list, mode: str = "vrp", profile: str = "auto", vehicle_count: int = 1):
    if len(stops) < 2:
        raise ToolError("Need at least 2 stops (first stop = depot).")
    pts = [_pt(s, f"stop {i + 1}") for i, s in enumerate(stops)]
    labels = [s.get("label") or f"Stop {i}" for i, s in enumerate(stops)]
    labels[0] = stops[0].get("label") or "Depot"
    depot = list(pts[0])

    vehicles = [{"id": v + 1, "profile": profile, "start": depot, **({"end": depot} if mode == "vrp" else {})}
                for v in range(int(min(max(vehicle_count, 1), 10)))]
    jobs = [{"id": i, "location": list(p), "service": 30} for i, p in enumerate(pts) if i > 0]

    data = await core._forward("POST", core.VROOM_URL,
                               {"vehicles": vehicles, "jobs": jobs, "options": {"g": True}})

    layers, plan = [], []
    for r in data.get("routes", []):
        color = VEHICLE_COLORS[(r["vehicle"] - 1) % len(VEHICLE_COLORS)]
        order = [s["id"] for s in r.get("steps", []) if s.get("type") == "job"]
        plan.append({"vehicle": r["vehicle"],
                     "visit_order": [labels[j] for j in order],
                     "duration_min": round(r.get("duration", 0) / 60, 1),
                     "distance_km": round(r.get("distance", 0) / 1000, 1)})
        if r.get("geometry"):
            line = _fc([{"type": "Feature",
                         "geometry": {"type": "LineString", "coordinates": core._decode_polyline6(r["geometry"])},
                         "properties": {"color": color}}])
            layers.append(_layer("line", f"Vehicle {r['vehicle']}", line, color))
        pins = [_point_feature(*pts[j], label=str(n), title=f"V{r['vehicle']} · #{n} {labels[j]}")
                for n, j in enumerate(order, start=1)]
        layers.append(_layer("points", f"Vehicle {r['vehicle']} stops", _fc(pins), color))

    layers.append(_layer("points", "Depot", _fc([_point_feature(*pts[0], label="D", title=labels[0])]), "#0f172a"))
    unassigned = [labels[u["id"]] for u in data.get("unassigned", [])]
    return ({"mode": mode, "vehicles": plan, "unassigned": unassigned,
             "total_duration_min": round(data.get("summary", {}).get("duration", 0) / 60, 1),
             "note": "Draft plan only. Not dispatched."}, layers)


# Tool name → function. Add a new tool = add a schema in TOOLS + a line here.
TOOL_FUNCS = {
    "search_address":        tool_search_address,
    "search_places":         tool_search_places,
    "get_route":             tool_get_route,
    "compute_isochrone":     tool_compute_isochrone,
    "find_nearest_facility": tool_find_nearest_facility,
    "optimize_stops":        tool_optimize_stops,
}

# Phase 3 + 5 analytics tools live in analytics.py (read-only for the agent)
TOOLS += analytics.AGENT_TOOLS
TOOL_FUNCS.update(analytics.AGENT_FUNCS)


async def run_tool(name: str, args: dict) -> tuple[dict, list]:
    """Run one tool safely. Errors become a message the model can read and recover from."""
    func = TOOL_FUNCS.get(name)
    if not func:
        return {"error": f"Unknown tool '{name}'"}, []
    try:
        return await func(**args)
    except ToolError as e:
        return {"error": str(e)}, []
    except HTTPException as e:                       # raised by core._forward / endpoints
        return {"error": f"Service error {e.status_code}: {str(e.detail)[:300]}"}, []
    except TypeError as e:                           # model sent an unexpected argument
        return {"error": f"Bad arguments for {name}: {e}"}, []
    except Exception as e:
        logger.exception("Tool %s crashed", name)
        return {"error": f"{name} failed: {type(e).__name__}"}, []


# =============================================================================
# REQUEST / RESPONSE MODELS
# =============================================================================

class ChatMessage(BaseModel):
    role: str                     # "user" | "assistant"
    content: str


class ChatContext(BaseModel):
    picked_point: dict | None = None    # {"lon","lat"} the user clicked with "Pick point"
    map_center:   dict | None = None    # {"lon","lat"} current view center
    map_zoom:     float | None = None
    map_bbox:     list[float] | None = None # [west, south, east, north] of the current view
    last_results: list | None = None    # compact tool results from the previous answer


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., min_length=1)
    context:  ChatContext = ChatContext()


# =============================================================================
# SIMPLE RATE LIMIT (protects your API bill). In-memory, per client IP.
# For several API replicas, move this to Redis or to Nginx limit_req.
# =============================================================================

_hits: dict[str, deque] = defaultdict(deque)


def _check_rate_limit(ip: str) -> None:
    now, q = time.time(), _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MIN:
        raise HTTPException(429, "Too many questions. Wait a minute and try again.")
    q.append(now)


def _log(record: dict) -> None:
    """Append one JSON line. These logs become your test set and show data gaps."""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        logger.warning("Could not write agent log: %s", e)


# =============================================================================
# SHARED: run a batch of tool calls and collect results for the map
# =============================================================================

class RunState:
    """Everything collected while answering one question."""
    def __init__(self):
        self.layers, self.trace, self.compact = [], [], []


async def _execute(calls: list[tuple[str, str, dict]], st: RunState) -> list[tuple[str, dict]]:
    """calls = [(call_id, tool_name, args)]. Runs them in parallel. Returns [(call_id, result)]."""
    t0 = time.time()
    outcomes = await asyncio.gather(*[run_tool(name, args) for _, name, args in calls])
    out = []
    for (cid, name, args), (result, new_layers) in zip(calls, outcomes):
        ok = "error" not in result
        st.trace.append({"tool": name, "input": args, "ok": ok, "ms": int((time.time() - t0) * 1000)})
        st.layers.extend(new_layers)
        if ok:
            st.compact.append({"tool": name, "result": result})
        out.append((cid, result))
    return out


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


# =============================================================================
# BRAIN 1 — Claude (Anthropic API)
# =============================================================================

async def _loop_anthropic(messages: list, st: RunState) -> str | None:
    for _ in range(MAX_STEPS):
        try:
            resp = await _get_llm().messages.create(
                model=AGENT_MODEL, max_tokens=1500, system=SYSTEM_PROMPT,
                tools=TOOLS, messages=messages,
            )
        except anthropic.APIError as e:
            logger.error("Claude API error: %s", e)
            raise HTTPException(502, "The AI service is unavailable. Try again shortly.")

        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip()

        calls = [(b.id, b.name, b.input) for b in resp.content if b.type == "tool_use"]
        results = await _execute(calls, st)
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": cid, "content": _dumps(r), "is_error": "error" in r}
            for cid, r in results]})
    return None


# =============================================================================
# BRAIN 2 — Any OpenAI-compatible server (Ollama, vLLM, LM Studio …)
# Same loop, different message format. Free and runs on your own server.
# =============================================================================

OPENAI_TOOLS = [{"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["input_schema"]}} for t in TOOLS]


async def _loop_openai(messages: list, st: RunState) -> str | None:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
    for _ in range(MAX_STEPS):
        try:
            r = await core.client.post(
                f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={"model": AGENT_MODEL, "messages": msgs, "tools": OPENAI_TOOLS, "temperature": 0.1},
                timeout=180,
            )
        except Exception as e:
            raise HTTPException(502, f"Model server unreachable at {LLM_BASE_URL}: {type(e).__name__}")
        if r.status_code != 200:
            raise HTTPException(502, f"Model server error {r.status_code}: {r.text[:200]}")

        msg = r.json()["choices"][0]["message"]
        tool_calls = msg.get("tool_calls") or []
        msgs.append({"role": "assistant", "content": msg.get("content") or "", **({"tool_calls": tool_calls} if tool_calls else {})})
        if not tool_calls:
            return (msg.get("content") or "").strip()

        calls = []
        for i, tc in enumerate(tool_calls):
            raw = tc.get("function", {}).get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                args = {"_invalid_json": raw}          # run_tool turns this into an error message
            calls.append((tc.get("id") or f"call_{i}", tc["function"]["name"], args))

        for cid, res in await _execute(calls, st):
            msgs.append({"role": "tool", "tool_call_id": cid, "content": _dumps(res)})
    return None


# =============================================================================
# BRAIN 3 — Mock: keyword rules, no AI. Tests tools, map drawing and the UI.
# It understands only a few patterns (English + Arabic):
#   "RRRD2929"                     → search_address
#   "... 10 20 30 minutes ..."     → compute_isochrone from the picked point
#   "nearest hospital/clinic/…"    → find_nearest_facility from the picked point
#   "route to the first/that one"  → get_route to the first result of the last answer
# =============================================================================

MOCK_FACILITY_WORDS = {
    "hospital":     ["hospital", "مستشفى"],
    "clinic":       ["clinic", "عيادة", "مستوصف"],
    "police":       ["police", "شرطة"],
    "fire station": ["fire", "إطفاء", "دفاع مدني"],
}
MOCK_ISO_WORDS   = ["minute", " min", "isochrone", "reach", "drive area", "دقيقة", "دقائق", "نطاق"]
MOCK_ROUTE_WORDS = ["route", "directions", "go to", "طريق", "مسار"]
MOCK_HELP = ("Test mode (no AI model connected). I understand: a short address like RRRD2929, "
             "'10 20 30 minute drive area', 'nearest hospital/clinic/police/fire station', "
             "'route to the first one' (pick a point first for these), and for the current map view: "
             "'hot spots', 'hospital coverage 15 min', 'suggest 5 sites', 'demand heatmap', 'wrong pins'.")
MOCK_ANALYTICS = [   # (words, tool) — checked in order, against the current map view
    (["hot spot", "hotspot", "بؤر", "fail"], "find_hotspots"),
    (["coverage", "share", "percent", "%", "تغطية", "نسبة"], "coverage_analysis"),
    (["site", "locker", "where should", "new branch", "موقع", "مواقع"], "suggest_sites"),
    (["pin", "offset", "delivery point", "wrong location", "دبوس"], "delivery_point_corrections"),
    (["heatmap", "heat map", "demand", "density", "كثافة", "طلب"], "area_heatmap"),
]


def _first_point(last_results: list | None):
    for item in (last_results or []):
        res = item.get("result", {})
        for key in ("facilities", "results"):
            for row in res.get(key, []):
                if "lon" in row and "lat" in row:
                    return {"lon": row["lon"], "lat": row["lat"]}, row.get("name") or row.get("vertical_address")
    return None, None


async def _loop_mock(question: str, ctx, st: RunState) -> str:
    q, pt = question.lower(), ctx.picked_point
    need_point = "Press 📍 Pick point and click the map first."

    async def call(name, args):
        (_, result), = await _execute([("mock", name, args)], st)
        return result

    # 0. Area analytics on the current map view
    tool = next((t for words, t in MOCK_ANALYTICS if any(w in q for w in words)), None)
    if tool:
        if not ctx.map_bbox:
            return "No map view received. Reload the page and try again."
        args = {"area": {"bbox": ctx.map_bbox}}
        ftype = next((t for t, words in MOCK_FACILITY_WORDS.items() if any(w in q for w in words)), "hospital")
        nums = [int(n) for n in re.findall(r"\d+", q)]
        if tool == "coverage_analysis":
            args.update(facility_type=ftype, minutes=next((n for n in nums if 1 <= n <= 60), 15))
        elif tool == "suggest_sites":
            args.update(n_sites=next((n for n in nums if 1 <= n <= 20), 5))
        r = await call(tool, args)
        if "error" in r:
            return r["error"]
        if tool == "coverage_analysis":
            return (f"**{r['coverage_pct']}%** of demand is within {r['minutes']} min of a {r['facility_type']} "
                    f"({r['facilities_considered']} facilities considered).")
        if tool == "find_hotspots":
            reason = (r.get("failure_reasons_in_hotspots") or [{}])[0].get("reason", "unknown")
            return f"**{r['hot_cells_95']}** hot-spot hexagons (95%+) in the last {r['days']} days. Top reason: {reason}."
        if tool == "suggest_sites":
            return (f"Coverage {r['coverage_before_pct']}% → **{r['coverage_after_pct']}%** with "
                    f"{len(r['sites'])} new sites. Check land and zoning before deciding.")
        if tool == "delivery_point_corrections":
            return f"**{r['addresses']}** addresses where drivers deliver far from the pin (median {r['median_offset_m']} m)."
        return f"Heatmap drawn: {r.get('cells', 0)} hexagons."

    # 1. Short address code, e.g. RRRD2929
    code = re.search(r"\b[a-z]{4}\d{4}\b", q)
    if code:
        r = await call("search_address", {"query": code.group().upper()})
        if "error" in r or not r.get("count"):
            return r.get("error") or r.get("message")
        return f"Found {r['count']} match(es): " + "; ".join(
            f"{x['rank']}) {x.get('vertical_address') or x.get('short_address')}" for x in r["results"])

    # 2. Nearest facility (checked before 'route' so "route to nearest police" works)
    ftype = next((t for t, words in MOCK_FACILITY_WORDS.items() if any(w in q for w in words)), None)
    if ftype and ("nearest" in q or "closest" in q or "أقرب" in q or "route" in q):
        if not pt:
            return need_point
        r = await call("find_nearest_facility", {"origin": pt, "facility_type": ftype,
                                                 "limit": 3, "max_distance_km": 10})
        if "error" in r or not r.get("count"):
            return r.get("error") or r.get("message")
        return f"Nearest {ftype} by driving time:\n" + "\n".join(
            f"{f['rank']}. **{f['name']}** — {f['drive_minutes']} min" for f in r["facilities"])

    # 3. Isochrone
    if any(w in q for w in MOCK_ISO_WORDS):
        if not pt:
            return need_point
        minutes = [int(n) for n in re.findall(r"\d+", q) if 1 <= int(n) <= 60][:4] or [10, 20, 30]
        profile = "pedestrian" if ("walk" in q or "مشي" in q) else "bicycle" if ("bike" in q or "bicycle" in q) else "auto"
        r = await call("compute_isochrone", {"origin": pt, "minutes": minutes, "profile": profile})
        return r.get("error") or f"Reachable areas drawn for {', '.join(map(str, r['bands_minutes']))} min ({profile})."

    # 4. Route to the first result of the previous answer
    if any(w in q for w in MOCK_ROUTE_WORDS):
        dest, name = _first_point(ctx.last_results)
        if not pt or not dest:
            return "Pick a point and run a search first, then ask for a route to the first result."
        r = await call("get_route", {"origin": pt, "destination": dest})
        return r.get("error") or f"Route to {name or 'the first result'}: **{r['duration_min']} min**, {r['distance_km']} km."

    return MOCK_HELP


# =============================================================================
# THE AGENT ENDPOINT
# =============================================================================

@router.post("/chat")
async def agent_chat(req: ChatRequest, request: Request):
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")
    _check_rate_limit(ip)

    # 1. Clean the history the browser sent
    history = [{"role": m.role, "content": m.content.strip()}
               for m in req.messages if m.role in ("user", "assistant") and m.content.strip()]
    history = history[-MAX_HISTORY:]
    while history and history[0]["role"] != "user":       # models require user first
        history.pop(0)
    if not history or history[-1]["role"] != "user":
        raise HTTPException(400, "The last message must be from the user.")
    question = history[-1]["content"]
    if len(question) > MAX_MESSAGE_CHARS:
        raise HTTPException(400, f"Message too long (max {MAX_MESSAGE_CHARS} characters).")

    # 2. Attach map context to the latest question so the agent knows "here"
    ctx = req.context
    ctx_lines = [f"Picked point: {json.dumps(ctx.picked_point) if ctx.picked_point else 'none'}"]
    if ctx.map_center:
        ctx_lines.append(f"Map center: {json.dumps(ctx.map_center)} (zoom {ctx.map_zoom})")
    if ctx.map_bbox:
        ctx_lines.append(f"Map bbox: {json.dumps(ctx.map_bbox)}")
    if ctx.last_results:
        ctx_lines.append("Results from the previous answer (for 'the second one', 'that hospital'): "
                         + _dumps(ctx.last_results)[:4000])
    history[-1] = {"role": "user", "content": f"{question}\n\n<map_context>\n" + "\n".join(ctx_lines) + "\n</map_context>"}

    # 3. Run the chosen brain
    request_id, started, st = str(uuid.uuid4()), time.time(), RunState()
    if AGENT_PROVIDER == "anthropic":
        reply = await _loop_anthropic(list(history), st)
    elif AGENT_PROVIDER == "openai":
        reply = await _loop_openai(list(history), st)
    else:
        reply = await _loop_mock(question, ctx, st)

    if reply is None:
        reply = "This needed too many steps. Please split it into smaller questions."

    _log({"id": request_id, "time": started, "ip": ip, "provider": AGENT_PROVIDER, "question": question,
          "picked_point": ctx.picked_point, "tools": st.trace, "reply": reply,
          "seconds": round(time.time() - started, 2)})

    return {
        "request_id": request_id,
        "reply": reply or "Done. See the map.",
        "layers": st.layers,
        "last_results": st.compact[-3:],   # browser sends this back next turn
        "tools_used": [t["tool"] for t in st.trace],
    }


# =============================================================================
# DEBUG — call one tool directly, no AI involved.
#   POST /api/agent/tool/compute_isochrone   body: {"origin": {...}, "minutes": [10]}
# Disable in production with AGENT_DEBUG_TOOLS=0.
# =============================================================================

@router.post("/tool/{name}")
async def agent_tool_direct(name: str, args: dict = Body(default={})):
    if not DEBUG_TOOLS:
        raise HTTPException(404, "Not found")
    result, layers = await run_tool(name, args)
    return {"tool": name, "result": result,
            "layers": [{"kind": l["kind"], "label": l["label"],
                        "features": len(l["geojson"].get("features", []))} for l in layers]}


class Feedback(BaseModel):
    request_id: str
    rating: str = Field(..., pattern="^(up|down)$")
    comment: str | None = Field(None, max_length=1000)


@router.post("/feedback")
async def agent_feedback(fb: Feedback):
    """👍 / 👎 from the chat panel. Review the 👎 ones weekly."""
    _log({"feedback_for": fb.request_id, "rating": fb.rating, "comment": fb.comment, "time": time.time()})
    return {"ok": True}


@router.get("/health")
async def agent_health():
    configured = {"anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
                  "openai": True, "mock": True}.get(AGENT_PROVIDER, False)
    return {"provider": AGENT_PROVIDER, "model": AGENT_MODEL, "configured": configured,
            "tools": list(TOOL_FUNCS), "debug_tools_endpoint": DEBUG_TOOLS}