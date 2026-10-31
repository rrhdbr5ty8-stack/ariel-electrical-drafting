"""Happy-path tests for tools/ifc_ingest.py.

The fixture is a tiny apartment generated with IfcOpenShell (no checked-in
binary). A real architect IFC is still the manual smoke input — see JOB_README.md.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ifc_ingest", ROOT / "tools" / "ifc_ingest.py")
ifc_ingest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ifc_ingest)

ifcopenshell = pytest.importorskip("ifcopenshell")
ezdxf = pytest.importorskip("ezdxf")
import ifcopenshell.api.aggregate
import ifcopenshell.api.context
import ifcopenshell.api.geometry
import ifcopenshell.api.owner
import ifcopenshell.api.project
import ifcopenshell.api.pset
import ifcopenshell.api.root
import ifcopenshell.api.spatial
import ifcopenshell.api.unit
import ifcopenshell.util.unit


def _x2(text: str) -> str:
    hexpart = "".join(f"{ord(ch):04X}" for ch in text)
    return f"\\X2\\{hexpart}\\X0\\"


def test_decode_ifc_hebrew_escapes():
    decode = ifc_ingest.decode_ifc_string
    assert decode("סלון") == "סלון"
    assert decode(_x2("שלום")) == "שלום"
    assert decode("Room " + _x2("סלון") + " 1") == "Room סלון 1"
    assert decode(decode(_x2("שלום"))) == "שלום"
    assert decode("\\X\\41") == "A"
    assert decode(None) == ""


def test_polygon_metrics_cm_to_m2():
    # 4 m by 3 m rectangle expressed in centimetres.
    ring = [(0, 0), (400, 0), (400, 300), (0, 300), (0, 0)]
    x, y, area = ifc_ingest.polygon_metrics(ring)
    assert x == pytest.approx(200)
    assert y == pytest.approx(150)
    assert area == pytest.approx(12)


def test_plan_lines_drop_cap_diagonals_on_a_closed_box():
    verts = [
        (0, 0, 0),
        (10, 0, 0),
        (10, 4, 0),
        (0, 4, 0),
        (0, 0, 8),
        (10, 0, 8),
        (10, 4, 8),
        (0, 4, 8),
    ]

    def quad(a, b, c, d):
        return [(a, b, c), (a, c, d)]

    tris = []
    tris += quad(0, 1, 2, 3)
    tris += quad(4, 7, 6, 5)
    tris += quad(0, 4, 5, 1)
    tris += quad(1, 5, 6, 2)
    tris += quad(2, 6, 7, 3)
    tris += quad(3, 7, 4, 0)
    raw = ifc_ingest.plan_lines_from_mesh(verts, tris)
    assert len(raw) == 8  # top ring + bottom ring
    deduped = ifc_ingest._dedup_segments(raw)
    assert len(deduped) == 4
    diagonal = {(0.0, 0.0), (10.0, 4.0)}
    for x1, y1, x2, y2 in deduped:
        assert {(round(x1, 1), round(y1, 1)), (round(x2, 1), round(y2, 1))} != diagonal


def _owner_for_ifc2x3(model):
    person = ifcopenshell.api.owner.add_person(
        model, identification="tester", family_name="User", given_name="Test"
    )
    app_org = ifcopenshell.api.owner.add_organisation(model, identification="APP", name="App Org")
    ifcopenshell.api.owner.add_person_and_organisation(model, person, app_org)
    ifcopenshell.api.owner.add_application(
        model,
        application_developer=app_org,
        version="0",
        application_full_name="ifc-ingest-test",
        application_identifier="ifc-ingest-test",
    )


def _place(model, product, x_m, y_m, z_m=0.0):
    import numpy as np

    matrix = np.eye(4)
    matrix[0, 3] = x_m
    matrix[1, 3] = y_m
    matrix[2, 3] = z_m
    ifcopenshell.api.geometry.edit_object_placement(model, product=product, matrix=matrix, is_si=True)


def build_apartment(path: Path, schema: str = "IFC4", length_raw: str = "MILLIMETERS") -> dict:
    """Write a one-room IFC. Returns expected electrical symbol keys."""
    model = ifcopenshell.api.project.create_file(version=schema)
    if schema == "IFC2X3":
        _owner_for_ifc2x3(model)
    project = ifcopenshell.api.root.create_entity(model, ifc_class="IfcProject", name="דירת הרצל")
    project.LongName = "דירת הרצל 10"
    ifcopenshell.api.unit.assign_unit(model, length={"is_metric": True, "raw": length_raw})
    ctx = ifcopenshell.api.context.add_context(model, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        model, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=ctx
    )
    foot = ifcopenshell.api.context.add_context(
        model, context_type="Model", context_identifier="FootPrint", target_view="MODEL_VIEW", parent=ctx
    )
    site = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSite", name="מגרש הרצל")
    site.SiteAddress = model.create_entity(
        "IfcPostalAddress",
        AddressLines=("רחוב הרצל 10",),
        Town="תל אביב",
        Country="ישראל",
    )
    building = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuilding", name="בניין א")
    storey = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuildingStorey", name="קומת קרקע")
    ifcopenshell.api.aggregate.assign_object(model, products=[site], relating_object=project)
    ifcopenshell.api.aggregate.assign_object(model, products=[building], relating_object=site)
    ifcopenshell.api.aggregate.assign_object(model, products=[storey], relating_object=building)
    client = ifcopenshell.api.owner.add_organisation(model, identification="CL1", name="משפחת ישראלי")
    ifcopenshell.api.owner.add_role(model, assigned_object=client, role="CLIENT")

    wall = ifcopenshell.api.root.create_entity(model, ifc_class="IfcWall", name="W-1")
    wall_rep = ifcopenshell.api.geometry.add_wall_representation(
        model, context=body, length=4.0, height=2.7, thickness=0.2
    )
    ifcopenshell.api.geometry.assign_representation(model, product=wall, representation=wall_rep)
    _place(model, wall, 1.0, 2.0)

    space = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSpace", name="S01")
    space.LongName = "סלון"
    scale = ifcopenshell.util.unit.calculate_unit_scale(model)

    def pu(metres):
        return metres / scale

    pts = [(pu(0), pu(0)), (pu(4), pu(0)), (pu(4), pu(3)), (pu(0), pu(3)), (pu(0), pu(0))]
    poly = model.createIfcPolyline([model.createIfcCartesianPoint(p) for p in pts])
    footprint = ifcopenshell.api.geometry.add_footprint_representation(model, context=foot, curves=[poly])
    ifcopenshell.api.geometry.assign_representation(model, product=space, representation=footprint)
    _place(model, space, 1.0, 2.0)
    qto = ifcopenshell.api.pset.add_qto(model, product=space, name="Qto_SpaceBaseQuantities")
    ifcopenshell.api.pset.edit_qto(model, qto=qto, properties={"NetFloorArea": 12.0})

    door = ifcopenshell.api.root.create_entity(model, ifc_class="IfcDoor", name="D1")
    door_rep = ifcopenshell.api.geometry.add_wall_representation(
        model, context=body, length=0.9, height=2.1, thickness=0.1
    )
    ifcopenshell.api.geometry.assign_representation(model, product=door, representation=door_rep)
    _place(model, door, 1.5, 2.0)

    window = ifcopenshell.api.root.create_entity(model, ifc_class="IfcWindow", name="WIN1")
    window_rep = ifcopenshell.api.geometry.add_wall_representation(
        model, context=body, length=1.2, height=1.2, thickness=0.08
    )
    ifcopenshell.api.geometry.assign_representation(model, product=window, representation=window_rep)
    _place(model, window, 3.0, 2.0)

    expected_keys = []
    if schema == "IFC2X3":
        board = ifcopenshell.api.root.create_entity(model, ifc_class="IfcElectricDistributionPoint", name="לוח דירתי")
        _place(model, board, 2.2, 3.4, 1.5)
        products = [wall, door, window, board]
        expected_keys = ["PANEL"]
    else:
        outlet = ifcopenshell.api.root.create_entity(model, ifc_class="IfcOutlet", name="שקע סלון")
        _place(model, outlet, 2.2, 3.4, 0.3)
        light = ifcopenshell.api.root.create_entity(model, ifc_class="IfcLightFixture", name="מנורה")
        _place(model, light, 2.5, 4.5, 2.4)
        cable = ifcopenshell.api.root.create_entity(model, ifc_class="IfcCableSegment", name="C1")
        _place(model, cable, 1.2, 2.2, 0.0)
        products = [wall, door, window, outlet, light, cable]
        expected_keys = ["SOCKET_2", "LIGHT"]

    ifcopenshell.api.aggregate.assign_object(model, products=[space], relating_object=storey)
    ifcopenshell.api.spatial.assign_container(model, products=products, relating_structure=storey)
    path.parent.mkdir(parents=True, exist_ok=True)
    model.write(str(path))
    return {"keys": expected_keys, "electrical": 1 if schema == "IFC2X3" else 3}


@pytest.mark.parametrize(
    "schema,length_raw",
    [
        ("IFC4", "MILLIMETERS"),
        ("IFC4", "METERS"),
        ("IFC2X3", "MILLIMETERS"),
    ],
)
def test_ingest_happy_path(tmp_path, schema, length_raw):
    ifc_path = tmp_path / f"apt_{schema}_{length_raw}.ifc"
    expected = build_apartment(ifc_path, schema, length_raw)
    job = tmp_path / "job"
    result = ifc_ingest.ingest(ifc_path, job, create_job=True)

    assert result["project"] == "דירת הרצל 10"
    assert result["client"] == "משפחת ישראלי"
    assert "הרצל" in result["address"] and "תל אביב" in result["address"]
    assert result["n_spaces"] == 1
    assert result["n_walls"] == 1
    assert result["n_openings"] == 2
    assert result["n_electrical"] == expected["electrical"]
    assert result["n_missing_geom"] == 0
    assert result["n_dxf_entities"] >= 3

    room = result["rooms"][0]
    assert room["name"] == "סלון"
    assert room["area"] == pytest.approx(12, abs=0.05)
    assert room["x"] == pytest.approx(200, abs=1)
    assert room["y"] == pytest.approx(150, abs=1)

    text = Path(result["job_yaml"]).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert data["job"]["project"] == "דירת הרצל 10"
    assert data["job"]["client"] == "משפחת ישראלי"
    assert data["input"]["type"] == "cad"
    assert data["input"]["file"] == "inputs/arch_from_ifc.dxf"
    assert data["input"]["skip_layers"] == ["A-FURN", "A-ANNO-TEXT"]
    assert data["sheet"]["plan_dwg_no"] == "E-01"
    assert "גבהי התקנה" in text
    assert "שם פרויקט" in text
    assert "לאישור המזמין" in text

    circuits = Path(result["circuits"]).read_text(encoding="utf-8").splitlines()
    assert circuits[0] == "no,desc,breaker,poles,cable,phase,group,kw,length,note"
    assert len(circuits) == 1 or circuits[1] == ""

    doc = ezdxf.readfile(result["dxf"])
    assert int(doc.header["$INSUNITS"]) == 5
    assert doc.layers.get("A-WALL").color == 8
    assert doc.layers.get("A-DOOR").color == 8
    assert doc.layers.get("A-WIND").color == 8
    kinds = {e.dxftype() for e in doc.modelspace()}
    assert kinds <= {"LWPOLYLINE", "LINE"}
    assert "סלון" not in Path(result["dxf"]).read_text(encoding="utf-8", errors="replace")

    wall_xs = []
    door_n = wind_n = 0
    for entity in doc.modelspace():
        layer = entity.dxf.layer
        pts = []
        if entity.dxftype() == "LWPOLYLINE":
            pts = [(p[0], p[1]) for p in entity.get_points("xy")]
        elif entity.dxftype() == "LINE":
            pts = [(entity.dxf.start.x, entity.dxf.start.y), (entity.dxf.end.x, entity.dxf.end.y)]
        if layer == "A-WALL":
            wall_xs.extend(p[0] for p in pts)
        elif layer == "A-DOOR":
            door_n += 1
        elif layer == "A-WIND":
            wind_n += 1
    assert wall_xs
    assert max(wall_xs) - min(wall_xs) == pytest.approx(400, abs=1.5)
    assert door_n >= 1 and wind_n >= 1

    sidecar = json.loads(Path(result["electrical_json"]).read_text(encoding="utf-8"))
    assert len(sidecar["items"]) == expected["electrical"]
    keys = [item["key"] for item in json.loads(Path(result["placements"]).read_text(encoding="utf-8"))]
    assert keys == expected["keys"]
    if schema != "IFC2X3":
        outlet = next(item for item in sidecar["items"] if item["ifc_class"] == "IfcOutlet")
        assert outlet["x"] == pytest.approx(120, abs=1)
        assert outlet["y"] == pytest.approx(140, abs=1)
        assert outlet["height"] == pytest.approx(30, abs=1)
        assert outlet["name"] == "שקע סלון"


def test_cli_summary_and_keeps_hand_edits(tmp_path, capsys):
    ifc_path = tmp_path / "apt.ifc"
    build_apartment(ifc_path, "IFC4", "MILLIMETERS")
    job = tmp_path / "new_job"
    rc = ifc_ingest.main([str(ifc_path), str(job), "--create-job"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "חללים" in captured.out and "Spaces" in captured.out
    assert "קירות" in captured.out and "Walls" in captured.out
    assert "פתחים" in captured.out
    assert "חשמל" in captured.out
    assert "run_pipeline.py" in captured.out
    assert str(job) in captured.out

    circuits = job / "circuits.csv"
    circuits.write_text(
        "no,desc,breaker,poles,cable,phase,group,kw,length,note\n"
        "1,תאורה,10A,1,1.5,L1,,0.2,12,keep\n",
        encoding="utf-8",
    )
    placements = job / "placements.json"
    placements.write_text('[{"key": "KEEP"}]\n', encoding="utf-8")
    text = (job / "job.yaml").read_text(encoding="utf-8")
    text = re.sub(r'(?m)^(  date: )(".*?")', r'\1"01/01/2020"', text, count=1)
    text += "\nsld:\n  preset: residential\n"
    (job / "job.yaml").write_text(text, encoding="utf-8")

    again = ifc_ingest.ingest(ifc_path, job)
    assert again["placements_status"] == "skipped-existing"
    assert json.loads(placements.read_text(encoding="utf-8")) == [{"key": "KEEP"}]
    kept = circuits.read_text(encoding="utf-8")
    assert kept.splitlines()[0] == "no,desc,breaker,poles,cable,phase,group,kw,length,note"
    assert "תאורה" in kept
    loaded = yaml.safe_load((job / "job.yaml").read_text(encoding="utf-8"))
    assert loaded["job"]["date"] == "01/01/2020"
    assert loaded["sld"]["preset"] == "residential"
    assert loaded["rooms"][0]["name"] == "סלון"
    assert "גבהי התקנה" in (job / "job.yaml").read_text(encoding="utf-8")


def test_keep_world_coordinates(tmp_path):
    ifc_path = tmp_path / "apt.ifc"
    build_apartment(ifc_path, "IFC4", "METERS")
    result = ifc_ingest.ingest(ifc_path, tmp_path / "job", keep_world_coords=True)
    room = result["rooms"][0]
    assert room["x"] == pytest.approx(300, abs=1)
    assert room["y"] == pytest.approx(350, abs=1)
    assert result["origin_shift_cm"] == [0.0, 0.0]


def test_missing_ifc_exits(tmp_path, capsys):
    rc = ifc_ingest.main([str(tmp_path / "missing.ifc"), str(tmp_path / "job")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "not found" in err.lower() or "IFC ingest failed" in err


def test_tessellated_wall_fallback(tmp_path):
    model = ifcopenshell.api.project.create_file(version="IFC4")
    ifcopenshell.api.root.create_entity(model, ifc_class="IfcProject", name="Tess")
    ifcopenshell.api.unit.assign_unit(model, length={"is_metric": True, "raw": "METERS"})
    ctx = ifcopenshell.api.context.add_context(model, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        model, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=ctx
    )
    wall = ifcopenshell.api.root.create_entity(model, ifc_class="IfcWall", name="TW")
    verts = [
        (0.0, 0.0, 0.0),
        (4.0, 0.0, 0.0),
        (4.0, 0.2, 0.0),
        (0.0, 0.2, 0.0),
        (0.0, 0.0, 2.7),
        (4.0, 0.0, 2.7),
        (4.0, 0.2, 2.7),
        (0.0, 0.2, 2.7),
    ]

    def quad(a, b, c, d):
        return [(a, b, c), (a, c, d)]

    # IFC coord index is 1-based.
    faces = []
    for a, b, c, d in (
        (1, 2, 3, 4),
        (5, 8, 7, 6),
        (1, 5, 6, 2),
        (2, 6, 7, 3),
        (3, 7, 8, 4),
        (4, 8, 5, 1),
    ):
        faces.extend(quad(a, b, c, d))
    mesh = model.create_entity(
        "IfcTriangulatedFaceSet",
        Coordinates=model.createIfcCartesianPointList3D(verts),
        CoordIndex=faces,
    )
    rep = model.createIfcShapeRepresentation(body, "Body", "Tessellation", [mesh])
    ifcopenshell.api.geometry.assign_representation(model, product=wall, representation=rep)
    ifc_path = tmp_path / "tess.ifc"
    model.write(str(ifc_path))

    result = ifc_ingest.ingest(ifc_path, tmp_path / "job")
    assert result["n_walls"] == 1
    assert result["n_missing_geom"] == 0
    assert result["n_dxf_entities"] >= 4
    doc = ezdxf.readfile(result["dxf"])
    xs, ys = [], []
    for entity in doc.modelspace():
        assert entity.dxf.layer == "A-WALL"
        if entity.dxftype() == "LINE":
            xs.extend([entity.dxf.start.x, entity.dxf.end.x])
            ys.extend([entity.dxf.start.y, entity.dxf.end.y])
        elif entity.dxftype() == "LWPOLYLINE":
            for p in entity.get_points("xy"):
                xs.append(p[0])
                ys.append(p[1])
    assert max(xs) - min(xs) == pytest.approx(400, abs=1.5)
    assert max(ys) - min(ys) == pytest.approx(20, abs=1.5)
    # The bottom-cap diagonal (0,0)-(400,20) must not be drawn.
    for entity in doc.modelspace():
        if entity.dxftype() != "LINE":
            continue
        ends = {
            (round(entity.dxf.start.x, 1), round(entity.dxf.start.y, 1)),
            (round(entity.dxf.end.x, 1), round(entity.dxf.end.y, 1)),
        }
        assert ends != {(0.0, 0.0), (400.0, 20.0)}
