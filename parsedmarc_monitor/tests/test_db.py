from __future__ import annotations

import json
import sqlite3
import stat
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from ipaddress import ip_network
from pathlib import Path

import pytest

from dmarc_monitor.db import Database
from dmarc_monitor.models import KnownSourceRule


def test_query_deliveries_returns_paginated_delivery_details(tmp_path: Path) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    report = load_fixture()
    db.persist_batch({"aggregate_reports": [report]}, rules())

    result = db.query_deliveries("2026-08-29", "2026-08-29")

    assert result.total == 2
    assert len(result.items) == 2
    assert result.items[0].dmarc_pass is False
    assert result.items[0].reporting_org == "receiver.example"
    assert result.items[0].report_id == "report-2026-08-29"
    assert result.items[0].header_from == "example.at"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"outcome": "other"}, "outcome"),
        ({"page": 0}, "page"),
        ({"page_size": 101}, "page size"),
    ],
)
def test_query_deliveries_validates_pagination_and_outcome(tmp_path: Path, kwargs, message: str) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    with pytest.raises(ValueError, match=message):
        db.query_deliveries("2026-08-29", "2026-08-29", **kwargs)


def test_query_deliveries_supports_failed_pagination_and_literal_search(tmp_path: Path) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    db.persist_batch({"aggregate_reports": [load_fixture()]}, rules())

    page = db.query_deliveries(
        "2026-08-29", "2026-08-29", outcome="failed", search="EXAMPLE.AT", page=2, page_size=1
    )

    assert page.total == 1
    assert page.page == 2
    assert page.items == ()
    assert db.query_deliveries("2026-08-29", "2026-08-29", search="%", page_size=100).total == 0


def test_query_deliveries_includes_both_date_boundaries_and_excludes_outside(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    reports = [
        delivery_report("2026-08-23", "before", passed=True),
        delivery_report("2026-08-24", "from-boundary", passed=True),
        delivery_report("2026-08-30", "to-boundary", passed=False),
        delivery_report("2026-08-31", "after", passed=False),
    ]
    db.persist_batch({"aggregate_reports": reports}, rules())

    result = db.query_deliveries("2026-08-24", "2026-08-30")

    assert result.total == 2
    assert [item.report_id for item in result.items] == ["to-boundary", "from-boundary"]
    assert {item.report_date for item in result.items} == {"2026-08-24", "2026-08-30"}


def test_query_deliveries_filters_passed_and_failed_outcomes(tmp_path: Path) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    db.persist_batch(
        {
            "aggregate_reports": [
                delivery_report("2026-08-29", "passed", passed=True),
                delivery_report("2026-08-29", "failed", passed=False),
            ]
        },
        rules(),
    )

    passed = db.query_deliveries("2026-08-29", "2026-08-29", outcome="passed")
    failed = db.query_deliveries("2026-08-29", "2026-08-29", outcome="failed")

    assert passed.total == 1
    assert [item.report_id for item in passed.items] == ["passed"]
    assert all(item.dmarc_pass for item in passed.items)
    assert failed.total == 1
    assert [item.report_id for item in failed.items] == ["failed"]
    assert all(not item.dmarc_pass for item in failed.items)


def test_query_deliveries_orders_failures_then_interval_end_then_row_id(tmp_path: Path) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    db.persist_batch(
        {
            "aggregate_reports": [
                delivery_report(
                    "2026-08-29", "old", passed=False, interval_end="2026-08-29 10:00:00"
                ),
                delivery_report(
                    "2026-08-29", "tie-low", passed=False, interval_end="2026-08-29 12:00:00"
                ),
                delivery_report(
                    "2026-08-29", "new", passed=False, interval_end="2026-08-29 13:00:00"
                ),
                delivery_report(
                    "2026-08-29", "tie-high", passed=False, interval_end="2026-08-29 12:00:00"
                ),
                delivery_report(
                    "2026-08-29", "newer-passed", passed=True, interval_end="2026-08-29 14:00:00"
                ),
            ]
        },
        rules(),
    )

    result = db.query_deliveries("2026-08-29", "2026-08-29")

    assert [item.report_id for item in result.items] == [
        "new",
        "tie-high",
        "tie-low",
        "old",
        "newer-passed",
    ]
    assert result.items[1].id > result.items[2].id


def test_query_deliveries_total_is_independent_of_page_size_with_later_page(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    db.persist_batch(
        {
            "aggregate_reports": [
                delivery_report("2026-08-29", f"failed-{index}", passed=False)
                for index in range(3)
            ]
        },
        rules(),
    )

    first = db.query_deliveries("2026-08-29", "2026-08-29", page=1, page_size=2)
    later = db.query_deliveries("2026-08-29", "2026-08-29", page=2, page_size=2)
    unpaged = db.query_deliveries("2026-08-29", "2026-08-29", page_size=100)

    assert first.total == later.total == unpaged.total == 3
    assert len(first.items) == 2
    assert len(later.items) == 1
    assert len(unpaged.items) == 3


@pytest.mark.parametrize(
    ("date_from", "date_to", "message"),
    [
        ("not-a-date", "2026-08-30", "ISO dates"),
        ("2026-02-30", "2026-08-30", "ISO dates"),
        ("2026-08-31", "2026-08-30", "reversed"),
    ],
)
def test_query_deliveries_validates_malformed_and_reversed_dates_at_database_boundary(
    tmp_path: Path, date_from: str, date_to: str, message: str
) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()

    with pytest.raises(ValueError, match=message):
        db.query_deliveries(date_from, date_to)


def test_query_deliveries_maps_all_diagnostic_fields(tmp_path: Path) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    db.persist_batch({"aggregate_reports": [load_fixture()]}, rules())

    result = db.query_deliveries("2026-08-29", "2026-08-29")
    failed, passed = result.items

    assert asdict(failed) == {
        "id": failed.id,
        "report_date": "2026-08-29",
        "interval_begin": "2026-08-29T00:00:00Z",
        "interval_end": "2026-08-29T23:59:59Z",
        "reporting_org": "receiver.example",
        "report_id": "report-2026-08-29",
        "policy_domain": "example.at",
        "source_ip": "198.51.100.20",
        "source_reverse_dns": None,
        "source_base_domain": None,
        "source_name": None,
        "source_asn": 64501,
        "source_as_name": "Unknown Network",
        "source_country": "US",
        "known_source_name": None,
        "classification": "unknown_fail",
        "message_count": 3,
        "header_from": "example.at",
        "envelope_from": "spoof.invalid",
        "disposition": "reject",
        "dkim_result": "fail",
        "spf_result": "fail",
        "dkim_aligned": False,
        "spf_aligned": False,
        "dmarc_pass": False,
    }
    assert asdict(passed) == {
        "id": passed.id,
        "report_date": "2026-08-29",
        "interval_begin": "2026-08-29T00:00:00Z",
        "interval_end": "2026-08-29T23:59:59Z",
        "reporting_org": "receiver.example",
        "report_id": "report-2026-08-29",
        "policy_domain": "example.at",
        "source_ip": "203.0.113.10",
        "source_reverse_dns": "mail.example.at",
        "source_base_domain": "example.at",
        "source_name": "mail.example.at",
        "source_asn": 64500,
        "source_as_name": "Example Mail",
        "source_country": "AT",
        "known_source_name": "Primary",
        "classification": "known_pass",
        "message_count": 10,
        "header_from": "example.at",
        "envelope_from": "example.at",
        "disposition": "none",
        "dkim_result": "pass",
        "spf_result": "pass",
        "dkim_aligned": True,
        "spf_aligned": True,
        "dmarc_pass": True,
    }


@pytest.mark.parametrize(
    "field, value, expected",
    [
        ("source_ip", "198.51.100.20", 1),
        ("source_reverse_dns", "MAIL.EXAMPLE.AT", 1),
        ("known_source_name", "PRIMARY", 1),
        ("header_from", "EXAMPLE.AT", 2),
        ("envelope_from", "SPOOF.INVALID", 1),
    ],
)
def test_query_deliveries_search_is_case_insensitive_for_required_fields(
    tmp_path: Path, field: str, value: str, expected: int
) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    db.persist_batch({"aggregate_reports": [load_fixture()]}, rules())

    assert db.query_deliveries("2026-08-29", "2026-08-29", search=value).total == expected


@pytest.mark.parametrize("needle", ["%", "_", "\\"])
def test_query_deliveries_escapes_like_wildcards(tmp_path: Path, needle: str) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    db.persist_batch({"aggregate_reports": [load_fixture()]}, rules())
    with sqlite3.connect(db.path) as connection:
        connection.execute(
            "UPDATE aggregate_rows SET envelope_from = ? WHERE dmarc_pass = 0",
            (f"literal{needle}value",),
        )

    assert (
        db.query_deliveries("2026-08-29", "2026-08-29", search=needle).total == 1
    )


def load_fixture() -> dict:
    path = Path(__file__).parent / "fixtures" / "aggregate_report.json"
    return json.loads(path.read_text(encoding="utf-8"))


def rules() -> tuple[KnownSourceRule, ...]:
    return (KnownSourceRule("Primary", ip_network("203.0.113.10/32"), None),)


def delivery_report(
    report_date: str,
    report_id: str,
    *,
    passed: bool,
    interval_end: str | None = None,
) -> dict:
    report = deepcopy(load_fixture())
    interval_begin = f"{report_date} 00:00:00"
    interval_end = interval_end or f"{report_date} 23:59:59"
    report["report_metadata"].update(
        {
            "report_id": report_id,
            "begin_date": interval_begin,
            "end_date": interval_end,
        }
    )
    record = deepcopy(report["records"][0 if passed else 1])
    record["interval_begin"] = interval_begin
    record["interval_end"] = interval_end
    record["alignment"]["dmarc"] = passed
    report["records"] = [record]
    return report


def create_realistic_v1_database(path: Path) -> None:
    db = Database(path)
    db.initialize()
    db.persist_batch(
        {"aggregate_reports": [load_fixture()]},
        rules(),
        datetime(2026, 8, 30, tzinfo=UTC),
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE reports SET report_fingerprint = ?",
            ("1194ca84fa5bec05788e12fd1a9f50026378993b1fc16b7f1bc4ed060364ada2",),
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(reports)").fetchall()
        }
        if "fingerprint_version" in columns:
            connection.execute("ALTER TABLE reports DROP COLUMN fingerprint_version")
        connection.execute(
            "UPDATE meta SET value = '1' WHERE key = 'schema_version'"
        )
        connection.execute(
            "DELETE FROM meta WHERE key = 'last_successful_ingestion_ts'"
        )


def test_initialize_creates_schema_and_private_database(tmp_path: Path) -> None:
    path = tmp_path / "dmarc.sqlite3"
    db = Database(path)
    db.initialize()

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    assert db.get_meta("schema_version") == "2"
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"reports", "aggregate_rows", "meta"} <= tables


def test_initialize_rejects_nonempty_database_without_meta_table(tmp_path: Path) -> None:
    path = tmp_path / "dmarc.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated_data (value TEXT NOT NULL)")
        connection.execute("INSERT INTO unrelated_data(value) VALUES('preserve me')")

    with pytest.raises(RuntimeError, match="meta"):
        Database(path).initialize()

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        value = connection.execute("SELECT value FROM unrelated_data").fetchone()[0]
    assert tables == {"unrelated_data"}
    assert value == "preserve me"


def test_initialize_rejects_meta_table_without_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "dmarc.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO meta(key, value) VALUES('other', 'preserve me')"
        )

    with pytest.raises(RuntimeError, match="schema version is missing"):
        Database(path).initialize()

    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT key, value FROM meta").fetchall()
    assert rows == [("other", "preserve me")]


def test_migrated_v1_exact_redelivery_is_singular_but_changed_content_is_distinct(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dmarc.sqlite3"
    create_realistic_v1_database(path)
    db = Database(path)

    db.initialize()

    original = load_fixture()
    exact_redelivery = db.persist_batch({"aggregate_reports": [original]}, rules())
    changed = deepcopy(original)
    changed["records"][0]["count"] = 11
    changed_delivery = db.persist_batch({"aggregate_reports": [changed]}, rules())
    repeated_changed_delivery = db.persist_batch(
        {"aggregate_reports": [changed]}, rules()
    )

    assert db.get_meta("schema_version") == "2"
    assert exact_redelivery.reports_inserted == 0
    assert exact_redelivery.rows_inserted == 0
    assert changed_delivery.reports_inserted == 1
    assert changed_delivery.rows_inserted == 2
    assert repeated_changed_delivery.reports_inserted == 0
    assert repeated_changed_delivery.rows_inserted == 0
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 2
        assert (
            connection.execute("SELECT COUNT(*) FROM aggregate_rows").fetchone()[0]
            == 4
        )


def test_initialize_rejects_malformed_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "dmarc.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO meta(key, value) VALUES('schema_version', 'abc');
            """
        )

    with pytest.raises(RuntimeError):
        Database(path).initialize()


def test_initialize_rejects_future_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "dmarc.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO meta(key, value) VALUES('schema_version', '999');
            """
        )

    with pytest.raises(RuntimeError):
        Database(path).initialize()


def test_persist_batch_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    batch = {"aggregate_reports": [load_fixture()]}

    first = db.persist_batch(batch, rules(), datetime(2026, 8, 31, tzinfo=UTC))
    second = db.persist_batch(batch, rules(), datetime(2026, 8, 31, tzinfo=UTC))

    assert first.reports_inserted == 1
    assert first.rows_inserted == 2
    assert second.reports_inserted == 0
    assert second.rows_inserted == 0
    counts = db.counts_for_report_date("2026-08-29")
    assert counts.messages == 13
    assert counts.passed == 10
    assert counts.failed == 3
    assert counts.known_fail == 0
    assert counts.unknown_pass == 0
    assert counts.unknown_fail == 3


def test_persist_batch_records_successful_ingestion_for_new_and_duplicate_batches(tmp_path: Path) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    batch = {"aggregate_reports": [load_fixture()]}

    db.persist_batch(batch, rules(), datetime(2026, 8, 31, 6, tzinfo=UTC))

    assert db.last_successful_ingestion() == "2026-08-31T06:00:00Z"
    assert db.latest_report_end_ts() == int(datetime(2026, 8, 29, 23, 59, 59, tzinfo=UTC).timestamp())

    db.persist_batch(batch, rules(), datetime(2026, 8, 31, 9, tzinfo=UTC))

    assert db.last_successful_ingestion() == "2026-08-31T09:00:00Z"


def test_persist_batch_rolls_back_ingestion_freshness_for_invalid_batch(tmp_path: Path) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    db.persist_batch(
        {"aggregate_reports": [load_fixture()]}, rules(), datetime(2026, 8, 31, 6, tzinfo=UTC)
    )
    invalid_report = load_fixture()
    invalid_report["report_metadata"] = "invalid"

    with pytest.raises(ValueError):
        db.persist_batch(
            {"aggregate_reports": [load_fixture(), invalid_report]},
            rules(),
            datetime(2026, 8, 31, 9, tzinfo=UTC),
        )

    assert db.last_successful_ingestion() == "2026-08-31T06:00:00Z"


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("missing metadata", lambda report: report.pop("report_metadata")),
        ("non-mapping metadata", lambda report: report.__setitem__("report_metadata", "invalid")),
        ("missing policy", lambda report: report.pop("policy_published")),
        ("non-mapping policy", lambda report: report.__setitem__("policy_published", "invalid")),
        ("empty organization", lambda report: report["report_metadata"].__setitem__("org_name", "")),
        ("empty report id", lambda report: report["report_metadata"].__setitem__("report_id", "")),
        ("empty policy domain", lambda report: report["policy_published"].__setitem__("domain", "")),
        ("invalid begin", lambda report: report["report_metadata"].__setitem__("begin_date", "invalid")),
        ("invalid end", lambda report: report["report_metadata"].__setitem__("end_date", "invalid")),
        (
            "end before begin",
            lambda report: report["report_metadata"].update(
                {"begin_date": "2026-08-30 00:00:00", "end_date": "2026-08-29 00:00:00"}
            ),
        ),
    ],
)
def test_persist_batch_rejects_invalid_report_identity_without_partial_insert(
    tmp_path: Path, case: str, mutate
) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    valid_report = load_fixture()
    invalid_report = deepcopy(valid_report)
    invalid_report["report_metadata"]["report_id"] = f"invalid-{case}"
    mutate(invalid_report)

    with pytest.raises(ValueError):
        db.persist_batch({"aggregate_reports": [valid_report, invalid_report]}, rules())

    with sqlite3.connect(db.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0


def test_persist_batch_uses_complete_report_content_for_identity(tmp_path: Path) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    original = load_fixture()
    changed_records = deepcopy(original)
    changed_records["records"][0]["count"] = 11
    batch = {"aggregate_reports": [original, changed_records]}

    first = db.persist_batch(batch, rules())
    redelivery = db.persist_batch(batch, rules())

    assert first.reports_inserted == 2
    assert first.rows_inserted == 4
    assert redelivery.reports_inserted == 0
    assert redelivery.rows_inserted == 0


def test_persist_uses_parsedmarc_dmarc_alignment_directly(tmp_path: Path) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    report = deepcopy(load_fixture())
    report["report_metadata"]["report_id"] = "direct-dmarc-alignment"
    record = report["records"][1]
    record["alignment"] = {"spf": False, "dkim": False, "dmarc": True}

    db.persist_batch({"aggregate_reports": [report]}, rules())

    counts = db.counts_for_report_date("2026-08-29")
    assert counts.passed == 13
    assert counts.failed == 0
    assert counts.unknown_pass == 3


def test_persists_known_source_identity_and_problem_sources(tmp_path: Path) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    report = deepcopy(load_fixture())
    report["report_metadata"]["report_id"] = "known-fail"
    report["records"][0]["alignment"]["dmarc"] = False

    db.persist_batch({"aggregate_reports": [report]}, rules())

    counts = db.counts_for_report_date("2026-08-29")
    assert counts.known_fail == 10
    problems = db.top_problem_sources("2026-08-29")
    assert problems[0].source_ip == "203.0.113.10"
    assert problems[0].known_source_name == "Primary"
    assert problems[0].classification == "known_fail"
    assert problems[0].message_count == 10


def test_query_metadata_counts_since_and_delete(tmp_path: Path) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    report = load_fixture()
    db.persist_batch({"aggregate_reports": [report]}, rules(), datetime(2026, 8, 31, 6, tzinfo=UTC))

    assert db.latest_report_date() == "2026-08-29"
    assert db.latest_report_metadata() == ("2026-08-31T06:00:00Z", "receiver.example")
    assert db.counts_since_report_date("2026-08-01").messages == 13
    assert db.counts_since_report_date("2026-08-30").messages == 0
    assert db.delete_reports_ending_before(int(datetime(2026, 8, 30, tzinfo=UTC).timestamp())) == 1
    assert db.latest_report_date() is None


def test_meta_round_trip_and_optimize(tmp_path: Path) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    assert db.get_meta("missing") is None
    db.set_meta("example", "value")
    assert db.get_meta("example") == "value"
    db.optimize()


def test_reclassify_sources_updates_stored_classifications_idempotently(tmp_path: Path) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    db.persist_batch({"aggregate_reports": [load_fixture()]}, ())

    assert db.counts_for_report_date("2026-08-29").unknown_pass == 10

    changed = db.reclassify_sources(rules())

    assert changed == 1
    assert db.counts_for_report_date("2026-08-29").unknown_pass == 0
    with sqlite3.connect(db.path) as connection:
        known_pass = connection.execute(
            "SELECT message_count FROM aggregate_rows WHERE known_source = 1 AND dmarc_pass = 1"
        ).fetchone()
        known_name = connection.execute(
            "SELECT known_source_name FROM aggregate_rows WHERE known_source = 1 AND dmarc_pass = 1"
        ).fetchone()
    assert known_pass[0] == 10
    assert known_name[0] == "Primary"
    assert db.reclassify_sources(rules()) == 0
