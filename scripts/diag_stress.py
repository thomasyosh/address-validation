"""Print how many parallel ASE HTTP calls a stress-test config can actually use."""
from __future__ import annotations

from address_validation.cli import (
    apply_fetch_mode_overrides,
    apply_performance_overrides,
    load_dataset_from_config,
)
from address_validation.config import get_routine_endpoint, load_config
from address_validation.dataset import iter_fetch_tasks
from address_validation.fetcher import build_job_units, get_effective_batch_size, get_fetch_mode
from address_validation.rate_limit import (
    get_endpoint_max_workers,
    get_endpoint_rps,
    get_performance_settings,
    get_request_concurrency,
)


def diag(
    *,
    workers: int,
    batch_size: int,
    fetch_mode: str = "batch",
    auto_parallel: bool | None = None,
) -> None:
    config = load_config("config.yaml")
    config = apply_performance_overrides(config, workers=workers, rps=0)
    config = apply_fetch_mode_overrides(
        config,
        fetch_mode=fetch_mode,
        batch_size=batch_size,
        concurrency="multi-thread",
    )
    if auto_parallel is not None:
        for endpoint in config["endpoints"]:
            if endpoint.get("name") == "ase_query_debug":
                endpoint.setdefault("request", {})["auto_parallel_batches"] = auto_parallel

    perf = get_performance_settings(config)
    endpoint = get_routine_endpoint(config)
    _, rows = load_dataset_from_config(config, None)
    jobs = [(endpoint, task) for task in iter_fetch_tasks(rows)]
    units = build_job_units(jobs, workers=workers)
    max_workers = get_endpoint_max_workers(endpoint, workers)
    request = endpoint.get("request") or {}
    effective_batch = get_effective_batch_size(endpoint, len(jobs), workers=max_workers)

    print(
        f"workers={workers} fetch_mode={fetch_mode} batch_size={batch_size} "
        f"auto_parallel={request.get('auto_parallel_batches')}"
    )
    print(f"  addresses={len(jobs)} http_units={len(units)}")
    print(f"  max_in_flight={min(workers, len(units), max_workers)}")
    print(f"  effective_batch={effective_batch} concurrency={get_request_concurrency(endpoint)}")
    print(f"  rps={get_endpoint_rps(endpoint, perf)} fetch_mode_resolved={get_fetch_mode(endpoint)}")
    print()


if __name__ == "__main__":
    print("Your command (batch 50, workers 40, auto_parallel from config):")
    diag(workers=40, batch_size=50)
    print("Batch 50 + auto_parallel_batches: true:")
    diag(workers=40, batch_size=50, auto_parallel=True)
    print("Extreme one-by-one (100 workers):")
    diag(workers=100, batch_size=1, fetch_mode="one")
    print("Extreme batch parallel (100 workers, auto_parallel true):")
    diag(workers=100, batch_size=50, auto_parallel=True)
