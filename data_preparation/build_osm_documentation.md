# OSM Topology Builder — Process Documentation

`build_osm_topology.py` reads a vector road dataset and produces a valid OSM XML file suitable for conversion to OSM PBF and use with routing engines such as Valhalla, OSRM, or GraphHopper.

---

## Table of Contents

1. [Overview](#overview)
2. [Usage](#usage)
3. [Dependencies](#dependencies)
4. [Pipeline Steps](#pipeline-steps)
   - [Step 1 — Ingest Source Data](#step-1--ingest-source-data)
   - [Step 2 — Validate and Explode Geometries](#step-2--validate-and-explode-geometries)
   - [Step 3 — Extract Vertices → Node Table](#step-3--extract-vertices--node-table)
   - [Step 4 — Build `way_nodes`](#step-4--build-way_nodes)
   - [Step 5 — Build `edges` (Road Attributes)](#step-5--build-edges-road-attributes)
   - [Step 6 — Initial Topology Validation](#step-6--initial-topology-validation)
   - [Step 7a — Node-to-Node Snapping](#step-7a--node-to-node-snapping)
   - [Step 7b — Point-to-Edge Snapping](#step-7b--point-to-edge-snapping)
   - [Step 8 — Write OSM XML](#step-8--write-osm-xml)
5. [Configuration Reference](#configuration-reference)
6. [Highway Classification](#highway-classification)
7. [Key Design Decisions](#key-design-decisions)
8. [Post-Processing](#post-processing)

---

## Overview

Every road feature (LineString) in the source data becomes an OSM `<way>`. Every vertex of every road becomes an OSM `<node>`. Two roads that share a vertex receive the **same node ID**, making the network topologically correct for routing engines.

```
Source file (.parquet / .gpkg / .geojson / .shp)
        │
        ▼
   [DuckDB pipeline — Steps 1–7]
        │
        ▼
   output.osm  ──→  osmium cat  ──→  output.osm.pbf
```

---

## Usage

```bash
python build_osm_topology.py <input_file> <output.osm> [memory_gb]
```

| Argument | Description |
|---|---|
| `input_file` | Path to the source road dataset (`.parquet`, `.gpkg`, `.geojson`, `.shp`) |
| `output.osm` | Path where the OSM XML file will be written |
| `memory_gb` | *(optional)* RAM limit for DuckDB in GB. Default: `8`. Set to ~60% of available RAM. |

**Recommended: convert to Parquet before running for best performance.**

```bash
ogr2ogr -f Parquet roads.parquet roads.gpkg
python build_osm_topology.py roads.parquet output.osm 16
```

---

## Dependencies

```bash
pip install duckdb lxml tqdm
```

| Package | Purpose |
|---|---|
| `duckdb` | In-process SQL engine — handles all geometry work and on-disk spill |
| `lxml` | Fast C-based XML writer — streams OSM XML to disk |
| `tqdm` | Terminal progress bars |

---

## Pipeline Steps

### Step 1 — Ingest Source Data

Reads the source road dataset into a DuckDB table called `raw_segments`.

**Two code paths depending on format:**

- **Parquet** — DuckDB reads GeoParquet natively without GDAL. Geometry is already parsed as a `GEOMETRY` object.
- **Other formats** (GeoPackage, GeoJSON, Shapefile) — read via GDAL's `st_read()`. Geometry is stored as raw WKB bytes and deferred to Step 2, where `try_cast()` can safely handle corrupt features.

**Columns produced:**

| Column | Description |
|---|---|
| `id` | Surrogate integer primary key (`ROW_NUMBER()`) — always unique |
| `orig_id` | Original `pkStreetID` from source (may have duplicates) |
| `name` | Road name (`EnglishName`) |
| `oneway` | `'yes'` if `Direction=1`, else `'no'` |
| `lanes` | Number of lanes as text; `'1'` if missing or zero |
| `maxspeed` | Speed limit as text; `NULL` if unknown (lets routing engines use defaults) |
| `highway` | OSM highway tag value (see [Highway Classification](#highway-classification)) |
| `geom_wkb` | Road geometry as WKB bytes |

> The filter `WHERE fkEmirateID = 4` in the Parquet path scopes the dataset to a specific region (emirate). Adjust as needed for other datasets.

---

### Step 2 — Validate and Explode Geometries

Repairs invalid geometries and normalises all features into individual LineStrings.

**Sub-steps:**

1. **Validate** — `ST_IsValid()` checks each geometry. Invalid ones are repaired with `ST_MakeValid()`. Corrupt WKB rows are silently dropped via `try_cast()`.
2. **Explode** — `ST_Dump()` splits any `MultiLineString` into individual `LineString` rows, so each becomes its own OSM way.
3. **Filter** — Features are dropped if they have fewer than 2 vertices, more than `MAX_VERTICES` (10,000) vertices, or remain invalid after repair.

---

### Step 3 — Extract Vertices → Node Table

Extracts every vertex from every road and deduplicates them into a `node_ids` table.

**Process:**

1. `ST_Points()` returns all vertices of a geometry as a `MultiPoint`.
2. `ST_Dump()` explodes it into individual `Point` rows, each tagged with `way_id` and sequence number `seq`.
3. Coordinates are **rounded** to `ROUNDING_DIGITS` decimal places (default: 7, ≈ 1.1 cm precision).
4. Two vertices that round to the same `(lon, lat)` become **one shared node** — this is what creates road-network topology.
5. Negative node IDs are assigned in deterministic order (`ROW_NUMBER() * -1`) to signal new (non-OSM-imported) data.

An index on `(lon, lat)` is created for fast lookups in Step 4.

---

### Step 4 — Build `way_nodes`

Joins every vertex back to its canonical node ID.

```
all_vertices (way_id, seq, lon, lat)
    JOIN node_ids ON (lon, lat)
    → way_nodes (way_id, seq, node_id)
```

`way_nodes` is the core topology table: each row encodes "the node at position `seq` in way `way_id` is node `node_id`".

---

### Step 5 — Build `edges` (Road Attributes)

Creates a one-row-per-road attributes table used later for OSM tag output.

| Column | OSM Tag | Default |
|---|---|---|
| `way_id` | *(internal key)* | — |
| `name` | `name` | `'unknown'` |
| `highway` | `highway` | `'road'` |
| `oneway` | `oneway` | `'no'` |
| `lanes` | `lanes` | `'1'` |
| `maxspeed` | `maxspeed` | `NULL` |
| `ref` | *(original source ID)* | — |

---

### Step 6 — Initial Topology Validation

Coordinate rounding in Step 3 can collapse adjacent vertices to the same node ID, creating artefacts that must be cleaned before snapping.

**Two artefact types removed:**

- **Consecutive duplicate node refs** — e.g. `[1, 2, 2, 3]` → `[1, 2, 3]`. Detected with a `LAG()` window function.
- **Degenerate ways** — ways with fewer than 2 distinct nodes, or where the only two refs point to the same node. Both the `way_nodes` rows and the `edges` row are deleted.

---

### Step 7a — Node-to-Node Snapping

Merges road endpoints that are very close but not identical — typically caused by floating-point drift, digitising errors, or region-boundary offsets.

**Two passes:**

| Pass | Candidates | Tolerance | Purpose |
|---|---|---|---|
| Pass 1 | All road endpoints | ≈ 2 m (`SNAP_TOL_TIGHT_DEG`) | Catches float drift that survives coordinate rounding |
| Pass 2 | Dangling endpoints only | ≈ 6 m (`SNAP_TOL_WIDE_DEG`) | Catches larger offsets at region boundaries and roundabout spoke tips |

A **dangling endpoint** is the start or end of a road that does not connect to any other road.

**Merge algorithm (Union-Find / Label Propagation):**

Merging is transitive: if A→B and B→C, then A, B, and C must all collapse to one node. This is handled by iterative label propagation:

1. Each node starts with its own ID as its label.
2. Each iteration, every node adopts the minimum label of all its neighbours.
3. Repeat until no labels change (convergence).
4. All nodes sharing a label are remapped to the minimum ID in the group.

After merging, `_clean_way_nodes()` runs again to remove any new artefacts.

---

### Step 7b — Point-to-Edge Snapping

Fixes remaining dangling endpoints that node-to-node snapping cannot resolve — specifically, roads that end **beside** another road rather than at one of its existing nodes (T-junctions, roundabout spokes).

**Two sub-steps:**

#### Step A — Snap to Nearest Existing Node (≈ 2 m square box)

For each dangling endpoint, search all road endpoints within a square bounding box. If a nearby node is found, merge directly using union-find. This is intentionally a square (not circle) search for speed; at ≈ 2 m the corner error is only 0.29 m, which is harmless.

#### Step B — Project onto Nearest Road Segment (≈ 6 m circle check)

For endpoints still dangling after Step A:

1. **Build a segment index** — one row per consecutive node pair, storing the segment's bounding box for fast spatial filtering.
2. **Project** — for each dangling endpoint P and candidate segment A→B, compute the perpendicular foot Q using the parametric formula:

   ```
   t = dot(P−A, B−A) / |B−A|²
   Q = A + t·(B−A)
   ```

   Only projections where `t ∈ (0.001, 0.999)` are kept — this ensures Q lands strictly inside the segment, not at its endpoints (which Step A already handles).

3. **Insert new node** at Q's rounded coordinates (reuses an existing node if one already exists there).
4. **Split the segment** — injects Q between A and B using a fractional sequence number (`seq_a + 0.5`), then renumbers with `ROW_NUMBER()`.
5. **Remap** the dangling endpoint's node ID to the new shared node.

A circle distance check (exact Euclidean) is used here because at 6 m the bounding-box corner error would be ~2 m — large enough to snap to the wrong segment.

---

### Step 8 — Write OSM XML

Streams nodes and ways from DuckDB to disk in valid OSM XML format. Memory usage is O(1) regardless of dataset size.

**Output format:**

```xml
<?xml version='1.0' encoding='utf-8'?>
<osm version="0.6" generator="build_osm_topology">
  <node id="-1" lat="25.1234567" lon="55.4567890" version="1" visible="true"/>
  ...
  <way id="-1" version="1" visible="true">
    <tag k="highway" v="primary"/>
    <tag k="name" v="Sheikh Zayed Road"/>
    <tag k="oneway" v="yes"/>
    <tag k="lanes" v="3"/>
    <nd ref="-1"/>
    <nd ref="-2"/>
  </way>
  ...
</osm>
```

**Memory strategy:**

- All `<node>` elements are written first (OSM requirement), then all `<way>` elements.
- `lxml.etree.xmlfile` flushes each element to disk immediately after writing.
- Way attributes are pre-loaded into a Python dict (~620 MB for large datasets) to avoid a concurrent DuckDB cursor.
- Way node references are streamed in chunks of 500,000 rows; one `<way>` element is held in memory at a time.
- Ways with fewer than 2 `<nd>` children are skipped.

---

## Configuration Reference

All distance tolerances are in degrees. Rough conversion: **1° ≈ 111,111 m**.

| Constant | Default | Approx. | Description |
|---|---|---|---|
| `ROUNDING_DIGITS` | `7` | ≈ 1.1 cm | Decimal places for vertex coordinate rounding |
| `MAX_VERTICES` | `10,000` | — | Maximum vertices per road segment |
| `SNAP_TOL_TIGHT_DEG` | `0.00002°` | ≈ 2 m | Node-to-node snap, Pass 1 (all endpoints) |
| `SNAP_TOL_WIDE_DEG` | `0.00005°` | ≈ 6 m | Node-to-node snap, Pass 2 (dangling only) |
| `ENDPOINT_SNAP_TOL_DEG` | `0.00002°` | ≈ 2 m | Point-to-edge Step A (square box) |
| `EDGE_SNAP_TOL_DEG` | `0.00005°` | ≈ 6 m | Point-to-edge Step B (circle check) |

---

## Highway Classification

Source data columns `FOW` (Form Of Way) and `Subtype` (road class) are mapped to OSM `highway` tag values:

| FOW | Subtype | OSM `highway` |
|---|---|---|
| 3 or 4 (slip/off-ramp) | 1 | `trunk_link` |
| 3 or 4 | 2 | `primary_link` |
| 3 or 4 | 3 | `secondary_link` |
| 3 or 4 | other | `tertiary_link` |
| other | 1 | `trunk` |
| other | 2 | `primary` |
| other | other | `road` |

---

## Key Design Decisions

**DuckDB as the processing engine** — All geometry operations and joins run inside DuckDB. This avoids loading large datasets into Python memory and allows automatic disk spill when RAM is exhausted. The working database is stored at `/tmp/<output_name>.duckdb`.

**Negative OSM IDs** — All generated node and way IDs are negative integers. This is the conventional signal that data was not imported from openstreetmap.org and allows OSM tools to handle it without conflict.

**Surrogate primary key** — `ROW_NUMBER()` is used as the internal `id` rather than `pkStreetID`, because the source column may contain duplicates. The original `pkStreetID` is preserved in the `ref` column.

**Fractional sequence injection** — Splitting a segment (Step 7b) is done by assigning the new node a sequence number of `seq_a + 0.5`, sorting, then renumbering with `ROW_NUMBER()`. This avoids rewriting sequence numbers for all unaffected nodes in the same way.

**Chunked Python processing** — Step 7a and 7b process dangling endpoints in configurable batches (`BATCH_SIZE = 50,000` and `PROJ_BATCH = 10,000`) to display progress bars and avoid building very large SQL `VALUES` literals.

---

## Post-Processing

Convert the OSM XML file to PBF format for use with routing engines:

```bash
osmium cat output.osm -o output.osm.pbf
```

The PBF file can then be loaded directly into Valhalla, OSRM, or GraphHopper.
