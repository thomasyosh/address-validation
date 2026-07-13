from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from address_validation.comparator import AccuracyAnalyzer, AccuracyItem
from address_validation.comparison_rules import ComparisonSettings
from address_validation.database import Database

ADDRESS_TYPE_LABELS = {
    "EADDRESS": "English",
    "CADDRESS": "Chinese",
}
ADDRESS_TYPE_ORDER = ("EADDRESS", "CADDRESS")


@dataclass
class SummaryRow:
    column_name: str
    number: int
    percentage: float
    address_type: str | None = None
    endpoint: str | None = None


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


def _address_type_label(address_type: str) -> str:
    return ADDRESS_TYPE_LABELS.get(address_type, address_type)


def _count_by_address_type(items: list[AccuracyItem]) -> dict[str, int]:
    counts = {address_type: 0 for address_type in ADDRESS_TYPE_ORDER}
    for item in items:
        counts[item.address_type] = counts.get(item.address_type, 0) + 1
    return counts


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
        type_totals = _count_by_address_type(items)

        rows = [
            SummaryRow(
                column_name="Number of Address",
                number=total_addresses,
                percentage=100.0,
            )
        ]

        for address_type in ADDRESS_TYPE_ORDER:
            count = type_totals.get(address_type, 0)
            if count == 0:
                continue
            rows.append(
                SummaryRow(
                    column_name=f"Number of {_address_type_label(address_type)} Address",
                    number=count,
                    percentage=(count / total_addresses * 100.0) if total_addresses else 0.0,
                    address_type=address_type,
                )
            )

        by_endpoint: dict[str, list[AccuracyItem]] = {}
        for item in items:
            by_endpoint.setdefault(item.endpoint, []).append(item)

        for endpoint_name, endpoint_items in by_endpoint.items():
            display_name = self.display_names.get(endpoint_name, endpoint_name)
            for address_type in ADDRESS_TYPE_ORDER:
                type_items = [item for item in endpoint_items if item.address_type == address_type]
                if not type_items:
                    continue
                type_total = type_totals.get(address_type, 0) or len(type_items)
                matched = sum(1 for item in type_items if item.status == "matched")
                percentage = (matched / type_total * 100.0) if type_total else 0.0
                rows.append(
                    SummaryRow(
                        column_name=f"{display_name} — {_address_type_label(address_type)}",
                        number=matched,
                        percentage=percentage,
                        address_type=address_type,
                        endpoint=endpoint_name,
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
                "address_type": row.address_type,
                "endpoint": row.endpoint,
            }
            for row in table.rows
        ],
    }


def match_summary_to_csv(table: MatchSummaryTable) -> str:
    lines = ["column_name,number,percentage,address_type,endpoint"]
    for row in table.rows:
        address_type = row.address_type or ""
        endpoint = row.endpoint or ""
        lines.append(
            f"{row.column_name},{row.number},{row.percentage:.2f}%,{address_type},{endpoint}"
        )
    return "\n".join(lines) + "\n"
