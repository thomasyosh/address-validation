from __future__ import annotations

import json
from typing import Any

from address_validation.comparison_rules import CoordinatePair, parse_coordinate_pair


def extract_value(data: Any, path: str | None) -> Any:
    if path is None or path == "":
        return data

    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def parse_response_json(response_body: str | None) -> Any:
    if not response_body:
        return None
    text = response_body.lstrip("\ufeff")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def select_response_item(payload: Any, response_settings: dict[str, Any]) -> Any:
    selection = response_settings.get("selection", "path")
    if payload is None:
        return None

    if selection == "root_first":
        return payload[0] if isinstance(payload, list) and payload else None

    if selection == "first_in_path":
        array_value = extract_value(payload, response_settings.get("array_path"))
        if isinstance(array_value, list) and array_value:
            return array_value[0]
        return None

    if selection == "first_in_data_buckets":
        data = extract_value(payload, response_settings.get("data_path", "data"))
        if not isinstance(data, dict):
            return None
        for value in data.values():
            if isinstance(value, list) and value:
                return value[0]
        return None

    coordinates_path = response_settings.get("coordinates_path")
    return extract_value(payload, coordinates_path) if coordinates_path else payload


def extract_endpoint_result(
    response_body: str | None,
    response_settings: dict[str, Any],
    *,
    easting_field: str = "easting",
    northing_field: str = "northing",
) -> tuple[CoordinatePair, str | None]:
    payload = parse_response_json(response_body)
    if payload is None:
        return CoordinatePair(None, None), None

    item = select_response_item(payload, response_settings)
    if item is None:
        return CoordinatePair(None, None), None

    coordinates_source = item
    coordinates_path = response_settings.get("item_coordinates_path")
    if coordinates_path:
        coordinates_source = extract_value(item, coordinates_path) or item

    coordinates = parse_coordinate_pair(
        coordinates_source,
        easting_field=easting_field,
        northing_field=northing_field,
    )

    building_csuid_path = response_settings.get("building_csuid_path")
    building_csuid = extract_value(item, building_csuid_path) if building_csuid_path else None
    if building_csuid is not None:
        building_csuid = str(building_csuid).strip() or None

    return coordinates, building_csuid


def extract_coordinates(
    response_body: str | None,
    coordinates_path: str | None,
    *,
    easting_field: str = "easting",
    northing_field: str = "northing",
) -> CoordinatePair:
    coordinates, _ = extract_endpoint_result(
        response_body,
        {
            "selection": "path",
            "coordinates_path": coordinates_path,
        },
        easting_field=easting_field,
        northing_field=northing_field,
    )
    return coordinates


def extract_building_csuid(response_body: str | None, building_csuid_path: str | None) -> str | None:
    _, building_csuid = extract_endpoint_result(
        response_body,
        {
            "selection": "path",
            "building_csuid_path": building_csuid_path,
        },
    )
    return building_csuid
