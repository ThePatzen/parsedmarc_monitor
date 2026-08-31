from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from ipaddress import ip_network
from pathlib import Path

import pytest

from dmarc_monitor.db import Database
from dmarc_monitor.metrics import build_metrics, run_daily_maintenance
from dmarc_monitor.models import KnownSourceRule


def fixture() -> dict:
    path = Path(__file__).parent / "fixtures" / "aggregate_report.json"
    return json.loads(path.read_text(encoding="utf-8"))


def rules() -> tuple[KnownSourceRule, ...]:
    return (KnownSourceRule("Primary", ip_network("203.0.113.0/24"), None),)


def make_report(report_id: str, date: str, first_count: int, second_count: int, *, second_pass: bool, second_ip: str) -> dict:
    report = deepcopy(fixture())
    report["report_metadata"]["report_id"] = report_id
    report["report_metadata"]["begin_date"] = f"{date} 00:00:00"
    report["report_metadata"]["end_date"] = f"{date} 23:59:59"
    report["records"][0]["count"] = first_count
    report["records"][1]["count"] = second_count
    report["records"][1]["source"]["ip_address"] = second_ip
    report["records"][1]["alignment"]["dmarc"] = second_pass
    for record in report["records"]:
        record["interval_begin"] = f"{date} 00:00:00"
        record["interval_end"] = f"{date} 23:59:59"
    return report


def test_build_metrics_uses_latest_period_and_weighted_30d_counts(tmp_path: Path) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    old = make_report("old", "2026-08-29", 10, 3, second_pass=True, second_ip="198.51.100.20")
    latest = make_report("latest", "2026-08-30", 5, 3, second_pass=False, second_ip="203.0.113.11")
    db.persist_batch({"aggregate_reports": [old]}, rules(), datetime(2026, 8, 30, 1, tzinfo=UTC))
    db.persist_batch({"aggregate_reports": [latest]}, rules(), datetime(2026, 8, 31, 1, tzinfo=UTC))

    snapshot = build_metrics(db, datetime(2026, 8, 31, 12, tzinfo=UTC))

    assert snapshot.latest_report_date == "2026-08-30"
    assert snapshot.messages_latest_period == 8
    assert snapshot.pass_latest_period == 5
    assert snapshot.fail_latest_period == 3
    assert snapshot.pass_rate_latest_period == 62.5
    assert snapshot.problem is True
    assert snapshot.messages_30d == 21
    assert snapshot.pass_rate_30d == pytest.approx(85.71, abs=0.01)
    assert snapshot.known_fail_30d == 3
    assert snapshot.unknown_pass_30d == 3
    assert snapshot.unknown_fail_30d == 0
    assert snapshot.last_report == "2026-08-31T01:00:00Z"
    assert snapshot.last_reporting_org == "receiver.example"
    assert snapshot.problem_sources[0].classification == "known_fail"
    assert snapshot.problem_sources[0].message_count == 3


def test_historical_problem_does_not_latch_when_latest_period_is_healthy(tmp_path: Path) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    old_problem = make_report("old-problem", "2026-08-29", 10, 3, second_pass=True, second_ip="198.51.100.20")
    latest_healthy = make_report("latest-healthy", "2026-08-30", 5, 3, second_pass=True, second_ip="203.0.113.11")
    db.persist_batch({"aggregate_reports": [old_problem]}, rules())
    db.persist_batch({"aggregate_reports": [latest_healthy]}, rules())

    snapshot = build_metrics(db, datetime(2026, 8, 31, tzinfo=UTC))

    assert snapshot.unknown_pass_30d == 3
    assert snapshot.problem is False
    assert snapshot.problem_sources == ()


def test_build_metrics_empty_database(tmp_path: Path) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()

    snapshot = build_metrics(db, datetime(2026, 8, 31, tzinfo=UTC))

    assert snapshot.latest_report_date is None
    assert snapshot.messages_latest_period == 0
    assert snapshot.pass_rate_latest_period is None
    assert snapshot.messages_30d == 0
    assert snapshot.pass_rate_30d is None
    assert snapshot.problem is False


def test_daily_maintenance_deletes_expired_reports_only_once_per_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(tmp_path / "dmarc.sqlite3")
    db.initialize()
    old = make_report("expired", "2026-05-01", 1, 0, second_pass=False, second_ip="198.51.100.20")
    new = make_report("keep", "2026-08-30", 1, 0, second_pass=False, second_ip="198.51.100.20")
    db.persist_batch({"aggregate_reports": [old, new]}, rules())

    assert run_daily_maintenance(db, 90, datetime(2026, 8, 31, 12, tzinfo=UTC)) is True
    assert db.latest_report_date() == "2026-08-30"
    assert db.get_meta("last_maintenance_date") == "2026-08-31"

    calls = {"delete": 0, "optimize": 0}
    original_delete = db.delete_reports_ending_before
    original_optimize = db.optimize

    def tracked_delete(cutoff: int) -> int:
        calls["delete"] += 1
        return original_delete(cutoff)

    def tracked_optimize() -> None:
        calls["optimize"] += 1
        original_optimize()

    monkeypatch.setattr(db, "delete_reports_ending_before", tracked_delete)
    monkeypatch.setattr(db, "optimize", tracked_optimize)

    assert run_daily_maintenance(db, 90, datetime(2026, 8, 31, 18, tzinfo=UTC)) is False
    assert calls == {"delete": 0, "optimize": 0}
