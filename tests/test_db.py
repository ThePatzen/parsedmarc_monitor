from __future__ import annotations

import json
import sqlite3
import stat
from copy import deepcopy
from datetime import UTC, datetime
from ipaddress import ip_network
from pathlib import Path

import pytest

from dmarc_monitor.db import Database
from dmarc_monitor.models import KnownSourceRule


def load_fixture() -> dict:
    path = Path(__file__).parent / "fixtures" / "aggregate_report.json"
    return json.loads(path.read_text(encoding="utf-8"))


def rules() -> tuple[KnownSourceRule, ...]:
    return (KnownSourceRule("Primary", ip_network("203.0.113.10/32"), None),)


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
