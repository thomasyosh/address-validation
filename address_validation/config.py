from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

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

    if "endpoints" not in config or not config["endpoints"]:
        raise ValueError("Config must define at least one endpoint under 'endpoints'.")

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
    endpoints: list[dict[str, Any]] = []

    for endpoint in config["endpoints"]:
        merged = copy.deepcopy(defaults)
        merged.update(endpoint)
        if "name" not in merged or "url" not in merged:
            raise ValueError("Each endpoint must include 'name' and 'url'.")
        endpoints.append(merged)

    return endpoints


def get_endpoint_by_name(config: dict[str, Any], name: str) -> dict[str, Any]:
    for endpoint in get_endpoints(config):
        if endpoint["name"] == name:
            return endpoint
    raise ValueError(f"Endpoint '{name}' not found in config.")


def get_routine_endpoint(config: dict[str, Any]) -> dict[str, Any]:
    routine = config.get("routine") or {}
    endpoint_name = routine.get("endpoint")
    if not endpoint_name:
        raise ValueError("Config must define routine.endpoint for validation runs.")
    return get_endpoint_by_name(config, endpoint_name)


def get_benchmark_endpoints(config: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    benchmark = config.get("benchmark") or {}
    baseline_name = benchmark.get("baseline_endpoint")
    endpoint_names = benchmark.get("endpoints") or []

    if not baseline_name:
        raise ValueError("Config must define benchmark.baseline_endpoint.")
    if not endpoint_names:
        raise ValueError("Config must define benchmark.endpoints.")

    endpoints = [get_endpoint_by_name(config, name) for name in endpoint_names]
    if baseline_name not in endpoint_names:
        raise ValueError("benchmark.baseline_endpoint must be included in benchmark.endpoints.")

    return baseline_name, endpoints
