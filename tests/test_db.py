from __future__ import annotations

import json
import sqlite3
import stat
from copy import deepcopy
from datetime import UTC, datetime
from ipaddress import ip_network
from pathlib import Path

from dmarc_monitor.db import Database
from dmarc_monitor.models import KnownSourceRule


def load_fixture() -> dict:
    path = Path(__file__).parent / "fixtures" / "aggregate_report.json"
    return json.loads(path.read_text(encoding="utf-8"))


def rules() -> tuple[KnownSourceRule, ...]:
    return (KnownSourceRule("Primary", ip_network("203.0.113.10/32"), None),)


def test_initialize_creates_schema_and_private_database(tmp_path: Path) -> None:
    path = tmp_path / "dmarc.sqlite3"
    db = Database(path)
    db.initialize()

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    assert db.get_meta("schema_version") == "1"
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"reports", "aggregate_rows", "meta"} <= tables


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
