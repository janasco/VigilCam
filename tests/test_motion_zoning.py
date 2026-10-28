import numpy as np

from app.services.motion_zoning import (
    MotionZoningEngine,
    PolygonZone,
    MotionZoneResult,
)


def make_frames():
    f1 = np.zeros((40, 40, 3), dtype=np.uint8)
    f2 = f1.copy()
    f2[5:15, 5:15] = 255
    return f1, f2


def test_motion_zoning_imports_and_basic():
    f1, f2 = make_frames()
    engine = MotionZoningEngine(40, 40)
    r1 = engine.process_and_evaluate(f1)
    r2 = engine.process_and_evaluate(f2)
    assert isinstance(r1, MotionZoneResult)
    assert r2.pixels_changed >= 0


def test_polygon_include_zone():
    f1, f2 = make_frames()
    zone = PolygonZone(points=[(0, 0), (39, 0), (39, 39), (0, 39)], mode="include", threshold_percentage=1)
    engine = MotionZoningEngine(40, 40, zones=[zone])
    engine.process_and_evaluate(f1)
    r = engine.process_and_evaluate(f2)
    assert r.triggered is True
    assert len(r.zones) == 1
    assert r.zones[0].mode == "include"
    assert r.zones[0].triggered is True
    assert r.zone_results is r.zones


def test_polygon_exclude_zone_suppresses():
    f1, f2 = make_frames()
    zone = PolygonZone(points=[(5, 5), (14, 5), (14, 14), (5, 14)], mode="exclude", threshold_percentage=1)
    # Whole frame include by default mask, exclude small moving box
    engine = MotionZoningEngine(40, 40, zones=[zone])
    engine.process_and_evaluate(f1)
    r = engine.process_and_evaluate(f2)
    # Per-zone result should not trigger for exclude
    assert r.zones[0].triggered is False


def test_per_zone_threshold_not_triggered():
    f1, f2 = make_frames()
    zone = PolygonZone(points=[(0, 0), (39, 0), (39, 39), (0, 39)], mode="include", threshold_percentage=99)
    engine = MotionZoningEngine(40, 40, zones=[zone])
    engine.process_and_evaluate(f1)
    r = engine.process_and_evaluate(f2)
    assert r.zones[0].triggered is False


def test_dict_zones_and_aliases():
    f1, f2 = make_frames()
    engine = MotionZoningEngine(frame_width=40, frame_height=40, polygon_zones=[{"points": [(0, 0), (39, 0), (39, 39), (0, 39)], "mode": "include", "threshold": 1}])
    engine.process_and_evaluate(f1)
    r = engine.process_and_evaluate(f2)
    assert len(r.zones) == 1
    # aliases
    assert r.per_zone == r.zones
    assert r.zone_motion == r.zones
    assert r.zone_results == r.zones