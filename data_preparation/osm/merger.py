"""
merger.py — merge per-shard OSM XML files and deduplicate border nodes.

After osmium merge, adjacent shards may share roads whose endpoint nodes
have the same (lat, lon) but different IDs (assigned independently per shard).
dedup_border_nodes() fixes this with a two-pass streaming scan — O(nodes) RAM.
"""

import os
import shutil
import subprocess
import sys

from lxml import etree


def merge_shards(
    shard_osm_files: list[str],
    merged_osm: str,
    deduped_osm: str,
    output_pbf: str,
) -> None:
    """
    merge_shards → dedup_border_nodes → osmium sort → PBF.

    Falls back to Python XML concatenation if osmium is not installed.
    """
    if shutil.which("osmium"):
        print(f"\n🔀  Merging {len(shard_osm_files)} shard OSM files with osmium …")
        subprocess.run(
            ["osmium", "merge"] + shard_osm_files + ["-o", merged_osm, "--overwrite"],
            check=True,
        )
        dedup_border_nodes(merged_osm, deduped_osm)

        print(f"\n📦  Sorting + writing PBF → {output_pbf} …")
        subprocess.run(
            ["osmium", "sort", deduped_osm, "-o", output_pbf, "--overwrite"],
            check=True,
        )
    else:
        print("\n⚠️  osmium not found — using Python XML concatenation (slower).")
        _concat_osm_xml(shard_osm_files, merged_osm)
        dedup_border_nodes(merged_osm, deduped_osm)
        print(f"   Output (unsorted OSM XML): {deduped_osm}")
        print(f"   Convert to PBF manually:")
        print(f"     osmium sort {deduped_osm} -o {output_pbf}")


def dedup_border_nodes(merged_osm: str, deduped_osm: str) -> None:
    """
    Two-pass streaming deduplication of border nodes by (lat, lon).

    Pass 1: scan all <node> elements, build coord → canonical_id dict.
            Duplicate coords get remapped to the first-seen ID.
    Pass 2: rewrite — skip duplicate <node> elements; rewrite <nd ref> via remap.

    Peak RAM: O(unique nodes) for coord_to_id dict (~300 MB for 3 M nodes).
    """
    print(f"\n🔗  Deduplicating border nodes …")

    coord_to_id: dict[tuple, int] = {}
    id_remap:    dict[int, int]   = {}

    # Pass 1
    for _, elem in etree.iterparse(merged_osm, events=("end",), tag="node"):
        nid = int(elem.get("id"))
        lat = round(float(elem.get("lat")), 7)
        lon = round(float(elem.get("lon")), 7)
        coord = (lat, lon)
        if coord in coord_to_id:
            id_remap[nid] = coord_to_id[coord]
        else:
            coord_to_id[coord] = nid
        elem.clear()

    del coord_to_id
    print(f"   Duplicate border nodes to merge: {len(id_remap):,}")

    # Pass 2
    skipped = remapped = 0
    with open(deduped_osm, "wb") as fh:
        with etree.xmlfile(fh, encoding="utf-8") as xf:
            xf.write_declaration()
            with xf.element("osm", version="0.6", generator="build_osm_topology"):
                for _, elem in etree.iterparse(merged_osm, events=("end",)):
                    if elem.tag == "node":
                        nid = int(elem.get("id"))
                        if nid in id_remap:
                            skipped += 1
                        else:
                            xf.write(elem)
                        elem.clear()
                    elif elem.tag == "way":
                        for nd in elem.findall("nd"):
                            ref = int(nd.get("ref"))
                            if ref in id_remap:
                                nd.set("ref", str(id_remap[ref]))
                                remapped += 1
                        xf.write(elem)
                        elem.clear()

    print(f"   Nodes removed (duplicates): {skipped:,}")
    print(f"   nd refs remapped:           {remapped:,}")


def _concat_osm_xml(shard_files: list[str], out_path: str) -> None:
    """Fallback: concatenate shard OSM files without osmium."""
    with open(out_path, "wb") as fh:
        with etree.xmlfile(fh, encoding="utf-8") as xf:
            xf.write_declaration()
            with xf.element("osm", version="0.6", generator="build_osm_topology"):
                for shard_file in shard_files:
                    for _, elem in etree.iterparse(shard_file, events=("end",)):
                        if elem.tag in ("node", "way"):
                            xf.write(elem)
                        elem.clear()
