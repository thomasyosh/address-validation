from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from address_validation.comparator import (
    AccuracyAnalyzer,
    AccuracyReport,
    BenchmarkAnalyzer,
    BenchmarkReport,
    MatchStatusComparison,
    RoutineComparator,
    RoutineComparison,
    accuracy_report_to_dict,
    benchmark_report_to_dict,
    match_status_comparison_to_dict,
    routine_comparison_to_dict,
)
from address_validation.comparison_rules import get_comparison_settings
from address_validation.config import (
    get_benchmark_endpoints,
    get_database_path,
    get_routine_endpoint,
    load_config,
)
from address_validation.database import Database
from address_validation.dataset import get_dataset_settings, load_address_dataset
from address_validation.fetcher import AddressFetcher, BenchmarkRunner, RoutineRunner
from address_validation.logging_utils import log_info, log_warn
from address_validation.proxy import get_proxy_settings
from address_validation.report_writer import ReportWriter, reports_enabled
from address_validation.summary import (
    MatchSummaryBuilder,
    MatchSummaryTable,
    get_endpoint_display_names,
    match_summary_to_csv,
    match_summary_to_dict,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Address validation toolkit: routine regression checks and "
            "multi-endpoint benchmark comparisons."
        )
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to YAML config file (default: config.yaml)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate",
        help="Routine validation against one endpoint for EADDRESS and CADDRESS rows",
    )
    validate_parser.add_argument("--label", help="Optional label for this run")
    validate_parser.add_argument("--notes", help="Optional notes for this run")
    validate_parser.add_argument("--dataset", help="Override dataset path from config")
    validate_parser.add_argument(
        "--criteria",
        choices=["coordinates", "building_csuid"],
        help="Override comparison.criteria from config (default/recommended: coordinates)",
    )
    validate_parser.add_argument(
        "--tolerance",
        type=float,
        help="Coordinate match radius in metres (default: 50). Common values: 50 or 100.",
    )
    validate_parser.add_argument(
        "--top-n",
        type=int,
        dest="top_n",
        help="Accept a match if ground truth appears in the top N endpoint results (default: 5).",
    )
    validate_parser.add_argument(
        "--compare-with-previous",
        action="store_true",
        help="Compare this run with the previous routine run",
    )
    validate_parser.add_argument(
        "--accuracy",
        action="store_true",
        help="Show ground-truth accuracy against EASTING/NORTHING (or optional BUILDING_CSUID)",
    )
    validate_parser.add_argument(
        "--workers",
        type=int,
        help="Concurrent worker threads (default from performance.workers)",
    )
    validate_parser.add_argument(
        "--rps",
        type=float,
        help="Max requests per second per endpoint (default from performance.requests_per_second)",
    )
    validate_parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        metavar="RUN_ID",
        help="Resume an interrupted routine run (latest incomplete if RUN_ID omitted)",
    )
    validate_parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="When resuming, also retry previously failed fetches",
    )
    validate_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Log every successful fetch (not only periodic progress)",
    )
    validate_parser.add_argument(
        "--fetch-mode",
        choices=["one", "batch"],
        help="ASE-style json_array endpoints: one address per request, or many in one array",
    )
    validate_parser.add_argument(
        "--batch-size",
        type=int,
        help="Addresses per HTTP request when fetch_mode=batch (use 1 for one-by-one)",
    )

    benchmark_parser = subparsers.add_parser(
        "benchmark",
        help="Benchmark multiple endpoints for EADDRESS and CADDRESS rows",
    )
    benchmark_parser.add_argument("--label", help="Optional label for this run")
    benchmark_parser.add_argument("--notes", help="Optional notes for this run")
    benchmark_parser.add_argument("--dataset", help="Override dataset path from config")
    benchmark_parser.add_argument(
        "--criteria",
        choices=["coordinates", "building_csuid"],
        help="Override comparison.criteria from config (default/recommended: coordinates)",
    )
    benchmark_parser.add_argument(
        "--tolerance",
        type=float,
        help="Coordinate match radius in metres (default: 50). Common values: 50 or 100.",
    )
    benchmark_parser.add_argument(
        "--top-n",
        type=int,
        dest="top_n",
        help="Accept a match if ground truth appears in the top N endpoint results (default: 5).",
    )
    benchmark_parser.add_argument(
        "--report",
        action="store_true",
        help="Print benchmark summary after fetching",
    )
    benchmark_parser.add_argument(
        "--summary",
        action="store_true",
        help="Print match-rate summary table after fetching",
    )
    benchmark_parser.add_argument(
        "--workers",
        type=int,
        help="Concurrent worker threads (default from performance.workers)",
    )
    benchmark_parser.add_argument(
        "--rps",
        type=float,
        help="Max requests per second per endpoint (default from performance.requests_per_second)",
    )
    benchmark_parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        metavar="RUN_ID",
        help="Resume an interrupted benchmark run (latest incomplete if RUN_ID omitted)",
    )
    benchmark_parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="When resuming, also retry previously failed fetches",
    )
    benchmark_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Log every successful fetch (not only periodic progress)",
    )
    benchmark_parser.add_argument(
        "--fetch-mode",
        choices=["one", "batch"],
        help="ASE-style json_array endpoints: one address per request, or many in one array",
    )
    benchmark_parser.add_argument(
        "--batch-size",
        type=int,
        help="Addresses per HTTP request when fetch_mode=batch (use 1 for one-by-one)",
    )

    list_parser = subparsers.add_parser("list-runs", help="List saved runs")
    list_parser.add_argument("--type", choices=["routine", "benchmark"], help="Filter by run type")
    list_parser.add_argument("--limit", type=int, default=20, help="Maximum runs to show")

    show_parser = subparsers.add_parser("show-run", help="Show results for a specific run")
    show_parser.add_argument("run_id", type=int, help="Run ID to inspect")

    compare_parser = subparsers.add_parser(
        "compare",
        help=(
            "Compare match status between runs "
            "(within-tolerance now vs previous / date / month)"
        ),
    )
    compare_parser.add_argument("--current", type=int, help="Current run ID (default: latest)")
    compare_parser.add_argument("--previous", type=int, help="Previous run ID")
    compare_parser.add_argument(
        "--with-previous",
        action="store_true",
        help="Compare current run with the immediately previous completed run",
    )
    compare_parser.add_argument(
        "--with-date",
        metavar="YYYY-MM-DD",
        help="Compare current run with the latest completed run on that date",
    )
    compare_parser.add_argument(
        "--with-month",
        metavar="YYYY-MM",
        help="Compare current run with the latest completed run in that month",
    )
    compare_parser.add_argument(
        "--previous-month",
        action="store_true",
        help="Compare current run with the latest completed run in the previous calendar month",
    )
    compare_parser.add_argument(
        "--type",
        choices=["routine", "benchmark"],
        default="routine",
        help="Run type to resolve when using date/month selectors (default: routine)",
    )
    compare_parser.add_argument(
        "--value-diff",
        action="store_true",
        help="Also show raw coordinate/value changes (routine runs only)",
    )
    compare_parser.add_argument("--json", action="store_true")
    compare_parser.add_argument(
        "--criteria",
        choices=["coordinates", "building_csuid"],
        help="Override comparison.criteria from config",
    )
    compare_parser.add_argument(
        "--tolerance",
        type=float,
        help="Coordinate match radius in metres (default: 50)",
    )
    compare_parser.add_argument(
        "--top-n",
        type=int,
        dest="top_n",
        help="Accept a match if ground truth appears in the top N endpoint results (default: 5).",
    )

    report_parser = subparsers.add_parser("report", help="Show benchmark performance report")
    report_parser.add_argument("run_id", type=int, nargs="?", help="Benchmark run ID")
    report_parser.add_argument("--json", action="store_true")
    report_parser.add_argument(
        "--summary",
        action="store_true",
        help="Also print the match-rate summary table",
    )

    summary_parser = subparsers.add_parser(
        "summary",
        help="Print match-rate summary table (Number of Address / endpoint counts)",
    )
    summary_parser.add_argument("run_id", type=int, nargs="?", help="Run ID")
    summary_parser.add_argument(
        "--criteria",
        choices=["coordinates", "building_csuid"],
        help="Override comparison.criteria from config",
    )
    summary_parser.add_argument(
        "--tolerance",
        type=float,
        help="Coordinate match radius in metres (default: 50)",
    )
    summary_parser.add_argument(
        "--top-n",
        type=int,
        dest="top_n",
        help="Accept a match if ground truth appears in the top N endpoint results (default: 5).",
    )
    summary_parser.add_argument("--json", action="store_true")
    summary_parser.add_argument(
        "--csv",
        metavar="PATH",
        help="Write summary table to a CSV file",
    )

    accuracy_parser = subparsers.add_parser(
        "accuracy",
        help="Compare API results with dataset EASTING/NORTHING (recommended) or BUILDING_CSUID",
    )
    accuracy_parser.add_argument("run_id", type=int, nargs="?", help="Run ID")
    accuracy_parser.add_argument(
        "--criteria",
        choices=["coordinates", "building_csuid"],
        help="Override comparison.criteria from config (default/recommended: coordinates)",
    )
    accuracy_parser.add_argument(
        "--tolerance",
        type=float,
        help="Coordinate match radius in metres (default: 50). Common values: 50 or 100.",
    )
    accuracy_parser.add_argument(
        "--top-n",
        type=int,
        dest="top_n",
        help="Accept a match if ground truth appears in the top N endpoint results (default: 5).",
    )
    accuracy_parser.add_argument("--json", action="store_true")

    write_report_parser = subparsers.add_parser(
        "write-report",
        help="Write/refresh the results/ report folder for an existing run",
    )
    write_report_parser.add_argument("run_id", type=int, nargs="?", help="Run ID (default: latest)")
    write_report_parser.add_argument(
        "--compare-with",
        type=int,
        metavar="RUN_ID",
        help="Optional previous run ID for match_diff files",
    )
    write_report_parser.add_argument(
        "--criteria",
        choices=["coordinates", "building_csuid"],
        help="Override comparison.criteria from config",
    )
    write_report_parser.add_argument(
        "--tolerance",
        type=float,
        help="Coordinate match radius in metres (default: 50)",
    )
    write_report_parser.add_argument(
        "--top-n",
        type=int,
        dest="top_n",
        help="Accept a match if ground truth appears in the top N endpoint results (default: 5).",
    )

    return parser


def apply_criteria_override(config: dict[str, Any], criteria: str | None) -> dict[str, Any]:
    if not criteria:
        return config
    updated = dict(config)
    updated["comparison"] = dict(config.get("comparison") or {})
    updated["comparison"]["criteria"] = criteria
    return updated


def apply_tolerance_override(config: dict[str, Any], tolerance: float | None) -> dict[str, Any]:
    if tolerance is None:
        return config
    updated = dict(config)
    updated["comparison"] = dict(config.get("comparison") or {})
    updated["comparison"]["coordinate_tolerance_meters"] = float(tolerance)
    return updated


def apply_top_n_override(config: dict[str, Any], top_n: int | None) -> dict[str, Any]:
    if top_n is None:
        return config
    updated = dict(config)
    updated["comparison"] = dict(config.get("comparison") or {})
    updated["comparison"]["top_n"] = int(top_n)
    return updated


def apply_performance_overrides(
    config: dict[str, Any],
    *,
    workers: int | None = None,
    rps: float | None = None,
) -> dict[str, Any]:
    if workers is None and rps is None:
        return config
    updated = dict(config)
    updated["performance"] = dict(config.get("performance") or {})
    if workers is not None:
        updated["performance"]["workers"] = workers
    if rps is not None:
        updated["performance"]["requests_per_second"] = rps
        endpoints = []
        for endpoint in config.get("endpoints") or []:
            endpoint_copy = dict(endpoint)
            rate_limit = dict(endpoint.get("rate_limit") or {})
            rate_limit["requests_per_second"] = rps
            endpoint_copy["rate_limit"] = rate_limit
            endpoints.append(endpoint_copy)
        updated["endpoints"] = endpoints
    return updated


def apply_fetch_mode_overrides(
    config: dict[str, Any],
    *,
    fetch_mode: str | None = None,
    batch_size: int | None = None,
) -> dict[str, Any]:
    """Apply one/batch overrides to endpoints that accept json_array bodies (e.g. ASE)."""
    if fetch_mode is None and batch_size is None:
        return config

    updated = dict(config)
    endpoints = []
    for endpoint in config.get("endpoints") or []:
        endpoint_copy = dict(endpoint)
        request = dict(endpoint.get("request") or {})
        if request.get("address_in") == "json_array":
            if fetch_mode is not None:
                request["fetch_mode"] = fetch_mode
            if batch_size is not None:
                request["batch_size"] = max(1, int(batch_size))
                if batch_size <= 1:
                    request["fetch_mode"] = "one"
                elif fetch_mode is None:
                    request["fetch_mode"] = request.get("fetch_mode") or "batch"
            endpoint_copy["request"] = request
        endpoints.append(endpoint_copy)
    updated["endpoints"] = endpoints
    return updated


def apply_comparison_overrides(
    config: dict[str, Any],
    *,
    criteria: str | None = None,
    tolerance: float | None = None,
    top_n: int | None = None,
) -> dict[str, Any]:
    updated = apply_tolerance_override(apply_criteria_override(config, criteria), tolerance)
    return apply_top_n_override(updated, top_n)


def load_dataset_from_config(config: dict[str, Any], override_path: str | None) -> tuple[Path, list[Any]]:
    dataset_settings = get_dataset_settings(config)
    dataset_path = Path(override_path or dataset_settings["path"])
    records = load_address_dataset(
        dataset_path,
        id_column=dataset_settings.get("id_column"),
        eaddress_column=dataset_settings.get("eaddress_column", "EADDRESS"),
        caddress_column=dataset_settings.get("caddress_column", "CADDRESS"),
        easting_column=dataset_settings.get("easting_column", "EASTING"),
        northing_column=dataset_settings.get("northing_column", "NORTHING"),
        building_csuid_column=dataset_settings.get("building_csuid_column", "BUILDING_CSUID"),
        sheet_name=dataset_settings.get("sheet_name"),
    )
    return dataset_path, records


def print_validation_summary(run_id: int, summaries: list[dict[str, Any]], criteria: str) -> None:
    success_count = sum(1 for item in summaries if item["saved"])
    print(
        f"Routine run {run_id} saved ({success_count}/{len(summaries)} fetches succeeded). "
        f"Criteria: {criteria}"
    )
    if len(summaries) <= 20:
        for item in summaries:
            status = "OK" if item["saved"] else "ERROR"
            print(
                f"  - row={item['row_id']} {item['address_type']}: {status} "
                f"(HTTP {item['response_code']})"
            )
            if item["error"]:
                print(f"    error: {item['error']}")
    else:
        error_items = [item for item in summaries if not item["saved"]]
        print(f"  Showing first {min(10, len(error_items))} errors only (large run).")
        for item in error_items[:10]:
            print(
                f"  - row={item['row_id']} {item['address_type']}: ERROR "
                f"(HTTP {item['response_code']}) {item['error']}"
            )


def print_benchmark_summary(run_id: int, summaries: list[dict[str, Any]], endpoint_count: int, criteria: str) -> None:
    task_count = len({(item["row_id"], item["address_type"]) for item in summaries})
    success_count = sum(1 for item in summaries if item["saved"])
    print(
        f"Benchmark run {run_id} saved "
        f"({task_count} address tasks x {endpoint_count} endpoints = {len(summaries)} requests, "
        f"{success_count} succeeded). Criteria: {criteria}"
    )


def print_routine_comparison(comparison: RoutineComparison) -> None:
    print(
        f"Routine comparison for endpoint '{comparison.endpoint}' "
        f"using criteria '{comparison.criteria}': "
        f"run {comparison.current_run_id} vs run {comparison.previous_run_id}"
    )
    print(f"Changed rows: {comparison.changed_count}")

    for item in comparison.addresses:
        if item.status in {"unchanged", "error_unchanged"}:
            continue
        print(f"\n[row={item.row_id} {item.address_type}] {item.status}")
        print(f"  address: {item.address}")
        print(
            f"  response codes: previous={item.previous_response_code}, "
            f"current={item.current_response_code}"
        )
        if item.previous_error or item.current_error:
            print(f"  previous error: {item.previous_error}")
            print(f"  current error: {item.current_error}")
        print(f"  previous value: {json.dumps(item.previous_value, ensure_ascii=False)}")
        print(f"  current value: {json.dumps(item.current_value, ensure_ascii=False)}")


def print_match_status_comparison(comparison: MatchStatusComparison) -> None:
    print(
        f"Match-status comparison: run {comparison.current_run_id} vs "
        f"run {comparison.previous_run_id}"
    )
    print(f"Criteria: {comparison.criteria}")
    if comparison.coordinate_tolerance_meters is not None:
        print(f"Tolerance: {comparison.coordinate_tolerance_meters:g} metres")
    if comparison.top_n is not None:
        print(f"Top-N ranking window: {comparison.top_n}")
    print(
        f"newly_matched (within tolerance now, not before): "
        f"{comparison.newly_matched_count}"
    )
    print(
        f"lost_match (within tolerance before, not now): "
        f"{comparison.lost_match_count}"
    )
    print(f"other status changes: {comparison.other_changed_count}")

    for item in comparison.differences:
        print(f"\n[{item.change}] row={item.row_id} {item.address_type} {item.endpoint}")
        print(f"  address: {item.address}")
        print(f"  previous status: {item.previous_status}")
        print(f"  current status: {item.current_status}")
        if item.previous_distance_m is not None or item.current_distance_m is not None:
            prev_d = (
                f"{item.previous_distance_m:.2f}"
                if item.previous_distance_m is not None
                else "n/a"
            )
            curr_d = (
                f"{item.current_distance_m:.2f}"
                if item.current_distance_m is not None
                else "n/a"
            )
            print(f"  distance_m: previous={prev_d}, current={curr_d}")


def print_accuracy_report(report: AccuracyReport) -> None:
    print(f"Accuracy report for run {report.run_id} [{report.run_type}]")
    print(f"Criteria: {report.criteria}")
    if report.coordinate_tolerance_meters is not None:
        print(f"Tolerance: {report.coordinate_tolerance_meters:g} metres")
    if report.top_n is not None:
        print(f"Top-N ranking window: {report.top_n}")
    if report.endpoint:
        print(f"Endpoint: {report.endpoint}")
    if report.match_rate is not None:
        print(
            f"Match rate: {report.match_rate * 100:.1f}% "
            f"({report.matched} matched, {report.not_found} not found, "
            f"{report.mismatched} mismatched, {report.not_comparable} not comparable)"
        )
    else:
        print("No comparable rows for the selected criteria.")

    for item in report.items:
        if item.matches is True:
            continue
        print(f"\n[row={item.row_id} {item.address_type}] {item.endpoint} -> {item.status}")
        print(f"  address: {item.address}")
        print(f"  expected: {json.dumps(item.expected, ensure_ascii=False)}")
        print(f"  actual: {json.dumps(item.actual, ensure_ascii=False)}")
        if item.distance_m is not None:
            print(f"  distance_m: {item.distance_m:.2f}")
        if item.match_rank is not None:
            print(f"  match_rank: {item.match_rank}")
        if item.error:
            print(f"  error: {item.error}")


def print_benchmark_report(report: BenchmarkReport) -> None:
    print(f"Benchmark report for run {report.run_id}")
    print(f"Baseline endpoint: {report.baseline_endpoint}")
    print(f"Criteria: {report.criteria}")
    print("")
    print(
        f"{'Endpoint':<24} {'Success':>8} {'Avg ms':>10} "
        f"{'Truth match':>12} {'Base match':>12} {'Faster':>8} {'Slower':>8}"
    )
    print("-" * 90)

    for summary in report.endpoints:
        truth_match = (
            f"{summary.ground_truth_match_rate * 100:.1f}%"
            if summary.ground_truth_match_rate is not None
            else "n/a"
        )
        base_match = (
            f"{summary.baseline_match_rate * 100:.1f}%"
            if summary.baseline_match_rate is not None
            else "n/a"
        )
        avg_latency = (
            f"{summary.avg_latency_ms:.1f}"
            if summary.avg_latency_ms is not None
            else "n/a"
        )
        faster = (
            str(summary.faster_than_baseline_count)
            if summary.faster_than_baseline_count is not None
            else "-"
        )
        slower = (
            str(summary.slower_than_baseline_count)
            if summary.slower_than_baseline_count is not None
            else "-"
        )
        print(
            f"{summary.endpoint:<24} "
            f"{summary.success_rate * 100:>7.1f}% "
            f"{avg_latency:>10} "
            f"{truth_match:>12} "
            f"{base_match:>12} "
            f"{faster:>8} "
            f"{slower:>8}"
        )


def print_match_summary(table: MatchSummaryTable) -> None:
    print(f"Match summary for run {table.run_id}")
    print(f"Criteria: {table.criteria}")
    if table.tolerance_meters is not None:
        print(f"Tolerance: {table.tolerance_meters:g} metres")
    if getattr(table, "top_n", None) is not None:
        print(f"Top-N ranking window: {table.top_n}")
    print("")
    print(f"{'column_name':<28} {'number':>10} {'percentage':>12}")
    print("-" * 52)
    for row in table.rows:
        print(f"{row.column_name:<28} {row.number:>10} {row.percentage:>11.2f}%")


def resolve_resume_run_id(
    database: Database,
    resume_arg: str | None,
    run_type: str,
) -> int | None:
    if resume_arg is None:
        return None
    if resume_arg == "latest":
        run_id = database.get_latest_incomplete_run_id(run_type)  # type: ignore[arg-type]
        if run_id is None:
            raise ValueError(f"No incomplete {run_type} run found to resume.")
        return run_id
    return int(resume_arg)


def write_auto_report(
    config: dict[str, Any],
    database: Database,
    settings: Any,
    run_id: int,
    *,
    compare_with_run_id: int | None = None,
) -> None:
    if not reports_enabled(config):
        return
    written = ReportWriter(database, settings, config).write_run_report(
        run_id,
        compare_with_run_id=compare_with_run_id,
        auto_compare_previous=True,
    )
    log_info(f"Report written to {written.directory.resolve()}")
    print(f"Report folder: {written.directory.resolve()}")


def resolve_compare_previous_run_id(
    args: argparse.Namespace,
    database: Database,
    current_run_id: int,
) -> int:
    selectors = [
        bool(args.previous),
        bool(args.with_previous),
        bool(args.with_date),
        bool(args.with_month),
        bool(args.previous_month),
    ]
    if sum(selectors) != 1:
        raise ValueError(
            "Choose exactly one of: --previous, --with-previous, "
            "--with-date, --with-month, --previous-month."
        )

    run_type = args.type
    if args.previous is not None:
        return int(args.previous)
    if args.with_previous:
        previous = database.get_previous_run_id(current_run_id, run_type=run_type)
        if previous is None:
            raise ValueError(f"No previous {run_type} run exists before run {current_run_id}.")
        return previous
    if args.with_date:
        previous = database.find_run_on_date(
            args.with_date,
            run_type=run_type,
            before_run_id=current_run_id,
        )
        if previous is None:
            raise ValueError(f"No completed {run_type} run found on {args.with_date}.")
        return previous
    if args.with_month:
        try:
            year_text, month_text = args.with_month.split("-", 1)
            year, month = int(year_text), int(month_text)
        except ValueError as exc:
            raise ValueError("Month must be YYYY-MM, e.g. 2026-06") from exc
        previous = database.find_run_in_month(
            year,
            month,
            run_type=run_type,
            before_run_id=current_run_id,
        )
        if previous is None:
            raise ValueError(f"No completed {run_type} run found in {args.with_month}.")
        return previous

    previous = database.find_previous_month_run_id(current_run_id, run_type=run_type)
    if previous is None:
        raise ValueError(
            f"No completed {run_type} run found in the previous calendar month "
            f"before run {current_run_id}."
        )
    return previous


def handle_validate(args: argparse.Namespace, config: dict[str, Any], database: Database) -> int:
    config = apply_comparison_overrides(
        config,
        criteria=args.criteria,
        tolerance=getattr(args, "tolerance", None),
        top_n=getattr(args, "top_n", None),
    )
    config = apply_performance_overrides(
        config,
        workers=getattr(args, "workers", None),
        rps=getattr(args, "rps", None),
    )
    config = apply_fetch_mode_overrides(
        config,
        fetch_mode=getattr(args, "fetch_mode", None),
        batch_size=getattr(args, "batch_size", None),
    )
    settings = get_comparison_settings(config)
    log_info("Loading dataset...")
    dataset_path, records = load_dataset_from_config(config, args.dataset)
    log_info(f"Loaded {len(records)} Excel rows from {dataset_path}")
    endpoint = get_routine_endpoint(config)
    log_info(f"Routine endpoint ready: {endpoint['name']}")
    fetcher = AddressFetcher(config)
    runner = RoutineRunner(config, database, fetcher)

    try:
        resume_run_id = resolve_resume_run_id(database, getattr(args, "resume", None), "routine")
    except ValueError as exc:
        log_warn(str(exc))
        return 1

    run_id, summaries = runner.run(
        records,
        endpoint,
        label=args.label,
        notes=args.notes,
        dataset_path=dataset_path,
        workers=getattr(args, "workers", None),
        resume_run_id=resume_run_id,
        retry_errors=getattr(args, "retry_errors", False),
        verbose=getattr(args, "verbose", False),
    )
    print_validation_summary(run_id, summaries, settings.criteria)
    log_info(f"Saved results are durable in SQLite (run {run_id}). Use --resume after a crash.")

    exit_code = 0
    if args.accuracy:
        log_info("Computing accuracy against EASTING/NORTHING ground truth...")
        report = AccuracyAnalyzer(database, settings).analyze_run(run_id)
        print()
        print_accuracy_report(report)
        if report.not_found or report.mismatched:
            exit_code = 1

    compare_with_run_id = None
    if args.compare_with_previous:
        comparator = RoutineComparator(database, settings)
        previous_run_id = database.get_previous_run_id(run_id, run_type="routine")
        if previous_run_id is None:
            print("No previous routine run available for comparison.")
        else:
            compare_with_run_id = previous_run_id
            match_diff = comparator.compare_match_status(run_id, previous_run_id)
            print()
            print_match_status_comparison(match_diff)
            if match_diff.has_differences:
                exit_code = 1

    write_auto_report(
        config,
        database,
        settings,
        run_id,
        compare_with_run_id=compare_with_run_id,
    )
    return exit_code


def handle_benchmark(args: argparse.Namespace, config: dict[str, Any], database: Database) -> int:
    config = apply_comparison_overrides(
        config,
        criteria=args.criteria,
        tolerance=getattr(args, "tolerance", None),
        top_n=getattr(args, "top_n", None),
    )
    config = apply_performance_overrides(
        config,
        workers=getattr(args, "workers", None),
        rps=getattr(args, "rps", None),
    )
    config = apply_fetch_mode_overrides(
        config,
        fetch_mode=getattr(args, "fetch_mode", None),
        batch_size=getattr(args, "batch_size", None),
    )
    settings = get_comparison_settings(config)
    log_info("Loading dataset...")
    dataset_path, records = load_dataset_from_config(config, args.dataset)
    log_info(f"Loaded {len(records)} Excel rows from {dataset_path}")
    baseline_endpoint, endpoints = get_benchmark_endpoints(config)
    log_info(f"Benchmark endpoints ready: {', '.join(item['name'] for item in endpoints)}")
    fetcher = AddressFetcher(config)
    runner = BenchmarkRunner(config, database, fetcher)

    try:
        resume_run_id = resolve_resume_run_id(database, getattr(args, "resume", None), "benchmark")
    except ValueError as exc:
        log_warn(str(exc))
        return 1

    run_id, summaries = runner.run(
        records,
        endpoints,
        label=args.label,
        notes=args.notes,
        dataset_path=dataset_path,
        workers=getattr(args, "workers", None),
        resume_run_id=resume_run_id,
        retry_errors=getattr(args, "retry_errors", False),
        verbose=getattr(args, "verbose", False),
    )
    print_benchmark_summary(run_id, summaries, len(endpoints), settings.criteria)
    log_info(f"Saved results are durable in SQLite (run {run_id}). Use --resume after a crash.")

    if args.report:
        report = BenchmarkAnalyzer(database, settings).analyze(run_id, baseline_endpoint)
        print()
        print_benchmark_report(report)

    if args.summary or args.report:
        table = MatchSummaryBuilder(
            database,
            settings,
            get_endpoint_display_names(config),
        ).build(run_id)
        print()
        print_match_summary(table)

    write_auto_report(config, database, settings, run_id)
    return 0


def handle_list_runs(args: argparse.Namespace, database: Database) -> int:
    runs = database.list_runs(run_type=args.type, limit=args.limit)
    if not runs:
        print("No runs found.")
        return 0

    for run in runs:
        label = f" label={run.label!r}" if run.label else ""
        endpoint = f" endpoint={run.endpoint_name!r}" if run.endpoint_name else ""
        criteria = f" criteria={run.comparison_criteria!r}" if run.comparison_criteria else ""
        status = f" status={run.status}"
        saved = database.count_results(run.id, run.run_type)
        print(
            f"run {run.id} [{run.run_type}] at {run.created_at}"
            f"{endpoint}{criteria}{status} saved={saved}{label}"
        )
    return 0


def handle_show_run(args: argparse.Namespace, database: Database) -> int:
    run = database.get_run(args.run_id)
    if run is None:
        print(f"Run {args.run_id} not found.", file=sys.stderr)
        return 1

    print(f"Run {run.id} [{run.run_type}] created at {run.created_at}")
    if run.endpoint_name:
        print(f"Endpoint: {run.endpoint_name}")
    if run.comparison_criteria:
        print(f"Criteria: {run.comparison_criteria}")
    if run.dataset_path:
        print(f"Dataset: {run.dataset_path}")
    if run.label:
        print(f"Label: {run.label}")
    if run.notes:
        print(f"Notes: {run.notes}")

    if run.run_type == "routine":
        results = database.get_validation_results(args.run_id)
        if not results:
            print("No validation results stored for this run.")
            return 0
        for result in results:
            print(f"\n[row={result.row_id} {result.address_type}] {result.address}")
            print(f"  endpoint: {result.endpoint}")
            print(f"  response_code: {result.response_code}")
            print(f"  expected: easting={result.expected_easting}, northing={result.expected_northing}, csuid={result.expected_building_csuid}")
            print(f"  coordinates: {result.coordinates}")
            print(f"  building_csuid: {result.building_csuid}")
            print(f"  comparison_value: {result.comparison_value}")
            if result.error:
                print(f"  error: {result.error}")
        return 0

    results = database.get_benchmark_results(args.run_id)
    if not results:
        print("No benchmark results stored for this run.")
        return 0

    for result in results:
        print(f"\n[row={result.row_id} {result.address_type}] {result.endpoint} -> {result.address}")
        print(f"  response_code: {result.response_code}")
        print(f"  latency_ms: {result.latency_ms}")
        print(f"  coordinates: {result.coordinates}")
        print(f"  building_csuid: {result.building_csuid}")
        print(f"  comparison_value: {result.comparison_value}")
        if result.error:
            print(f"  error: {result.error}")
    return 0


def handle_compare(args: argparse.Namespace, config: dict[str, Any], database: Database) -> int:
    config = apply_comparison_overrides(
        config,
        criteria=getattr(args, "criteria", None),
        tolerance=getattr(args, "tolerance", None),
        top_n=getattr(args, "top_n", None),
    )
    settings = get_comparison_settings(config)
    comparator = RoutineComparator(database, settings)

    current_run_id = args.current
    if current_run_id is None:
        current_run_id = database.get_latest_run_id(run_type=args.type)
        if current_run_id is None:
            print(f"No {args.type} runs available.", file=sys.stderr)
            return 1

    try:
        previous_run_id = resolve_compare_previous_run_id(args, database, current_run_id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    match_diff = comparator.compare_match_status(current_run_id, previous_run_id)

    if args.json:
        payload: dict[str, Any] = {
            "match_status": match_status_comparison_to_dict(match_diff),
        }
        if args.value_diff:
            current_run = database.get_run(current_run_id)
            if current_run and current_run.run_type == "routine":
                value_diff = comparator.compare_runs(current_run_id, previous_run_id)
                payload["value_diff"] = routine_comparison_to_dict(value_diff)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print_match_status_comparison(match_diff)
        if args.value_diff:
            current_run = database.get_run(current_run_id)
            if current_run and current_run.run_type == "routine":
                print()
                print_routine_comparison(comparator.compare_runs(current_run_id, previous_run_id))
            else:
                print("\nNote: --value-diff only applies to routine validation runs.")

    if reports_enabled(config):
        written = ReportWriter(database, settings, config).write_run_report(
            current_run_id,
            compare_with_run_id=previous_run_id,
            auto_compare_previous=False,
        )
        print(f"\nReport folder: {written.directory.resolve()}")

    return 1 if match_diff.has_differences else 0


def handle_report(args: argparse.Namespace, config: dict[str, Any], database: Database) -> int:
    run_id = args.run_id or database.get_latest_run_id(run_type="benchmark")
    if run_id is None:
        print("No benchmark runs available.", file=sys.stderr)
        return 1

    run = database.get_run(run_id)
    if run is None or run.run_type != "benchmark":
        print(f"Benchmark run {run_id} not found.", file=sys.stderr)
        return 1

    baseline_endpoint, _ = get_benchmark_endpoints(config)
    settings = get_comparison_settings(config)
    report = BenchmarkAnalyzer(database, settings).analyze(run_id, baseline_endpoint)

    if args.json:
        print(json.dumps(benchmark_report_to_dict(report), indent=2, ensure_ascii=False))
    else:
        print_benchmark_report(report)

    if args.summary or not args.json:
        table = MatchSummaryBuilder(
            database,
            settings,
            get_endpoint_display_names(config),
        ).build(run_id)
        print()
        print_match_summary(table)
    return 0


def handle_summary(args: argparse.Namespace, config: dict[str, Any], database: Database) -> int:
    config = apply_comparison_overrides(
        config,
        criteria=args.criteria,
        tolerance=getattr(args, "tolerance", None),
        top_n=getattr(args, "top_n", None),
    )
    settings = get_comparison_settings(config)
    run_id = args.run_id or database.get_latest_run_id()
    if run_id is None:
        print("No runs available.", file=sys.stderr)
        return 1

    table = MatchSummaryBuilder(
        database,
        settings,
        get_endpoint_display_names(config),
    ).build(run_id)

    if args.csv:
        Path(args.csv).write_text(match_summary_to_csv(table), encoding="utf-8")
        print(f"Wrote summary CSV to {args.csv}")

    if args.json:
        print(json.dumps(match_summary_to_dict(table), indent=2, ensure_ascii=False))
    else:
        print_match_summary(table)
    return 0


def handle_accuracy(args: argparse.Namespace, config: dict[str, Any], database: Database) -> int:
    config = apply_comparison_overrides(
        config,
        criteria=args.criteria,
        tolerance=getattr(args, "tolerance", None),
        top_n=getattr(args, "top_n", None),
    )
    settings = get_comparison_settings(config)
    run_id = args.run_id or database.get_latest_run_id()
    if run_id is None:
        print("No runs available.", file=sys.stderr)
        return 1

    report = AccuracyAnalyzer(database, settings).analyze_run(run_id)
    if args.json:
        print(json.dumps(accuracy_report_to_dict(report), indent=2, ensure_ascii=False))
    else:
        print_accuracy_report(report)

    return 1 if report.not_found or report.mismatched else 0


def handle_write_report(args: argparse.Namespace, config: dict[str, Any], database: Database) -> int:
    config = apply_comparison_overrides(
        config,
        criteria=getattr(args, "criteria", None),
        tolerance=getattr(args, "tolerance", None),
        top_n=getattr(args, "top_n", None),
    )
    settings = get_comparison_settings(config)
    run_id = args.run_id or database.get_latest_run_id()
    if run_id is None:
        print("No runs available.", file=sys.stderr)
        return 1

    try:
        written = ReportWriter(database, settings, config).write_run_report(
            run_id,
            compare_with_run_id=getattr(args, "compare_with", None),
            auto_compare_previous=getattr(args, "compare_with", None) is None,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Report folder: {written.directory.resolve()}")
    for path in written.files:
        if path.name != "LATEST.txt":
            print(f"  - {path.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    proxy_settings = get_proxy_settings(config)
    if proxy_settings.enabled:
        log_info(f"Proxy: {proxy_settings.redacted_summary()}")
    else:
        log_info("Proxy: not configured (set HTTPS_PROXY if public APIs need company proxy)")

    database = Database(get_database_path(config))

    if args.command == "validate":
        return handle_validate(args, config, database)
    if args.command == "benchmark":
        return handle_benchmark(args, config, database)
    if args.command == "list-runs":
        return handle_list_runs(args, database)
    if args.command == "show-run":
        return handle_show_run(args, database)
    if args.command == "compare":
        return handle_compare(args, config, database)
    if args.command == "report":
        return handle_report(args, config, database)
    if args.command == "summary":
        return handle_summary(args, config, database)
    if args.command == "accuracy":
        return handle_accuracy(args, config, database)
    if args.command == "write-report":
        return handle_write_report(args, config, database)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
