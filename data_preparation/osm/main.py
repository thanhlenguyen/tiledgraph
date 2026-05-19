"""
main.py — CLI entry point for the OSM topology builder.

USAGE
  python -m osm_builder <input.parquet> <output.osm.pbf> [options]

  or directly:
  python main.py <input.parquet> <output.osm.pbf> [options]

OPTIONS
  --regions R C      Shard grid rows × cols (default: 4 5 → 20 shards)
  --tile-size DEG    ST_Node tile size in degrees (default: 0.02)
  --memory-gb GB     DuckDB memory limit per shard (default: 8)
  --workers N        Parallel shard workers (default: 1)
  --resume           Skip shards whose .osm already exists (crash recovery)
  --keep-shards      Keep per-shard work files after merge (for debugging)

SHARD SIZING GUIDE (KSA ~1.15 M segments)
  --regions 4 5  → 20 shards, ~60–200 k segs/shard  ← recommended
  --regions 5 6  → 30 shards, ~40–130 k segs/shard  (very safe, slower)

DEPENDENCIES
  pip install duckdb lxml
  sudo apt install osmium-tool
"""

import argparse
import glob
import multiprocessing
import os
import shutil
import sys

from partition import partition_parquet, reconstruct_shards_from_disk
from shard_processor import process_shard
from merger import merge_shards


# ─────────────────────────────────────────────────────────────────────────────
# Worker (multiprocessing entry point)
# ─────────────────────────────────────────────────────────────────────────────

def _worker(args: tuple) -> tuple[str, bool]:
    shard, out_osm, tile_size, memory_gb, id_offset, n_workers = args
    label = f"shard ({shard['row']},{shard['col']})"
    print(f"\n🔧  Processing {label} — {shard['count']:,} rows …")
    try:
        n = process_shard(shard, out_osm, tile_size, memory_gb, id_offset, n_workers)
        print(f"✅  {label} done → {n:,} ways → {os.path.basename(out_osm)}")
        return out_osm, True
    except Exception as e:
        import traceback
        print(f"❌  {label} FAILED: {e}")
        traceback.print_exc()
        return out_osm, False


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build OSM PBF from GeoParquet road network via regional shards"
    )
    parser.add_argument("input_parquet", help="Input GeoParquet file")
    parser.add_argument("output_pbf",    help="Output OSM PBF file")
    parser.add_argument("--regions", nargs=2, type=int, default=[4, 5],
                        metavar=("ROWS", "COLS"),
                        help="Shard grid (default: 4 5 → 20 shards)")
    parser.add_argument("--tile-size", type=float, default=0.02,
                        help="ST_Node tile size in degrees (default: 0.02)")
    parser.add_argument("--memory-gb", type=int, default=8,
                        help="DuckDB memory limit per shard in GB (default: 8)")
    parser.add_argument("--workers", "--worker", dest="workers",
                        type=int, default=1,
                        help="Parallel shard workers (default: 1)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip shards whose .osm already exists")
    parser.add_argument("--keep-shards", action="store_true",
                        help="Keep per-shard work files after merge")
    args = parser.parse_args()

    input_parquet = os.path.abspath(args.input_parquet)
    output_pbf    = os.path.abspath(args.output_pbf)
    n_rows, n_cols = args.regions

    # All intermediate files go into <output_stem>_work/
    work_dir  = os.path.splitext(output_pbf)[0] + "_work"
    shard_dir = os.path.join(work_dir, "shards")
    osm_dir   = os.path.join(work_dir, "osm")
    os.makedirs(osm_dir, exist_ok=True)

    print(f"""
╔══════════════════════════════════════════════════════════╗
║         build_osm_topology — regional shard mode         ║
╠══════════════════════════════════════════════════════════╣
  Input   : {input_parquet}
  Output  : {output_pbf}
  Grid    : {n_rows} rows × {n_cols} cols = {n_rows * n_cols} shards
  Tile    : {args.tile_size}°
  Memory  : {args.memory_gb} GB / shard
  Workers : {args.workers}
  Resume  : {'yes (skipping completed shards)' if args.resume else 'no'}
╚══════════════════════════════════════════════════════════╝
""")

    # ── Step 0: Partition (or restore from disk if --resume) ──────────────────
    if args.resume and os.path.isdir(shard_dir):
        shards = reconstruct_shards_from_disk(shard_dir)
        if not shards:
            shards = partition_parquet(input_parquet, shard_dir, n_rows, n_cols)
    else:
        shards = partition_parquet(input_parquet, shard_dir, n_rows, n_cols)

    # ── Step 1: Build task list ───────────────────────────────────────────────
    tasks:         list[tuple] = []
    skipped_shards: list[tuple[str, bool]] = []

    for idx, shard in enumerate(shards):
        out_osm = os.path.join(osm_dir, f"shard_{shard['row']:02d}_{shard['col']:02d}.osm")
        if args.resume and os.path.exists(out_osm) and os.path.getsize(out_osm) > 0:
            print(f"   --resume: shard ({shard['row']},{shard['col']}) already done → {os.path.basename(out_osm)}")
            skipped_shards.append((out_osm, True))
        else:
            tasks.append((shard, out_osm, args.tile_size, args.memory_gb, idx + 1, args.workers))

    n_todo    = len(tasks)
    n_skipped = len(skipped_shards)
    suffix    = f" ({n_skipped} resumed/skipped)" if n_skipped else ""
    print(f"\n🚀  Processing {n_todo} shards{suffix} …")

    # ── Step 2: Run shards ────────────────────────────────────────────────────
    if args.workers > 1:
        with multiprocessing.Pool(args.workers) as pool:
            new_results = pool.map(_worker, tasks)
    else:
        new_results = [_worker(t) for t in tasks]

    results = skipped_shards + new_results

    failed = [p for p, ok in results if not ok]
    if failed:
        print(f"\n❌  {len(failed)} shards failed:")
        for p in failed:
            print(f"   {p}")
        sys.exit(1)

    shard_osm_files = [p for p, ok in results if ok]

    # ── Step 3: Merge shards → PBF ───────────────────────────────────────────
    merged_osm  = os.path.join(work_dir, "merged.osm")
    deduped_osm = os.path.join(work_dir, "deduped.osm")
    merge_shards(shard_osm_files, merged_osm, deduped_osm, output_pbf)

    # ── Step 4: Cleanup ───────────────────────────────────────────────────────
    if not args.keep_shards:
        shutil.rmtree(work_dir, ignore_errors=True)
        print("   Work directory removed.")
    else:
        print(f"   Work files kept in: {work_dir}")

    print(f"\n✅  Done: {output_pbf}")
    print(f"\nUse with Valhalla:  valhalla_build_tiles -c valhalla.json {output_pbf}")


if __name__ == "__main__":
    main()
