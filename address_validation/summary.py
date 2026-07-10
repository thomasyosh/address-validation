from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from address_validation.comparator import AccuracyAnalyzer, AccuracyItem
from address_validation.comparison_rules import ComparisonSettings
from address_validation.database import Database


@dataclass
class SummaryRow:
    column_name: str
    number: int
    percentage: float


@dataclass
class MatchSummaryTable:
    run_id: int
    criteria: str
    tolerance_meters: float | None
    rows: list[SummaryRow]
    top_n: int | None = None

    @property
    def total_addresses(self) -> int:
        return self.rows[0].number if self.rows else 0


def get_endpoint_display_names(config: dict[str, Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for endpoint in config.get("endpoints") or []:
        name = endpoint.get("name")
        if not name:
            continue
        names[name] = endpoint.get("display_name") or name
    return names


class MatchSummaryBuilder:
    def __init__(
        self,
        database: Database,
        settings: ComparisonSettings,
        display_names: dict[str, str] | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.display_names = display_names or {}
        self.accuracy_analyzer = AccuracyAnalyzer(database, settings)

    def build(self, run_id: int) -> MatchSummaryTable:
        report = self.accuracy_analyzer.analyze_run(run_id)
        items = report.items

        address_keys = {(item.row_id, item.address_type) for item in items}
        total_addresses = len(address_keys) if address_keys else report.total

        rows = [
            SummaryRow(
                column_name="Number of Address",
                number=total_addresses,
                percentage=100.0,
            )
        ]

        by_endpoint: dict[str, list[AccuracyItem]] = {}
        for item in items:
            by_endpoint.setdefault(item.endpoint, []).append(item)

        for endpoint_name, endpoint_items in by_endpoint.items():
            matched = sum(1 for item in endpoint_items if item.status == "matched")
            percentage = (matched / total_addresses * 100.0) if total_addresses else 0.0
            rows.append(
                SummaryRow(
                    column_name=self.display_names.get(endpoint_name, endpoint_name),
                    number=matched,
                    percentage=percentage,
                )
            )

        return MatchSummaryTable(
            run_id=run_id,
            criteria=self.settings.criteria,
            tolerance_meters=(
                self.settings.coordinate_tolerance
                if self.settings.criteria == "coordinates"
                else None
            ),
            rows=rows,
            top_n=self.settings.top_n,
        )


def match_summary_to_dict(table: MatchSummaryTable) -> dict[str, Any]:
    return {
        "run_id": table.run_id,
        "criteria": table.criteria,
        "coordinate_tolerance_meters": table.tolerance_meters,
        "top_n": table.top_n,
        "rows": [
            {
                "column_name": row.column_name,
                "number": row.number,
                "percentage": round(row.percentage, 2),
            }
            for row in table.rows
        ],
    }


def match_summary_to_csv(table: MatchSummaryTable) -> str:
    lines = ["column_name,number,percentage"]
    for row in table.rows:
        lines.append(f"{row.column_name},{row.number},{row.percentage:.2f}%")
    return "\n".join(lines) + "\n"
