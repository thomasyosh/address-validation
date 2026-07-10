from __future__ import annotations

import copy
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

from address_validation.comparison_rules import (
    ComparisonSettings,
    build_comparison_payload,
    coordinates_to_text,
    get_comparison_settings,
)
from address_validation.database import Database
from address_validation.dataset import FetchTask, iter_fetch_tasks
from address_validation.proxy import apply_no_proxy_env, get_proxy_settings
from address_validation.rate_limit import (
    PerformanceSettings,
    RateLimiter,
    compute_backoff_seconds,
    get_endpoint_rps,
    get_performance_settings,
    parse_retry_after,
)
from address_validation.result_parser import extract_endpoint_result


def build_request(endpoint: dict[str, Any], address: str) -> dict[str, Any]:
    request_settings = endpoint.get("request", {})
    address_in = request_settings.get("address_in", "json")
    address_key = request_settings.get("address_key", "address")

    params = copy.deepcopy(endpoint.get("params") or {})
    json_body = copy.deepcopy(endpoint.get("json") or {})
    data = copy.deepcopy(endpoint.get("data") or None)

    if address_in == "params":
        params[address_key] = address
    elif address_in == "json_array":
        json_body[address_key] = [address]
    elif address_in == "json":
        json_body[address_key] = address
    elif address_in == "data":
        if data is None:
            data = {}
        if isinstance(data, dict):
            data[address_key] = address
    else:
        raise ValueError(f"Unsupported address_in value: {address_in}")

    return {
        "method": endpoint.get("method", "GET").upper(),
        "url": endpoint["url"],
        "headers": endpoint.get("headers"),
        "params": params or None,
        "json": json_body or None,
        "data": data,
    }


def get_endpoint_coordinate_fields(endpoint: dict[str, Any], settings: ComparisonSettings) -> tuple[str, str]:
    coordinate_fields = endpoint.get("response", {}).get("coordinate_fields") or {}
    return (
        coordinate_fields.get("easting", settings.easting_field),
        coordinate_fields.get("northing", settings.northing_field),
    )


class AddressFetcher:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.timeout = float(config.get("defaults", {}).get("timeout_seconds", 30))
        self.comparison_settings = get_comparison_settings(config)
        self.performance = get_performance_settings(config)
        self.proxy_settings = get_proxy_settings(config)
        apply_no_proxy_env(self.proxy_settings)
        self._client: httpx.Client | None = None
        self._limiters: dict[str, RateLimiter] = {}

    def get_limiter(self, endpoint: dict[str, Any]) -> RateLimiter:
        name = endpoint["name"]
        if name not in self._limiters:
            self._limiters[name] = RateLimiter(get_endpoint_rps(endpoint, self.performance))
        return self._limiters[name]

    @contextmanager
    def session(self) -> Iterator["AddressFetcher"]:
        proxy = self.proxy_settings.as_httpx_proxy()
        limits = httpx.Limits(
            max_connections=max(self.performance.workers * 2, 10),
            max_keepalive_connections=self.performance.workers,
        )
        with httpx.Client(
            timeout=self.timeout,
            proxy=proxy,
            trust_env=True,
            limits=limits,
        ) as client:
            self._client = client
            try:
                yield self
            finally:
                self._client = None

    def fetch_task(self, endpoint: dict[str, Any], task: FetchTask) -> dict[str, Any]:
        request = build_request(endpoint, task.address)
        response_settings = endpoint.get("response", {})
        easting_field, northing_field = get_endpoint_coordinate_fields(
            endpoint,
            self.comparison_settings,
        )
        limiter = self.get_limiter(endpoint)

        status_code: int | None = None
        response_body: str | None = None
        error: str | None = None
        latency_ms: float | None = None
        attempts = 0

        while True:
            attempts += 1
            limiter.wait()
            try:
                client = self._client
                owns_client = False
                if client is None:
                    client = httpx.Client(
                        timeout=self.timeout,
                        proxy=self.proxy_settings.as_httpx_proxy(),
                        trust_env=True,
                    )
                    owns_client = True

                try:
                    started = time.perf_counter()
                    response = client.request(**request)
                    latency_ms = (time.perf_counter() - started) * 1000
                    status_code = response.status_code
                    response_body = response.text

                    if status_code in self.performance.retry_status_codes:
                        if attempts <= self.performance.max_retries:
                            retry_after = parse_retry_after(response.headers.get("Retry-After"))
                            delay = compute_backoff_seconds(
                                attempts - 1,
                                self.performance.retry_backoff_seconds,
                                retry_after=retry_after,
                            )
                            time.sleep(delay)
                            continue
                        error = (
                            f"HTTP {status_code} after {attempts} attempts "
                            f"(possible rate-limit / IP block)"
                        )
                        break

                    response.raise_for_status()
                    error = None
                    break
                finally:
                    if owns_client:
                        client.close()

            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code if exc.response is not None else status_code
                response_body = exc.response.text if exc.response is not None else response_body
                if (
                    status_code in self.performance.retry_status_codes
                    and attempts <= self.performance.max_retries
                ):
                    retry_after = None
                    if exc.response is not None:
                        retry_after = parse_retry_after(exc.response.headers.get("Retry-After"))
                    delay = compute_backoff_seconds(
                        attempts - 1,
                        self.performance.retry_backoff_seconds,
                        retry_after=retry_after,
                    )
                    time.sleep(delay)
                    continue
                error = str(exc)
                break
            except httpx.TransportError as exc:
                if attempts <= self.performance.max_retries:
                    delay = compute_backoff_seconds(
                        attempts - 1,
                        self.performance.retry_backoff_seconds,
                    )
                    time.sleep(delay)
                    continue
                error = str(exc)
                break
            except httpx.HTTPError as exc:
                error = str(exc)
                if hasattr(exc, "response") and exc.response is not None:
                    status_code = exc.response.status_code
                    response_body = exc.response.text
                break

        coordinates, building_csuid = extract_endpoint_result(
            response_body,
            response_settings,
            easting_field=easting_field,
            northing_field=northing_field,
        )
        comparison_value = build_comparison_payload(
            criteria=self.comparison_settings.criteria,
            coordinates=coordinates,
            building_csuid=building_csuid,
        )

        return {
            "row_id": task.row_id,
            "address_type": task.address_type,
            "address": task.address,
            "endpoint": endpoint["name"],
            "coordinates": coordinates_to_text(coordinates),
            "building_csuid": building_csuid,
            "comparison_value": comparison_value,
            "response_code": status_code,
            "expected_easting": task.easting,
            "expected_northing": task.northing,
            "expected_building_csuid": task.building_csuid,
            "chinese_address": task.address_type == "CADDRESS",
            "latency_ms": latency_ms,
            "error": error,
            "response_body": response_body,
            "attempts": attempts,
        }


def _print_progress(completed: int, total: int, started_at: float, errors: int) -> None:
    elapsed = max(time.perf_counter() - started_at, 0.001)
    rate = completed / elapsed
    remaining = total - completed
    eta = remaining / rate if rate > 0 else 0
    print(
        f"Progress: {completed}/{total} "
        f"({completed / total * 100:.1f}%) "
        f"errors={errors} "
        f"{rate:.1f} req/s "
        f"ETA {eta / 60:.1f} min"
    )


def run_jobs_concurrently(
    fetcher: AddressFetcher,
    jobs: list[tuple[dict[str, Any], FetchTask]],
    *,
    on_result: Callable[[dict[str, Any]], None],
    workers: int | None = None,
) -> list[dict[str, Any]]:
    total = len(jobs)
    if total == 0:
        return []

    worker_count = workers or fetcher.performance.workers
    worker_count = max(1, min(worker_count, total))
    progress_every = fetcher.performance.progress_every
    results: list[dict[str, Any]] = []
    errors = 0
    completed = 0
    started_at = time.perf_counter()

    print(
        f"Fetching {total} requests with {worker_count} workers "
        f"(default {fetcher.performance.requests_per_second:g} req/s per endpoint, "
        f"max retries {fetcher.performance.max_retries})."
    )

    with fetcher.session():
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(fetcher.fetch_task, endpoint, task): (endpoint, task)
                for endpoint, task in jobs
            }
            for future in as_completed(future_map):
                fetched = future.result()
                on_result(fetched)
                results.append(fetched)
                completed += 1
                if fetched.get("error"):
                    errors += 1
                if completed == 1 or completed % progress_every == 0 or completed == total:
                    _print_progress(completed, total, started_at, errors)

    return results


class RoutineRunner:
    def __init__(
        self,
        config: dict[str, Any],
        database: Database,
        fetcher: AddressFetcher,
    ) -> None:
        self.config = config
        self.database = database
        self.fetcher = fetcher
        self.comparison_settings = get_comparison_settings(config)
        self.performance = get_performance_settings(config)

    def run(
        self,
        rows: list[Any],
        endpoint: dict[str, Any],
        *,
        label: str | None = None,
        notes: str | None = None,
        dataset_path: str | Path,
        workers: int | None = None,
        resume_run_id: int | None = None,
        retry_errors: bool = False,
    ) -> tuple[int, list[dict[str, Any]]]:
        if resume_run_id is not None:
            run = self.database.get_run(resume_run_id)
            if run is None or run.run_type != "routine":
                raise ValueError(f"Routine run {resume_run_id} not found.")
            run_id = resume_run_id
            self.database.mark_run_status(run_id, "in_progress")
            saved_keys = self.database.get_saved_validation_keys(
                run_id,
                successful_only=not retry_errors,
            )
        else:
            run_id = self.database.create_run(
                "routine",
                label=label,
                notes=notes,
                endpoint_name=endpoint["name"],
                dataset_path=str(dataset_path),
                comparison_criteria=self.comparison_settings.criteria,
            )
            saved_keys = set()

        all_jobs = [(endpoint, task) for task in iter_fetch_tasks(rows)]
        jobs = [
            (endpoint, task)
            for endpoint, task in all_jobs
            if (task.row_id, task.address_type) not in saved_keys
        ]
        skipped = len(all_jobs) - len(jobs)
        if skipped:
            print(f"Resuming run {run_id}: skipping {skipped} already saved results.")

        batch: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []

        def flush_batch() -> None:
            nonlocal batch
            if not batch:
                return
            self.database.save_validation_results_batch(run_id, batch)
            for fetched in batch:
                summaries.append(
                    {
                        "row_id": fetched["row_id"],
                        "address_type": fetched["address_type"],
                        "address": fetched["address"],
                        "saved": fetched["error"] is None,
                        "response_code": fetched["response_code"],
                        "comparison_value": fetched["comparison_value"],
                        "error": fetched["error"],
                    }
                )
            batch = []

        def on_result(fetched: dict[str, Any]) -> None:
            batch.append(fetched)
            if len(batch) >= self.performance.batch_save_size:
                flush_batch()

        try:
            if jobs:
                run_jobs_concurrently(
                    self.fetcher,
                    jobs,
                    on_result=on_result,
                    workers=workers,
                )
            flush_batch()
            self.database.mark_run_status(run_id, "completed")
        except BaseException:
            flush_batch()
            self.database.mark_run_status(run_id, "interrupted")
            raise

        return run_id, summaries


class BenchmarkRunner:
    def __init__(
        self,
        config: dict[str, Any],
        database: Database,
        fetcher: AddressFetcher,
    ) -> None:
        self.config = config
        self.database = database
        self.fetcher = fetcher
        self.comparison_settings = get_comparison_settings(config)
        self.performance = get_performance_settings(config)

    def run(
        self,
        rows: list[Any],
        endpoints: list[dict[str, Any]],
        *,
        label: str | None = None,
        notes: str | None = None,
        dataset_path: str | Path,
        workers: int | None = None,
        resume_run_id: int | None = None,
        retry_errors: bool = False,
    ) -> tuple[int, list[dict[str, Any]]]:
        if resume_run_id is not None:
            run = self.database.get_run(resume_run_id)
            if run is None or run.run_type != "benchmark":
                raise ValueError(f"Benchmark run {resume_run_id} not found.")
            run_id = resume_run_id
            self.database.mark_run_status(run_id, "in_progress")
            saved_keys = self.database.get_saved_benchmark_keys(
                run_id,
                successful_only=not retry_errors,
            )
        else:
            run_id = self.database.create_run(
                "benchmark",
                label=label,
                notes=notes,
                dataset_path=str(dataset_path),
                comparison_criteria=self.comparison_settings.criteria,
            )
            saved_keys = set()

        all_jobs = [
            (endpoint, task)
            for task in iter_fetch_tasks(rows)
            for endpoint in endpoints
        ]
        jobs = [
            (endpoint, task)
            for endpoint, task in all_jobs
            if (task.row_id, task.address_type, endpoint["name"]) not in saved_keys
        ]
        skipped = len(all_jobs) - len(jobs)
        if skipped:
            print(f"Resuming run {run_id}: skipping {skipped} already saved results.")

        batch: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []

        def flush_batch() -> None:
            nonlocal batch
            if not batch:
                return
            self.database.save_benchmark_results_batch(run_id, batch)
            for fetched in batch:
                summaries.append(
                    {
                        "row_id": fetched["row_id"],
                        "address_type": fetched["address_type"],
                        "endpoint": fetched["endpoint"],
                        "saved": fetched["error"] is None,
                        "response_code": fetched["response_code"],
                        "latency_ms": fetched["latency_ms"],
                        "comparison_value": fetched["comparison_value"],
                        "error": fetched["error"],
                    }
                )
            batch = []

        def on_result(fetched: dict[str, Any]) -> None:
            batch.append(fetched)
            if len(batch) >= self.performance.batch_save_size:
                flush_batch()

        try:
            if jobs:
                run_jobs_concurrently(
                    self.fetcher,
                    jobs,
                    on_result=on_result,
                    workers=workers,
                )
            flush_batch()
            self.database.mark_run_status(run_id, "completed")
        except BaseException:
            flush_batch()
            self.database.mark_run_status(run_id, "interrupted")
            raise

        return run_id, summaries
