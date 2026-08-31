from __future__ import annotations

import json
import sqlite3
from ipaddress import ip_network
from pathlib import Path

from dmarc_monitor.db import Database
from dmarc_monitor.mailbox import MailboxRunner
from dmarc_monitor.metrics import build_metrics
from dmarc_monitor.models import KnownSourceRule, MqttSettings, Settings
from dmarc_monitor.main import main

FIXTURE = Path(__file__).parent / "fixtures" / "aggregate_report.json"


def _batch() -> dict[str, object]:
    return {"aggregate_reports": [json.loads(FIXTURE.read_text(encoding="utf-8"))]}


def _settings() -> Settings:
    return Settings(
        imap_host="imap.example.test",
        imap_port=993,
        imap_ssl=True,
        imap_skip_certificate_verification=False,
        imap_username="dmarc@example.test",
        imap_password="secret",
        reports_folder="INBOX",
        archive_folder="Archive",
        check_timeout=60,
        retention_days=90,
        known_sources=(
            KnownSourceRule("Primary MX", ip_network("203.0.113.0/24"), None),
        ),
        log_level="info",
    )


def test_metrics_rebuild_identically_after_database_reopen(tmp_path: Path) -> None:
    path = tmp_path / "dmarc.sqlite3"
    database = Database(path)
    database.initialize()
    database.persist_batch(_batch(), _settings().known_sources)
    before = build_metrics(database)
    del database

    reopened = Database(path)
    reopened.initialize()
    after = build_metrics(reopened)

    assert after == before
    assert after.messages_latest_period == 13
    assert after.messages_30d == 13


class ThrowingPublisher:
    def __init__(self) -> None:
        self.storage_health: list[tuple[bool, str | None]] = []

    def set_storage_health(self, ok: bool, error: str | None = None) -> None:
        self.storage_health.append((ok, error))

    def publish_snapshot(self, snapshot: object) -> None:
        raise RuntimeError("mqtt unavailable")

    def set_imap_ok(self, ok: bool) -> None:
        pass


def test_duplicate_batch_remains_idempotent_during_mqtt_outage(tmp_path: Path) -> None:
    path = tmp_path / "dmarc.sqlite3"
    database = Database(path)
    database.initialize()
    publisher = ThrowingPublisher()
    runner = MailboxRunner(_settings(), database, publisher, stop_event=NeverStop())

    assert runner.save_batch(_batch()) is True
    assert runner.save_batch(_batch()) is True

    with sqlite3.connect(path) as conn:
        reports = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        rows = conn.execute("SELECT COUNT(*) FROM aggregate_rows").fetchone()[0]
    snapshot = build_metrics(database)
    assert reports == 1
    assert rows == 2
    assert snapshot.messages_latest_period == 13


class NeverStop:
    def is_set(self) -> bool:
        return False

    def wait(self, timeout: float) -> bool:
        return False


class FakeDatabase:
    instances: list["FakeDatabase"] = []

    def __init__(self, path: Path) -> None:
        self.path = path
        self.initialized = False
        FakeDatabase.instances.append(self)

    def initialize(self) -> None:
        self.initialized = True


class FakePublisher:
    instances: list["FakePublisher"] = []

    def __init__(self, settings: MqttSettings, app_version: str, snapshot_provider) -> None:
        self.settings = settings
        self.app_version = app_version
        self.snapshot_provider = snapshot_provider
        self.started = False
        self.stopped = False
        self.snapshots: list[object] = []
        FakePublisher.instances.append(self)

    def start(self) -> None:
        self.started = True

    def publish_snapshot(self, snapshot: object) -> None:
        self.snapshots.append(snapshot)

    def stop(self) -> None:
        self.stopped = True


class FakeRunner:
    instances: list["FakeRunner"] = []

    def __init__(self, settings: Settings, database: object, publisher: object, stop_event: object) -> None:
        self.settings = settings
        self.database = database
        self.publisher = publisher
        self.stop_event = stop_event
        self.ran = False
        FakeRunner.instances.append(self)

    def run_forever(self) -> None:
        self.ran = True


def test_main_constructs_lifecycle_in_order_and_stops_publisher(monkeypatch) -> None:
    import dmarc_monitor.main as module

    calls: list[str] = []
    FakeDatabase.instances.clear()
    FakePublisher.instances.clear()
    FakeRunner.instances.clear()
    settings = _settings()
    mqtt = MqttSettings("mqtt", 1883, "user", "password")
    rebuilt_snapshot = object()

    monkeypatch.setattr(module, "load_settings", lambda path: calls.append("settings") or settings)
    monkeypatch.setattr(module, "load_mqtt_settings", lambda env: calls.append("mqtt_settings") or mqtt)
    monkeypatch.setattr(module, "Database", FakeDatabase)
    monkeypatch.setattr(
        module,
        "run_daily_maintenance",
        lambda db, retention_days: calls.append("maintenance") or True,
    )
    monkeypatch.setattr(module, "build_metrics", lambda db: calls.append("metrics") or rebuilt_snapshot)
    monkeypatch.setattr(module, "MqttPublisher", FakePublisher)
    monkeypatch.setattr(module, "MailboxRunner", FakeRunner)
    monkeypatch.setattr(module.signal, "signal", lambda *args: None)

    result = main([])

    database = FakeDatabase.instances[-1]
    publisher = FakePublisher.instances[-1]
    runner = FakeRunner.instances[-1]
    assert result == 0
    assert database.initialized is True
    assert publisher.started is True
    assert publisher.snapshots == [rebuilt_snapshot]
    assert runner.ran is True
    assert publisher.stopped is True
    assert calls[:4] == ["settings", "mqtt_settings", "maintenance", "metrics"]


def test_main_returns_nonzero_for_invalid_startup_config(monkeypatch) -> None:
    import dmarc_monitor.main as module

    monkeypatch.setattr(module, "load_settings", lambda path: (_ for _ in ()).throw(ValueError("invalid options")))

    assert main([]) != 0
