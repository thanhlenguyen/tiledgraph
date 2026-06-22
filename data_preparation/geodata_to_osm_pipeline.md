# Geodata → OSM Pipeline Documentation

**Script:** `build_osm_topology.py`  
**Purpose:** Convert any vector road dataset to a topologically correct OSM XML file suitable for routing engines (Valhalla, OSRM, GraphHopper).

---

## Overview

Every road feature (LineString) becomes an OSM `<way>`. Every vertex becomes an OSM `<node>`. Two roads sharing a vertex receive the **same node ID**, which is what makes routing engines understand they are connected — this is called *topological correctness*.

```bash
# Usage
python build_osm_topology.py <input_file> <output.osm> [memory_gb]

# Convert to PBF for routing engines
osmium cat output.osm -o output.osm.pbf
```

**Supported input formats:** `.parquet` (fastest, native DuckDB), `.gpkg`, `.geojson`, `.shp`

---

## Pipeline Steps

### Step 1 — Ingest Source Data

Reads the source file into a DuckDB table called `raw_segments`.

**Two ingestion paths:**

| Path | Format | Geometry column | Notes |
|------|--------|----------------|-------|
| Parquet | `.parquet` | Already parsed `GEOMETRY` | Native DuckDB reader, no GDAL, fastest |
| GDAL | `.gpkg`, `.geojson`, `.shp` | Raw `WKB` bytes | `ST_AsWKB()` defers GEOS parsing to Step 2 |

**Columns produced:**

| Column | Description |
|--------|-------------|
| `id` | Surrogate integer PK (`ROW_NUMBER()`) — always unique |
| `orig_id` | Original `pkStreetID` from source |
| `name` | Road name (`EnglishName`) |
| `oneway` | `'yes'` if `Direction=1`, else `'no'` |
| `lanes` | Number of lanes; defaults to `'1'` if missing/zero |
| `maxspeed` | Speed limit as text; `NULL` if unknown |
| `highway` | OSM highway tag (derived from `FOW` + `Subtype`) |
| `geom_wkb` | Road geometry (WKB bytes or parsed GEOMETRY) |

**Highway classification logic (`FOW` + `Subtype`):**

```
FOW ∈ {3,4} + Subtype=1 → trunk_link
FOW ∈ {3,4} + Subtype=2 → primary_link
FOW ∈ {3,4} + Subtype=3 → secondary_link
FOW ∈ {3,4}             → tertiary_link
Subtype=1                → trunk
Subtype=2                → primary
else                     → road
```

---

### Step 2 — Validate & Repair Geometries

Repairs invalid geometries and explodes multi-part features.

- **`ST_IsValid` / `ST_MakeValid`** — fixes self-intersections and other geometry errors
- **`ST_Dump`** — explodes `MultiLineString` → individual `LineString` parts, one row each
- Rows with fewer than 2 vertices or more than `MAX_VERTICES` (10,000) are dropped
- Invalid geometries after repair are dropped

---

### Step 3 — Extract Vertices → Node Table

Extracts all vertices, rounds coordinates, and deduplicates into a node table.

- **`ST_Points` + `ST_Dump`** — extracts every vertex as a `Point`
- **`ROUND(..., 7)`** — rounds to 7 decimal places (~1.1 cm precision at equator)
- Two vertices rounding to the same `(lon, lat)` become **one shared OSM node**
- Negative node IDs signal "new data" to OSM tools

```
7 decimal places ≈ 1.1 cm precision at the equator
Shared nodes (%) = (1 - unique_nodes / total_vertices) × 100
```

---

### Step 4 — Build `way_nodes` Table

Joins every vertex back to its deduplicated node ID, preserving vertex order.

**Schema:** `(way_id, seq, node_id)`

Every row means: *"at position `seq` in way `way_id`, the node is `node_id`."*

This is the core topology table — two ways sharing a `node_id` are connected in the routing graph.

---

### Step 5 — Build `edges` Table

Stores road attributes (OSM tag values) separately from geometry.

**Schema:** `(way_id, name, highway, oneway, lanes, maxspeed, ref)`

`COALESCE` fills safe defaults for any `NULL` values:
- `name` → `'unknown'`
- `highway` → `'road'`
- `oneway` → `'no'`
- `lanes` → `'1'`

---

### Step 6 — Topology Validation

Coordinate rounding in Step 3 can collapse adjacent vertices to the same node, creating artefacts. This step removes them before snapping runs.

**`_clean_way_nodes()` does two things:**

1. **Remove consecutive duplicate node refs** — e.g. `[1, 2, 2, 3]` → `[1, 2, 3]` using `LAG()` window function
2. **Drop degenerate ways** — ways with fewer than 2 distinct nodes, or closed 2-node loops (`[5, 5]`)

---

### Step 7a — Node-to-Node Snapping

Merges road endpoints that are very close but not identical, fixing connectivity gaps caused by floating-point drift or small digitising errors.

```
Way A ────────────────●  lon: 55.2712340, lat: 25.1983210
Way B  ●──────────────   lon: 55.2712342, lat: 25.1983211  ← 0.2 mm off
→ Merged to one shared node
```

**Two passes:**

| Pass | Candidates | Tolerance | Purpose |
|------|-----------|-----------|---------|
| Pass 1 | All road endpoints | ≈ 2 m (0.00002°) | Float drift surviving coordinate rounding |
| Pass 2 | Dangling endpoints only | ≈ 6 m (0.00005°) | Region-boundary offsets, roundabout spoke tips |

A **dangling endpoint** is the start or end of a road not shared with any other road.

**Merge algorithm — Union-Find (label propagation):**

1. Assign each node its own ID as initial label
2. Each iteration: every node takes the minimum label of all its neighbours
3. Repeat until no labels change (convergence)
4. All nodes with the same final label collapse to one canonical node (minimum ID)

This handles transitive chains: if A→B and B→C, then A, B, and C all merge to the same node.

---

### Step 7b — Point-to-Edge Snapping

Fixes dangling endpoints that lie *beside* another road (not at its endpoint).

```
Way A  ────────────────────────────────
Way B            ● P  (dangling, should connect to Way A)

→ After snapping:
Way A  ──────────●──────────────────────   ← split at projection point
Way B            ● P  (now connected)
```

**Two steps:**

#### Step A — Snap to Nearest Node (≈ 2 m, square bbox)

For each dangling endpoint, search all road endpoints within a square bounding box. If a match exists, merge using union-find.

Square bbox is intentional here — at 2 m the corner error is only 0.29 m, negligible. Avoids the cost of a `SQRT` call for each candidate.

#### Step B — Project onto Nearest Segment (≈ 6 m, circle check)

For endpoints still dangling after Step A:

1. **Build segment index** — one row per consecutive node pair, with bounding box
2. **Find candidate segments** — bbox overlaps the search area around point P
3. **Parametric projection** onto each candidate:

```
dx = B.x - A.x,  dy = B.y - A.y
t  = dot(P-A, B-A) / |B-A|²          # position along segment AB
Q  = A + t*(B-A)                      # foot of perpendicular from P
d  = |P - Q|                          # perpendicular distance
```

   Keep only projections where `t ∈ (0.001, 0.999)` — strictly inside the segment, not at endpoints.

4. **Pick the closest valid projection** per dangling endpoint
5. **Insert new node** at rounded projection coordinates
6. **Split the target segment** by injecting fractional sequence number `seq_a + 0.5`, then renumbering with `ROW_NUMBER()`
7. **Remap** dangling endpoint → new shared node

Circle check (exact distance) is used here because at 6 m the corner error would be ~1.7 m — large enough to snap to the wrong segment.

---

### Step 8 — Write OSM XML

Streams the final OSM XML to disk using `lxml`'s incremental writer (`xmlfile`). RAM usage stays constant regardless of dataset size.

**OSM XML structure:**

```xml
<osm version="0.6">
  <node id="-1" lat="25.123" lon="55.456" version="1" visible="true"/>
  ...
  <way id="-1" version="1" visible="true">
    <tag k="highway" v="primary"/>
    <tag k="name" v="Sheikh Zayed Road"/>
    <nd ref="-1"/>
    <nd ref="-2"/>
  </way>
  ...
</osm>
```

**Memory strategy:**

- All `<node>` elements are written before any `<way>` (OSM requirement)
- Nodes streamed in chunks of 100,000 rows
- Way attributes pre-loaded into a Python dict keyed by `way_id`
- Way node refs streamed in chunks of 500,000 rows
- Each element is serialised to disk immediately and freed

---

## Configuration Reference

### Tolerances

| Constant | Value | Approx. distance | Used in |
|----------|-------|-----------------|---------|
| `ROUNDING_DIGITS` | 7 | ~1.1 cm | Step 3 — coordinate rounding |
| `SNAP_TOL_TIGHT_DEG` | 0.00002° | ~2 m | Step 7a Pass 1 — all endpoints |
| `SNAP_TOL_WIDE_DEG` | 0.00005° | ~6 m | Step 7a Pass 2 — dangles only |
| `ENDPOINT_SNAP_TOL_DEG` | 0.00002° | ~2 m | Step 7b Step A — node search |
| `EDGE_SNAP_TOL_DEG` | 0.00005° | ~6 m | Step 7b Step B — segment projection |
| `MAX_VERTICES` | 10,000 | — | Step 2 — artefact filter |

### DuckDB Settings

| Setting | Default | Notes |
|---------|---------|-------|
| `memory_limit` | 8 GB | Set to ~60% of available RAM |
| `threads` | `cpu_count - 2` | Leaves 2 cores for OS |
| `temp_directory` | `/tmp` | Fast Linux filesystem |
| `preserve_insertion_order` | `false` | Allows reordering for speed |

---

## Dependencies

```bash
pip install duckdb lxml tqdm
```

| Package | Role |
|---------|------|
| `duckdb` | In-process analytical SQL + geometry (`ST_*` functions) + on-disk spill |
| `lxml` | Fast C-based incremental XML writer |
| `tqdm` | Terminal progress bars |

For non-Parquet inputs, GDAL must be available (via `duckdb`'s `spatial` extension).

---

## Next Steps After the Script

```bash
# Convert OSM XML to PBF (required by most routing engines)
osmium cat output.osm -o output.osm.pbf

# For Valhalla — build tiles from PBF
valhalla_build_tiles -c valhalla.json output.osm.pbf

# For OSRM — extract and contract
osrm-extract output.osm.pbf -p car.lua
osrm-contract output.osrm
```
