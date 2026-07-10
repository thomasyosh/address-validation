from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from address_validation.comparison_rules import (
    ComparisonSettings,
    building_csuid_match,
    evaluate_candidates,
    evaluate_coordinates,
    parse_coordinate_pair,
    values_equal_for_criteria,
)
from address_validation.database import BenchmarkResult, Database, ValidationResult, parse_json_text


@dataclass
class AddressComparison:
    row_id: int
    address_type: str
    address: str
    status: str
    current_value: Any
    previous_value: Any
    current_response_code: int | None
    previous_response_code: int | None
    current_error: str | None
    previous_error: str | None


@dataclass
class RoutineComparison:
    current_run_id: int
    previous_run_id: int
    endpoint: str
    criteria: str
    addresses: list[AddressComparison]

    @property
    def has_differences(self) -> bool:
        return any(item.status not in {"unchanged", "error_unchanged"} for item in self.addresses)

    @property
    def changed_count(self) -> int:
        return sum(1 for item in self.addresses if item.status == "changed")


@dataclass
class MatchStatusDiffItem:
    """One address whose within-tolerance match status changed between runs."""

    row_id: int
    address_type: str
    address: str
    endpoint: str
    change: str
    current_status: str | None
    previous_status: str | None
    current_distance_m: float | None
    previous_distance_m: float | None


@dataclass
class MatchStatusComparison:
    """
    Diff focused on ground-truth match status (e.g. within 50m / top-N).

    newly_matched: matched now, not matched previously
    lost_match: matched previously, not matched now
    """

    current_run_id: int
    previous_run_id: int
    criteria: str
    coordinate_tolerance_meters: float | None
    differences: list[MatchStatusDiffItem]
    top_n: int | None = None

    @property
    def newly_matched_count(self) -> int:
        return sum(1 for item in self.differences if item.change == "newly_matched")

    @property
    def lost_match_count(self) -> int:
        return sum(1 for item in self.differences if item.change == "lost_match")

    @property
    def other_changed_count(self) -> int:
        return sum(
            1
            for item in self.differences
            if item.change not in {"newly_matched", "lost_match"}
        )

    @property
    def has_differences(self) -> bool:
        return bool(self.differences)


@dataclass
class AccuracyItem:
    row_id: int
    address_type: str
    address: str
    endpoint: str
    status: str
    expected: Any
    actual: Any
    matches: bool | None
    error: str | None
    distance_m: float | None = None
    match_rank: int | None = None


@dataclass
class AccuracyReport:
    run_id: int
    run_type: str
    criteria: str
    endpoint: str | None
    total: int
    matched: int
    not_found: int
    mismatched: int
    not_comparable: int
    match_rate: float | None
    coordinate_tolerance_meters: float | None
    items: list[AccuracyItem]
    top_n: int | None = None


@dataclass
class EndpointBenchmarkSummary:
    endpoint: str
    total_requests: int
    success_count: int
    error_count: int
    success_rate: float
    avg_latency_ms: float | None
    ground_truth_match_rate: float | None
    baseline_match_rate: float | None
    faster_than_baseline_count: int | None
    slower_than_baseline_count: int | None


@dataclass
class BenchmarkReport:
    run_id: int
    baseline_endpoint: str
    criteria: str
    endpoints: list[EndpointBenchmarkSummary]
    address_details: list[dict[str, Any]]


class RoutineComparator:
    def __init__(self, database: Database, settings: ComparisonSettings) -> None:
        self.database = database
        self.settings = settings

    def compare_runs(self, current_run_id: int, previous_run_id: int) -> RoutineComparison:
        current_results = {
            (result.row_id, result.address_type): result
            for result in self.database.get_validation_results(current_run_id)
        }
        previous_results = {
            (result.row_id, result.address_type): result
            for result in self.database.get_validation_results(previous_run_id)
        }

        endpoint_name = self._resolve_endpoint_name(current_results, previous_results)
        keys = sorted(set(current_results) | set(previous_results))
        comparisons = [
            self._compare_address(
                key=key,
                current=current_results.get(key),
                previous=previous_results.get(key),
            )
            for key in keys
        ]

        return RoutineComparison(
            current_run_id=current_run_id,
            previous_run_id=previous_run_id,
            endpoint=endpoint_name,
            criteria=self.settings.criteria,
            addresses=comparisons,
        )

    def compare_with_previous(self, run_id: int) -> RoutineComparison:
        previous_run_id = self.database.get_previous_run_id(run_id, run_type="routine")
        if previous_run_id is None:
            raise ValueError(f"No previous routine run exists before run {run_id}.")
        return self.compare_runs(run_id, previous_run_id)

    def compare_match_status(
        self,
        current_run_id: int,
        previous_run_id: int,
    ) -> MatchStatusComparison:
        """Compare whether each address was within tolerance vs ground truth."""
        analyzer = AccuracyAnalyzer(self.database, self.settings)
        current_items = {
            (item.row_id, item.address_type, item.endpoint): item
            for item in analyzer.analyze_run(current_run_id).items
        }
        previous_items = {
            (item.row_id, item.address_type, item.endpoint): item
            for item in analyzer.analyze_run(previous_run_id).items
        }
        keys = sorted(set(current_items) | set(previous_items))
        differences: list[MatchStatusDiffItem] = []

        for key in keys:
            current = current_items.get(key)
            previous = previous_items.get(key)
            current_matched = bool(current and current.matches is True)
            previous_matched = bool(previous and previous.matches is True)

            if current is None:
                change = "missing_in_current"
            elif previous is None:
                change = "missing_in_previous"
            elif current_matched and not previous_matched:
                change = "newly_matched"
            elif previous_matched and not current_matched:
                change = "lost_match"
            elif (current.status if current else None) != (previous.status if previous else None):
                change = "status_changed"
            else:
                continue

            sample = current or previous
            assert sample is not None
            differences.append(
                MatchStatusDiffItem(
                    row_id=sample.row_id,
                    address_type=sample.address_type,
                    address=sample.address,
                    endpoint=sample.endpoint,
                    change=change,
                    current_status=current.status if current else None,
                    previous_status=previous.status if previous else None,
                    current_distance_m=current.distance_m if current else None,
                    previous_distance_m=previous.distance_m if previous else None,
                )
            )

        return MatchStatusComparison(
            current_run_id=current_run_id,
            previous_run_id=previous_run_id,
            criteria=self.settings.criteria,
            coordinate_tolerance_meters=(
                self.settings.coordinate_tolerance
                if self.settings.criteria == "coordinates"
                else None
            ),
            differences=differences,
            top_n=self.settings.top_n,
        )

    def compare_match_status_with_previous(self, run_id: int) -> MatchStatusComparison:
        run = self.database.get_run(run_id)
        run_type = run.run_type if run else "routine"
        previous_run_id = self.database.get_previous_run_id(run_id, run_type=run_type)
        if previous_run_id is None:
            raise ValueError(f"No previous {run_type} run exists before run {run_id}.")
        return self.compare_match_status(run_id, previous_run_id)

    def _compare_address(
        self,
        key: tuple[int, str],
        current: ValidationResult | None,
        previous: ValidationResult | None,
    ) -> AddressComparison:
        row_id, address_type = key

        if current is None:
            return AddressComparison(
                row_id=row_id,
                address_type=address_type,
                address=previous.address if previous else "",
                status="missing_in_current",
                current_value=None,
                previous_value=parse_json_text(previous.comparison_value) if previous else None,
                current_response_code=None,
                previous_response_code=previous.response_code if previous else None,
                current_error=None,
                previous_error=previous.error if previous else None,
            )

        if previous is None:
            return AddressComparison(
                row_id=row_id,
                address_type=address_type,
                address=current.address,
                status="missing_in_previous",
                current_value=parse_json_text(current.comparison_value),
                previous_value=None,
                current_response_code=current.response_code,
                previous_response_code=None,
                current_error=current.error,
                previous_error=None,
            )

        value_match = values_equal_for_criteria(
            current.comparison_value,
            previous.comparison_value,
            criteria=self.settings.criteria,
            coordinate_tolerance=self.settings.coordinate_tolerance,
            easting_field=self.settings.easting_field,
            northing_field=self.settings.northing_field,
        )
        status = "unchanged"
        if current.error or previous.error:
            status = "error_state_changed" if current.error != previous.error else "error_unchanged"
        elif not value_match or current.response_code != previous.response_code:
            status = "changed"

        return AddressComparison(
            row_id=row_id,
            address_type=address_type,
            address=current.address,
            status=status,
            current_value=parse_json_text(current.comparison_value),
            previous_value=parse_json_text(previous.comparison_value),
            current_response_code=current.response_code,
            previous_response_code=previous.response_code,
            current_error=current.error,
            previous_error=previous.error,
        )

    @staticmethod
    def _resolve_endpoint_name(
        current_results: dict[tuple[int, str], ValidationResult],
        previous_results: dict[tuple[int, str], ValidationResult],
    ) -> str:
        for result in current_results.values():
            return result.endpoint
        for result in previous_results.values():
            return result.endpoint
        return "unknown"


class AccuracyAnalyzer:
    def __init__(self, database: Database, settings: ComparisonSettings) -> None:
        self.database = database
        self.settings = settings

    def analyze_run(self, run_id: int) -> AccuracyReport:
        run = self.database.get_run(run_id)
        if run is None:
            raise ValueError(f"Run {run_id} not found.")

        if run.run_type == "routine":
            results = self.database.get_validation_results(run_id)
            endpoint = results[0].endpoint if results else run.endpoint_name
            items = [self._accuracy_from_validation(result, endpoint or "unknown") for result in results]
        else:
            results = self.database.get_benchmark_results(run_id)
            items = [self._accuracy_from_benchmark(result) for result in results]
            endpoint = None

        matched = sum(1 for item in items if item.status == "matched")
        not_found = sum(1 for item in items if item.status == "not_found")
        mismatched = sum(1 for item in items if item.status == "mismatched")
        not_comparable = sum(1 for item in items if item.status == "not_comparable")
        comparable = matched + not_found + mismatched

        return AccuracyReport(
            run_id=run_id,
            run_type=run.run_type,
            criteria=self.settings.criteria,
            endpoint=endpoint,
            total=len(items),
            matched=matched,
            not_found=not_found,
            mismatched=mismatched,
            not_comparable=not_comparable,
            match_rate=(matched / comparable) if comparable else None,
            coordinate_tolerance_meters=(
                self.settings.coordinate_tolerance
                if self.settings.criteria == "coordinates"
                else None
            ),
            items=items,
            top_n=self.settings.top_n,
        )

    def _accuracy_from_validation(self, result: ValidationResult, endpoint: str) -> AccuracyItem:
        matches, expected, actual, status, distance_m, match_rank = self._evaluate_result(result)
        return AccuracyItem(
            row_id=result.row_id,
            address_type=result.address_type,
            address=result.address,
            endpoint=endpoint,
            status=status,
            expected=expected,
            actual=actual,
            matches=matches,
            error=result.error,
            distance_m=distance_m,
            match_rank=match_rank,
        )

    def _accuracy_from_benchmark(self, result: BenchmarkResult) -> AccuracyItem:
        matches, expected, actual, status, distance_m, match_rank = self._evaluate_result(result)
        return AccuracyItem(
            row_id=result.row_id,
            address_type=result.address_type,
            address=result.address,
            endpoint=result.endpoint,
            status=status,
            expected=expected,
            actual=actual,
            matches=matches,
            error=result.error,
            distance_m=distance_m,
            match_rank=match_rank,
        )

    def _evaluate_result(
        self,
        result: ValidationResult | BenchmarkResult,
    ) -> tuple[bool | None, Any, Any, str, float | None, int | None]:
        if result.error:
            return False, self._expected_value(result), self._actual_value(result), "not_found", None, None

        candidates = parse_json_text(result.candidates)
        if isinstance(candidates, list) and candidates:
            evaluation = evaluate_candidates(
                candidates,
                criteria=self.settings.criteria,
                expected_easting=result.expected_easting,
                expected_northing=result.expected_northing,
                expected_building_csuid=result.expected_building_csuid,
                tolerance_meters=self.settings.coordinate_tolerance,
                top_n=self.settings.top_n,
            )
            if self.settings.criteria == "coordinates":
                expected = {
                    "easting": result.expected_easting,
                    "northing": result.expected_northing,
                }
                actual_value = {
                    "easting": evaluation.matched_easting,
                    "northing": evaluation.matched_northing,
                }
                if evaluation.matches is not True:
                    # Fall back to top-1 for display when nothing in top_n matched.
                    actual_value = self._actual_value(result)
            else:
                expected = result.expected_building_csuid
                actual_value = evaluation.matched_building_csuid
                if evaluation.matches is not True:
                    actual_value = result.building_csuid
            return (
                evaluation.matches,
                expected,
                actual_value,
                evaluation.status,
                evaluation.distance_m,
                evaluation.match_rank,
            )

        # Legacy rows without stored candidates: compare the single saved value.
        if self.settings.criteria == "coordinates":
            actual = parse_coordinate_pair(
                parse_json_text(result.coordinates),
                easting_field=self.settings.easting_field,
                northing_field=self.settings.northing_field,
            )
            expected = {
                "easting": result.expected_easting,
                "northing": result.expected_northing,
            }
            evaluation = evaluate_coordinates(
                actual,
                result.expected_easting,
                result.expected_northing,
                self.settings.coordinate_tolerance,
            )
            actual_value = {
                "easting": actual.easting,
                "northing": actual.northing,
            }
            return (
                evaluation.matches,
                expected,
                actual_value,
                evaluation.status,
                evaluation.distance_m,
                evaluation.match_rank if evaluation.matches else None,
            )

        expected = result.expected_building_csuid
        actual_value = result.building_csuid
        matches = building_csuid_match(actual_value, expected)
        status = "matched" if matches else "mismatched"
        if matches is None:
            status = "not_comparable"
        return matches, expected, actual_value, status, None, (1 if matches else None)

    def _expected_value(self, result: ValidationResult | BenchmarkResult) -> Any:
        if self.settings.criteria == "coordinates":
            return {
                "easting": result.expected_easting,
                "northing": result.expected_northing,
            }
        return result.expected_building_csuid

    def _actual_value(self, result: ValidationResult | BenchmarkResult) -> Any:
        if self.settings.criteria == "coordinates":
            pair = parse_coordinate_pair(
                parse_json_text(result.coordinates),
                easting_field=self.settings.easting_field,
                northing_field=self.settings.northing_field,
            )
            return {"easting": pair.easting, "northing": pair.northing}
        return result.building_csuid


class BenchmarkAnalyzer:
    def __init__(self, database: Database, settings: ComparisonSettings) -> None:
        self.database = database
        self.settings = settings
        self.accuracy_analyzer = AccuracyAnalyzer(database, settings)

    def analyze(self, run_id: int, baseline_endpoint: str) -> BenchmarkReport:
        results = self.database.get_benchmark_results(run_id)
        by_endpoint: dict[str, list[BenchmarkResult]] = {}
        by_task: dict[tuple[int, str], dict[str, BenchmarkResult]] = {}

        for result in results:
            by_endpoint.setdefault(result.endpoint, []).append(result)
            by_task.setdefault((result.row_id, result.address_type), {})[result.endpoint] = result

        accuracy_items = {
            (item.row_id, item.address_type, item.endpoint): item
            for item in self.accuracy_analyzer.analyze_run(run_id).items
        }

        summaries = [
            self._summarize_endpoint(endpoint, rows, baseline_endpoint, by_task, accuracy_items)
            for endpoint, rows in sorted(by_endpoint.items())
        ]

        address_details = [
            self._address_detail(task_key, endpoint_rows, baseline_endpoint, accuracy_items)
            for task_key, endpoint_rows in sorted(by_task.items())
        ]

        return BenchmarkReport(
            run_id=run_id,
            baseline_endpoint=baseline_endpoint,
            criteria=self.settings.criteria,
            endpoints=summaries,
            address_details=address_details,
        )

    def _summarize_endpoint(
        self,
        endpoint: str,
        rows: list[BenchmarkResult],
        baseline_endpoint: str,
        by_task: dict[tuple[int, str], dict[str, BenchmarkResult]],
        accuracy_items: dict[tuple[int, str, str], AccuracyItem],
    ) -> EndpointBenchmarkSummary:
        total = len(rows)
        success_count = sum(1 for row in rows if row.error is None and row.comparison_value is not None)
        error_count = total - success_count
        latencies = [row.latency_ms for row in rows if row.latency_ms is not None]
        avg_latency = sum(latencies) / len(latencies) if latencies else None

        ground_truth_matches = 0
        ground_truth_comparable = 0
        baseline_matches = 0
        baseline_comparable = 0
        faster_count = 0
        slower_count = 0

        for (row_id, address_type), endpoint_rows in by_task.items():
            accuracy = accuracy_items.get((row_id, address_type, endpoint))
            if accuracy and accuracy.matches is not None:
                ground_truth_comparable += 1
                if accuracy.matches:
                    ground_truth_matches += 1

            baseline = endpoint_rows.get(baseline_endpoint)
            current = endpoint_rows.get(endpoint)
            if baseline is None or current is None or endpoint == baseline_endpoint:
                continue

            if baseline.comparison_hash and current.comparison_hash:
                baseline_comparable += 1
                if baseline.comparison_hash == current.comparison_hash:
                    baseline_matches += 1

            if current.latency_ms is not None and baseline.latency_ms is not None:
                if current.latency_ms < baseline.latency_ms:
                    faster_count += 1
                elif current.latency_ms > baseline.latency_ms:
                    slower_count += 1

        return EndpointBenchmarkSummary(
            endpoint=endpoint,
            total_requests=total,
            success_count=success_count,
            error_count=error_count,
            success_rate=(success_count / total) if total else 0.0,
            avg_latency_ms=avg_latency,
            ground_truth_match_rate=(
                ground_truth_matches / ground_truth_comparable
                if ground_truth_comparable
                else None
            ),
            baseline_match_rate=(
                baseline_matches / baseline_comparable if baseline_comparable else None
            ),
            faster_than_baseline_count=faster_count if endpoint != baseline_endpoint else None,
            slower_than_baseline_count=slower_count if endpoint != baseline_endpoint else None,
        )

    def _address_detail(
        self,
        task_key: tuple[int, str],
        endpoint_rows: dict[str, BenchmarkResult],
        baseline_endpoint: str,
        accuracy_items: dict[tuple[int, str, str], AccuracyItem],
    ) -> dict[str, Any]:
        row_id, address_type = task_key
        address = next(iter(endpoint_rows.values())).address
        endpoint_results: dict[str, Any] = {}
        baseline = endpoint_rows.get(baseline_endpoint)

        for endpoint, row in endpoint_rows.items():
            accuracy = accuracy_items.get((row_id, address_type, endpoint))
            endpoint_results[endpoint] = {
                "response_code": row.response_code,
                "latency_ms": row.latency_ms,
                "coordinates": parse_json_text(row.coordinates),
                "building_csuid": row.building_csuid,
                "comparison_value": parse_json_text(row.comparison_value),
                "matches_ground_truth": accuracy.matches if accuracy else None,
                "matches_baseline": (
                    baseline is not None
                    and row.comparison_hash is not None
                    and baseline.comparison_hash == row.comparison_hash
                ),
                "error": row.error,
            }

        fastest_endpoint = min(
            (
                (endpoint, row.latency_ms)
                for endpoint, row in endpoint_rows.items()
                if row.latency_ms is not None
            ),
            key=lambda item: item[1],
            default=(None, None),
        )[0]

        return {
            "row_id": row_id,
            "address_type": address_type,
            "address": address,
            "fastest_endpoint": fastest_endpoint,
            "endpoints": endpoint_results,
        }


def routine_comparison_to_dict(comparison: RoutineComparison) -> dict[str, Any]:
    return {
        "current_run_id": comparison.current_run_id,
        "previous_run_id": comparison.previous_run_id,
        "endpoint": comparison.endpoint,
        "criteria": comparison.criteria,
        "has_differences": comparison.has_differences,
        "changed_count": comparison.changed_count,
        "addresses": [
            {
                "row_id": item.row_id,
                "address_type": item.address_type,
                "address": item.address,
                "status": item.status,
                "current_value": item.current_value,
                "previous_value": item.previous_value,
                "current_response_code": item.current_response_code,
                "previous_response_code": item.previous_response_code,
                "current_error": item.current_error,
                "previous_error": item.previous_error,
            }
            for item in comparison.addresses
        ],
    }


def match_status_comparison_to_dict(comparison: MatchStatusComparison) -> dict[str, Any]:
    return {
        "current_run_id": comparison.current_run_id,
        "previous_run_id": comparison.previous_run_id,
        "criteria": comparison.criteria,
        "coordinate_tolerance_meters": comparison.coordinate_tolerance_meters,
        "top_n": comparison.top_n,
        "has_differences": comparison.has_differences,
        "newly_matched_count": comparison.newly_matched_count,
        "lost_match_count": comparison.lost_match_count,
        "other_changed_count": comparison.other_changed_count,
        "differences": [
            {
                "row_id": item.row_id,
                "address_type": item.address_type,
                "address": item.address,
                "endpoint": item.endpoint,
                "change": item.change,
                "current_status": item.current_status,
                "previous_status": item.previous_status,
                "current_distance_m": item.current_distance_m,
                "previous_distance_m": item.previous_distance_m,
            }
            for item in comparison.differences
        ],
    }


def accuracy_report_to_dict(report: AccuracyReport) -> dict[str, Any]:
    return {
        "run_id": report.run_id,
        "run_type": report.run_type,
        "criteria": report.criteria,
        "coordinate_tolerance_meters": report.coordinate_tolerance_meters,
        "top_n": report.top_n,
        "endpoint": report.endpoint,
        "total": report.total,
        "matched": report.matched,
        "not_found": report.not_found,
        "mismatched": report.mismatched,
        "not_comparable": report.not_comparable,
        "match_rate": report.match_rate,
        "items": [
            {
                "row_id": item.row_id,
                "address_type": item.address_type,
                "address": item.address,
                "endpoint": item.endpoint,
                "status": item.status,
                "expected": item.expected,
                "actual": item.actual,
                "matches": item.matches,
                "distance_m": item.distance_m,
                "match_rank": item.match_rank,
                "error": item.error,
            }
            for item in report.items
            if item.matches is not True
        ],
    }


def benchmark_report_to_dict(report: BenchmarkReport) -> dict[str, Any]:
    return {
        "run_id": report.run_id,
        "baseline_endpoint": report.baseline_endpoint,
        "criteria": report.criteria,
        "endpoints": [
            {
                "endpoint": summary.endpoint,
                "total_requests": summary.total_requests,
                "success_count": summary.success_count,
                "error_count": summary.error_count,
                "success_rate": summary.success_rate,
                "avg_latency_ms": summary.avg_latency_ms,
                "ground_truth_match_rate": summary.ground_truth_match_rate,
                "baseline_match_rate": summary.baseline_match_rate,
                "faster_than_baseline_count": summary.faster_than_baseline_count,
                "slower_than_baseline_count": summary.slower_than_baseline_count,
            }
            for summary in report.endpoints
        ],
        "address_details": report.address_details,
    }
