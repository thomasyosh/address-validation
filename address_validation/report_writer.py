from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any

from address_validation.comparator import (
    AccuracyAnalyzer,
    AccuracyReport,
    BenchmarkAnalyzer,
    MatchStatusComparison,
    RoutineComparator,
    accuracy_report_to_dict,
    benchmark_report_to_dict,
    match_status_comparison_to_dict,
    routine_comparison_to_dict,
)
from address_validation.comparison_rules import ComparisonSettings
from address_validation.config import get_benchmark_endpoints
from address_validation.database import Database, Run
from address_validation.summary import (
    MatchSummaryBuilder,
    MatchSummaryTable,
    get_endpoint_display_names,
    match_summary_to_csv,
    match_summary_to_dict,
)


@dataclass
class WrittenReport:
    run_id: int
    directory: Path
    files: list[Path]


def get_reports_directory(config: dict[str, Any]) -> Path:
    reports = config.get("reports") or {}
    return Path(reports.get("directory") or "results")


def reports_enabled(config: dict[str, Any]) -> bool:
    reports = config.get("reports") or {}
    return bool(reports.get("auto_write", True))


# Excel on Windows often misreads UTF-8 CSV without a BOM (Chinese shows as "?").
CSV_EXCEL_ENCODING = "utf-8-sig"


def write_csv_content(path: Path, content: str) -> Path:
    path.write_text(content, encoding=CSV_EXCEL_ENCODING)
    return path


class ReportWriter:
    """Write durable human/machine-readable reports after every run."""

    def __init__(
        self,
        database: Database,
        settings: ComparisonSettings,
        config: dict[str, Any],
    ) -> None:
        self.database = database
        self.settings = settings
        self.config = config
        self.root = get_reports_directory(config)
        self.display_names = get_endpoint_display_names(config)

    def write_run_report(
        self,
        run_id: int,
        *,
        compare_with_run_id: int | None = None,
        auto_compare_previous: bool = True,
    ) -> WrittenReport:
        run = self.database.get_run(run_id)
        if run is None:
            raise ValueError(f"Run {run_id} not found.")

        folder = self._run_folder(run)
        folder.mkdir(parents=True, exist_ok=True)
        files: list[Path] = []

        accuracy = AccuracyAnalyzer(self.database, self.settings).analyze_run(run_id)
        summary = MatchSummaryBuilder(
            self.database,
            self.settings,
            self.display_names,
        ).build(run_id)

        files.append(self._write_text(folder / "summary.txt", self._summary_text(summary, accuracy, run)))
        files.append(self._write_csv(folder / "summary.csv", match_summary_to_csv(summary)))
        files.append(
            self._write_json(
                folder / "summary.json",
                {
                    "run": self._run_meta(run),
                    "summary": match_summary_to_dict(summary),
                    "accuracy": {
                        "matched": accuracy.matched,
                        "not_found": accuracy.not_found,
                        "mismatched": accuracy.mismatched,
                        "not_comparable": accuracy.not_comparable,
                        "match_rate": accuracy.match_rate,
                        "coordinate_tolerance_meters": accuracy.coordinate_tolerance_meters,
                    },
                },
            )
        )
        files.append(self._write_json(folder / "accuracy.json", accuracy_report_to_dict(accuracy)))
        files.append(self._write_csv(folder / "mismatches.csv", self._mismatches_csv(accuracy)))

        if run.run_type == "benchmark":
            baseline_endpoint, _ = get_benchmark_endpoints(self.config)
            benchmark = BenchmarkAnalyzer(self.database, self.settings).analyze(
                run_id,
                baseline_endpoint,
            )
            files.append(
                self._write_json(folder / "benchmark.json", benchmark_report_to_dict(benchmark))
            )
            files.append(self._write_text(folder / "benchmark.txt", self._benchmark_text(benchmark)))

        previous_run_id = compare_with_run_id
        if previous_run_id is None and auto_compare_previous:
            previous_run_id = self.database.get_previous_run_id(run_id, run_type=run.run_type)

        if previous_run_id is not None:
            match_diff = RoutineComparator(self.database, self.settings).compare_match_status(
                run_id,
                previous_run_id,
            )
            files.append(
                self._write_json(
                    folder / "match_diff.json",
                    match_status_comparison_to_dict(match_diff),
                )
            )
            files.append(
                self._write_csv(folder / "match_diff.csv", self._match_diff_csv(match_diff))
            )
            files.append(
                self._write_text(folder / "match_diff.txt", self._match_diff_text(match_diff))
            )

            if run.run_type == "routine":
                value_diff = RoutineComparator(self.database, self.settings).compare_runs(
                    run_id,
                    previous_run_id,
                )
                files.append(
                    self._write_json(
                        folder / "value_diff.json",
                        routine_comparison_to_dict(value_diff),
                    )
                )

        readme = self._readme_text(run, folder, previous_run_id)
        files.append(self._write_text(folder / "README.txt", readme))

        latest_pointer = self.root / "LATEST.txt"
        latest_pointer.parent.mkdir(parents=True, exist_ok=True)
        latest_pointer.write_text(f"{folder.resolve()}\n", encoding="utf-8")
        files.append(latest_pointer)

        return WrittenReport(run_id=run_id, directory=folder, files=files)

    def _run_folder(self, run: Run) -> Path:
        stamp = self._safe_stamp(run.created_at)
        return self.root / f"run_{run.id:04d}_{stamp}_{run.run_type}"

    @staticmethod
    def _safe_stamp(created_at: str) -> str:
        try:
            parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        except ValueError:
            return created_at.replace(":", "").replace(" ", "_")[:20]

    @staticmethod
    def _run_meta(run: Run) -> dict[str, Any]:
        return {
            "id": run.id,
            "run_type": run.run_type,
            "created_at": run.created_at,
            "label": run.label,
            "notes": run.notes,
            "endpoint_name": run.endpoint_name,
            "dataset_path": run.dataset_path,
            "comparison_criteria": run.comparison_criteria,
            "status": run.status,
        }

    def _summary_text(
        self,
        summary: MatchSummaryTable,
        accuracy: AccuracyReport,
        run: Run,
    ) -> str:
        lines = [
            f"Run {run.id} [{run.run_type}] at {run.created_at}",
            f"Criteria: {summary.criteria}",
        ]
        if summary.tolerance_meters is not None:
            lines.append(f"Tolerance: {summary.tolerance_meters:g} metres")
        if summary.top_n is not None:
            lines.append(f"Top-N ranking window: {summary.top_n}")
        if run.endpoint_name:
            lines.append(f"Endpoint: {run.endpoint_name}")
        lines.append("")
        lines.append(f"{'column_name':<42} {'number':>10} {'percentage':>12}")
        lines.append("-" * 66)
        for row in summary.rows:
            lines.append(f"{row.column_name:<42} {row.number:>10} {row.percentage:>11.2f}%")
        lines.append("")
        if accuracy.match_rate is not None:
            lines.append(
                f"Accuracy: {accuracy.match_rate * 100:.1f}% "
                f"(matched={accuracy.matched}, not_found={accuracy.not_found}, "
                f"mismatched={accuracy.mismatched}, not_comparable={accuracy.not_comparable})"
            )
        lines.append("")
        lines.append("Open mismatches.csv for addresses outside the tolerance.")
        lines.append("Open match_diff.csv when a previous run was available for comparison.")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _mismatches_csv(report: AccuracyReport) -> str:
        buffer = StringIO()
        writer = csv.DictWriter(
            buffer,
            fieldnames=[
                "row_id",
                "address_type",
                "address",
                "endpoint",
                "status",
                "distance_m",
                "match_rank",
                "expected",
                "actual",
                "error",
            ],
        )
        writer.writeheader()
        for item in report.items:
            if item.matches is True:
                continue
            writer.writerow(
                {
                    "row_id": item.row_id,
                    "address_type": item.address_type,
                    "address": item.address,
                    "endpoint": item.endpoint,
                    "status": item.status,
                    "distance_m": (
                        f"{item.distance_m:.2f}" if item.distance_m is not None else ""
                    ),
                    "match_rank": item.match_rank if item.match_rank is not None else "",
                    "expected": json.dumps(item.expected, ensure_ascii=False),
                    "actual": json.dumps(item.actual, ensure_ascii=False),
                    "error": item.error or "",
                }
            )
        return buffer.getvalue()

    @staticmethod
    def _match_diff_csv(comparison: MatchStatusComparison) -> str:
        buffer = StringIO()
        writer = csv.DictWriter(
            buffer,
            fieldnames=[
                "change",
                "row_id",
                "address_type",
                "address",
                "endpoint",
                "current_status",
                "previous_status",
                "current_distance_m",
                "previous_distance_m",
            ],
        )
        writer.writeheader()
        for item in comparison.differences:
            writer.writerow(
                {
                    "change": item.change,
                    "row_id": item.row_id,
                    "address_type": item.address_type,
                    "address": item.address,
                    "endpoint": item.endpoint,
                    "current_status": item.current_status or "",
                    "previous_status": item.previous_status or "",
                    "current_distance_m": (
                        f"{item.current_distance_m:.2f}"
                        if item.current_distance_m is not None
                        else ""
                    ),
                    "previous_distance_m": (
                        f"{item.previous_distance_m:.2f}"
                        if item.previous_distance_m is not None
                        else ""
                    ),
                }
            )
        return buffer.getvalue()

    @staticmethod
    def _match_diff_text(comparison: MatchStatusComparison) -> str:
        lines = [
            (
                f"Match-status diff: run {comparison.current_run_id} vs "
                f"run {comparison.previous_run_id}"
            ),
            f"Criteria: {comparison.criteria}",
        ]
        if comparison.coordinate_tolerance_meters is not None:
            lines.append(f"Tolerance: {comparison.coordinate_tolerance_meters:g} metres")
        lines.append(
            f"newly_matched (within tolerance now, not before): {comparison.newly_matched_count}"
        )
        lines.append(
            f"lost_match (within tolerance before, not now): {comparison.lost_match_count}"
        )
        lines.append(f"other status changes: {comparison.other_changed_count}")
        lines.append("")
        for item in comparison.differences:
            lines.append(
                f"[{item.change}] row={item.row_id} {item.address_type} {item.address}"
            )
            lines.append(
                f"  previous={item.previous_status} -> current={item.current_status}"
            )
            if item.previous_distance_m is not None or item.current_distance_m is not None:
                lines.append(
                    f"  distance_m: previous={item.previous_distance_m} "
                    f"current={item.current_distance_m}"
                )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _benchmark_text(report: Any) -> str:
        lines = [
            f"Benchmark report for run {report.run_id}",
            f"Baseline endpoint: {report.baseline_endpoint}",
            f"Criteria: {report.criteria}",
            "",
            (
                f"{'Endpoint':<24} {'Success':>8} {'Avg ms':>10} "
                f"{'Truth match':>12} {'Base match':>12}"
            ),
            "-" * 70,
        ]
        for summary in report.endpoints:
            truth = (
                f"{summary.ground_truth_match_rate * 100:.1f}%"
                if summary.ground_truth_match_rate is not None
                else "n/a"
            )
            base = (
                f"{summary.baseline_match_rate * 100:.1f}%"
                if summary.baseline_match_rate is not None
                else "n/a"
            )
            avg = (
                f"{summary.avg_latency_ms:.1f}"
                if summary.avg_latency_ms is not None
                else "n/a"
            )
            lines.append(
                f"{summary.endpoint:<24} "
                f"{summary.success_rate * 100:>7.1f}% "
                f"{avg:>10} "
                f"{truth:>12} "
                f"{base:>12}"
            )
        return "\n".join(lines) + "\n"

    def _readme_text(
        self,
        run: Run,
        folder: Path,
        previous_run_id: int | None,
    ) -> str:
        lines = [
            "Address Search Validation — run report",
            "",
            f"Run ID: {run.id}",
            f"Type: {run.run_type}",
            f"Created: {run.created_at}",
            f"Folder: {folder.resolve()}",
            "",
            "Files:",
            "  summary.txt / summary.csv / summary.json  — match counts within tolerance",
            "  accuracy.json                             — full accuracy payload",
            "  mismatches.csv                            — addresses NOT within tolerance",
        ]
        if run.run_type == "benchmark":
            lines.append("  benchmark.txt / benchmark.json           — endpoint speed/accuracy")
        if previous_run_id is not None:
            lines.extend(
                [
                    f"  match_diff.*                             — vs run {previous_run_id}",
                    "      newly_matched = within tolerance now, not in previous run",
                    "      lost_match    = within tolerance previously, not now",
                ]
            )
        else:
            lines.append("  (no previous run available for match_diff)")
        lines.append("")
        lines.append("Find the newest report path in results/LATEST.txt")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _write_text(path: Path, content: str) -> Path:
        path.write_text(content, encoding="utf-8")
        return path

    @staticmethod
    def _write_csv(path: Path, content: str) -> Path:
        return write_csv_content(path, content)

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> Path:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path
