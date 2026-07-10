from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

RunType = Literal["routine", "benchmark"]
RunStatus = Literal["in_progress", "completed", "interrupted"]
AddressType = Literal["EADDRESS", "CADDRESS"]


@dataclass
class Run:
    id: int
    run_type: RunType
    created_at: str
    label: str | None
    notes: str | None
    endpoint_name: str | None
    dataset_path: str | None
    comparison_criteria: str | None
    status: str = "completed"
    completed_at: str | None = None


@dataclass
class ValidationResult:
    id: int
    run_id: int
    row_id: int
    address_type: str
    address: str
    endpoint: str
    coordinates: str | None
    building_csuid: str | None
    comparison_value: str | None
    comparison_hash: str | None
    response_code: int | None
    expected_easting: float | None
    expected_northing: float | None
    expected_building_csuid: str | None
    chinese_address: bool
    error: str | None
    created_at: str
    updated_at: str


@dataclass
class BenchmarkResult(ValidationResult):
    latency_ms: float | None = None


class Database:
    SCHEMA_VERSION = 3

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=60)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self.connect() as connection:
            current_version = connection.execute("PRAGMA user_version").fetchone()[0]
            # Only wipe truly legacy schemas (pre-validation_results design).
            if current_version and current_version < 2:
                connection.executescript(
                    """
                    DROP TABLE IF EXISTS fetch_results;
                    DROP TABLE IF EXISTS validation_results;
                    DROP TABLE IF EXISTS benchmark_results;
                    DROP TABLE IF EXISTS runs;
                    """
                )

            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    label TEXT,
                    notes TEXT,
                    endpoint_name TEXT,
                    dataset_path TEXT,
                    comparison_criteria TEXT,
                    status TEXT NOT NULL DEFAULT 'in_progress',
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS validation_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    row_id INTEGER NOT NULL,
                    address_type TEXT NOT NULL,
                    address TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    coordinates TEXT,
                    building_csuid TEXT,
                    comparison_value TEXT,
                    comparison_hash TEXT,
                    response_code INTEGER,
                    expected_easting REAL,
                    expected_northing REAL,
                    expected_building_csuid TEXT,
                    chinese_address INTEGER NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
                    UNIQUE (run_id, row_id, address_type)
                );

                CREATE TABLE IF NOT EXISTS benchmark_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    row_id INTEGER NOT NULL,
                    address_type TEXT NOT NULL,
                    address TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    coordinates TEXT,
                    building_csuid TEXT,
                    comparison_value TEXT,
                    comparison_hash TEXT,
                    response_code INTEGER,
                    expected_easting REAL,
                    expected_northing REAL,
                    expected_building_csuid TEXT,
                    chinese_address INTEGER NOT NULL,
                    latency_ms REAL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
                    UNIQUE (run_id, row_id, address_type, endpoint)
                );

                CREATE INDEX IF NOT EXISTS idx_validation_results_run_id
                    ON validation_results(run_id);
                CREATE INDEX IF NOT EXISTS idx_validation_results_row_id
                    ON validation_results(row_id);
                CREATE INDEX IF NOT EXISTS idx_benchmark_results_run_id
                    ON benchmark_results(run_id);
                CREATE INDEX IF NOT EXISTS idx_benchmark_results_endpoint
                    ON benchmark_results(endpoint);
                CREATE INDEX IF NOT EXISTS idx_runs_status
                    ON runs(status);
                """
            )
            self._ensure_column(connection, "runs", "status", "TEXT NOT NULL DEFAULT 'in_progress'")
            self._ensure_column(connection, "runs", "completed_at", "TEXT")
            connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        existing = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def create_run(
        self,
        run_type: RunType,
        *,
        label: str | None = None,
        notes: str | None = None,
        endpoint_name: str | None = None,
        dataset_path: str | None = None,
        comparison_criteria: str | None = None,
    ) -> int:
        created_at = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO runs (
                    run_type, created_at, label, notes, endpoint_name,
                    dataset_path, comparison_criteria, status, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'in_progress', NULL)
                """,
                (
                    run_type,
                    created_at,
                    label,
                    notes,
                    endpoint_name,
                    dataset_path,
                    comparison_criteria,
                ),
            )
            return int(cursor.lastrowid)

    def mark_run_status(self, run_id: int, status: RunStatus) -> None:
        completed_at = utc_now() if status == "completed" else None
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = ?, completed_at = COALESCE(?, completed_at)
                WHERE id = ?
                """,
                (status, completed_at, run_id),
            )

    def save_validation_result(self, run_id: int, **fields: Any) -> int:
        return self._save_result(
            "validation_results",
            run_id,
            fields,
            conflict_columns=("run_id", "row_id", "address_type"),
        )

    def save_benchmark_result(self, run_id: int, **fields: Any) -> int:
        return self._save_result(
            "benchmark_results",
            run_id,
            fields,
            conflict_columns=("run_id", "row_id", "address_type", "endpoint"),
        )

    def save_validation_results_batch(self, run_id: int, rows: list[dict[str, Any]]) -> None:
        self._save_results_batch(
            "validation_results",
            run_id,
            rows,
            conflict_columns=("run_id", "row_id", "address_type"),
        )

    def save_benchmark_results_batch(self, run_id: int, rows: list[dict[str, Any]]) -> None:
        self._save_results_batch(
            "benchmark_results",
            run_id,
            rows,
            conflict_columns=("run_id", "row_id", "address_type", "endpoint"),
        )

    def _build_payload(self, run_id: int, fields: dict[str, Any], *, benchmark: bool) -> dict[str, Any]:
        now = utc_now()
        payload = {
            "run_id": run_id,
            "row_id": fields["row_id"],
            "address_type": fields["address_type"],
            "address": fields["address"],
            "endpoint": fields["endpoint"],
            "coordinates": fields.get("coordinates"),
            "building_csuid": fields.get("building_csuid"),
            "comparison_value": fields.get("comparison_value"),
            "comparison_hash": hash_text(fields.get("comparison_value")),
            "response_code": fields.get("response_code"),
            "expected_easting": fields.get("expected_easting"),
            "expected_northing": fields.get("expected_northing"),
            "expected_building_csuid": fields.get("expected_building_csuid"),
            "chinese_address": int(fields.get("chinese_address", False)),
            "error": fields.get("error"),
            "created_at": now,
            "updated_at": now,
        }
        if benchmark:
            payload["latency_ms"] = fields.get("latency_ms")
        return payload

    def _save_result(
        self,
        table_name: str,
        run_id: int,
        fields: dict[str, Any],
        conflict_columns: tuple[str, ...],
    ) -> int:
        benchmark = table_name == "benchmark_results"
        payload = self._build_payload(run_id, fields, benchmark=benchmark)
        columns = list(payload.keys())
        placeholders = ", ".join("?" for _ in columns)
        update_clause = ", ".join(
            f"{column} = excluded.{column}"
            for column in columns
            if column not in {"created_at", *conflict_columns}
        )
        conflict = ", ".join(conflict_columns)

        with self.connect() as connection:
            cursor = connection.execute(
                f"""
                INSERT INTO {table_name} ({", ".join(columns)})
                VALUES ({placeholders})
                ON CONFLICT({conflict}) DO UPDATE SET
                    {update_clause}
                """,
                tuple(payload[column] for column in columns),
            )
            return int(cursor.lastrowid)

    def _save_results_batch(
        self,
        table_name: str,
        run_id: int,
        rows: list[dict[str, Any]],
        conflict_columns: tuple[str, ...],
    ) -> None:
        if not rows:
            return
        benchmark = table_name == "benchmark_results"
        payloads = [self._build_payload(run_id, row, benchmark=benchmark) for row in rows]
        columns = list(payloads[0].keys())
        placeholders = ", ".join("?" for _ in columns)
        update_clause = ", ".join(
            f"{column} = excluded.{column}"
            for column in columns
            if column not in {"created_at", *conflict_columns}
        )
        conflict = ", ".join(conflict_columns)
        values = [tuple(payload[column] for column in columns) for payload in payloads]

        with self.connect() as connection:
            connection.executemany(
                f"""
                INSERT INTO {table_name} ({", ".join(columns)})
                VALUES ({placeholders})
                ON CONFLICT({conflict}) DO UPDATE SET
                    {update_clause}
                """,
                values,
            )

    def list_runs(self, run_type: RunType | None = None, limit: int = 50) -> list[Run]:
        query = """
            SELECT
                id, run_type, created_at, label, notes,
                endpoint_name, dataset_path, comparison_criteria,
                COALESCE(status, 'completed') AS status,
                completed_at
            FROM runs
        """
        params: list[Any] = []
        if run_type:
            query += " WHERE run_type = ?"
            params.append(run_type)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [Run(**dict(row)) for row in rows]

    def get_run(self, run_id: int) -> Run | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    id, run_type, created_at, label, notes,
                    endpoint_name, dataset_path, comparison_criteria,
                    COALESCE(status, 'completed') AS status,
                    completed_at
                FROM runs WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
        return Run(**dict(row)) if row else None

    def get_latest_incomplete_run_id(self, run_type: RunType) -> int | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM runs
                WHERE run_type = ? AND status IN ('in_progress', 'interrupted')
                ORDER BY id DESC
                LIMIT 1
                """,
                (run_type,),
            ).fetchone()
        return int(row["id"]) if row else None

    def get_previous_run_id(self, run_id: int, run_type: RunType | None = None) -> int | None:
        run = self.get_run(run_id)
        if run is None:
            return None

        query = "SELECT id FROM runs WHERE id < ?"
        params: list[Any] = [run_id]
        if run_type:
            query += " AND run_type = ?"
            params.append(run_type)
        if run.endpoint_name:
            query += " AND endpoint_name = ?"
            params.append(run.endpoint_name)
        if run.comparison_criteria:
            query += " AND comparison_criteria = ?"
            params.append(run.comparison_criteria)
        query += " AND COALESCE(status, 'completed') = 'completed'"
        query += " ORDER BY id DESC LIMIT 1"

        with self.connect() as connection:
            row = connection.execute(query, params).fetchone()
        return int(row["id"]) if row else None

    def get_latest_run_id(self, run_type: RunType | None = None) -> int | None:
        query = "SELECT id FROM runs"
        params: list[Any] = []
        if run_type:
            query += " WHERE run_type = ?"
            params.append(run_type)
        query += " ORDER BY id DESC LIMIT 1"

        with self.connect() as connection:
            row = connection.execute(query, params).fetchone()
        return int(row["id"]) if row else None

    def get_saved_validation_keys(
        self,
        run_id: int,
        *,
        successful_only: bool = True,
    ) -> set[tuple[int, str]]:
        query = """
            SELECT row_id, address_type
            FROM validation_results
            WHERE run_id = ?
        """
        if successful_only:
            query += " AND error IS NULL"
        with self.connect() as connection:
            rows = connection.execute(query, (run_id,)).fetchall()
        return {(int(row["row_id"]), row["address_type"]) for row in rows}

    def get_saved_benchmark_keys(
        self,
        run_id: int,
        *,
        successful_only: bool = True,
    ) -> set[tuple[int, str, str]]:
        query = """
            SELECT row_id, address_type, endpoint
            FROM benchmark_results
            WHERE run_id = ?
        """
        if successful_only:
            query += " AND error IS NULL"
        with self.connect() as connection:
            rows = connection.execute(query, (run_id,)).fetchall()
        return {
            (int(row["row_id"]), row["address_type"], row["endpoint"])
            for row in rows
        }

    def count_results(self, run_id: int, run_type: RunType) -> int:
        table = "validation_results" if run_type == "routine" else "benchmark_results"
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM {table} WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["count"])

    def get_validation_results(self, run_id: int) -> list[ValidationResult]:
        return self._get_results("validation_results", run_id)

    def get_benchmark_results(self, run_id: int) -> list[BenchmarkResult]:
        return self._get_results("benchmark_results", run_id, benchmark=True)

    def _get_results(
        self,
        table_name: str,
        run_id: int,
        *,
        benchmark: bool = False,
    ) -> list[Any]:
        latency_column = ", latency_ms" if benchmark else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    id, run_id, row_id, address_type, address, endpoint,
                    coordinates, building_csuid, comparison_value, comparison_hash,
                    response_code, expected_easting, expected_northing,
                    expected_building_csuid, chinese_address, error,
                    created_at, updated_at{latency_column}
                FROM {table_name}
                WHERE run_id = ?
                ORDER BY row_id, address_type, endpoint
                """,
                (run_id,),
            ).fetchall()

        results: list[Any] = []
        for row in rows:
            data = dict(row)
            data["chinese_address"] = bool(data["chinese_address"])
            if benchmark:
                results.append(BenchmarkResult(**data))
            else:
                results.append(ValidationResult(**data))
        return results


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_json_text(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
