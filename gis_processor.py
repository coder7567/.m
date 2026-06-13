#!/usr/bin/env python3
"""
RUT_TRAILBLAZER GIS Processor
Parses raw OpenStreetMap (OSM) XML extracts and USFS/BLM spatial data,
classifying road segments into the RUT Road Type Hierarchy and exporting to GeoJSON.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

try:
    import osmium  # type: ignore
    OSMIUM_AVAILABLE = True
except Exception:
    osmium = None  # type: ignore
    OSMIUM_AVAILABLE = False

# ROAD TYPE HIERARCHY CONSTANTS
LEVEL_C = "Level C Roads (Unmaintained Dirt)"
LEVEL_B = "Level B Roads (Gravel/Dirt)"
UNVERIFIED_TRAILS = "Unverified Trails / Two-Tracks"
PRIMITIVE_ROADS = "Primitive Roads"
RESIDENTIAL_SHORTCUTS = "Residential Shortcuts"
PAVED_ROADS = "Paved Roads / Highways"

_FEATURES_START_RE = re.compile(r'"features"\s*:\s*\[')


def classify_osm_highway(tags: Dict[str, str]) -> str:
    """
    Classifies an OSM way into the RUT Road Type Hierarchy based on tags.
    """
    highway = tags.get("highway", "")
    surface = tags.get("surface", "").lower()
    tracktype = tags.get("tracktype", "").lower()
    service = tags.get("service", "").lower()
    if not highway:
        return PAVED_ROADS
    # Level C: Unmaintained/rough tracks
    if highway == "track" and (tracktype in ("grade5", "grade4") or surface in ("dirt", "mud", "clay", "earth")):
        return LEVEL_C
    # Level B: Maintained unpaved roads (gravel, dirt)
    if highway == "track" and (tracktype in ("grade2", "grade3") or surface in ("gravel", "fine_gravel", "pebbles", "crushed_rock")):
        return LEVEL_B

    # Unverified Trails / Two-Tracks
    if highway in ("path", "bridleway", "footway") or tags.get("motorcycle") == "yes" or tags.get("4wd_only") == "yes":
        return UNVERIFIED_TRAILS
    # Primitive Roads
    if highway in ("unclassified", "track") and surface in ("unpaved", "sand", "grass"):
        return PRIMITIVE_ROADS
    # Residential Shortcuts
    if highway == "residential" and service in ("alley", "driveway"):
        return RESIDENTIAL_SHORTCUTS
    # Default to Paved/Highways for standard roads unless explicitly unpaved
    if highway in ("motorway", "trunk", "primary", "secondary", "tertiary", "residential", "unclassified", "service"):
        if surface in ("dirt", "gravel", "unpaved", "sand", "pebbles", "mud"):
            return LEVEL_B
        return PAVED_ROADS
    return PAVED_ROADS


def write_feature(file_handle, feature: Dict[str, Any], is_first: bool) -> bool:
    """
    Stream a single GeoJSON feature immediately to disk.
    Returns the updated is_first flag.
    """
    if not is_first:
        file_handle.write(",")
    file_handle.write(json.dumps(feature, ensure_ascii=False, separators=(",", ":")))
    return False


def _open_geojson_stream(output_path: str):
    fh = open(output_path, "w", encoding="utf-8")
    fh.write('{"type":"FeatureCollection","features":[')
    return fh


def _close_geojson_stream(file_handle) -> None:
    file_handle.write("]}")
    file_handle.flush()
    file_handle.close()


def _extract_geojson_feature_array_start(buffer: str) -> Optional[int]:
    match = _FEATURES_START_RE.search(buffer)
    if not match:
        return None
    return match.end()


def _iter_geojson_features(geojson_path: str) -> Iterator[Dict[str, Any]]:
    """
    Stream features from a GeoJSON FeatureCollection without materializing the full file.
    """
    decoder = json.JSONDecoder()
    chunk_size = 1 << 20

    with open(geojson_path, "r", encoding="utf-8") as fh:
        buffer = ""
        start_idx: Optional[int] = None

        while start_idx is None:
            chunk = fh.read(chunk_size)
            if not chunk:
                return
            buffer += chunk
            start_idx = _extract_geojson_feature_array_start(buffer)
            if start_idx is not None:
                buffer = buffer[start_idx:]
                break

        idx = 0
        while True:
            while True:
                if idx >= len(buffer):
                    chunk = fh.read(chunk_size)
                    if not chunk:
                        return
                    buffer = buffer[idx:] + chunk
                    idx = 0
                    continue
                ch = buffer[idx]
                if ch in " \t\r\n,":
                    idx += 1
                    continue
                break

            if idx >= len(buffer):
                continue
            if buffer[idx] == "]":
                return

            try:
                obj, end = decoder.raw_decode(buffer, idx)
                yield obj
                idx = end
            except json.JSONDecodeError:
                chunk = fh.read(chunk_size)
                if not chunk:
                    raise
                buffer = buffer[idx:] + chunk
                idx = 0


def _iter_relevant_osm_tags(tags: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    """Keep only the tags needed by the existing classification and output schema."""
    relevant_keys = {"highway", "surface", "tracktype", "service", "motorcycle", "4wd_only", "name"}
    result: Dict[str, str] = {}
    for key, value in tags:
        if key in relevant_keys and value is not None:
            result[key] = value
    return result


class _StreamingOsmiumHandler(osmium.SimpleHandler if OSMIUM_AVAILABLE else object):
    def __init__(self, writer, is_first: bool):
        if OSMIUM_AVAILABLE:
            super().__init__()
        self.writer = writer
        self.is_first = is_first
        self.count = 0

    def way(self, w):  # type: ignore[override]
        tags = _iter_relevant_osm_tags((tag.k, tag.v) for tag in w.tags)
        if "highway" not in tags:
            return

        coords: List[List[float]] = []
        for node in w.nodes:
            if node.location.valid():
                coords.append([node.location.lon, node.location.lat])

        if len(coords) < 2:
            return

        road_type = classify_osm_highway(tags)
        feature = {
            "type": "Feature",
            "properties": {
                "id": f"osm_{w.id}",
                "source": "openstreetmap",
                "road_type": road_type,
                "name": tags.get("name", "Unnamed Path"),
                "surface": tags.get("surface", "unknown"),
                "highway_tag": tags.get("highway"),
                "tracktype": tags.get("tracktype", "unknown"),
            },
            "geometry": {
                "type": "LineString",
                "coordinates": coords,
            },
        }
        self.is_first = write_feature(self.writer, feature, self.is_first)
        self.count += 1


def parse_osm_xml_pyosmium(osm_file_path: str, writer, is_first: bool) -> Tuple[bool, int]:
    """
    Preferred OSM path: pyosmium streaming with node location cache.
    """
    if not OSMIUM_AVAILABLE:
        raise RuntimeError("pyosmium is not available")
    if not os.path.exists(osm_file_path):
        raise FileNotFoundError(f"OSM file not found: {osm_file_path}")

    print(f"Parsing OSM XML with pyosmium: {osm_file_path}...")
    handler = _StreamingOsmiumHandler(writer, is_first)
    handler.apply_file(osm_file_path, locations=True, idx="sparse_mem_array")
    print(f"Successfully processed {handler.count} roads from OSM.")
    return handler.is_first, handler.count


def parse_osm_xml_python(osm_file_path: str, writer, is_first: bool) -> Tuple[bool, int]:
    """
    Pure-Python fallback using ElementTree.iterparse in two passes.
    Pass 1: collect highway ways + required node IDs.
    Pass 2: collect only coordinates for required node IDs.
    Then stream features directly to the GeoJSON writer.
    """
    if not os.path.exists(osm_file_path):
        raise FileNotFoundError(f"OSM file not found: {osm_file_path}")

    print(f"Parsing OSM XML with Python fallback: {osm_file_path}...")

    required_nodes: set[int] = set()
    temp_way_file = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".ndjson")
    temp_way_path = temp_way_file.name
    way_count = 0

    try:
        # PASS 1: collect only highway ways and required node ids.
        context = ET.iterparse(osm_file_path, events=("end",))
        for _, elem in context:
            if elem.tag != "way":
                if elem.tag == "node":
                    elem.clear()
                continue

            way_id = elem.get("id")
            tags: Dict[str, str] = {}
            node_refs: List[int] = []

            for child in elem:
                if child.tag == "tag":
                    k = child.get("k")
                    v = child.get("v")
                    if k and v and k in {"highway", "surface", "tracktype", "service", "motorcycle", "4wd_only", "name"}:
                        tags[k] = v
                elif child.tag == "nd":
                    ref = child.get("ref")
                    if ref is not None:
                        try:
                            node_refs.append(int(ref))
                        except ValueError:
                            pass

            if "highway" in tags and way_id is not None and len(node_refs) >= 2:
                required_nodes.update(node_refs)
                temp_way_file.write(json.dumps({"id": way_id, "tags": tags, "node_refs": node_refs}, separators=(",", ":")))
                temp_way_file.write("\n")
                way_count += 1

            elem.clear()

        temp_way_file.close()

        # PASS 2: collect coordinates only for required nodes.
        nodes: Dict[int, Tuple[float, float]] = {}
        context = ET.iterparse(osm_file_path, events=("end",))
        for _, elem in context:
            if elem.tag == "node":
                node_id = elem.get("id")
                if node_id is not None:
                    try:
                        node_int = int(node_id)
                    except ValueError:
                        node_int = -1
                    if node_int in required_nodes:
                        try:
                            lat = float(elem.get("lat"))
                            lon = float(elem.get("lon"))
                            nodes[node_int] = (lon, lat)
                        except (ValueError, TypeError):
                            pass
                elem.clear()
            elif elem.tag == "way":
                elem.clear()

        # FINAL: stream features immediately.
        print(f"Processing geometries for {way_count} ways...")
        processed = 0
        with open(temp_way_path, "r", encoding="utf-8") as way_reader:
            for line in way_reader:
                if not line.strip():
                    continue
                record = json.loads(line)
                tags = record["tags"]
                node_refs = record["node_refs"]
                coords: List[List[float]] = []
                for ref in node_refs:
                    coord = nodes.get(ref)
                    if coord is not None:
                        coords.append([coord[0], coord[1]])

                if len(coords) < 2:
                    continue

                road_type = classify_osm_highway(tags)
                feature = {
                    "type": "Feature",
                    "properties": {
                        "id": f"osm_{record['id']}",
                        "source": "openstreetmap",
                        "road_type": road_type,
                        "name": tags.get("name", "Unnamed Path"),
                        "surface": tags.get("surface", "unknown"),
                        "highway_tag": tags.get("highway"),
                        "tracktype": tags.get("tracktype", "unknown"),
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": coords,
                    },
                }
                is_first = write_feature(writer, feature, is_first)
                processed += 1

        print(f"Successfully processed {processed} roads from OSM.")
        return is_first, processed
    finally:
        try:
            temp_way_file.close()
        except Exception:
            pass
        try:
            os.remove(temp_way_path)
        except OSError:
            pass


def ingest_usfs_blm_geojson(geojson_path: str, source: str, writer, is_first: bool) -> Tuple[bool, int]:
    """
    Parses agency GeoJSON (BLM or USFS) and maps attributes to the RUT hierarchy.
    Streams features one at a time to avoid materializing large files in memory.
    """
    if not os.path.exists(geojson_path):
        print(f"Warning: {source} file not found at {geojson_path}. Skipping.")
        return is_first, 0

    print(f"Parsing {source} GeoJSON: {geojson_path}...")
    processed = 0

    for feat in _iter_geojson_features(geojson_path):
        props = feat.get("properties", {})
        geom = feat.get("geometry", {})

        if geom.get("type") not in ("LineString", "MultiLineString"):
            continue

        # USFS/BLM attributes mapping
        # USFS often uses 'SURFACE_TYPE' or 'OPER_MAINT_LEVEL'
        # BLM access datasets use similar attributes.
        surf_type = (props.get("SURFACE_TYPE") or props.get("surface") or "").lower()
        maint_level = str(props.get("OPER_MAINT_LEVEL") or props.get("maint_level") or "")

        # Mapping rules based on typical USFS/BLM attributes
        if maint_level in ("1", "Basic Custodial Care (Closed)") or "dirt" in surf_type or "native" in surf_type:
            road_type = LEVEL_C
        elif maint_level in ("2", "High Clearance Vehicles") or "gravel" in surf_type or "crushed" in surf_type:
            road_type = LEVEL_B
        elif "paved" in surf_type or "asphalt" in surf_type or maint_level in ("4", "5"):
            road_type = PAVED_ROADS
        else:
            road_type = PRIMITIVE_ROADS

        new_feat = {
            "type": "Feature",
            "properties": {
                "id": f"{source.lower()}_{props.get('OBJECTID') or props.get('id') or processed}",
                "source": source.lower(),
                "road_type": road_type,
                "name": props.get("ROAD_NAME") or props.get("name") or "Unnamed Agency Route",
                "surface": surf_type or "unknown",
                "agency_maint_level": maint_level,
            },
            "geometry": geom,
        }
        is_first = write_feature(writer, new_feat, is_first)
        processed += 1

    print(f"Successfully processed {processed} roads from {source}.")
    return is_first, processed


def export_to_geojson(output_path: str):
    """
    Open output immediately and write the header up front.
    Caller is responsible for closing the FeatureCollection.
    """
    print(f"Writing unified output to {output_path}...")
    return _open_geojson_stream(output_path)


def _choose_osm_mode(requested_mode: str) -> str:
    requested_mode = requested_mode.lower().strip()
    if requested_mode == "auto":
        return "pyosmium" if OSMIUM_AVAILABLE else "python"
    if requested_mode == "pyosmium" and not OSMIUM_AVAILABLE:
        print("Warning: pyosmium requested but not available. Falling back to Python XML parser.")
        return "python"
    return requested_mode


def main():
    parser = argparse.ArgumentParser(description="RUT TrailBlazer GIS Processing Pipeline")
    parser.add_argument("--osm", help="Path to raw OSM XML file", default=None)
    parser.add_argument("--usfs", help="Path to USFS GeoJSON file", default=None)
    parser.add_argument("--blm", help="Path to BLM GeoJSON file", default=None)
    parser.add_argument("--output", help="Path to write final GeoJSON output", required=True)
    parser.add_argument(
        "--osm-parser",
        choices=("auto", "pyosmium", "python"),
        default="auto",
        help="OSM parser backend. Default auto-selects pyosmium when available.",
    )
    args = parser.parse_args()

    osm_mode = _choose_osm_mode(args.osm_parser)
    out_fh = export_to_geojson(args.output)
    total_written = 0
    is_first = True

    try:
        if args.osm:
            try:
                if osm_mode == "pyosmium":
                    is_first, count = parse_osm_xml_pyosmium(args.osm, out_fh, is_first)
                else:
                    is_first, count = parse_osm_xml_python(args.osm, out_fh, is_first)
                total_written += count
            except Exception as e:
                print(f"Error parsing OSM XML: {e}")

        if args.usfs:
            try:
                is_first, count = ingest_usfs_blm_geojson(args.usfs, "USFS", out_fh, is_first)
                total_written += count
            except Exception as e:
                print(f"Error parsing USFS GeoJSON: {e}")

        if args.blm:
            try:
                is_first, count = ingest_usfs_blm_geojson(args.blm, "BLM", out_fh, is_first)
                total_written += count
            except Exception as e:
                print(f"Error parsing BLM GeoJSON: {e}")

        if total_written == 0:
            print("Warning: No features were processed.")
    finally:
        _close_geojson_stream(out_fh)
        gc.collect()


if __name__ == "__main__":
    main()
