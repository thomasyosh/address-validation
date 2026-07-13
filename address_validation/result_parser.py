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


def select_response_item(
    payload: Any,
    response_settings: dict[str, Any],
    *,
    query_address: str | None = None,
) -> Any:
    items = select_response_items(
        payload,
        response_settings,
        query_address=query_address,
        limit=1,
    )
    return items[0] if items else None


def select_response_items(
    payload: Any,
    response_settings: dict[str, Any],
    *,
    query_address: str | None = None,
    limit: int | None = None,
) -> list[Any]:
    """Return ranked response items (best-first) from an endpoint payload."""
    selection = response_settings.get("selection", "path")
    if payload is None:
        return []

    items: list[Any] = []
    if selection == "root_first":
        if isinstance(payload, list):
            items = list(payload)
        elif payload is not None:
            items = [payload]
    elif selection == "first_in_path":
        array_value = extract_value(payload, response_settings.get("array_path"))
        if isinstance(array_value, list):
            items = list(array_value)
        elif array_value is not None:
            items = [array_value]
    elif selection == "first_in_data_buckets":
        data = extract_value(payload, response_settings.get("data_path", "data"))
        if isinstance(data, dict):
            bucket = None
            if query_address is not None:
                bucket = _lookup_data_bucket(data, query_address)
            else:
                for value in data.values():
                    if isinstance(value, list) and value:
                        bucket = value
                        break
                    if value is not None:
                        bucket = value
                        break
            if isinstance(bucket, list):
                items = list(bucket)
            elif bucket is not None:
                items = [bucket]
    else:
        coordinates_path = response_settings.get("coordinates_path")
        value = extract_value(payload, coordinates_path) if coordinates_path else payload
        if isinstance(value, list):
            items = list(value)
        elif value is not None:
            items = [value]

    if limit is not None:
        return items[: max(0, int(limit))]
    return items


def _normalize_address_key(value: str) -> str:
    return " ".join(str(value).strip().split()).casefold()


def _lookup_data_bucket(data: dict[str, Any], query_address: str) -> Any:
    if query_address in data:
        return data[query_address]
    needle = _normalize_address_key(query_address)
    for key, value in data.items():
        if _normalize_address_key(str(key)) == needle:
            return value
    return None


def extract_endpoint_result(
    response_body: str | None,
    response_settings: dict[str, Any],
    *,
    easting_field: str = "easting",
    northing_field: str = "northing",
    query_address: str | None = None,
) -> tuple[CoordinatePair, str | None]:
    candidates = extract_endpoint_candidates(
        response_body,
        response_settings,
        easting_field=easting_field,
        northing_field=northing_field,
        query_address=query_address,
        limit=1,
    )
    if not candidates:
        return CoordinatePair(None, None), None
    first = candidates[0]
    return (
        CoordinatePair(first.get("easting"), first.get("northing")),
        first.get("building_csuid"),
    )


def extract_endpoint_candidates(
    response_body: str | None,
    response_settings: dict[str, Any],
    *,
    easting_field: str = "easting",
    northing_field: str = "northing",
    query_address: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """
    Extract ranked candidates from an endpoint response.

    Each candidate is:
      {rank, easting, northing, building_csuid}
    where rank is 1-based (1 = top result).
    """
    payload = parse_response_json(response_body)
    if payload is None:
        return []

    items = select_response_items(
        payload,
        response_settings,
        query_address=query_address,
        limit=limit,
    )
    candidates: list[dict[str, Any]] = []
    coordinates_path = response_settings.get("item_coordinates_path")
    building_csuid_path = response_settings.get("building_csuid_path")

    for index, item in enumerate(items, start=1):
        coordinates_source = item
        if coordinates_path:
            coordinates_source = extract_value(item, coordinates_path) or item
        coordinates = parse_coordinate_pair(
            coordinates_source,
            easting_field=easting_field,
            northing_field=northing_field,
        )
        building_csuid = extract_value(item, building_csuid_path) if building_csuid_path else None
        if building_csuid is not None:
            building_csuid = str(building_csuid).strip() or None
        candidates.append(
            {
                "rank": index,
                "easting": coordinates.easting,
                "northing": coordinates.northing,
                "building_csuid": building_csuid,
            }
        )
    return candidates


def slice_response_body_for_address(
    response_body: str | None,
    response_settings: dict[str, Any],
    query_address: str,
) -> str | None:
    """Keep only the matching data bucket so batch responses stay small in SQLite."""
    if not response_body:
        return response_body
    if response_settings.get("selection") != "first_in_data_buckets":
        return response_body

    payload = parse_response_json(response_body)
    if not isinstance(payload, dict):
        return response_body

    data_path = response_settings.get("data_path", "data")
    data = extract_value(payload, data_path)
    if not isinstance(data, dict):
        return response_body

    bucket = _lookup_data_bucket(data, query_address)
    sliced = dict(payload)
    # Rebuild nested data path only for the top-level "data" case used by ASE.
    if data_path == "data":
        sliced["data"] = {query_address: bucket} if bucket is not None else {}
    else:
        sliced[data_path] = {query_address: bucket} if bucket is not None else {}
    return json.dumps(sliced, ensure_ascii=False)


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
