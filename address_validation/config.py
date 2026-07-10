from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from address_validation.logging_utils import log_info, log_warn

DEFAULT_CONFIG_PATH = Path("config.yaml")
EXAMPLE_CONFIG_PATH = Path("config.example.yaml")
LOCAL_CONFIG_PATH = Path("config.local.yaml")


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. "
            f"Copy {EXAMPLE_CONFIG_PATH} to {DEFAULT_CONFIG_PATH} and edit it."
        )

    with config_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    local_path = LOCAL_CONFIG_PATH
    if local_path.exists():
        with local_path.open("r", encoding="utf-8") as handle:
            local_config = yaml.safe_load(handle) or {}
        config = _deep_merge(config, local_config)

    endpoints = [
        endpoint
        for endpoint in (config.get("endpoints") or [])
        if isinstance(endpoint, dict) and endpoint.get("name") and endpoint.get("url")
    ]
    if not endpoints:
        raise ValueError(
            "Config must define at least one valid endpoint under 'endpoints' "
            "(each needs 'name' and 'url')."
        )
    config["endpoints"] = endpoints
    return config


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def get_database_path(config: dict[str, Any]) -> Path:
    db_path = config.get("database", {}).get("path", "data/address_validation.db")
    return Path(db_path)


def get_endpoints(config: dict[str, Any]) -> list[dict[str, Any]]:
    defaults = copy.deepcopy(config.get("defaults", {}))
    # Keep defaults.headers separate so endpoint headers can merge instead of replace.
    default_headers = copy.deepcopy(defaults.pop("headers", {}) or {})
    endpoints: list[dict[str, Any]] = []

    for endpoint in config.get("endpoints") or []:
        if not isinstance(endpoint, dict):
            log_warn(f"Skipping invalid endpoint entry: {endpoint!r}")
            continue
        if "name" not in endpoint or "url" not in endpoint:
            log_warn(f"Skipping endpoint missing name/url: {endpoint!r}")
            continue

        merged = copy.deepcopy(defaults)
        merged.update(endpoint)
        headers = copy.deepcopy(default_headers)
        headers.update(copy.deepcopy(endpoint.get("headers") or {}))
        merged["headers"] = headers
        endpoints.append(merged)

    return endpoints


def find_endpoint_by_name(config: dict[str, Any], name: str) -> dict[str, Any] | None:
    for endpoint in get_endpoints(config):
        if endpoint["name"] == name:
            return endpoint
    return None


def get_endpoint_by_name(config: dict[str, Any], name: str) -> dict[str, Any]:
    endpoint = find_endpoint_by_name(config, name)
    if endpoint is None:
        raise ValueError(f"Endpoint '{name}' not found in config.")
    return endpoint


def get_routine_endpoint(config: dict[str, Any]) -> dict[str, Any]:
    """
    Resolve the routine endpoint loosely.

    Preference order:
    1. routine.endpoint if present and defined
    2. benchmark.baseline_endpoint if present and defined
    3. first configured endpoint
    """
    available = get_endpoints(config)
    if not available:
        raise ValueError("No usable endpoints are configured.")

    routine = config.get("routine") or {}
    endpoint_name = routine.get("endpoint")
    if endpoint_name:
        endpoint = find_endpoint_by_name(config, endpoint_name)
        if endpoint is not None:
            return endpoint
        log_warn(
            f"routine.endpoint '{endpoint_name}' is not defined under endpoints; "
            "falling back to another configured endpoint."
        )

    benchmark = config.get("benchmark") or {}
    baseline_name = benchmark.get("baseline_endpoint")
    if baseline_name:
        endpoint = find_endpoint_by_name(config, baseline_name)
        if endpoint is not None:
            log_info(f"Using benchmark.baseline_endpoint '{baseline_name}' for routine validation.")
            return endpoint

    log_info(f"Using first configured endpoint '{available[0]['name']}' for routine validation.")
    return available[0]


def get_benchmark_endpoints(config: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """
    Resolve benchmark endpoints loosely.

    - Missing names in benchmark.endpoints are skipped with a warning
    - If benchmark.endpoints is omitted, all configured endpoints are used
    - If baseline is missing, the first selected endpoint becomes baseline
    """
    available = {endpoint["name"]: endpoint for endpoint in get_endpoints(config)}
    if not available:
        raise ValueError("No usable endpoints are configured.")

    benchmark = config.get("benchmark") or {}
    requested_names = benchmark.get("endpoints")
    baseline_name = benchmark.get("baseline_endpoint")

    if requested_names:
        selected: list[dict[str, Any]] = []
        for name in requested_names:
            endpoint = available.get(name)
            if endpoint is None:
                log_warn(
                    f"benchmark.endpoints includes '{name}', but it is not defined "
                    "under endpoints — skipping."
                )
                continue
            selected.append(endpoint)
    else:
        selected = list(available.values())
        log_info("benchmark.endpoints not set; using all configured endpoints.")

    if not selected:
        raise ValueError(
            "No benchmark endpoints could be resolved. "
            "Define at least one endpoint in config endpoints:."
        )

    selected_names = {endpoint["name"] for endpoint in selected}
    if baseline_name and baseline_name in selected_names:
        resolved_baseline = baseline_name
    else:
        if baseline_name and baseline_name not in selected_names:
            log_warn(
                f"benchmark.baseline_endpoint '{baseline_name}' is unavailable; "
                f"using '{selected[0]['name']}' as baseline."
            )
        resolved_baseline = selected[0]["name"]

    log_info(
        f"Benchmark will use {len(selected)} endpoint(s): "
        + ", ".join(endpoint["name"] for endpoint in selected)
        + f" (baseline={resolved_baseline})"
    )
    return resolved_baseline, selected
