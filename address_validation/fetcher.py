from __future__ import annotations

import copy
import random
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
from address_validation.proxy import apply_no_proxy_env, get_proxy_settings, host_bypasses_proxy
from address_validation.rate_limit import (
    RateLimiter,
    compute_backoff_seconds,
    get_endpoint_rps,
    get_performance_settings,
    parse_retry_after,
    should_retry_status,
)
from address_validation.result_parser import extract_endpoint_result, slice_response_body_for_address
from address_validation.logging_utils import log_info, log_warn
import os


def resolve_verify_ssl(config: dict[str, Any]) -> bool:
    """
    Company PCs behind SSL-inspecting proxies often need verify=False.
    Priority: ADDRESS_VALIDATION_VERIFY_SSL env -> defaults.verify_ssl -> False
    """
    env_value = os.environ.get("ADDRESS_VALIDATION_VERIFY_SSL")
    if env_value is not None:
        return env_value.strip().lower() in {"1", "true", "yes", "on"}

    defaults = config.get("defaults") or {}
    if "verify_ssl" in defaults:
        return bool(defaults["verify_ssl"])

    # Default for this corporate intranet/proxy setup.
    return False


BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def build_request(endpoint: dict[str, Any], address: str | list[str]) -> dict[str, Any]:
    request_settings = endpoint.get("request", {})
    address_in = request_settings.get("address_in", "json")
    address_key = request_settings.get("address_key", "address")
    addresses = [address] if isinstance(address, str) else list(address)
    if not addresses:
        raise ValueError("At least one address is required to build a request")

    params = copy.deepcopy(endpoint.get("params") or {})
    json_body = copy.deepcopy(endpoint.get("json") or {})
    data = copy.deepcopy(endpoint.get("data") or None)
    headers = copy.deepcopy(endpoint.get("headers") or {})
    headers.setdefault("User-Agent", BROWSER_USER_AGENT)
    headers.setdefault("Accept", "application/json")

    # When calling an intranet service by IP, keep the original hostname in Host.
    host_header = endpoint.get("host_header") or request_settings.get("host_header")
    if host_header:
        headers["Host"] = host_header

    if address_in == "params":
        if len(addresses) != 1:
            raise ValueError("params address_in only supports one address per request")
        params[address_key] = addresses[0]
    elif address_in == "json_array":
        json_body[address_key] = addresses
        headers.setdefault("Content-Type", "application/json")
    elif address_in == "json":
        if len(addresses) != 1:
            raise ValueError("json address_in only supports one address per request")
        json_body[address_key] = addresses[0]
        headers.setdefault("Content-Type", "application/json")
    elif address_in == "data":
        if len(addresses) != 1:
            raise ValueError("data address_in only supports one address per request")
        if data is None:
            data = {}
        if isinstance(data, dict):
            data[address_key] = addresses[0]
    else:
        raise ValueError(f"Unsupported address_in value: {address_in}")

    return {
        "method": endpoint.get("method", "GET").upper(),
        "url": endpoint["url"],
        "headers": headers,
        "params": params or None,
        "json": json_body or None,
        "data": data,
    }


def get_fetch_mode(endpoint: dict[str, Any]) -> str:
    """Return 'one' (single address) or 'batch' (array of addresses in one request)."""
    request_settings = endpoint.get("request") or {}
    mode = str(request_settings.get("fetch_mode", "one")).strip().lower()
    if mode in {"batch", "array", "many"}:
        if request_settings.get("address_in", "json") != "json_array":
            return "one"
        return "batch"
    return "one"


def get_batch_size(endpoint: dict[str, Any]) -> int:
    request_settings = endpoint.get("request") or {}
    if get_fetch_mode(endpoint) != "batch":
        return 1
    return max(1, int(request_settings.get("batch_size", 50)))


def build_job_units(
    jobs: list[tuple[dict[str, Any], FetchTask]],
) -> list[tuple[dict[str, Any], list[FetchTask]]]:
    """Group endpoint/task pairs into HTTP request units (1 task or a batch)."""
    by_endpoint: dict[str, tuple[dict[str, Any], list[FetchTask]]] = {}
    order: list[str] = []
    for endpoint, task in jobs:
        name = endpoint["name"]
        if name not in by_endpoint:
            by_endpoint[name] = (endpoint, [])
            order.append(name)
        by_endpoint[name][1].append(task)

    units: list[tuple[dict[str, Any], list[FetchTask]]] = []
    for name in order:
        endpoint, tasks = by_endpoint[name]
        size = get_batch_size(endpoint)
        for index in range(0, len(tasks), size):
            units.append((endpoint, tasks[index : index + size]))
    return units


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
        self.verify_ssl = resolve_verify_ssl(config)
        self.comparison_settings = get_comparison_settings(config)
        self.performance = get_performance_settings(config)
        self.proxy_settings = get_proxy_settings(config)
        apply_no_proxy_env(self.proxy_settings)
        self._proxy_client: httpx.Client | None = None
        self._direct_client: httpx.Client | None = None
        self._session_active = False
        self._limiters: dict[str, RateLimiter] = {}

    def get_limiter(self, endpoint: dict[str, Any]) -> RateLimiter:
        name = endpoint["name"]
        if name not in self._limiters:
            self._limiters[name] = RateLimiter(get_endpoint_rps(endpoint, self.performance))
        return self._limiters[name]

    def _create_client(self, *, use_proxy: bool) -> httpx.Client:
        proxy = self.proxy_settings.as_httpx_proxy() if use_proxy else None
        limits = httpx.Limits(
            max_connections=max(self.performance.workers * 4, 40),
            max_keepalive_connections=max(self.performance.workers * 2, 20),
        )
        return httpx.Client(
            timeout=self.timeout,
            proxy=proxy,
            # When we choose proxy/direct ourselves, do not let env proxy override it.
            trust_env=False,
            verify=self.verify_ssl,
            limits=limits,
            headers={"User-Agent": BROWSER_USER_AGENT},
        )

    def _client_for_url(self, url: str, *, force_direct: bool = False) -> httpx.Client:
        bypass = force_direct or host_bypasses_proxy(url, self.proxy_settings.no_proxy)
        if bypass or not self.proxy_settings.enabled:
            if self._direct_client is None:
                self._direct_client = self._create_client(use_proxy=False)
            return self._direct_client
        if self._proxy_client is None:
            self._proxy_client = self._create_client(use_proxy=True)
        return self._proxy_client

    def describe_route(self, url: str, *, force_direct: bool = False) -> str:
        if force_direct or host_bypasses_proxy(url, self.proxy_settings.no_proxy):
            return "direct intranet (no company proxy)"
        if self.proxy_settings.enabled:
            return f"via proxy ({self.proxy_settings.redacted_summary()})"
        return "direct (no proxy configured)"

    @contextmanager
    def session(self) -> Iterator["AddressFetcher"]:
        self._proxy_client = None
        self._direct_client = None
        self._session_active = True
        try:
            yield self
        finally:
            self._session_active = False
            if self._proxy_client is not None:
                self._proxy_client.close()
                self._proxy_client = None
            if self._direct_client is not None:
                self._direct_client.close()
                self._direct_client = None

    def fetch_task(self, endpoint: dict[str, Any], task: FetchTask) -> dict[str, Any]:
        return self.fetch_tasks(endpoint, [task])[0]

    def fetch_tasks(self, endpoint: dict[str, Any], tasks: list[FetchTask]) -> list[dict[str, Any]]:
        if not tasks:
            return []

        addresses = [task.address for task in tasks]
        request = build_request(endpoint, addresses)

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
        label = (
            f"batch={len(tasks)}"
            if len(tasks) > 1
            else f"row={tasks[0].row_id} {tasks[0].address_type}"
        )

        while True:
            attempts += 1
            limiter.wait()
            try:
                owns_client = False
                force_direct = bool(endpoint.get("force_direct", False))
                if self._session_active:
                    client = self._client_for_url(request["url"], force_direct=force_direct)
                else:
                    bypass = force_direct or host_bypasses_proxy(
                        request["url"],
                        self.proxy_settings.no_proxy,
                    )
                    client = self._create_client(
                        use_proxy=self.proxy_settings.enabled and not bypass
                    )
                    owns_client = True

                try:
                    started = time.perf_counter()
                    response = client.request(**request)
                    latency_ms = (time.perf_counter() - started) * 1000
                    status_code = response.status_code
                    response_body = response.text

                    if status_code in self.performance.no_retry_status_codes:
                        error = f"HTTP {status_code} (no retry)"
                        log_warn(
                            f"HTTP {status_code} from {endpoint['name']} "
                            f"{label} — skipping retries, continuing"
                        )
                        break

                    if should_retry_status(status_code, self.performance, attempts):
                        retry_after = parse_retry_after(response.headers.get("Retry-After"))
                        delay = compute_backoff_seconds(
                            attempts - 1,
                            self.performance.retry_backoff_seconds,
                            retry_after=retry_after,
                            max_seconds=self.performance.max_retry_backoff_seconds,
                        )
                        log_warn(
                            f"HTTP {status_code} from {endpoint['name']} {label} "
                            f"(retry {attempts}/{self.performance.max_retries}), "
                            f"sleeping {delay:.1f}s"
                        )
                        time.sleep(delay)
                        continue

                    if status_code in self.performance.retry_status_codes:
                        error = f"HTTP {status_code} after {attempts} attempts"
                        log_warn(
                            f"Giving up on {endpoint['name']} {label} after {attempts} attempts "
                            f"(HTTP {status_code})"
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
                if status_code in self.performance.no_retry_status_codes:
                    error = f"HTTP {status_code} (no retry)"
                    log_warn(
                        f"HTTP {status_code} from {endpoint['name']} "
                        f"{label} — skipping retries, continuing"
                    )
                    break
                if should_retry_status(status_code, self.performance, attempts):
                    retry_after = None
                    if exc.response is not None:
                        retry_after = parse_retry_after(exc.response.headers.get("Retry-After"))
                    delay = compute_backoff_seconds(
                        attempts - 1,
                        self.performance.retry_backoff_seconds,
                        retry_after=retry_after,
                        max_seconds=self.performance.max_retry_backoff_seconds,
                    )
                    log_warn(
                        f"HTTP {status_code} from {endpoint['name']} {label} "
                        f"(retry {attempts}/{self.performance.max_retries}), "
                        f"sleeping {delay:.1f}s"
                    )
                    time.sleep(delay)
                    continue
                error = str(exc)
                log_warn(f"Giving up on {endpoint['name']} {label}: {error}")
                break
            except httpx.TransportError as exc:
                if attempts <= self.performance.max_retries:
                    delay = compute_backoff_seconds(
                        attempts - 1,
                        self.performance.retry_backoff_seconds,
                        max_seconds=self.performance.max_retry_backoff_seconds,
                    )
                    log_warn(
                        f"Transport error from {endpoint['name']}: {exc} "
                        f"(retry {attempts}/{self.performance.max_retries}), "
                        f"sleeping {delay:.1f}s"
                    )
                    time.sleep(delay)
                    continue
                error = str(exc)
                log_warn(f"Giving up on {endpoint['name']} {label}: {error}")
                break
            except httpx.HTTPError as exc:
                error = str(exc)
                if hasattr(exc, "response") and exc.response is not None:
                    status_code = exc.response.status_code
                    response_body = exc.response.text
                log_warn(f"Giving up on {endpoint['name']} {label}: {error}")
                break

        results: list[dict[str, Any]] = []
        for task in tasks:
            coordinates, building_csuid = extract_endpoint_result(
                response_body,
                response_settings,
                easting_field=easting_field,
                northing_field=northing_field,
                query_address=task.address,
            )
            comparison_value = build_comparison_payload(
                criteria=self.comparison_settings.criteria,
                coordinates=coordinates,
                building_csuid=building_csuid,
            )
            per_address_body = (
                slice_response_body_for_address(response_body, response_settings, task.address)
                if response_body and len(tasks) > 1
                else response_body
            )
            results.append(
                {
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
                    "response_body": per_address_body,
                    "attempts": attempts,
                }
            )
        return results


def _print_progress(completed: int, total: int, started_at: float, errors: int) -> None:
    elapsed = max(time.perf_counter() - started_at, 0.001)
    rate = completed / elapsed
    remaining = total - completed
    eta = remaining / rate if rate > 0 else 0
    log_info(
        f"Progress {completed}/{total} ({completed / total * 100:.1f}%) "
        f"errors={errors} rate={rate:.1f} req/s ETA={eta / 60:.1f} min"
    )


def _should_log_request(completed: int, total: int, progress_every: int, verbose: bool) -> bool:
    if verbose:
        return True
    if total <= 50:
        return True
    return completed == 1 or completed % progress_every == 0 or completed == total


def run_jobs_concurrently(
    fetcher: AddressFetcher,
    jobs: list[tuple[dict[str, Any], FetchTask]],
    *,
    on_result: Callable[[dict[str, Any]], None],
    workers: int | None = None,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    total = len(jobs)
    if total == 0:
        log_info("No pending requests to fetch (all already saved or dataset empty).")
        return []

    units = build_job_units(jobs)
    worker_count = workers or fetcher.performance.workers
    worker_count = max(1, min(worker_count, len(units)))
    progress_every = fetcher.performance.progress_every
    results: list[dict[str, Any]] = []
    errors = 0
    completed = 0
    started_at = time.perf_counter()

    shuffle_jobs = bool((fetcher.config.get("performance") or {}).get("shuffle_jobs", True))
    if shuffle_jobs:
        random.shuffle(units)
        log_info("Job order: randomized across addresses and endpoints")
    else:
        log_info("Job order: sequential (performance.shuffle_jobs=false)")

    endpoint_names = sorted({endpoint["name"] for endpoint, _ in units})
    log_info(f"Starting fetch: {total} address tasks in {len(units)} HTTP requests")
    log_info(f"Endpoints: {', '.join(endpoint_names)}")
    for endpoint_name in endpoint_names:
        sample_endpoint = next(endpoint for endpoint, _ in units if endpoint["name"] == endpoint_name)
        endpoint_rps = get_endpoint_rps(sample_endpoint, fetcher.performance)
        mode = get_fetch_mode(sample_endpoint)
        batch_size = get_batch_size(sample_endpoint)
        mode_label = f"fetch_mode={mode}" + (f", batch_size={batch_size}" if mode == "batch" else "")
        log_info(
            f"Route {endpoint_name}: "
            f"{fetcher.describe_route(sample_endpoint['url'], force_direct=bool(sample_endpoint.get('force_direct')))}, "
            f"RPS={endpoint_rps:g}, {mode_label}"
        )
    log_info(
        f"Workers={worker_count}, "
        f"default RPS={fetcher.performance.requests_per_second:g}/endpoint, "
        f"max_retries={fetcher.performance.max_retries}"
    )
    if fetcher.proxy_settings.enabled:
        log_info(f"Proxy: {fetcher.proxy_settings.redacted_summary()}")
    else:
        log_info("Proxy: disabled (using direct connection / system env if any)")
    log_info(f"SSL verify: {fetcher.verify_ssl}")
    log_info(f"NO_PROXY: {fetcher.proxy_settings.no_proxy}")
    log_info("Submitting requests and waiting for first response...")

    with fetcher.session():
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(fetcher.fetch_tasks, endpoint, tasks): (endpoint, tasks)
                for endpoint, tasks in units
            }
            for future in as_completed(future_map):
                endpoint, tasks = future_map[future]
                fetched_rows = future.result()
                for fetched in fetched_rows:
                    on_result(fetched)
                    results.append(fetched)
                    completed += 1
                    if fetched.get("error"):
                        errors += 1
                        log_warn(
                            f"{endpoint['name']} row={fetched.get('row_id')} "
                            f"{fetched.get('address_type')} "
                            f"HTTP={fetched.get('response_code')} error={fetched['error']}"
                        )
                    elif _should_log_request(completed, total, progress_every, verbose):
                        preview = str(fetched.get("address") or "")[:60]
                        if len(str(fetched.get("address") or "")) > 60:
                            preview += "..."
                        log_info(
                            f"OK {endpoint['name']} row={fetched.get('row_id')} "
                            f"{fetched.get('address_type')} "
                            f"HTTP={fetched.get('response_code')} "
                            f"latency={fetched.get('latency_ms'):.0f}ms "
                            f"address={preview!r}"
                        )

                    if completed == 1 or completed % progress_every == 0 or completed == total:
                        _print_progress(completed, total, started_at, errors)

    log_info(f"Fetch finished: {completed} done, {errors} errors")
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
        verbose: bool = False,
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
            log_info(f"Resuming routine run {run_id}")
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
            log_info(f"Created routine run {run_id}")

        log_info(f"Dataset: {dataset_path}")
        log_info(f"Endpoint: {endpoint['name']} ({endpoint['url']})")
        log_info(f"Criteria: {self.comparison_settings.criteria}")

        all_jobs = [(endpoint, task) for task in iter_fetch_tasks(rows)]
        jobs = [
            (endpoint, task)
            for endpoint, task in all_jobs
            if (task.row_id, task.address_type) not in saved_keys
        ]
        skipped = len(all_jobs) - len(jobs)
        log_info(f"Address tasks loaded: {len(all_jobs)} (skipped saved: {skipped})")
        if skipped:
            log_info(f"Resuming run {run_id}: skipping {skipped} already saved results.")

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
                    verbose=verbose,
                )
            else:
                log_info("Nothing left to fetch for this routine run.")
            flush_batch()
            self.database.mark_run_status(run_id, "completed")
            log_info(f"Routine run {run_id} marked completed")
        except BaseException:
            flush_batch()
            self.database.mark_run_status(run_id, "interrupted")
            log_warn(f"Routine run {run_id} marked interrupted")
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
        verbose: bool = False,
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
            log_info(f"Resuming benchmark run {run_id}")
        else:
            run_id = self.database.create_run(
                "benchmark",
                label=label,
                notes=notes,
                dataset_path=str(dataset_path),
                comparison_criteria=self.comparison_settings.criteria,
            )
            saved_keys = set()
            log_info(f"Created benchmark run {run_id}")

        log_info(f"Dataset: {dataset_path}")
        log_info(f"Endpoints: {', '.join(endpoint['name'] for endpoint in endpoints)}")
        log_info(f"Criteria: {self.comparison_settings.criteria}")

        all_jobs = [
            (endpoint, task)
            for task in iter_fetch_tasks(rows)
            for endpoint in endpoints
        ]
        # Shuffle here too so resume subsets stay mixed across endpoints.
        if bool((self.config.get("performance") or {}).get("shuffle_jobs", True)):
            random.shuffle(all_jobs)

        jobs = [
            (endpoint, task)
            for endpoint, task in all_jobs
            if (task.row_id, task.address_type, endpoint["name"]) not in saved_keys
        ]
        skipped = len(all_jobs) - len(jobs)
        log_info(f"Request jobs loaded: {len(all_jobs)} (skipped saved: {skipped})")
        if skipped:
            log_info(f"Resuming run {run_id}: skipping {skipped} already saved results.")

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
                    verbose=verbose,
                )
            else:
                log_info("Nothing left to fetch for this benchmark run.")
            flush_batch()
            self.database.mark_run_status(run_id, "completed")
            log_info(f"Benchmark run {run_id} marked completed")
        except BaseException:
            flush_batch()
            self.database.mark_run_status(run_id, "interrupted")
            log_warn(f"Benchmark run {run_id} marked interrupted")
            raise

        return run_id, summaries
