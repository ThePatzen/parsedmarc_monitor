from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .classifier import match_known_source
from .models import (
    CountSummary,
    DeliveryDetail,
    DeliveryPage,
    KnownSourceRule,
    PersistResult,
    ProblemSource,
)

CURRENT_SCHEMA_VERSION = 3
Migration = Callable[[sqlite3.Connection], None]

_CURRENT_SCHEMA_SCRIPT = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    report_fingerprint TEXT NOT NULL UNIQUE,
    fingerprint_version INTEGER NOT NULL DEFAULT 2,
    org_name TEXT NOT NULL,
    report_id TEXT NOT NULL,
    policy_domain TEXT NOT NULL,
    begin_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    received_ts INTEGER NOT NULL,
    raw_schema TEXT,
    xml_namespace TEXT,
    org_email TEXT,
    org_extra_contact_info TEXT,
    generator TEXT,
    report_errors TEXT NOT NULL DEFAULT '[]',
    timespan_requires_normalization INTEGER NOT NULL DEFAULT 0,
    original_timespan_seconds INTEGER,
    policy_adkim TEXT,
    policy_aspf TEXT,
    policy_p TEXT,
    policy_sp TEXT,
    policy_pct TEXT,
    policy_fo TEXT,
    policy_np TEXT,
    policy_testing TEXT,
    policy_discovery_method TEXT,
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
    source_type TEXT,
    source_as_domain TEXT,
    interval_begin_ts INTEGER NOT NULL,
    interval_end_ts INTEGER NOT NULL,
    report_date TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    header_from TEXT NOT NULL,
    envelope_from TEXT,
    envelope_to TEXT,
    disposition TEXT,
    dkim_result TEXT,
    spf_result TEXT,
    dkim_aligned INTEGER NOT NULL,
    spf_aligned INTEGER NOT NULL,
    dmarc_pass INTEGER NOT NULL,
    policy_override_reasons TEXT NOT NULL DEFAULT '[]',
    dkim_auth_results TEXT NOT NULL DEFAULT '[]',
    spf_auth_results TEXT NOT NULL DEFAULT '[]',
    normalized_timespan INTEGER NOT NULL DEFAULT 0,
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
    """Mark legacy metadata fingerprints for one-time promotion."""
    connection.execute(
        "ALTER TABLE reports ADD COLUMN "
        "fingerprint_version INTEGER NOT NULL DEFAULT 1"
    )


def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
    """Store the additional normalized fields exposed by parsedmarc."""
    report_columns = (
        ("xml_namespace", "TEXT"),
        ("org_email", "TEXT"),
        ("org_extra_contact_info", "TEXT"),
        ("generator", "TEXT"),
        ("report_errors", "TEXT NOT NULL DEFAULT '[]'"),
        ("timespan_requires_normalization", "INTEGER NOT NULL DEFAULT 0"),
        ("original_timespan_seconds", "INTEGER"),
        ("policy_adkim", "TEXT"),
        ("policy_aspf", "TEXT"),
        ("policy_p", "TEXT"),
        ("policy_sp", "TEXT"),
        ("policy_pct", "TEXT"),
        ("policy_fo", "TEXT"),
        ("policy_np", "TEXT"),
        ("policy_testing", "TEXT"),
        ("policy_discovery_method", "TEXT"),
    )
    row_columns = (
        ("source_type", "TEXT"),
        ("source_as_domain", "TEXT"),
        ("envelope_to", "TEXT"),
        ("policy_override_reasons", "TEXT NOT NULL DEFAULT '[]'"),
        ("dkim_auth_results", "TEXT NOT NULL DEFAULT '[]'"),
        ("spf_auth_results", "TEXT NOT NULL DEFAULT '[]'"),
        ("normalized_timespan", "INTEGER NOT NULL DEFAULT 0"),
    )
    for column, definition in report_columns:
        connection.execute(f"ALTER TABLE reports ADD COLUMN {column} {definition}")
    for column, definition in row_columns:
        connection.execute(f"ALTER TABLE aggregate_rows ADD COLUMN {column} {definition}")


MIGRATIONS: dict[int, Migration] = {1: _migrate_v1_to_v2, 2: _migrate_v2_to_v3}


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
                table_names = {
                    str(row["name"])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                }
                if "meta" not in table_names:
                    if table_names:
                        raise RuntimeError(
                            "database meta table is missing from a nonempty schema"
                        )
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
    def _required_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{field_name} must be a mapping")
        return value

    @staticmethod
    def _required_string(mapping: Mapping[str, Any], field_name: str) -> str:
        value = mapping.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string")
        return value

    def _validated_report_identity(
        self, report: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], str, str, str, int, int]:
        metadata = self._required_mapping(report.get("report_metadata"), "report_metadata")
        policy = self._required_mapping(report.get("policy_published"), "policy_published")
        org_name = self._required_string(metadata, "org_name")
        report_id = self._required_string(metadata, "report_id")
        policy_domain = self._required_string(policy, "domain")
        begin_ts = self._timestamp(metadata.get("begin_date"))
        end_ts = self._timestamp(metadata.get("end_date"))
        if end_ts < begin_ts:
            raise ValueError("report end timestamp cannot be before begin timestamp")
        return metadata, policy, org_name, report_id, policy_domain, begin_ts, end_ts

    @staticmethod
    def _report_fingerprint(report: Mapping[str, Any]) -> str:
        canonical = json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _json_array(value: Any) -> str:
        if not isinstance(value, list):
            value = []
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _optional_integer(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _decode_json_array(value: Any) -> tuple[dict[str, Any], ...]:
        try:
            decoded = json.loads(value) if isinstance(value, str) else value
        except (TypeError, ValueError):
            return ()
        if not isinstance(decoded, list):
            return ()
        return tuple(item for item in decoded if isinstance(item, dict))

    @staticmethod
    def _decode_json_strings(value: Any) -> tuple[str, ...]:
        try:
            decoded = json.loads(value) if isinstance(value, str) else value
        except (TypeError, ValueError):
            return ()
        if not isinstance(decoded, list):
            return ()
        return tuple(item for item in decoded if isinstance(item, str))

    @staticmethod
    def _legacy_report_fingerprint(report: Mapping[str, Any]) -> str:
        metadata = report["report_metadata"]
        policy = report["policy_published"]
        identity = [
            metadata["org_name"],
            metadata["report_id"],
            policy["domain"],
            metadata["begin_date"],
            metadata["end_date"],
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
                (
                    metadata, policy, org_name, report_id, policy_domain, begin_ts, end_ts
                ) = self._validated_report_identity(report)
                fingerprint = self._report_fingerprint(report)
                existing = connection.execute(
                    "SELECT 1 FROM reports WHERE report_fingerprint = ?",
                    (fingerprint,),
                ).fetchone()
                if existing is not None:
                    continue

                legacy_fingerprint = self._legacy_report_fingerprint(report)
                promoted = connection.execute(
                    """
                    UPDATE reports
                    SET report_fingerprint = ?, fingerprint_version = 2
                    WHERE report_fingerprint = ? AND fingerprint_version = 1
                    """,
                    (fingerprint, legacy_fingerprint),
                )
                if promoted.rowcount:
                    continue

                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO reports(
                        report_fingerprint, fingerprint_version,
                        org_name, report_id, policy_domain,
                        begin_ts, end_ts, received_ts, raw_schema, created_at,
                        xml_namespace, org_email, org_extra_contact_info, generator,
                        report_errors, timespan_requires_normalization,
                        original_timespan_seconds, policy_adkim, policy_aspf,
                        policy_p, policy_sp, policy_pct, policy_fo, policy_np,
                        policy_testing, policy_discovery_method
                    ) VALUES (?, 2, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        self._optional_text(report.get("xml_namespace")),
                        self._optional_text(metadata.get("org_email")),
                        self._optional_text(metadata.get("org_extra_contact_info")),
                        self._optional_text(metadata.get("generator")),
                        self._json_array(metadata.get("errors")),
                        int(bool(metadata.get("timespan_requires_normalization", False))),
                        self._optional_integer(metadata.get("original_timespan_seconds")),
                        self._optional_text(policy.get("adkim")),
                        self._optional_text(policy.get("aspf")),
                        self._optional_text(policy.get("p")),
                        self._optional_text(policy.get("sp")),
                        self._optional_text(policy.get("pct")),
                        self._optional_text(policy.get("fo")),
                        self._optional_text(policy.get("np")),
                        self._optional_text(policy.get("testing")),
                        self._optional_text(policy.get("discovery_method")),
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
                    auth_results = record.get("auth_results") or {}
                    if not isinstance(auth_results, Mapping):
                        auth_results = {}
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
                            source_type, source_as_domain,
                            interval_begin_ts, interval_end_ts, report_date, message_count,
                            header_from, envelope_from, envelope_to, disposition,
                            dkim_result, spf_result, dkim_aligned, spf_aligned, dmarc_pass,
                            policy_override_reasons, dkim_auth_results, spf_auth_results,
                            normalized_timespan, known_source, known_source_name
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            self._optional_text(source.get("type")),
                            self._optional_text(source.get("as_domain")),
                            interval_begin_ts,
                            interval_end_ts,
                            report_date,
                            message_count,
                            str(identifiers.get("header_from") or ""),
                            self._optional_text(identifiers.get("envelope_from")),
                            self._optional_text(identifiers.get("envelope_to")),
                            policy_evaluated.get("disposition"),
                            policy_evaluated.get("dkim"),
                            policy_evaluated.get("spf"),
                            int(bool(alignment.get("dkim", False))),
                            int(bool(alignment.get("spf", False))),
                            int(dmarc_pass),
                            self._json_array(policy_evaluated.get("policy_override_reasons")),
                            self._json_array(auth_results.get("dkim")),
                            self._json_array(auth_results.get("spf")),
                            int(bool(record.get("normalized_timespan", False))),
                            int(known_source_name is not None),
                            known_source_name,
                        ),
                    )
                    rows_inserted += 1
            connection.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("last_successful_ingestion_ts", str(received_ts)),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        return PersistResult(reports_inserted=reports_inserted, rows_inserted=rows_inserted)

    def reclassify_sources(self, rules: Sequence[KnownSourceRule]) -> int:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT id, source_ip, source_reverse_dns, known_source, known_source_name "
                "FROM aggregate_rows"
            ).fetchall()
            changed = 0
            for row in rows:
                known_source_name = match_known_source(
                    str(row["source_ip"]), row["source_reverse_dns"], rules
                )
                known_source = int(known_source_name is not None)
                if (
                    known_source != int(row["known_source"])
                    or known_source_name != row["known_source_name"]
                ):
                    connection.execute(
                        "UPDATE aggregate_rows SET known_source = ?, known_source_name = ? WHERE id = ?",
                        (known_source, known_source_name, row["id"]),
                    )
                    changed += 1
            connection.commit()
            return changed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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

    def latest_report_end_ts(self) -> int | None:
        with self._connect() as connection:
            row = connection.execute("SELECT MAX(end_ts) AS end_ts FROM reports").fetchone()
        return int(row["end_ts"]) if row and row["end_ts"] is not None else None

    def last_successful_ingestion(self) -> str | None:
        value = self.get_meta("last_successful_ingestion_ts")
        if value is None:
            return None
        return datetime.fromtimestamp(int(value), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

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

    @staticmethod
    def _delivery_where(
        date_from: str, date_to: str, outcome: str, search: str
    ) -> tuple[str, tuple[Any, ...]]:
        try:
            start = date.fromisoformat(date_from)
            end = date.fromisoformat(date_to)
        except (TypeError, ValueError) as exc:
            raise ValueError("delivery dates must be ISO dates") from exc
        if end < start:
            raise ValueError("delivery date range is reversed")
        if outcome not in {"all", "passed", "failed"}:
            raise ValueError("delivery outcome must be all, passed, or failed")
        clauses = ["a.report_date >= ?", "a.report_date <= ?"]
        params: list[Any] = [start.isoformat(), end.isoformat()]
        if outcome != "all":
            clauses.append("a.dmarc_pass = ?")
            params.append(1 if outcome == "passed" else 0)
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append(
                "(a.source_ip LIKE ? ESCAPE '\\' COLLATE NOCASE OR "
                "a.source_reverse_dns LIKE ? ESCAPE '\\' COLLATE NOCASE OR "
                "a.known_source_name LIKE ? ESCAPE '\\' COLLATE NOCASE OR "
                "a.header_from LIKE ? ESCAPE '\\' COLLATE NOCASE OR "
                "a.envelope_from LIKE ? ESCAPE '\\' COLLATE NOCASE)"
            )
            params.extend([f"%{escaped}%"] * 5)
        return " AND ".join(clauses), tuple(params)

    def _delivery_detail(self, row: sqlite3.Row) -> DeliveryDetail:
        classification = ("known_" if row["known_source"] else "unknown_") + (
            "pass" if row["dmarc_pass"] else "fail"
        )

        def format_ts(value: object) -> str:
            return datetime.fromtimestamp(int(value), tz=UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

        return DeliveryDetail(
            id=int(row["id"]), report_date=str(row["report_date"]),
            interval_begin=format_ts(row["interval_begin_ts"]), interval_end=format_ts(row["interval_end_ts"]),
            reporting_org=str(row["org_name"]), report_id=str(row["report_id"]), policy_domain=str(row["policy_domain"]),
            source_ip=str(row["source_ip"]),
            source_reverse_dns=row["source_reverse_dns"],
            source_base_domain=row["source_base_domain"],
            source_name=row["source_name"],
            source_asn=int(row["source_asn"]) if row["source_asn"] is not None else None,
            source_as_name=row["source_as_name"],
            source_country=row["source_country"],
            known_source_name=row["known_source_name"],
            classification=classification,
            message_count=int(row["message_count"]),
            header_from=str(row["header_from"]),
            envelope_from=row["envelope_from"],
            disposition=row["disposition"],
            dkim_result=row["dkim_result"],
            spf_result=row["spf_result"],
            dkim_aligned=bool(row["dkim_aligned"]),
            spf_aligned=bool(row["spf_aligned"]),
            dmarc_pass=bool(row["dmarc_pass"]),
            report_org_email=row["org_email"],
            report_org_extra_contact_info=row["org_extra_contact_info"],
            report_generator=row["generator"],
            report_errors=self._decode_json_strings(row["report_errors"]),
            xml_schema=row["xml_schema"],
            xml_namespace=row["xml_namespace"],
            timespan_requires_normalization=bool(row["timespan_requires_normalization"]),
            original_timespan_seconds=(
                int(row["original_timespan_seconds"])
                if row["original_timespan_seconds"] is not None
                else None
            ),
            policy_adkim=row["policy_adkim"],
            policy_aspf=row["policy_aspf"],
            policy_p=row["policy_p"],
            policy_sp=row["policy_sp"],
            policy_pct=row["policy_pct"],
            policy_fo=row["policy_fo"],
            policy_np=row["policy_np"],
            policy_testing=row["policy_testing"],
            policy_discovery_method=row["policy_discovery_method"],
            source_type=row["source_type"],
            source_as_domain=row["source_as_domain"],
            envelope_to=row["envelope_to"],
            policy_override_reasons=self._decode_json_array(row["policy_override_reasons"]),
            dkim_auth_results=self._decode_json_array(row["dkim_auth_results"]),
            spf_auth_results=self._decode_json_array(row["spf_auth_results"]),
            normalized_timespan=bool(row["normalized_timespan"]),
        )

    def query_deliveries(
        self,
        date_from: str,
        date_to: str,
        outcome: str = "all",
        search: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> DeliveryPage:
        if page < 1:
            raise ValueError("delivery page must be positive")
        if not 1 <= page_size <= 100:
            raise ValueError("delivery page size must be between 1 and 100")
        where, params = self._delivery_where(date_from, date_to, outcome, search)
        base = "FROM aggregate_rows a JOIN reports r ON r.id = a.report_id_fk WHERE " + where
        with self._connect() as connection:
            total = int(connection.execute("SELECT COUNT(*) " + base, params).fetchone()[0])
            rows = connection.execute(
                "SELECT a.*, r.org_name, r.report_id, r.policy_domain, "
                "r.raw_schema AS xml_schema, "
                "r.xml_namespace, r.org_email, r.org_extra_contact_info, r.generator, "
                "r.report_errors, r.timespan_requires_normalization, "
                "r.original_timespan_seconds, r.policy_adkim, r.policy_aspf, "
                "r.policy_p, r.policy_sp, r.policy_pct, r.policy_fo, r.policy_np, "
                "r.policy_testing, r.policy_discovery_method " + base
                + " ORDER BY a.dmarc_pass ASC, a.interval_end_ts DESC, a.id DESC LIMIT ? OFFSET ?",
                params + (page_size, (page - 1) * page_size),
            ).fetchall()
        return DeliveryPage(tuple(self._delivery_detail(row) for row in rows), total, page, page_size)

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
