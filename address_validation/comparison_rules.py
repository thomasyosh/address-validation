from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Literal

ComparisonCriteria = Literal["coordinates", "building_csuid"]
DEFAULT_COORDINATE_TOLERANCE_METERS = 50.0


@dataclass
class CoordinatePair:
    easting: float | None
    northing: float | None


@dataclass
class ComparisonSettings:
    criteria: ComparisonCriteria
    coordinate_tolerance: float
    easting_field: str
    northing_field: str


@dataclass
class CoordinateMatchResult:
    """Result of comparing API coordinates with ground-truth EASTING/NORTHING."""

    matches: bool | None
    status: str
    distance_m: float | None


def get_comparison_settings(config: dict[str, Any]) -> ComparisonSettings:
    comparison = config.get("comparison") or {}
    coordinate_fields = comparison.get("coordinate_fields") or {}
    criteria = comparison.get("criteria", "coordinates")
    if criteria not in {"coordinates", "building_csuid"}:
        raise ValueError("comparison.criteria must be 'coordinates' or 'building_csuid'.")

    # Preferred key: coordinate_tolerance_meters. Keep coordinate_tolerance as alias.
    tolerance = comparison.get(
        "coordinate_tolerance_meters",
        comparison.get("coordinate_tolerance", DEFAULT_COORDINATE_TOLERANCE_METERS),
    )

    return ComparisonSettings(
        criteria=criteria,
        coordinate_tolerance=float(tolerance),
        easting_field=coordinate_fields.get("easting", "easting"),
        northing_field=coordinate_fields.get("northing", "northing"),
    )


def parse_coordinate_pair(
    value: Any,
    *,
    easting_field: str = "easting",
    northing_field: str = "northing",
) -> CoordinatePair:
    if value is None:
        return CoordinatePair(None, None)

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return CoordinatePair(None, None)

    if isinstance(value, dict):
        return CoordinatePair(
            _to_float(value.get(easting_field)),
            _to_float(value.get(northing_field)),
        )

    return CoordinatePair(None, None)


def coordinates_to_text(pair: CoordinatePair) -> str | None:
    if pair.easting is None and pair.northing is None:
        return None
    return json.dumps(
        {"easting": pair.easting, "northing": pair.northing},
        sort_keys=True,
    )


def coordinate_distance_m(
    actual: CoordinatePair,
    expected_easting: float | None,
    expected_northing: float | None,
) -> float | None:
    if expected_easting is None or expected_northing is None:
        return None
    if actual.easting is None or actual.northing is None:
        return None

    delta_e = actual.easting - expected_easting
    delta_n = actual.northing - expected_northing
    return math.hypot(delta_e, delta_n)


def evaluate_coordinates(
    actual: CoordinatePair,
    expected_easting: float | None,
    expected_northing: float | None,
    tolerance_meters: float,
) -> CoordinateMatchResult:
    """
    HK1980 Grid easting/northing are in metres, so hypot distance is metres.

    - within tolerance -> matched
    - beyond tolerance or missing API coordinates -> not_found
    - missing ground truth -> not_comparable
    """
    if expected_easting is None or expected_northing is None:
        return CoordinateMatchResult(None, "not_comparable", None)

    if actual.easting is None or actual.northing is None:
        return CoordinateMatchResult(False, "not_found", None)

    distance = coordinate_distance_m(actual, expected_easting, expected_northing)
    if distance is None:
        return CoordinateMatchResult(False, "not_found", None)

    if distance <= tolerance_meters:
        return CoordinateMatchResult(True, "matched", distance)

    return CoordinateMatchResult(False, "not_found", distance)


def coordinates_match(
    actual: CoordinatePair,
    expected_easting: float | None,
    expected_northing: float | None,
    tolerance: float,
) -> bool | None:
    return evaluate_coordinates(
        actual,
        expected_easting,
        expected_northing,
        tolerance,
    ).matches


def building_csuid_match(actual: str | None, expected: str | None) -> bool | None:
    if expected is None or expected == "":
        return None
    if actual is None or actual == "":
        return False
    return str(actual).strip() == str(expected).strip()


def build_comparison_payload(
    *,
    criteria: ComparisonCriteria,
    coordinates: CoordinatePair,
    building_csuid: str | None,
) -> str | None:
    if criteria == "coordinates":
        return coordinates_to_text(coordinates)
    return building_csuid


def values_equal_for_criteria(
    left: str | None,
    right: str | None,
    *,
    criteria: ComparisonCriteria,
    coordinate_tolerance: float,
    easting_field: str,
    northing_field: str,
) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False

    if criteria == "building_csuid":
        return str(left).strip() == str(right).strip()

    left_pair = parse_coordinate_pair(left, easting_field=easting_field, northing_field=northing_field)
    right_pair = parse_coordinate_pair(right, easting_field=easting_field, northing_field=northing_field)
    if left_pair.easting is None or left_pair.northing is None:
        return False
    if right_pair.easting is None or right_pair.northing is None:
        return False

    return coordinates_match(
        left_pair,
        right_pair.easting,
        right_pair.northing,
        coordinate_tolerance,
    ) is True


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
