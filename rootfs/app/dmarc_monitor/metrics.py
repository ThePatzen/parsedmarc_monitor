from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .db import Database
from .models import CountSummary, MetricsSnapshot


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _pass_rate(counts: CountSummary) -> float | None:
    if counts.messages <= 0:
        return None
    return round(counts.passed / counts.messages * 100, 2)


def build_metrics(database: Database, now: datetime | None = None) -> MetricsSnapshot:
    current = _utc_now(now)
    latest_date = database.latest_report_date()
    latest = database.counts_for_report_date(latest_date) if latest_date else CountSummary(0, 0, 0, 0, 0, 0)
    cutoff_date = (current.date() - timedelta(days=29)).isoformat()
    rolling = database.counts_since_report_date(cutoff_date)
    last_report, last_reporting_org = database.latest_report_metadata()
    problem = latest.known_fail > 0 or latest.unknown_pass > 0
    problem_sources = database.top_problem_sources(latest_date) if latest_date and problem else ()

    return MetricsSnapshot(
        latest_report_date=latest_date,
        messages_latest_period=latest.messages,
        pass_latest_period=latest.passed,
        fail_latest_period=latest.failed,
        pass_rate_latest_period=_pass_rate(latest),
        messages_30d=rolling.messages,
        pass_rate_30d=_pass_rate(rolling),
        known_fail_30d=rolling.known_fail,
        unknown_pass_30d=rolling.unknown_pass,
        unknown_fail_30d=rolling.unknown_fail,
        last_report=last_report,
        last_reporting_org=last_reporting_org,
        problem=problem,
        problem_sources=problem_sources,
    )


def run_daily_maintenance(
    database: Database,
    retention_days: int,
    now: datetime | None = None,
) -> bool:
    current = _utc_now(now)
    today = current.date().isoformat()
    if database.get_meta("last_maintenance_date") == today:
        return False
    cutoff_ts = int((current - timedelta(days=retention_days)).timestamp())
    database.delete_reports_ending_before(cutoff_ts)
    database.optimize()
    database.set_meta("last_maintenance_date", today)
    return True
