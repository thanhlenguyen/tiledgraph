# OSM Topology Builder — Workflow Documentation

Convert a GeoParquet street network into a routable `osm.pbf` file via parallel geographic sharding.

---

## Overview

```
GeoParquet
    │
    ▼
┌─────────────────┐
│  1. Partition   │  Split into R×C overlapping geographic shards
└────────┬────────┘
         │  (per shard, parallel)
         ▼
┌─────────────────┐
│  2. Ingest &    │  Validate, explode MULTI*, snap, dedup geometry
│     Repair      │
└────────┬────────┘
         ▼
┌─────────────────┐
│  3. Tiled       │  ST_Node tile-by-tile → tile Parquets in /tmp
│     ST_Node     │
└────────┬────────┘
         ▼
┌─────────────────┐
│  4. Attribute   │  Join noded segments ← scalar attributes
│     Join        │
└────────┬────────┘
         ▼
┌─────────────────┐
│  5. Vertex      │  Explode linestrings → individual (lat, lon) points
│     Extraction  │
└────────┬────────┘
         ▼
┌─────────────────┐
│  6. Node Dedup  │  DISTINCT (lat, lon) → assign stable negative node IDs
└────────┬────────┘
         ▼
┌─────────────────┐
│  7. Way→Node    │  Join vertices back to node IDs → way_nodes refs
│     Refs        │
└────────┬────────┘
         ▼
┌─────────────────┐
│  8. OSM XML     │  Stream <node> + <way> elements to .osm file
│     Write       │
└────────┬────────┘
         │  (after all shards)
         ▼
┌─────────────────┐
│  9. Merge &     │  osmium merge → dedup border nodes → osmium sort → .pbf
│     Dedup       │
└─────────────────┘
```

---

## Step-by-Step Reference

### Step 1 — Partition (`partition.py`)

The input Parquet is read once to compute the global bounding box, then divided into an R×C grid of overlapping shards.

| Detail | Value |
|---|---|
| Default grid | 4 rows × 5 cols = 20 shards |
| Overlap factor | 50% of cell width/height on each side |
| Spatial filter | `ST_Intersects(geometry, expanded_envelope)` |
| Output | `<work_dir>/shards/shard_RR_CC.parquet` |

**Why overlap?** Roads that cross a cell boundary must be fully noded in at least one shard. The 50% overlap guarantees this for any road shorter than one cell width.

Empty shards (0 rows) are silently skipped and produce no output file.

---

### Step 2 — Ingest & Repair (`shard_processor.py`, Steps 1–4)

Each shard Parquet is loaded into DuckDB and cleaned in three passes:

1. **Column mapping** — raw fields (`pkStreetID`, `EnglishName`, `FOW`, `Subtype`, `Direction`, `NoOfLane`, `SpeedLimit`) are mapped to OSM-style attributes (`id`, `name`, `highway`, `oneway`, `lanes`, `maxspeed`).
2. **Geometry validation** — invalid geometries are repaired with `ST_MakeValid`.
3. **Explode + snap + dedup** — `MULTI*` geometries are exploded to single parts via `ST_Dump`; coordinates are snapped to a 1 µdeg grid (`ST_ReducePrecision(geom, 1e-6)`); exact duplicate geometries are removed with `QUALIFY ROW_NUMBER() OVER (PARTITION BY ST_AsText(geom)) = 1`.

The cleaned table is then split into two:

- `seg_geoms` — `(id, geom)` only, used during noding
- `seg_attrs` — `(id, name, highway, oneway, lanes, maxspeed)` only, joined after noding

Splitting halves the buffer pool pressure during the tile loop.

---

### Step 3 — Tiled ST_Node (`noding.py`)

`ST_Node` planarizes a set of linestrings — it splits every crossing pair at their intersection point. Running it on the full shard at once causes OOM for large shards (~500k segments), so noding is done tile-by-tile.

**Tile loop:**

1. Compute a fine grid over the shard using `--tile-size` (default `0.02°` ≈ 2 km).
2. Find occupied cells with one query using `ST_Centroid` — avoids N×M empty-cell probes.
3. For each occupied cell:
   - Fetch all segments intersecting the **expanded** tile (50% overlap) for accurate noding at edges.
   - Run `ST_Node` on the collected geometry.
   - Keep only segments whose centroid falls inside the **core** tile — prevents the same segment appearing in two adjacent tiles.
   - Write results to an individual Parquet file in `/tmp/tiles_RR_CC/`.
4. After the loop, create a DuckDB VIEW `noded_segments` over `read_parquet(/tmp/tiles_RR_CC/*.parquet)`.

**OOM recovery:** if a tile triggers an out-of-memory error, it is automatically subdivided into a 4×4 sub-grid (up to 3 levels deep) and retried.

**Memory model:** each tile Parquet is written and flushed immediately; the temp table `_tile_noded` is dropped after each tile. Buffer pool usage stays flat regardless of tile count.

---

### Step 4 — Attribute Join (`shard_processor.py`, Step 6)

The `noded_segments` VIEW (geometry + `src_id` FK) is joined to `seg_attrs` on `src_id = id` and written to `/tmp/edges_RR_CC.parquet`.

Output columns: `way_id, geom_wkb, name, highway, oneway, lanes, maxspeed`

`way_id` is a globally unique negative integer: `(id_offset × 1,000,000,000 + seg_id) × -1`, where `id_offset` is the 1-based shard index. Negative IDs flag synthetic (non-OSM-origin) data to osmium.

---

### Step 5 — Vertex Extraction (`shard_processor.py`, Step 7)

Each edge's `geom_wkb` is unpacked with `ST_Points` + `ST_Dump` to yield one row per vertex:

```
(way_id, seq, lon, lat)
```

Output: `/tmp/ep_RR_CC.parquet`

---

### Step 6 — Node Deduplication (`shard_processor.py`, Step 8)

`SELECT DISTINCT lat, lon` over the vertex table collapses shared endpoints (intersections). Each unique coordinate is assigned a stable negative node ID:

```
node_id = (id_base + ROW_NUMBER()) × -1
```

Output: `/tmp/ni_RR_CC.parquet` — columns: `node_id, lat, lon`

---

### Step 7 — Way→Node Reference List (`shard_processor.py`, Step 9)

The vertex table (`ep`) is joined to the node-ID table (`ni`) on `(lon, lat)` to produce the way→node reference list:

```
(way_id, seq, node_id)
```

Output: `/tmp/wn_RR_CC.parquet`

---

### Step 8 — OSM XML Write (`xml_writer.py`)

Streams `<node>` and `<way>` elements to a `.osm` file with O(CHUNK) peak RAM (CHUNK = 50,000 rows).

**Pre-sort phase** (DuckDB external sort, spills to `/tmp`):

- `wn_parquet` → `wn_sorted` ordered by `(way_id, seq)`
- `edges_parquet` → `ed_sorted` ordered by `way_id`

**`<node>` pass:** streams `ni_parquet` ordered by `node_id` via `fetchmany`.

**`<way>` pass — two-pointer merge:**

- `ed_cur` iterates `ed_sorted` (one row per way)
- `wn_cur` iterates `wn_sorted` (many rows per way)
- Both are sorted by `way_id`; they are advanced together without a hash join or in-memory buffer — O(2×CHUNK) RAM regardless of shard size.

> **Important:** each cursor must use an independent `con.cursor().execute(...)` call. Using `con.execute()` directly shares connection-level fetch state between cursors, causing `ed_cur.fetchmany()` to silently return rows from the last query executed — producing a 3-column row where 6 are expected and raising `ValueError: not enough values to unpack`.

Output: `<work_dir>/osm/shard_RR_CC.osm`

---

### Step 9 — Merge, Dedup & PBF (`merger.py`)

After all shards complete:

1. **`osmium merge`** — concatenates all shard `.osm` files into `merged.osm`.
2. **Border node dedup** — two-pass streaming scan over `merged.osm`:
   - Pass 1: build `coord → canonical_id` dict; populate `id_remap` for duplicates.
   - Pass 2: rewrite XML — skip duplicate `<node>` elements; rewrite `<nd ref>` attributes via `id_remap`.
3. **`osmium sort`** — sorts `deduped.osm` by element type and ID → final `output.pbf`.

Peak RAM for dedup is O(unique nodes) — roughly 300 MB for 3 million nodes.

If `osmium` is not installed, steps 1 and 3 fall back to a pure-Python XML concatenation path (produces unsorted XML; PBF conversion must be done manually).

---

## File Layout

```
<output_stem>_work/
├── shards/
│   ├── shard_00_01.parquet     # partitioned input (per cell + overlap)
│   ├── shard_00_02.parquet
│   └── ...
├── osm/
│   ├── shard_00_01.osm         # per-shard OSM XML
│   ├── shard_00_02.osm
│   └── ...
├── merged.osm                  # osmium merge output
└── deduped.osm                 # after border node dedup

/tmp/
├── tiles_RR_CC/                # tile Parquets (per shard, deleted after join)
│   ├── tile_00001_00042.parquet
│   └── ...
├── edges_RR_CC.parquet         # way_id + geom + attributes
├── ep_RR_CC.parquet            # exploded vertices
├── ni_RR_CC.parquet            # node IDs
└── wn_RR_CC.parquet            # way→node refs
```

All `/tmp` intermediates are deleted at the end of each shard. The `_work/` directory is deleted after the final PBF is written (unless `--keep-shards` is passed).

---

## Configuration Reference

| Flag | Default | Effect |
|---|---|---|
| `--regions R C` | `4 5` | Shard grid rows × cols |
| `--tile-size DEG` | `0.02` | ST_Node tile size in degrees |
| `--memory-gb GB` | `8` | DuckDB memory limit per shard |
| `--workers N` | `1` | Parallel shard workers (multiprocessing) |
| `--resume` | off | Skip shards whose `.osm` already exists |
| `--keep-shards` | off | Retain `_work/` directory after completion |

### Shard sizing guide (KSA ~1.15 M segments)

| Grid | Shards | Approx rows/shard |
|---|---|---|
| `4 5` | 20 | 60,000 – 200,000 |
| `5 6` | 30 | 40,000 – 130,000 |

Smaller shards use less memory per worker but increase total overhead from tile-loop startup and osmium merge time.

---

## Known Bugs & Fixes

### `ValueError: not enough values to unpack (expected 6, got 3)`

**Location:** `xml_writer.py`, line unpacking `ed_row`

**Cause:** `con.execute()` returns the connection object itself; calling `fetchmany` on a shared connection fetches from whichever query was executed last. When `wn_cur` is opened after `ed_cur`, subsequent `fetchmany` calls on `ed_cur` silently return `wn_sorted` rows (3 columns) instead of `ed_sorted` rows (6 columns).

**Fix:** open each cursor with `con.cursor().execute(...)` to get an independent result set:

```python
# Before (broken):
ed_cur = _ChunkedCursor(con.execute(f"SELECT ... FROM read_parquet('{ed_sorted}') ..."))
wn_cur = _ChunkedCursor(con.execute(f"SELECT ... FROM read_parquet('{wn_sorted}') ..."))

# After (fixed):
ed_cur = _ChunkedCursor(con.cursor().execute(f"SELECT ... FROM read_parquet('{ed_sorted}') ..."))
wn_cur = _ChunkedCursor(con.cursor().execute(f"SELECT ... FROM read_parquet('{wn_sorted}') ..."))
```

Apply the same fix to `node_cur` for consistency.