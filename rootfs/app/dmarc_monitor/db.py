from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .classifier import match_known_source
from .models import CountSummary, KnownSourceRule, PersistResult, ProblemSource

CURRENT_SCHEMA_VERSION = 2
Migration = Callable[[sqlite3.Connection], None]

_CURRENT_SCHEMA_SCRIPT = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    report_fingerprint TEXT NOT NULL UNIQUE,
    org_name TEXT NOT NULL,
    report_id TEXT NOT NULL,
    policy_domain TEXT NOT NULL,
    begin_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    received_ts INTEGER NOT NULL,
    raw_schema TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS aggregate_rows (
    id INTEGER PRIMARY KEY,
    report_id_fk INTEGER NOT NULL,
    source_ip TEXT NOT NULL,
    source_reverse_dns TEXT,
    source_base_domain TEXT,
    source_name TEXT,
    source_asn INTEGER,
    source_as_name TEXT,
    source_country TEXT,
    interval_begin_ts INTEGER NOT NULL,
    interval_end_ts INTEGER NOT NULL,
    report_date TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    header_from TEXT NOT NULL,
    envelope_from TEXT,
    disposition TEXT,
    dkim_result TEXT,
    spf_result TEXT,
    dkim_aligned INTEGER NOT NULL,
    spf_aligned INTEGER NOT NULL,
    dmarc_pass INTEGER NOT NULL,
    known_source INTEGER NOT NULL,
    known_source_name TEXT,
    FOREIGN KEY(report_id_fk) REFERENCES reports(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_reports_begin_end ON reports(begin_ts, end_ts);
CREATE INDEX IF NOT EXISTS idx_aggregate_rows_source_ip ON aggregate_rows(source_ip);
CREATE INDEX IF NOT EXISTS idx_aggregate_rows_header_from ON aggregate_rows(header_from);
CREATE INDEX IF NOT EXISTS idx_aggregate_rows_dmarc_pass ON aggregate_rows(dmarc_pass);
CREATE INDEX IF NOT EXISTS idx_aggregate_rows_known_source ON aggregate_rows(known_source);
CREATE INDEX IF NOT EXISTS idx_aggregate_rows_report_date ON aggregate_rows(report_date);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """Establish the migration framework and freshness metadata contract."""


MIGRATIONS: dict[int, Migration] = {1: _migrate_v1_to_v2}


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            try:
                connection.execute("BEGIN")
                meta_exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
                ).fetchone()
                if meta_exists is None:
                    self._create_current_schema(connection)
                    self._set_schema_version(connection, CURRENT_SCHEMA_VERSION)
                else:
                    schema_version = self._read_schema_version(connection)
                    while schema_version < CURRENT_SCHEMA_VERSION:
                        migration = MIGRATIONS.get(schema_version)
                        if migration is None:
                            raise RuntimeError(f"no migration found for schema version {schema_version}")
                        migration(connection)
                        schema_version += 1
                        self._set_schema_version(connection, schema_version)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        os.chmod(self.path, 0o600)

    @staticmethod
    def _create_current_schema(connection: sqlite3.Connection) -> None:
        for statement in _CURRENT_SCHEMA_SCRIPT.split(";"):
            if statement.strip():
                connection.execute(statement)

    @staticmethod
    def _read_schema_version(connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            raise RuntimeError("database schema version is missing")
        try:
            schema_version = int(row["value"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("database schema version is malformed") from exc
        if schema_version < 1:
            raise RuntimeError(f"unsupported database schema version {schema_version}")
        if schema_version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(f"database schema version {schema_version} is newer than supported")
        return schema_version

    @staticmethod
    def _set_schema_version(connection: sqlite3.Connection, schema_version: int) -> None:
        connection.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(schema_version),),
        )

    @staticmethod
    def _timestamp(value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("timestamp cannot be boolean")
        if isinstance(value, (int, float)):
            return int(value)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("timestamp must be a non-empty string or number")
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("invalid parsedmarc timestamp") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp())

    @staticmethod
    def _report_fingerprint(report: Mapping[str, Any]) -> str:
        metadata = report.get("report_metadata") or {}
        policy = report.get("policy_published") or {}
        identity = [
            metadata.get("org_name"),
            metadata.get("report_id"),
            policy.get("domain"),
            metadata.get("begin_date"),
            metadata.get("end_date"),
        ]
        canonical = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def persist_batch(
        self,
        batch: Mapping[str, Any],
        rules: Sequence[KnownSourceRule],
        received_at: datetime | None = None,
    ) -> PersistResult:
        received = received_at or datetime.now(UTC)
        if received.tzinfo is None:
            received = received.replace(tzinfo=UTC)
        received_ts = int(received.timestamp())
        reports_inserted = 0
        rows_inserted = 0

        connection = self._connect()
        try:
            connection.execute("BEGIN")
            for report in batch.get("aggregate_reports", []) or []:
                if not isinstance(report, Mapping):
                    raise ValueError("aggregate report must be a mapping")
                metadata = report.get("report_metadata") or {}
                policy = report.get("policy_published") or {}
                org_name = str(metadata.get("org_name") or "")
                report_id = str(metadata.get("report_id") or "")
                policy_domain = str(policy.get("domain") or "")
                begin_ts = self._timestamp(metadata.get("begin_date"))
                end_ts = self._timestamp(metadata.get("end_date"))
                fingerprint = self._report_fingerprint(report)
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO reports(
                        report_fingerprint, org_name, report_id, policy_domain,
                        begin_ts, end_ts, received_ts, raw_schema, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fingerprint,
                        org_name,
                        report_id,
                        policy_domain,
                        begin_ts,
                        end_ts,
                        received_ts,
                        str(report.get("xml_schema") or ""),
                        received_ts,
                    ),
                )
                if cursor.rowcount == 0:
                    continue
                report_fk = int(cursor.lastrowid)
                reports_inserted += 1

                for record in report.get("records", []) or []:
                    if not isinstance(record, Mapping):
                        raise ValueError("aggregate record must be a mapping")
                    source = record.get("source") or {}
                    alignment = record.get("alignment") or {}
                    policy_evaluated = record.get("policy_evaluated") or {}
                    identifiers = record.get("identifiers") or {}
                    source_ip = str(source.get("ip_address") or "")
                    reverse_dns_value = source.get("reverse_dns")
                    reverse_dns = str(reverse_dns_value) if reverse_dns_value else None
                    known_source_name = match_known_source(source_ip, reverse_dns, rules)
                    dmarc_pass = bool(alignment.get("dmarc", False))
                    interval_begin_ts = self._timestamp(record.get("interval_begin", metadata.get("begin_date")))
                    interval_end_ts = self._timestamp(record.get("interval_end", metadata.get("end_date")))
                    report_date = datetime.fromtimestamp(interval_begin_ts, tz=UTC).date().isoformat()
                    message_count = int(record.get("count") or 0)
                    if message_count < 0:
                        raise ValueError("aggregate record count cannot be negative")

                    connection.execute(
                        """
                        INSERT INTO aggregate_rows(
                            report_id_fk, source_ip, source_reverse_dns, source_base_domain,
                            source_name, source_asn, source_as_name, source_country,
                            interval_begin_ts, interval_end_ts, report_date, message_count,
                            header_from, envelope_from, disposition, dkim_result, spf_result,
                            dkim_aligned, spf_aligned, dmarc_pass, known_source, known_source_name
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            report_fk,
                            source_ip,
                            reverse_dns,
                            source.get("base_domain"),
                            source.get("name"),
                            source.get("asn"),
                            source.get("as_name"),
                            source.get("country"),
                            interval_begin_ts,
                            interval_end_ts,
                            report_date,
                            message_count,
                            str(identifiers.get("header_from") or ""),
                            identifiers.get("envelope_from"),
                            policy_evaluated.get("disposition"),
                            policy_evaluated.get("dkim"),
                            policy_evaluated.get("spf"),
                            int(bool(alignment.get("dkim", False))),
                            int(bool(alignment.get("spf", False))),
                            int(dmarc_pass),
                            int(known_source_name is not None),
                            known_source_name,
                        ),
                    )
                    rows_inserted += 1
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        return PersistResult(reports_inserted=reports_inserted, rows_inserted=rows_inserted)

    @staticmethod
    def _summary(row: sqlite3.Row | None) -> CountSummary:
        if row is None:
            return CountSummary(0, 0, 0, 0, 0, 0)
        return CountSummary(
            messages=int(row["messages"] or 0),
            passed=int(row["passed"] or 0),
            failed=int(row["failed"] or 0),
            known_fail=int(row["known_fail"] or 0),
            unknown_pass=int(row["unknown_pass"] or 0),
            unknown_fail=int(row["unknown_fail"] or 0),
        )

    def _counts(self, where: str, params: tuple[Any, ...]) -> CountSummary:
        sql = f"""
            SELECT
                COALESCE(SUM(message_count), 0) AS messages,
                COALESCE(SUM(CASE WHEN dmarc_pass = 1 THEN message_count ELSE 0 END), 0) AS passed,
                COALESCE(SUM(CASE WHEN dmarc_pass = 0 THEN message_count ELSE 0 END), 0) AS failed,
                COALESCE(SUM(CASE WHEN known_source = 1 AND dmarc_pass = 0 THEN message_count ELSE 0 END), 0) AS known_fail,
                COALESCE(SUM(CASE WHEN known_source = 0 AND dmarc_pass = 1 THEN message_count ELSE 0 END), 0) AS unknown_pass,
                COALESCE(SUM(CASE WHEN known_source = 0 AND dmarc_pass = 0 THEN message_count ELSE 0 END), 0) AS unknown_fail
            FROM aggregate_rows
            WHERE {where}
        """
        with self._connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return self._summary(row)

    def latest_report_date(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT MAX(report_date) AS report_date FROM aggregate_rows").fetchone()
        return row["report_date"] if row and row["report_date"] else None

    def counts_for_report_date(self, report_date: str) -> CountSummary:
        return self._counts("report_date = ?", (report_date,))

    def counts_since_report_date(self, cutoff_date: str) -> CountSummary:
        return self._counts("report_date >= ?", (cutoff_date,))

    def latest_report_metadata(self) -> tuple[str | None, str | None]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT received_ts, org_name FROM reports ORDER BY received_ts DESC, id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None, None
        received = datetime.fromtimestamp(int(row["received_ts"]), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return received, str(row["org_name"])

    def top_problem_sources(self, report_date: str, limit: int = 5) -> tuple[ProblemSource, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    source_ip,
                    source_reverse_dns,
                    known_source_name,
                    CASE
                        WHEN known_source = 1 AND dmarc_pass = 0 THEN 'known_fail'
                        WHEN known_source = 0 AND dmarc_pass = 1 THEN 'unknown_pass'
                    END AS classification,
                    SUM(message_count) AS message_count
                FROM aggregate_rows
                WHERE report_date = ?
                  AND ((known_source = 1 AND dmarc_pass = 0) OR (known_source = 0 AND dmarc_pass = 1))
                GROUP BY source_ip, source_reverse_dns, known_source_name, classification
                ORDER BY message_count DESC, source_ip ASC
                LIMIT ?
                """,
                (report_date, limit),
            ).fetchall()
        return tuple(
            ProblemSource(
                source_ip=str(row["source_ip"]),
                source_reverse_dns=row["source_reverse_dns"],
                known_source_name=row["known_source_name"],
                classification=str(row["classification"]),
                message_count=int(row["message_count"]),
            )
            for row in rows
        )

    def delete_reports_ending_before(self, cutoff_ts: int) -> int:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM reports WHERE end_ts < ?", (cutoff_ts,))
            return int(cursor.rowcount)

    def optimize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA optimize")
        finally:
            connection.close()

    def get_meta(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
