"""
xml_writer.py — stream OSM XML from Parquet files with O(CHUNK) peak RAM.

DESIGN
  All large data (edges, way_nodes, node_ids) lives in Parquet files on /tmp.
  Nothing is held in DuckDB tables during the write pass.

  <node> pass:
    DuckDB external-sorts ni_parquet by node_id (spills to temp_directory).
    fetchmany(CHUNK) streams one chunk at a time → O(CHUNK) RAM.

  <way> pass:
    Both wn_parquet and edges_parquet are pre-sorted into new Parquet files
    via DuckDB COPY … ORDER BY (external sort, spills to /tmp).
    Then a _ChunkedCursor two-pointer merge in Python joins them by way_id
    without any hash table or in-memory sort buffer — O(2×CHUNK) RAM.

WHY _ChunkedCursor (not nested closures):
    Python `nonlocal` inside def closures nested under multiple `with`
    statements silently fails to rebind the outer variable in some CPython
    versions, leaving stale buffer references and causing wrong-column-count
    unpacking errors.  A class with instance variables is immune to this.
"""

import os

import duckdb
from lxml import etree


CHUNK = 50_000   # rows per fetchmany call


class _ChunkedCursor:
    """
    Wraps a DuckDB cursor as a peekable row-by-row iterator.

    peek() — return next row without consuming it (None if exhausted)
    next() — consume and return next row (None if exhausted)

    All state is in instance variables — no nonlocal, no closures.
    """

    def __init__(self, cursor: duckdb.DuckDBPyRelation, chunk: int = CHUNK):
        self._cur   = cursor
        self._chunk = chunk
        self._buf:  list = []
        self._pos:  int  = 0
        self._done: bool = False
        self._fill()

    def _fill(self) -> None:
        if self._done:
            return
        rows = self._cur.fetchmany(self._chunk)
        if rows:
            self._buf = rows
            self._pos = 0
        else:
            self._buf = []
            self._pos = 0
            self._done = True

    def peek(self):
        """Return next row without consuming, or None if exhausted."""
        if self._pos >= len(self._buf):
            self._fill()
        if self._pos >= len(self._buf):
            return None
        return self._buf[self._pos]

    def next(self):
        """Consume and return next row, or None if exhausted."""
        row = self.peek()
        if row is not None:
            self._pos += 1
        return row


def write_osm_xml(
    con: duckdb.DuckDBPyConnection,
    out_path: str,
    edges_parquet: str,
    wn_parquet: str,
    ni_parquet: str,
) -> None:
    """
    Write an OSM XML file to out_path.

    Parameters
    ----------
    con            : DuckDB connection (used for pre-sort COPY steps)
    out_path       : destination .osm file
    edges_parquet  : way attributes  — columns: way_id, name, highway, oneway, lanes, maxspeed
    wn_parquet     : way→node refs   — columns: way_id, seq, node_id  (unsorted OK)
    ni_parquet     : node coords     — columns: node_id, lat, lon      (unsorted OK)
    """
    wn_sorted = wn_parquet.replace(".parquet", "_sorted.parquet")
    ed_sorted = edges_parquet.replace(".parquet", "_sorted.parquet")

    # ── Pre-sort via DuckDB external sort (spills to temp_directory) ───────────
    # Sorting inside COPY uses DuckDB's merge-sort, which spills to disk —
    # peak RAM ≈ sort_spill_threshold (default 1 GB), not total row count.
    # We sort here (before opening the XML file) so any spill I/O doesn't
    # interleave with the lxml streaming write.
    print("      Sorting wn_parquet …")
    con.execute(f"""
        COPY (
            SELECT way_id, seq, node_id
            FROM read_parquet('{wn_parquet}')
            ORDER BY way_id, seq
        ) TO '{wn_sorted}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    print("      Sorting edges_parquet …")
    con.execute(f"""
        COPY (
            SELECT way_id, name, highway, oneway, lanes, maxspeed
            FROM read_parquet('{edges_parquet}')
            ORDER BY way_id
        ) TO '{ed_sorted}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    # ── Stream XML ─────────────────────────────────────────────────────────────
    with open(out_path, "wb") as fh:
        with etree.xmlfile(fh, encoding="utf-8") as xf:
            xf.write_declaration()
            with xf.element("osm", version="0.6", generator="build_osm_topology"):

                # ── <node> elements ────────────────────────────────────────────
                # DuckDB external-sort ni_parquet; fetchmany streams CHUNK rows.
                node_cur = _ChunkedCursor(con.cursor().execute(
                    f"SELECT node_id, lat, lon "
                    f"FROM read_parquet('{ni_parquet}') ORDER BY node_id"
                ))
                while True:
                    row = node_cur.next()
                    if row is None:
                        break
                    node_id, lat, lon = row
                    xf.write(etree.Element("node", {
                        "id":      str(node_id),
                        "lat":     f"{lat:.7f}",
                        "lon":     f"{lon:.7f}",
                        "version": "1",
                        "visible": "true",
                    }))

                # ── <way> elements — two-pointer merge ─────────────────────────
                # ed_cur:  one row per way  (way_id, name, highway, …)
                # wn_cur:  many rows per way (way_id, seq, node_id)
                # Both sorted by way_id — we advance them together.
                # No JOIN, no hash table, no sort buffer — O(2×CHUNK) RAM.
                ed_cur = _ChunkedCursor(con.cursor().execute(
                    f"SELECT way_id, name, highway, oneway, lanes, maxspeed "
                    f"FROM read_parquet('{ed_sorted}') ORDER BY way_id"
                ))
                wn_cur = _ChunkedCursor(con.cursor().execute(
                    f"SELECT way_id, seq, node_id "
                    f"FROM read_parquet('{wn_sorted}') ORDER BY way_id, seq"
                ))

                way_elem    = None
                current_wid = None

                while True:
                    wn_row = wn_cur.next()
                    if wn_row is None:
                        break

                    wn_wid, _seq, nd_ref = wn_row   # always exactly 3 columns

                    if wn_wid != current_wid:
                        # Flush previous <way>
                        if way_elem is not None:
                            xf.write(way_elem)

                        current_wid = wn_wid

                        # Advance ed_cur until ew_id >= wn_wid
                        while True:
                            ed_row = ed_cur.peek()
                            if ed_row is None:
                                break                   # edge cursor exhausted
                            ew_id = ed_row[0]           # first column = way_id
                            if ew_id < wn_wid:
                                ed_cur.next()           # skip orphaned edge row
                            else:
                                break

                        # Build <way> element with tags (if matched edge found)
                        way_elem = etree.Element("way", {
                            "id": str(wn_wid), "version": "1", "visible": "true",
                        })
                        ed_row = ed_cur.peek()
                        if ed_row is not None and ed_row[0] == wn_wid:
                            ed_cur.next()   # consume matched edge row
                            # Unpack all 6 columns — guaranteed by ed_sorted schema
                            _, name, highway, oneway, lanes, maxspeed = ed_row
                            for k, v in [
                                ("highway",  highway),
                                ("name",     name),
                                ("oneway",   oneway),
                                ("lanes",    lanes),
                                ("maxspeed", maxspeed),
                            ]:
                                if v is not None and str(v).strip():
                                    etree.SubElement(way_elem, "tag", {"k": k, "v": str(v)})

                    # Append <nd ref="…"> to current way
                    etree.SubElement(way_elem, "nd", {"ref": str(nd_ref)})

                # Flush last way
                if way_elem is not None:
                    xf.write(way_elem)

    # ── Cleanup sorted intermediates ───────────────────────────────────────────
    for p in (wn_sorted, ed_sorted):
        try:
            os.remove(p)
        except OSError:
            pass
