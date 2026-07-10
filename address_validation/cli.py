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
    RoutineComparator,
    RoutineComparison,
    accuracy_report_to_dict,
    benchmark_report_to_dict,
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

    list_parser = subparsers.add_parser("list-runs", help="List saved runs")
    list_parser.add_argument("--type", choices=["routine", "benchmark"], help="Filter by run type")
    list_parser.add_argument("--limit", type=int, default=20, help="Maximum runs to show")

    show_parser = subparsers.add_parser("show-run", help="Show results for a specific run")
    show_parser.add_argument("run_id", type=int, help="Run ID to inspect")

    compare_parser = subparsers.add_parser("compare", help="Compare routine validation runs")
    compare_parser.add_argument("--current", type=int, help="Current run ID")
    compare_parser.add_argument("--previous", type=int, help="Previous run ID")
    compare_parser.add_argument("--with-previous", action="store_true")
    compare_parser.add_argument("--json", action="store_true")

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
    accuracy_parser.add_argument("--json", action="store_true")

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
    return updated


def apply_comparison_overrides(
    config: dict[str, Any],
    *,
    criteria: str | None = None,
    tolerance: float | None = None,
) -> dict[str, Any]:
    return apply_tolerance_override(apply_criteria_override(config, criteria), tolerance)


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


def print_accuracy_report(report: AccuracyReport) -> None:
    print(f"Accuracy report for run {report.run_id} [{report.run_type}]")
    print(f"Criteria: {report.criteria}")
    if report.coordinate_tolerance_meters is not None:
        print(f"Tolerance: {report.coordinate_tolerance_meters:g} metres")
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


def handle_validate(args: argparse.Namespace, config: dict[str, Any], database: Database) -> int:
    config = apply_comparison_overrides(
        config,
        criteria=args.criteria,
        tolerance=getattr(args, "tolerance", None),
    )
    config = apply_performance_overrides(
        config,
        workers=getattr(args, "workers", None),
        rps=getattr(args, "rps", None),
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

    if args.compare_with_previous:
        comparator = RoutineComparator(database, settings)
        previous_run_id = database.get_previous_run_id(run_id, run_type="routine")
        if previous_run_id is None:
            print("No previous routine run available for comparison.")
            return exit_code

        comparison = comparator.compare_runs(run_id, previous_run_id)
        print()
        print_routine_comparison(comparison)
        if comparison.has_differences:
            exit_code = 1

    return exit_code


def handle_benchmark(args: argparse.Namespace, config: dict[str, Any], database: Database) -> int:
    config = apply_comparison_overrides(
        config,
        criteria=args.criteria,
        tolerance=getattr(args, "tolerance", None),
    )
    config = apply_performance_overrides(
        config,
        workers=getattr(args, "workers", None),
        rps=getattr(args, "rps", None),
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
    settings = get_comparison_settings(config)
    comparator = RoutineComparator(database, settings)

    if args.with_previous:
        if args.current is None:
            latest_run_id = database.get_latest_run_id(run_type="routine")
            if latest_run_id is None:
                print("No routine runs available.", file=sys.stderr)
                return 1
            args.current = latest_run_id
        try:
            comparison = comparator.compare_with_previous(args.current)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    else:
        if args.current is None or args.previous is None:
            print("Provide both --current and --previous, or use --with-previous.", file=sys.stderr)
            return 1
        comparison = comparator.compare_runs(args.current, args.previous)

    if args.json:
        print(json.dumps(routine_comparison_to_dict(comparison), indent=2, ensure_ascii=False))
    else:
        print_routine_comparison(comparison)

    return 1 if comparison.has_differences else 0


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

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
