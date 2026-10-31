#!/usr/bin/env python3
"""Local IFC2x3 / IFC4 → Job OS ingest (Ariel Mushana electrical drafting).

No Autodesk APS, no Revit license, no paid APIs. IfcOpenShell reads the IFC;
ezdxf writes a gray architectural DXF the Job OS pipeline can import.

    python3 -m pip install -r tools/requirements-ifc.txt
    python3 tools/ifc_ingest.py model.ifc /workspace/jobs/herzl_10 --create-job
    python3 tools/run_pipeline.py /workspace/jobs/herzl_10

Rooms are written in centimetres (x, y) and square metres (area), matching
tools/job_template.yaml. The DXF is in centimetres ($INSUNITS = 5) and is
shifted so the plan's minimum corner is (0, 0), which lines up with
``cad_base: null`` (extents corner lands on the origin).

A real architect IFC is the manual smoke input. ``tests/test_ifc_ingest.py``
builds a tiny synthetic model with IfcOpenShell and runs this script.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import traceback
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np

CIRCUITS_HEADER = "no,desc,breaker,poles,cable,phase,group,kw,length,note"

# Point-symbol guesses. Confirm against `python3 tools/symbol_editor.py list`
# before running the pipeline. Classes without an entry are counted and written
# to inputs/ifc_electrical.json only.
SYMBOL_BY_CLASS = {
    "IfcOutlet": "SOCKET_2",
    "IfcLightFixture": "LIGHT",
    "IfcLamp": "LIGHT",
    "IfcSwitchingDevice": "SWITCH",
    "IfcElectricDistributionBoard": "PANEL",
    "IfcElectricDistributionPoint": "PANEL",  # IFC2X3
    "IfcAlarm": "SMOKE",
    "IfcCommunicationsAppliance": "DATA",
    "IfcAudioVisualAppliance": "TV",
}

ELECTRICAL_CLASSES = (
    "IfcOutlet",
    "IfcLightFixture",
    "IfcLamp",
    "IfcSwitchingDevice",
    "IfcElectricDistributionBoard",
    "IfcElectricDistributionPoint",
    "IfcCableSegment",
    "IfcCableCarrierSegment",
    "IfcCableCarrierFitting",
    "IfcCableFitting",
    "IfcJunctionBox",
    "IfcElectricAppliance",
    "IfcProtectiveDevice",
    "IfcTransformer",
    "IfcMotorConnection",
    "IfcAudioVisualAppliance",
    "IfcCommunicationsAppliance",
    "IfcAlarm",
    "IfcSensor",
    "IfcActuator",
    "IfcElectricFlowStorageDevice",
    "IfcElectricGenerator",
    "IfcSolarDevice",
)

LAYER_BY_KIND = {
    "wall": "A-WALL",
    "door": "A-DOOR",
    "window": "A-WIND",
}

TEMPLATE_PATH = Path(__file__).with_name("job_template.yaml")

_X2_RE = re.compile(r"\\X2\\([0-9A-Fa-f]+)\\X0\\")
_X4_RE = re.compile(r"\\X4\\([0-9A-Fa-f]+)\\X0\\")
_X_RE = re.compile(r"\\X\\([0-9A-Fa-f]{2})")
_S_RE = re.compile(r"\\S\\(.)")


def decode_ifc_string(value) -> str:
    """Best-effort IFC string → plain Unicode, including Hebrew.

    IfcOpenShell usually returns decoded Python strings. Some files still carry
    ``\\X2\\....\\X0\\`` escapes (typical of IFC2X3). Decoding is idempotent on
    text that is already Unicode.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(part for part in (decode_ifc_string(v) for v in value) if part)
    if hasattr(value, "wrappedValue"):
        return decode_ifc_string(value.wrappedValue)
    if not isinstance(value, str):
        value = str(value)

    def _chars(hexpart: str, width: int) -> str:
        out = []
        for i in range(0, len(hexpart) - width + 1, width):
            chunk = hexpart[i : i + width]
            try:
                out.append(chr(int(chunk, 16)))
            except ValueError:
                continue
        return "".join(out)

    text = _X2_RE.sub(lambda m: _chars(m.group(1), 4), value)
    text = _X4_RE.sub(lambda m: _chars(m.group(1), 8), text)
    text = _X_RE.sub(lambda m: chr(int(m.group(1), 16)), text)
    text = _S_RE.sub(lambda m: chr(ord(m.group(1)[0]) + 128), text)
    text = text.replace("\\\\", "\\")
    cleaned = []
    for ch in text:
        o = ord(ch)
        if ch in "\n\t":
            cleaned.append(" ")
        elif o < 32 or o == 0x7F:
            continue
        elif 0x200E <= o <= 0x200F or 0x202A <= o <= 0x202E or o == 0xFEFF:
            continue
        else:
            cleaned.append(ch)
    text = "".join(cleaned)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def yaml_number(value: float) -> str:
    number = round(float(value), 2)
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def polygon_metrics(points_cm: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Return (centroid_x_cm, centroid_y_cm, area_m2) for one ring."""
    pts = [(float(x), float(y)) for x, y in points_cm]
    if len(pts) >= 2 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 0.05:
        pts = pts[:-1]
    if not pts:
        return 0.0, 0.0, 0.0
    if len(pts) < 3:
        return (
            sum(p[0] for p in pts) / len(pts),
            sum(p[1] for p in pts) / len(pts),
            0.0,
        )
    twice = 0.0
    cx = 0.0
    cy = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        cross = x1 * y2 - x2 * y1
        twice += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    area_cm2 = twice * 0.5
    if abs(area_cm2) < 1e-6:
        return (
            sum(p[0] for p in pts) / n,
            sum(p[1] for p in pts) / n,
            0.0,
        )
    return cx / (6.0 * area_cm2), cy / (6.0 * area_cm2), abs(area_cm2) / 10000.0


def _point_in_ring(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    pts = ring
    if len(pts) >= 2 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 0.05:
        pts = pts[:-1]
    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xinter = (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1
            if x < xinter:
                inside = not inside
    return inside


def room_from_loops(loops: list[list[tuple[float, float]]]) -> tuple[float, float, float] | None:
    """Largest ring is the footprint; smaller rings inside it are holes."""
    measured = []
    for loop in loops:
        if len(loop) < 3:
            continue
        x, y, area = polygon_metrics(loop)
        if area <= 0:
            continue
        measured.append((area, x, y, loop))
    if not measured:
        return None
    measured.sort(key=lambda item: item[0], reverse=True)
    area, x, y, outer = measured[0]
    for hole_area, hx, hy, _hole in measured[1:]:
        if _point_in_ring(hx, hy, outer):
            area -= hole_area
    return x, y, max(area, 0.0)


def plan_lines_from_mesh(vertices_cm, triangles, z_tol_cm: float = 2.0):
    """Plan outline of a solid mesh, as XY segments in centimetres.

    A closed solid shares every edge between two faces, so "boundary edges"
    (used once) do not exist. The plan outline is the perimeter of the
    horizontal caps: an edge kept when it belongs to exactly one horizontal
    face (the other face is vertical). Diagonals inside a cap belong to two
    horizontal triangles and are dropped. Top and bottom caps of a vertical
    wall both contribute the same plan rectangle.
    """
    horizontal_faces: dict[tuple[int, int], int] = defaultdict(int)
    n = len(vertices_cm)
    for tri in triangles:
        if len(tri) != 3:
            continue
        i, j, k = (int(tri[0]), int(tri[1]), int(tri[2]))
        if min(i, j, k) < 0 or max(i, j, k) >= n:
            continue
        zs = (vertices_cm[i][2], vertices_cm[j][2], vertices_cm[k][2])
        if max(zs) - min(zs) > z_tol_cm:
            continue
        for a, b in ((i, j), (j, k), (k, i)):
            if a == b:
                continue
            if a > b:
                a, b = b, a
            horizontal_faces[(a, b)] += 1
    segments = []
    for (a, b), count in horizontal_faces.items():
        if count != 1:
            continue
        p = vertices_cm[a]
        q = vertices_cm[b]
        if abs(p[2] - q[2]) > z_tol_cm:
            continue
        if math.hypot(p[0] - q[0], p[1] - q[1]) < 0.2:
            continue
        segments.append((float(p[0]), float(p[1]), float(q[0]), float(q[1])))
    return segments


def _dedup_segments(segments):
    seen = set()
    out = []
    for x1, y1, x2, y2 in segments:
        a = (round(x1, 1), round(y1, 1))
        b = (round(x2, 1), round(y2, 1))
        if a == b:
            continue
        key = (a, b) if a <= b else (b, a)
        if key in seen:
            continue
        seen.add(key)
        out.append((x1, y1, x2, y2))
    return out


def _safe_by_type(model, ifc_class: str):
    try:
        return model.by_type(ifc_class)
    except RuntimeError:
        return []
    except Exception:
        return []


def _length_scale_to_m(model) -> float:
    import ifcopenshell.util.unit as unit

    try:
        scale = float(unit.calculate_unit_scale(model, "LENGTHUNIT"))
    except Exception:
        scale = 1.0
    if scale <= 0:
        return 1.0
    return scale


def area_scale_to_m2(model) -> float:
    """Multiply a project area quantity by this to get square metres.

    IfcOpenShell's ``calculate_unit_scale`` applies an SI prefix once. In IFC,
    ``MILLI`` + ``SQUARE_METRE`` means square millimetres (prefix on the metre,
    then squared), so the prefix is applied again here.
    """
    import ifcopenshell.util.unit as unit

    try:
        scale = float(unit.calculate_unit_scale(model, "AREAUNIT"))
    except Exception:
        scale = 1.0
    if scale <= 0:
        scale = 1.0
    try:
        assigned = unit.get_project_unit(model, "AREAUNIT")
    except Exception:
        assigned = None
    if assigned is not None and assigned.is_a("IfcSIUnit"):
        name = assigned.Name or ""
        if assigned.Prefix and ("SQUARE" in name or "CUBIC" in name):
            prefix = float(unit.get_prefix_multiplier(assigned.Prefix))
            scale *= prefix
            if "CUBIC" in name:
                scale *= prefix
    return scale


def _apply_matrix(matrix, points):
    out = []
    for pt in points:
        if len(pt) >= 3:
            x, y, z = float(pt[0]), float(pt[1]), float(pt[2])
        else:
            x, y, z = float(pt[0]), float(pt[1]), 0.0
        transformed = matrix @ np.array([x, y, z, 1.0], dtype=float)
        out.append((float(transformed[0]), float(transformed[1]), float(transformed[2])))
    return out


def _element_matrix(element):
    import ifcopenshell.util.placement as placement

    local = getattr(element, "ObjectPlacement", None)
    if local is None:
        return np.eye(4)
    try:
        if local.is_a("IfcLocalPlacement"):
            return np.array(placement.get_local_placement(local), dtype=float)
    except Exception:
        return np.eye(4)
    return np.eye(4)


def _axis2_matrix(placement_entity):
    import ifcopenshell.util.placement as placement

    if placement_entity is None:
        return np.eye(4)
    try:
        return np.array(placement.get_axis2placement(placement_entity), dtype=float)
    except Exception:
        return np.eye(4)


def _xy(points_xyz):
    return [(float(p[0]), float(p[1])) for p in points_xyz]


def _to_cm(points_xyz, scale_to_m: float):
    factor = scale_to_m * 100.0
    return [(float(p[0]) * factor, float(p[1]) * factor) for p in points_xyz]


def _curve_points(curve):
    if curve is None:
        return []
    try:
        if curve.is_a("IfcPolyline"):
            pts = []
            for point in curve.Points or []:
                coords = tuple(float(c) for c in point.Coordinates)
                pts.append(coords)
            return pts
        if curve.is_a("IfcIndexedPolyCurve"):
            return _indexed_polycurve_points(curve)
        if curve.is_a("IfcCompositeCurve"):
            pts = []
            for segment in curve.Segments or []:
                parent = getattr(segment, "ParentCurve", None)
                pts.extend(_curve_points(parent))
            return pts
        if curve.is_a("IfcTrimmedCurve"):
            return _curve_points(getattr(curve, "BasisCurve", None))
        if curve.is_a("IfcGeometricCurveSet") or curve.is_a("IfcGeometricSet"):
            pts = []
            for element in curve.Elements or []:
                pts.extend(_curve_points(element))
            return pts
    except Exception:
        return []
    return []


def _indexed_polycurve_points(curve):
    point_list = getattr(curve, "Points", None)
    if point_list is None or not hasattr(point_list, "CoordList"):
        return []
    coords = [tuple(float(c) for c in row) for row in point_list.CoordList]
    segments = getattr(curve, "Segments", None)
    if not segments:
        return coords
    ordered = []
    for segment in segments:
        indexes = _index_values(segment)
        if not indexes:
            continue
        for index in indexes:
            pos = int(index) - 1
            if 0 <= pos < len(coords):
                if not ordered or ordered[-1] != coords[pos]:
                    ordered.append(coords[pos])
    return ordered or coords


def _index_values(segment):
    if isinstance(segment, (list, tuple)):
        return list(segment)
    wrapped = getattr(segment, "wrappedValue", None)
    if isinstance(wrapped, (list, tuple)):
        return list(wrapped)
    for attr in ("CoordIndex",):
        value = getattr(segment, attr, None)
        if isinstance(value, (list, tuple)):
            return list(value)
    try:
        return [int(v) for v in segment]
    except Exception:
        return []


def _profile_position(profile, points_2d):
    pos = getattr(profile, "Position", None)
    if pos is None:
        return [(float(x), float(y), 0.0) for x, y in points_2d]
    loc = pos.Location.Coordinates
    ox, oy = float(loc[0]), float(loc[1])
    angle = 0.0
    ref = getattr(pos, "RefDirection", None)
    if ref is not None:
        ratios = ref.DirectionRatios
        angle = math.atan2(float(ratios[1]), float(ratios[0]))
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    out = []
    for x, y in points_2d:
        out.append((x * cos_a - y * sin_a + ox, x * sin_a + y * cos_a + oy, 0.0))
    return out


def _profile_loops(profile):
    if profile is None:
        return []
    try:
        if profile.is_a("IfcArbitraryClosedProfileDef"):
            loops = [_curve_points(profile.OuterCurve)]
            for inner in getattr(profile, "InnerCurves", None) or []:
                loops.append(_curve_points(inner))
            return [loop for loop in loops if len(loop) >= 2]
        if profile.is_a("IfcRectangleProfileDef") or profile.is_a("IfcRoundedRectangleProfileDef"):
            half_x = float(profile.XDim) / 2.0
            half_y = float(profile.YDim) / 2.0
            rect = [
                (-half_x, -half_y),
                (half_x, -half_y),
                (half_x, half_y),
                (-half_x, half_y),
                (-half_x, -half_y),
            ]
            return [_profile_position(profile, rect)]
        if profile.is_a("IfcCircleProfileDef"):
            radius = float(profile.Radius)
            steps = 16
            circle = [
                (radius * math.cos(2 * math.pi * i / steps), radius * math.sin(2 * math.pi * i / steps))
                for i in range(steps + 1)
            ]
            return [_profile_position(profile, circle)]
    except Exception:
        return []
    return []


def _extrusion_is_vertical(item, world_matrix) -> bool:
    direction = getattr(item, "ExtrudedDirection", None)
    ratios = (0.0, 0.0, 1.0)
    if direction is not None:
        raw = list(direction.DirectionRatios)
        while len(raw) < 3:
            raw.append(0.0)
        ratios = (float(raw[0]), float(raw[1]), float(raw[2]))
    vec = world_matrix @ np.array([ratios[0], ratios[1], ratios[2], 0.0], dtype=float)
    length = float(np.linalg.norm(vec[:3]))
    if length < 1e-9:
        return True
    return abs(float(vec[2])) / length >= 0.5


def _loops_from_item(item, matrix):
    """Return plan loops in project units (XYZ), or None when this item is not a plan profile."""
    if item is None:
        return []
    try:
        if item.is_a("IfcExtrudedAreaSolid"):
            solid = _axis2_matrix(getattr(item, "Position", None))
            world = matrix @ solid
            if not _extrusion_is_vertical(item, world):
                return []
            loops = []
            for loop in _profile_loops(item.SweptArea):
                xyz = loop if loop and len(loop[0]) >= 3 else [(p[0], p[1], 0.0) for p in loop]
                loops.append(_apply_matrix(world, xyz))
            return loops
        if item.is_a("IfcBooleanResult") or item.is_a("IfcBooleanClippingResult"):
            return _loops_from_item(getattr(item, "FirstOperand", None), matrix)
        if item.is_a("IfcMappedItem"):
            import ifcopenshell.util.placement as placement

            mapped = placement.get_mappeditem_transformation(item)
            if mapped is None:
                return []
            combined = matrix @ np.array(mapped, dtype=float)
            rep = item.MappingSource.MappedRepresentation
            loops = []
            for sub in rep.Items or []:
                loops.extend(_loops_from_item(sub, combined))
            return loops
        if item.is_a("IfcGeometricCurveSet") or item.is_a("IfcGeometricSet"):
            loops = []
            for element in item.Elements or []:
                pts = _curve_points(element)
                if len(pts) >= 2:
                    loops.append(_apply_matrix(matrix, pts))
            return loops
        if item.is_a("IfcPolyline") or item.is_a("IfcIndexedPolyCurve") or item.is_a("IfcCompositeCurve"):
            pts = _curve_points(item)
            if len(pts) >= 2:
                return [_apply_matrix(matrix, pts)]
    except Exception:
        return []
    return []


def _rep_rank(element, rep) -> int:
    ident = (getattr(rep, "RepresentationIdentifier", None) or "").lower()
    if element.is_a("IfcSpace"):
        order = {"footprint": 0, "plan": 1, "body": 2, "axis": 3}
    else:
        order = {"body": 0, "footprint": 1, "plan": 2, "axis": 3}
    return order.get(ident, 5)


def _semantic_loops_cm(element, scale_to_m: float):
    rep = getattr(element, "Representation", None)
    if rep is None:
        return []
    reps = list(rep.Representations or [])
    reps.sort(key=lambda item: _rep_rank(element, item))
    matrix = _element_matrix(element)
    for shape_rep in reps:
        loops = []
        for item in shape_rep.Items or []:
            loops.extend(_loops_from_item(item, matrix))
        if loops:
            return [_to_cm(loop, scale_to_m) for loop in loops if len(loop) >= 2]
    return []


def _tessellation_segments_cm(element, scale_to_m: float):
    rep = getattr(element, "Representation", None)
    if rep is None:
        return []
    matrix = _element_matrix(element)
    segments = []
    for shape_rep in rep.Representations or []:
        for item in shape_rep.Items or []:
            segments.extend(_segments_from_tessellation(item, matrix, scale_to_m))
    return _dedup_segments(segments)


def _segments_from_tessellation(item, matrix, scale_to_m: float):
    if item is None:
        return []
    try:
        if item.is_a("IfcMappedItem"):
            import ifcopenshell.util.placement as placement

            mapped = placement.get_mappeditem_transformation(item)
            if mapped is None:
                return []
            combined = matrix @ np.array(mapped, dtype=float)
            segments = []
            for sub in item.MappingSource.MappedRepresentation.Items or []:
                segments.extend(_segments_from_tessellation(sub, combined, scale_to_m))
            return segments
        if item.is_a("IfcBooleanResult"):
            return _segments_from_tessellation(getattr(item, "FirstOperand", None), matrix, scale_to_m)
        coords, tris = _mesh_coords_and_tris(item)
        if coords and tris:
            world = _apply_matrix(matrix, coords)
            factor = scale_to_m * 100.0
            verts_cm = [(p[0] * factor, p[1] * factor, p[2] * factor) for p in world]
            return plan_lines_from_mesh(verts_cm, tris)
        if item.is_a("IfcFacetedBrep") or item.is_a("IfcFacetedBrepWithVoids"):
            return _brep_plan_segments(item, matrix, scale_to_m)
    except Exception:
        return []
    return []


def _mesh_coords_and_tris(item):
    if item.is_a("IfcTriangulatedFaceSet"):
        coords = [tuple(float(c) for c in row) for row in item.Coordinates.CoordList]
        tris = []
        for tri in item.CoordIndex or []:
            idxs = [int(i) - 1 for i in tri]
            if len(idxs) == 3:
                tris.append(tuple(idxs))
        return coords, tris
    if item.is_a("IfcPolygonalFaceSet"):
        coords = [tuple(float(c) for c in row) for row in item.Coordinates.CoordList]
        tris = []
        for face in item.Faces or []:
            idxs = [int(i) - 1 for i in (face.CoordIndex or [])]
            for i in range(1, len(idxs) - 1):
                tris.append((idxs[0], idxs[i], idxs[i + 1]))
        return coords, tris
    return [], []


def _brep_plan_segments(item, matrix, scale_to_m: float):
    shell = getattr(item, "Outer", None)
    if shell is None:
        return []
    factor = scale_to_m * 100.0
    segments = []
    for face in getattr(shell, "CfsFaces", None) or []:
        for bound in getattr(face, "Bounds", None) or []:
            poly = getattr(bound, "Bound", None)
            if poly is None or not poly.is_a("IfcPolyLoop"):
                continue
            local = []
            for point in poly.Polygon or []:
                coords = point.Coordinates
                z = float(coords[2]) if len(coords) > 2 else 0.0
                local.append((float(coords[0]), float(coords[1]), z))
            if len(local) < 2:
                continue
            world = _apply_matrix(matrix, local)
            zs = [p[2] * factor for p in world]
            if max(zs) - min(zs) > 2.0:
                continue
            xy = [(p[0] * factor, p[1] * factor) for p in world]
            for i in range(len(xy)):
                x1, y1 = xy[i]
                x2, y2 = xy[(i + 1) % len(xy)]
                segments.append((x1, y1, x2, y2))
    return segments


def _geom_segments_cm(element):
    try:
        import ifcopenshell.geom as geom
    except Exception:
        return []
    settings = getattr(_geom_segments_cm, "_settings", None)
    if settings is None:
        settings = geom.settings()
        settings.set(settings.USE_WORLD_COORDS, True)
        _geom_segments_cm._settings = settings
    try:
        shape = geom.create_shape(settings, element)
    except Exception:
        return []
    verts = list(shape.geometry.verts)
    faces = list(shape.geometry.faces)
    if len(verts) < 9 or len(faces) < 3:
        return []
    vertices = []
    for i in range(0, len(verts), 3):
        # geom + USE_WORLD_COORDS is metres, already in world coordinates.
        vertices.append((verts[i] * 100.0, verts[i + 1] * 100.0, verts[i + 2] * 100.0))
    tris = []
    for i in range(0, len(faces) - 2, 3):
        tris.append((faces[i], faces[i + 1], faces[i + 2]))
    return _dedup_segments(plan_lines_from_mesh(vertices, tris))


def extract_plan_cm(element, scale_to_m: float, allow_geom: bool = True):
    """Return (loops_xy_cm, segments_xy_cm) in world centimetres, before origin shift."""
    loops = _semantic_loops_cm(element, scale_to_m)
    if loops:
        return loops, []
    segments = _tessellation_segments_cm(element, scale_to_m)
    if segments:
        return [], segments
    if allow_geom:
        segments = _geom_segments_cm(element)
        if segments:
            return [], segments
    return [], []


def _quantity_area_m2(element, model) -> float | None:
    import ifcopenshell.util.element as element_util

    try:
        psets = element_util.get_psets(element) or {}
    except Exception:
        return None
    scale = area_scale_to_m2(model)
    preferred = ("NetFloorArea", "GrossFloorArea", "NetArea", "GrossArea")
    found = {key: None for key in preferred}
    for props in psets.values():
        if not isinstance(props, dict):
            continue
        for key in preferred:
            value = props.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                if found[key] is None:
                    found[key] = float(value) * scale
    for key in preferred:
        if found[key] is not None:
            return found[key]
    return None


def _choose_area(qto_m2: float | None, computed_m2: float) -> float:
    if qto_m2 is None:
        return computed_m2
    if computed_m2 > 0.05 and (qto_m2 > computed_m2 * 20.0 or qto_m2 < computed_m2 / 20.0):
        # Quantity unit and the stated project area unit disagree. The polygon wins.
        return computed_m2
    return qto_m2


def _product_name(element, fallback: str) -> str:
    long_name = decode_ifc_string(getattr(element, "LongName", None))
    name = decode_ifc_string(getattr(element, "Name", None))
    if long_name:
        return long_name
    if name:
        return name
    object_type = decode_ifc_string(getattr(element, "ObjectType", None))
    return object_type or fallback


def _has_client_role(entity) -> bool:
    roles = getattr(entity, "Roles", None) or []
    for role in roles:
        if getattr(role, "Role", None) == "CLIENT":
            return True
        custom = decode_ifc_string(getattr(role, "UserDefinedRole", None))
        lowered = custom.lower()
        if "client" in lowered or "owner" in lowered or "לקוח" in custom or "מזמין" in custom:
            return True
    return False


def _person_name(person) -> str:
    parts = []
    for attr in ("PrefixTitles", "GivenName", "MiddleNames", "FamilyName", "SuffixTitles"):
        value = getattr(person, attr, None)
        text = decode_ifc_string(value)
        if text:
            parts.append(text)
    return " ".join(parts)


def _find_client(model) -> str:
    for org in _safe_by_type(model, "IfcOrganization"):
        if _has_client_role(org):
            name = decode_ifc_string(getattr(org, "Name", None))
            if name:
                return name
    for person in _safe_by_type(model, "IfcPerson"):
        if _has_client_role(person):
            name = _person_name(person)
            if name:
                return name
    for project in _safe_by_type(model, "IfcProject"):
        try:
            import ifcopenshell.util.element as element_util

            psets = element_util.get_psets(project) or {}
        except Exception:
            psets = {}
        for props in psets.values():
            if not isinstance(props, dict):
                continue
            for key, value in props.items():
                if key == "id" or not isinstance(value, str):
                    continue
                label = key.lower()
                if label in {"client", "owner", "buildingowner"} or "לקוח" in key or "מזמין" in key:
                    text = decode_ifc_string(value)
                    if text:
                        return text
    return ""


def _format_address(address) -> str:
    if address is None or not hasattr(address, "is_a"):
        return ""
    if not address.is_a("IfcPostalAddress"):
        return ""
    parts = []
    for line in address.AddressLines or []:
        text = decode_ifc_string(line)
        if text:
            parts.append(text)
    for attr in ("Town", "Region", "PostalCode", "Country"):
        text = decode_ifc_string(getattr(address, attr, None))
        if text and text not in parts:
            parts.append(text)
    return ", ".join(parts)


def _find_address(model) -> str:
    for building in _safe_by_type(model, "IfcBuilding"):
        text = _format_address(getattr(building, "BuildingAddress", None))
        if text:
            return text
    for site in _safe_by_type(model, "IfcSite"):
        text = _format_address(getattr(site, "SiteAddress", None))
        if text:
            return text
    return ""


def _find_project_name(model) -> str:
    candidates = []
    for project in _safe_by_type(model, "IfcProject"):
        candidates.append(decode_ifc_string(getattr(project, "LongName", None)))
        candidates.append(decode_ifc_string(getattr(project, "Name", None)))
    for building in _safe_by_type(model, "IfcBuilding"):
        candidates.append(decode_ifc_string(getattr(building, "LongName", None)))
        candidates.append(decode_ifc_string(getattr(building, "Name", None)))
    for site in _safe_by_type(model, "IfcSite"):
        candidates.append(decode_ifc_string(getattr(site, "LongName", None)))
        candidates.append(decode_ifc_string(getattr(site, "Name", None)))
    for text in candidates:
        if text:
            return text
    return ""


def _storey_z_m(element, scale_to_m: float) -> float | None:
    import ifcopenshell.util.element as element_util
    import ifcopenshell.util.placement as placement

    try:
        container = element_util.get_container(element)
    except Exception:
        container = None
    if container is None or not container.is_a("IfcBuildingStorey"):
        return None
    try:
        return float(placement.get_storey_elevation(container)) * scale_to_m
    except Exception:
        return None


def _placement_cm(element, scale_to_m: float):
    """Return (x_cm, y_cm, z_m, rot_deg) from the world placement, or None."""
    if getattr(element, "ObjectPlacement", None) is None:
        return None
    matrix = _element_matrix(element)
    factor = scale_to_m * 100.0
    x = float(matrix[0][3]) * factor
    y = float(matrix[1][3]) * factor
    z_m = float(matrix[2][3]) * scale_to_m
    rot = math.degrees(math.atan2(float(matrix[1][0]), float(matrix[0][0])))
    return x, y, z_m, rot


def _snap90(degrees: float) -> int:
    snapped = int(round(degrees / 90.0)) * 90
    return snapped % 360


def _height_cm(z_m: float, storey_z_m: float | None) -> float | None:
    base = storey_z_m if storey_z_m is not None else 0.0
    height = (z_m - base) * 100.0
    if height < -5.0 or height > 600.0:
        return None
    return round(height, 1)


def _collect_xy(loops, segments, extra_points):
    xs = []
    ys = []
    for loop in loops:
        for x, y in loop:
            xs.append(x)
            ys.append(y)
    for x1, y1, x2, y2 in segments:
        xs.extend((x1, x2))
        ys.extend((y1, y2))
    for x, y in extra_points:
        xs.append(x)
        ys.append(y)
    return xs, ys


def _shift_loops(loops, ox, oy):
    return [[(x - ox, y - oy) for x, y in loop] for loop in loops]


def _shift_segments(segments, ox, oy):
    return [(x1 - ox, y1 - oy, x2 - ox, y2 - oy) for x1, y1, x2, y2 in segments]


def _load_template_text() -> str:
    if TEMPLATE_PATH.is_file():
        return TEMPLATE_PATH.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Job template not found: {TEMPLATE_PATH}")


def _split_comment(rest: str) -> tuple[str, str]:
    pos = rest.find(" #")
    if pos == -1:
        return rest, ""
    return rest[:pos], rest[pos:]


def _set_yaml_key(text: str, section: str, key: str, literal: str) -> str:
    lines = text.splitlines()
    current = None
    section_at = None
    found = False
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        match = re.match(r"([A-Za-z0-9_]+)\s*:(.*)$", stripped)
        if not match:
            continue
        name, rest = match.group(1), match.group(2)
        if indent == 0:
            current = name
            if name == section:
                section_at = index
            continue
        if current == section and indent == 2 and name == key:
            _value, comment = _split_comment(rest)
            lines[index] = f"  {key}: {literal}{comment}"
            found = True
            break
    if not found:
        insert = f"  {key}: {literal}"
        if section_at is None:
            lines.extend(["", f"{section}:", insert])
        else:
            lines.insert(section_at + 1, insert)
    return "\n".join(lines) + "\n"


def _replace_rooms_block(text: str, rooms: list[dict]) -> str:
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith("rooms:"):
            start = index
            break
    block = ["rooms:                   # x,y ס\"מ ; area מ\"ר — IFC ingest"]
    for room in rooms:
        name = yaml_string(room["name"])
        block.append(
            "  - {"
            f"x: {yaml_number(room['x'])}, "
            f"y: {yaml_number(room['y'])}, "
            f"name: {name}, "
            f"area: {yaml_number(room['area'])}"
            "}"
        )
    if not rooms:
        block = ['rooms: []                # [{x: 230, y: 150, name: "סלון", area: 24.3}]  (ס"מ)']
    if start is None:
        lines.extend([""] + block)
        return "\n".join(lines) + "\n"
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() == "" or line.lstrip().startswith("#"):
            look = end + 1
            while look < len(lines) and (not lines[look].strip() or lines[look].lstrip().startswith("#")):
                look += 1
            if look >= len(lines) or lines[look][0] not in " \t":
                break
            end += 1
            continue
        if line[0] not in " \t":
            break
        end += 1
    lines[start:end] = block
    return "\n".join(lines) + "\n"


def _write_job_yaml(path: Path, *, project: str, client: str, address: str, dxf_rel: str, rooms: list[dict]) -> None:
    import yaml

    if path.is_file():
        text = path.read_text(encoding="utf-8")
    else:
        text = _load_template_text()
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    job = data.get("job") if isinstance(data.get("job"), dict) else {}
    if project:
        text = _set_yaml_key(text, "job", "project", yaml_string(project))
    if client:
        text = _set_yaml_key(text, "job", "client", yaml_string(client))
    if address:
        text = _set_yaml_key(text, "job", "address", yaml_string(address))
    if not decode_ifc_string(job.get("date")):
        text = _set_yaml_key(text, "job", "date", yaml_string(date.today().strftime("%d/%m/%Y")))
    text = _set_yaml_key(text, "input", "type", "cad")
    text = _set_yaml_key(text, "input", "file", dxf_rel if re.fullmatch(r"[A-Za-z0-9_./+-]+", dxf_rel) else yaml_string(dxf_rel))
    inp = data.get("input") if isinstance(data.get("input"), dict) else {}
    if "skip_layers" not in inp or inp.get("skip_layers") is None:
        text = _set_yaml_key(text, "input", "skip_layers", '["A-FURN", "A-ANNO-TEXT"]')
    text = _replace_rooms_block(text, rooms)
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        parsed = None
    if not isinstance(parsed, dict) or (parsed.get("input") or {}).get("type") != "cad":
        # The source YAML was too irregular to patch. Rewrite the data and keep every key.
        parsed = data or {}
        parsed.setdefault("job", {})
        parsed.setdefault("input", {})
        if project:
            parsed["job"]["project"] = project
        if client:
            parsed["job"]["client"] = client
        if address:
            parsed["job"]["address"] = address
        if not decode_ifc_string(parsed["job"].get("date")):
            parsed["job"]["date"] = date.today().strftime("%d/%m/%Y")
        parsed["input"]["type"] = "cad"
        parsed["input"]["file"] = dxf_rel
        if parsed["input"].get("skip_layers") is None:
            parsed["input"]["skip_layers"] = ["A-FURN", "A-ANNO-TEXT"]
        parsed["rooms"] = rooms
        text = yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False, width=120)
    path.write_text(text, encoding="utf-8")


def _write_dxf(path: Path, drawn: list[dict]) -> int:
    import ezdxf
    from ezdxf import units

    doc = ezdxf.new("R2010")
    doc.units = units.CM
    doc.header["$INSUNITS"] = 5
    doc.header["$MEASUREMENT"] = 1
    for layer_name in ("A-WALL", "A-DOOR", "A-WIND"):
        layer = doc.layers.add(layer_name)
        layer.color = 8
        layer.dxf.lineweight = 18
    msp = doc.modelspace()
    count = 0
    for item in drawn:
        layer = item["layer"]
        for loop in item["loops"]:
            if _add_polyline(msp, loop, layer):
                count += 1
        for segment in item["segments"]:
            x1, y1, x2, y2 = segment
            if math.hypot(x2 - x1, y2 - y1) < 0.2:
                continue
            msp.add_line((round(x1, 2), round(y1, 2)), (round(x2, 2), round(y2, 2)), dxfattribs={"layer": layer})
            count += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    return count


def _add_polyline(msp, loop, layer: str) -> bool:
    clean = []
    for x, y in loop:
        point = (round(float(x), 2), round(float(y), 2))
        if not clean or math.hypot(point[0] - clean[-1][0], point[1] - clean[-1][1]) >= 0.05:
            clean.append(point)
    if len(clean) < 2:
        return False
    closed = math.hypot(clean[0][0] - clean[-1][0], clean[0][1] - clean[-1][1]) < 0.05
    if closed:
        clean = clean[:-1]
    if len(clean) < 2:
        return False
    msp.add_lwpolyline(clean, close=closed, dxfattribs={"layer": layer})
    return True


def _write_circuits_if_missing(path: Path) -> bool:
    if path.is_file():
        return False
    path.write_text(CIRCUITS_HEADER + "\n", encoding="utf-8")
    return True


def _write_placements(path: Path, items: list[dict], force: bool) -> str:
    """Return 'written', 'skipped-existing', or 'empty'."""
    symbols = []
    for item in items:
        if not item.get("key") or item.get("x") is None or item.get("y") is None:
            continue
        record = {
            "key": item["key"],
            "x": item["x"],
            "y": item["y"],
            "rot": item.get("rot") if item.get("rot") is not None else 0,
        }
        if item.get("height") is not None:
            record["height"] = item["height"]
        symbols.append(record)
    if not symbols:
        return "empty"
    if path.is_file() and not force:
        return "skipped-existing"
    path.write_text(json.dumps(symbols, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return "written"


def format_summary(result: dict) -> str:
    lines = [
        "IFC → Job OS  |  קליטת IFC מקומית (בלי Autodesk APS)",
        f"Project / פרויקט: {result['project'] or '—'}",
        f"Client / לקוח: {result['client'] or '—'}",
        f"Address / כתובת: {result['address'] or '—'}",
        f"Spaces / חללים: {result['n_spaces']}",
        f"Walls / קירות: {result['n_walls']}",
        f"Openings / פתחים: {result['n_openings']}",
        f"Electrical / חשמל: {result['n_electrical']}",
        "Coordinates / קואורדינטות: x,y בס\"מ (פינה מינימלית ב-0,0), שטח במ\"ר. DXF בס\"מ.",
        f"DXF: {result['dxf']}",
        f"job.yaml: {result['job_yaml']}",
        f"circuits.csv: {result['circuits']}",
        f"electrical: {result['electrical_json']}",
    ]
    if result.get("placements"):
        lines.append(f"placements.json: {result['placements']} ({result['placements_status']})")
        lines.append(
            "Symbol keys are a guess / מפתחות הסמלים הם ניחוש — לאמת לפני הפייפליין: "
            "python3 tools/symbol_editor.py list"
        )
    elif result["n_electrical"]:
        lines.append("placements.json: not written (no mapped point symbols, or --no-placements)")
    if result["n_missing_geom"]:
        lines.append(
            f"Missing plan geometry / בלי גיאומטריית תוכנית: {result['n_missing_geom']} walls or openings"
        )
    lines.append(f"Next / המשך: python3 tools/run_pipeline.py {result['job_dir']}")
    return "\n".join(lines)


def ingest(
    ifc_path,
    job_dir,
    *,
    create_job: bool = False,
    dxf_name: str = "arch_from_ifc.dxf",
    keep_world_coords: bool = False,
    force_placements: bool = False,
    no_placements: bool = False,
) -> dict:
    import ifcopenshell

    ifc_path = Path(ifc_path).expanduser().resolve()
    job_dir = Path(job_dir).expanduser().resolve()
    if not ifc_path.is_file():
        raise FileNotFoundError(f"IFC file not found: {ifc_path}")
    if "/" in dxf_name or "\\" in dxf_name or dxf_name in {"", ".", ".."}:
        raise ValueError("dxf name must be a file name, not a path")
    if job_dir.exists() and not job_dir.is_dir():
        raise NotADirectoryError(f"job_dir is not a directory: {job_dir}")
    created = not job_dir.exists()
    # --create-job matches new_job.py's wording. A missing directory is created either way.
    job_dir.mkdir(parents=True, exist_ok=True)
    if created:
        flag = " --create-job" if create_job else ""
        print(f"Created job directory{flag}: {job_dir}", file=sys.stderr)
    inputs = job_dir / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)

    model = ifcopenshell.open(str(ifc_path))
    schema = model.schema or ""
    if schema not in {"IFC2X3", "IFC4", "IFC4X3"} and not schema.startswith("IFC4"):
        print(f"Warning: schema {schema} is outside IFC2X3/IFC4; continuing best-effort.", file=sys.stderr)

    scale = _length_scale_to_m(model)
    project = _find_project_name(model)
    client = _find_client(model)
    address = _find_address(model)

    drawn = []
    room_points = []  # (name, x, y, area) unshifted cm
    missing_geom = 0

    walls = _safe_by_type(model, "IfcWall")
    doors = _safe_by_type(model, "IfcDoor")
    windows = _safe_by_type(model, "IfcWindow")
    spaces = _safe_by_type(model, "IfcSpace")

    arch_loops = []
    arch_segments = []
    for element, kind in (
        *((wall, "wall") for wall in walls),
        *((door, "door") for door in doors),
        *((window, "window") for window in windows),
    ):
        loops, segments = extract_plan_cm(element, scale, allow_geom=True)
        if not loops and not segments:
            missing_geom += 1
        drawn.append({"layer": LAYER_BY_KIND[kind], "loops": loops, "segments": segments})
        arch_loops.extend(loops)
        arch_segments.extend(segments)

    for index, space in enumerate(spaces, start=1):
        loops, segments = extract_plan_cm(space, scale, allow_geom=True)
        measured = room_from_loops(loops) if loops else None
        qto = _quantity_area_m2(space, model)
        placement = _placement_cm(space, scale)
        if measured is not None:
            x, y, computed = measured
            area = _choose_area(qto, computed)
        elif placement is not None:
            x, y = placement[0], placement[1]
            area = qto or 0.0
        elif qto is not None:
            x, y, area = 0.0, 0.0, qto
        else:
            x, y, area = 0.0, 0.0, 0.0
        name = _product_name(space, f"חלל {index}")
        room_points.append((name, x, y, area))
        # Space outlines help anchor the origin but are not drawn (rooms live in job.yaml).
        arch_loops.extend(loops)
        arch_segments.extend(segments)

    electrical = []
    seen = set()
    for ifc_class in ELECTRICAL_CLASSES:
        for element in _safe_by_type(model, ifc_class):
            # by_type on a parent would duplicate; these classes are leaves, but guard anyway.
            gid = getattr(element, "GlobalId", None) or str(element.id())
            if gid in seen:
                continue
            seen.add(gid)
            placement = _placement_cm(element, scale)
            loops, segments = extract_plan_cm(element, scale, allow_geom=False)
            path = []
            for loop in loops:
                path.extend([[round(x, 1), round(y, 1)] for x, y in loop])
            record = {
                "ifc_class": element.is_a(),
                "name": _product_name(element, ""),
                "global_id": gid,
                "key": SYMBOL_BY_CLASS.get(element.is_a()) or SYMBOL_BY_CLASS.get(ifc_class),
                "placed": placement is not None,
                "x": placement[0] if placement else None,
                "y": placement[1] if placement else None,
                "rot": _snap90(placement[3]) if placement else None,
                "height": None,
                "path_cm": path or None,
            }
            if placement is not None:
                record["height"] = _height_cm(placement[2], _storey_z_m(element, scale))
            electrical.append(record)

    origin_points = []
    for name, x, y, _area in room_points:
        origin_points.append((x, y))
    xs, ys = _collect_xy(arch_loops, arch_segments, origin_points)
    if keep_world_coords or not xs:
        ox = oy = 0.0
    else:
        ox, oy = min(xs), min(ys)

    shifted_drawn = []
    for item in drawn:
        shifted_drawn.append(
            {
                "layer": item["layer"],
                "loops": _shift_loops(item["loops"], ox, oy),
                "segments": _shift_segments(item["segments"], ox, oy),
            }
        )
    rooms = []
    for name, x, y, area in room_points:
        rooms.append(
            {
                "x": round(x - ox, 1),
                "y": round(y - oy, 1),
                "name": name,
                "area": round(area, 2),
            }
        )
    for record in electrical:
        if record["x"] is not None:
            record["x"] = round(record["x"] - ox, 1)
            record["y"] = round(record["y"] - oy, 1)
        if record["path_cm"]:
            record["path_cm"] = [[round(x - ox, 1), round(y - oy, 1)] for x, y in record["path_cm"]]

    dxf_path = inputs / dxf_name
    dxf_rel = f"inputs/{dxf_name}"
    entity_count = _write_dxf(dxf_path, shifted_drawn)
    job_yaml = job_dir / "job.yaml"
    _write_job_yaml(
        job_yaml,
        project=project,
        client=client,
        address=address,
        dxf_rel=dxf_rel,
        rooms=rooms,
    )
    circuits = job_dir / "circuits.csv"
    _write_circuits_if_missing(circuits)

    electrical_path = inputs / "ifc_electrical.json"
    sidecar = {
        "source_ifc": str(ifc_path),
        "schema": schema,
        "length_unit_to_m": scale,
        "origin_shift_cm": [round(ox, 2), round(oy, 2)],
        "note": (
            "Symbol keys are guesses for Job OS placements.json. "
            "Confirm with tools/symbol_editor.py list before run_pipeline."
        ),
        "items": electrical,
    }
    electrical_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    placements_path = job_dir / "placements.json"
    if no_placements:
        placements_status = "disabled"
        placements_out = None
    else:
        placements_status = _write_placements(placements_path, electrical, force_placements)
        placements_out = str(placements_path) if placements_status in {"written", "skipped-existing"} else None

    return {
        "project": project,
        "client": client,
        "address": address,
        "n_spaces": len(spaces),
        "n_walls": len(walls),
        "n_openings": len(doors) + len(windows),
        "n_electrical": len(electrical),
        "n_missing_geom": missing_geom,
        "n_dxf_entities": entity_count,
        "rooms": rooms,
        "dxf": str(dxf_path),
        "job_yaml": str(job_yaml),
        "circuits": str(circuits),
        "electrical_json": str(electrical_path),
        "placements": placements_out,
        "placements_status": placements_status,
        "job_dir": str(job_dir),
        "created_job_dir": created,
        "origin_shift_cm": [ox, oy],
        "schema": schema,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local IFC2x3/IFC4 → Job OS ingest (no Autodesk APS, no paid APIs)."
    )
    parser.add_argument("ifc", help="Path to an IFC2X3 or IFC4 file")
    parser.add_argument("job_dir", help="Job directory. Created if it does not exist.")
    parser.add_argument(
        "--create-job",
        action="store_true",
        help="Create the job directory when missing (also the default). Follows tools/new_job.py: a job folder plus circuits.csv.",
    )
    parser.add_argument("--dxf-name", default="arch_from_ifc.dxf", help="File name inside inputs/ (default: arch_from_ifc.dxf)")
    parser.add_argument(
        "--keep-world-coords",
        action="store_true",
        help="Do not shift the plan minimum corner to (0, 0). Default shift matches cad_base: null.",
    )
    parser.add_argument(
        "--force-placements",
        action="store_true",
        help="Overwrite placements.json when electrical symbols were found.",
    )
    parser.add_argument(
        "--no-placements",
        action="store_true",
        help="Do not write placements.json. inputs/ifc_electrical.json is still written.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = ingest(
            args.ifc,
            args.job_dir,
            create_job=args.create_job,
            dxf_name=args.dxf_name,
            keep_world_coords=args.keep_world_coords,
            force_placements=args.force_placements,
            no_placements=args.no_placements,
        )
    except FileNotFoundError as exc:
        print(f"IFC ingest failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"IFC ingest failed: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1
    print(format_summary(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
