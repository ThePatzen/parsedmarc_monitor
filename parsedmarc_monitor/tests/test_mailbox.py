from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from dmarc_monitor.models import PersistResult, Settings
from dmarc_monitor.mailbox import MailboxRunner, create_imap_connection


@pytest.fixture
def settings() -> Settings:
    return Settings(
        imap_host="imap.example.test",
        imap_port=993,
        imap_ssl=True,
        imap_skip_certificate_verification=False,
        imap_username="dmarc@example.test",
        imap_password="secret-password",
        reports_folder="INBOX",
        archive_folder="Archive/DMARC",
        check_timeout=30,
        retention_days=90,
        known_sources=(),
        log_level="info",
    )


class RecordingPublisher:
    def __init__(self, *, raise_on_publish: bool = False) -> None:
        self.storage_health: list[tuple[bool, str | None]] = []
        self.imap_health: list[bool] = []
        self.snapshots: list[object] = []
        self.raise_on_publish = raise_on_publish

    def ensure_started(self) -> None:
        pass

    def set_storage_health(self, ok: bool, error: str | None = None) -> None:
        self.storage_health.append((ok, error))

    def set_imap_ok(self, ok: bool) -> None:
        self.imap_health.append(ok)

    def publish_snapshot(self, snapshot: object) -> None:
        self.snapshots.append(snapshot)
        if self.raise_on_publish:
            raise RuntimeError("mqtt offline")


class StubDatabase:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.persisted: list[object] = []

    def persist_batch(self, batch: object, known_sources: object) -> PersistResult:
        if self.error:
            raise self.error
        self.persisted.append((batch, known_sources))
        return PersistResult(reports_inserted=1, rows_inserted=1)

    def get_meta(self, key: str) -> str | None:
        from datetime import UTC, datetime

        return datetime.now(UTC).date().isoformat()

    def latest_report_date(self) -> str | None:
        return None

    def latest_report_end_ts(self) -> int | None:
        return None

    def last_successful_ingestion(self) -> str | None:
        return None

    def counts_since_report_date(self, cutoff_date: str):
        from dmarc_monitor.models import CountSummary

        return CountSummary(0, 0, 0, 0, 0, 0)

    def latest_report_metadata(self) -> tuple[None, None]:
        return None, None


def test_save_batch_rejects_when_sqlite_does_not_accept_batch(settings: Settings) -> None:
    error = sqlite3.OperationalError("disk full")
    database = StubDatabase(error=error)
    publisher = RecordingPublisher()
    runner = MailboxRunner(settings, database, publisher, stop_event=FakeStopEvent())

    accepted = runner.save_batch({"aggregate_reports": []})

    assert accepted is False
    assert publisher.storage_health == [(False, "disk full")]
    assert publisher.snapshots == []


def test_save_batch_accepts_after_commit_even_when_mqtt_publish_fails(settings: Settings) -> None:
    database = StubDatabase()
    publisher = RecordingPublisher(raise_on_publish=True)
    runner = MailboxRunner(settings, database, publisher, stop_event=FakeStopEvent())

    accepted = runner.save_batch({"aggregate_reports": []})

    assert accepted is True
    assert publisher.storage_health == [(True, None)]
    assert len(database.persisted) == 1


class FakeStopEvent:
    def __init__(self) -> None:
        self.stopped = False
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self.stopped

    def set(self) -> None:
        self.stopped = True

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return self.stopped


def test_run_forever_uses_durable_callback_for_initial_and_watch_pass(settings: Settings) -> None:
    event = FakeStopEvent()
    publisher = RecordingPublisher()
    connection = object()
    calls: list[tuple[str, object, dict[str, object]]] = []
    parser_config = object()

    def get_reports(conn: object, **kwargs: object) -> None:
        calls.append(("initial", conn, kwargs))

    def watch_inbox(conn: object, callback: object, **kwargs: object) -> None:
        calls.append(("watch", conn, {"callback": callback, **kwargs}))
        event.set()

    runner = MailboxRunner(
        settings,
        StubDatabase(),
        publisher,
        stop_event=event,
        connection_factory=lambda _: connection,
        get_reports=get_reports,
        watch_inbox=watch_inbox,
        parser_config_factory=lambda: parser_config,
    )

    runner.run_forever()

    assert [name for name, _, _ in calls] == ["initial", "watch"]
    initial = calls[0][2]
    watch = calls[1][2]
    for kwargs in (initial, watch):
        assert kwargs["reports_folder"] == "INBOX"
        assert kwargs["archive_folder"] == "Archive/DMARC"
        assert kwargs["delete"] is False
        assert kwargs["batch_size"] == 10
        assert kwargs["max_unsaved_retries"] == 2_147_483_647
        assert kwargs["config"] is parser_config
    assert initial["save_callback"] == runner.save_batch
    assert watch["callback"] == runner.save_batch
    assert watch["config_reloading"]() is True
    assert publisher.imap_health[0] is True


def test_run_forever_retries_connection_with_exponential_backoff(settings: Settings) -> None:
    event = FakeStopEvent()
    publisher = RecordingPublisher()
    attempts = 0

    def connection_factory(_: Settings) -> object:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise ConnectionError(f"attempt {attempts}")
        event.set()
        raise ConnectionError("stop")

    runner = MailboxRunner(
        settings,
        StubDatabase(),
        publisher,
        stop_event=event,
        connection_factory=connection_factory,
        get_reports=lambda *args, **kwargs: None,
        watch_inbox=lambda *args, **kwargs: None,
        parser_config_factory=lambda: object(),
    )

    runner.run_forever()

    assert event.waits == [5, 10]
    assert publisher.imap_health[:2] == [False, False]


def test_run_forever_asks_mqtt_to_recover_before_each_imap_attempt(settings: Settings) -> None:
    event = FakeStopEvent()
    lifecycle: list[str] = []

    class RecoveryRecordingPublisher(RecordingPublisher):
        def ensure_started(self) -> None:
            lifecycle.append("mqtt")

    publisher = RecoveryRecordingPublisher()
    attempts = 0

    def connection_factory(_: Settings) -> object:
        nonlocal attempts
        attempts += 1
        lifecycle.append("imap")
        if attempts == 3:
            event.set()
        raise ConnectionError("offline")

    runner = MailboxRunner(
        settings,
        StubDatabase(),
        publisher,
        stop_event=event,
        connection_factory=connection_factory,
        get_reports=lambda *args, **kwargs: None,
        watch_inbox=lambda *args, **kwargs: None,
        parser_config_factory=lambda: object(),
    )

    runner.run_forever()

    assert lifecycle == ["mqtt", "imap", "mqtt", "imap", "mqtt", "imap"]


def test_backoff_is_capped_at_300_seconds(settings: Settings) -> None:
    event = FakeStopEvent()
    publisher = RecordingPublisher()
    attempts = 0

    def connection_factory(_: Settings) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 9:
            event.set()
        raise ConnectionError("offline")

    runner = MailboxRunner(
        settings,
        StubDatabase(),
        publisher,
        stop_event=event,
        connection_factory=connection_factory,
        get_reports=lambda *args, **kwargs: None,
        watch_inbox=lambda *args, **kwargs: None,
        parser_config_factory=lambda: object(),
    )

    runner.run_forever()

    assert event.waits == [5, 10, 20, 40, 80, 160, 300, 300]


def test_create_imap_connection_passes_tls_verification_and_timeout(settings: Settings) -> None:
    captured: dict[str, object] = {}

    class FakeImapConnection:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    result = create_imap_connection(settings, FakeImapConnection)

    assert isinstance(result, FakeImapConnection)
    assert captured["host"] == "imap.example.test"
    assert captured["user"] == "dmarc@example.test"
    assert captured["password"] == "secret-password"
    assert captured["port"] == 993
    assert captured["ssl"] is True
    assert captured["verify"] is True
    assert captured["timeout"] == 30
    assert captured["max_retries"] == 4


def test_create_imap_connection_can_disable_certificate_verification(settings: Settings) -> None:
    captured: dict[str, object] = {}

    class FakeImapConnection:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    insecure = replace(settings, imap_skip_certificate_verification=True)
    create_imap_connection(insecure, FakeImapConnection)

    assert captured["verify"] is False
