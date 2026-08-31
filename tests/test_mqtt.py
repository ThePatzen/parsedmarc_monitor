from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

from dmarc_monitor.models import MetricsSnapshot, MqttSettings, ProblemSource
from dmarc_monitor.mqtt import (
    DIAGNOSTICS_TOPIC,
    DISCOVERY_TOPIC,
    STATE_TOPIC,
    STATUS_TOPIC,
    MqttPublisher,
    RuntimeHealth,
    build_diagnostics_payload,
    build_discovery_payload,
    build_state_payload,
)


EXPECTED_ENTITY_IDS = {
    "sensor.dmarc_latest_report_date",
    "sensor.dmarc_messages_latest_period",
    "sensor.dmarc_pass_latest_period",
    "sensor.dmarc_fail_latest_period",
    "sensor.dmarc_pass_rate_latest_period",
    "sensor.dmarc_messages_30d",
    "sensor.dmarc_pass_rate_30d",
    "sensor.dmarc_known_fail_30d",
    "sensor.dmarc_unknown_pass_30d",
    "sensor.dmarc_unknown_fail_30d",
    "sensor.dmarc_last_report",
    "sensor.dmarc_last_reporting_org",
    "sensor.dmarc_last_successful_ingestion",
    "sensor.dmarc_report_age_hours",
    "binary_sensor.dmarc_problem",
    "binary_sensor.dmarc_data_stale",
}


def snapshot(problem: bool = True, messages: int = 13) -> MetricsSnapshot:
    return MetricsSnapshot(
        latest_report_date="2026-08-30",
        messages_latest_period=messages,
        pass_latest_period=10,
        fail_latest_period=3,
        pass_rate_latest_period=76.92,
        messages_30d=42,
        pass_rate_30d=90.48,
        known_fail_30d=2,
        unknown_pass_30d=1,
        unknown_fail_30d=5,
        last_report="2026-08-31T01:00:00Z",
        last_reporting_org="receiver.example",
        last_successful_ingestion="2026-08-31T01:15:00Z",
        report_age_hours=7.5,
        data_stale=False,
        problem=problem,
        problem_sources=(
            ProblemSource("203.0.113.10", "mail.example.at", "Primary", "known_fail", 2),
        ),
    )


def test_shared_payloads_are_bounded_and_stable() -> None:
    state = build_state_payload(snapshot())
    assert set(state) == {
        "latest_report_date", "messages_latest_period", "pass_latest_period",
        "fail_latest_period", "pass_rate_latest_period", "messages_30d",
        "pass_rate_30d", "known_fail_30d", "unknown_pass_30d",
        "unknown_fail_30d", "last_report", "last_reporting_org",
        "last_successful_ingestion", "report_age_hours", "data_stale", "problem",
    }
    assert state["last_successful_ingestion"] == "2026-08-31T01:15:00Z"
    assert state["report_age_hours"] == 7.5
    assert state["data_stale"] is False
    health = RuntimeHealth(imap_ok=False, storage_ok=False, last_storage_error="x" * 500)
    diagnostics = build_diagnostics_payload(snapshot(), health)
    assert diagnostics["imap_ok"] is False
    assert diagnostics["storage_ok"] is False
    assert len(diagnostics["last_storage_error"]) == 300
    assert len(diagnostics["problem_sources"]) == 1
    assert "password" not in json.dumps(diagnostics).lower()


def test_discovery_contains_one_device_and_all_stable_entity_ids() -> None:
    payload = build_discovery_payload("0.1.0")

    assert DISCOVERY_TOPIC == "homeassistant/device/dmarc_monitor/config"
    assert STATUS_TOPIC == "dmarc_monitor/status"
    assert STATE_TOPIC == "dmarc_monitor/state"
    assert DIAGNOSTICS_TOPIC == "dmarc_monitor/diagnostics"
    assert payload["dev"]["ids"] == ["ha_dmarc_monitor"]
    assert payload["dev"]["sw"] == "0.1.0"
    assert payload["state_topic"] == STATE_TOPIC
    assert payload["availability_topic"] == STATUS_TOPIC
    components = payload["cmps"]
    assert len(components) == 16
    assert {component["default_entity_id"] for component in components.values()} == EXPECTED_ENTITY_IDS
    assert len({component["unique_id"] for component in components.values()}) == 16
    assert all(component["p"] in {"sensor", "binary_sensor"} for component in components.values())
    assert components["pass_rate_latest_period"]["unit_of_measurement"] == "%"
    assert "unknown" in components["pass_rate_latest_period"]["value_template"]
    assert components["last_report"]["device_class"] == "timestamp"
    assert components["last_successful_ingestion"]["device_class"] == "timestamp"
    assert "unknown" in components["last_successful_ingestion"]["value_template"]
    assert components["report_age_hours"]["unit_of_measurement"] == "h"
    assert components["report_age_hours"]["device_class"] == "duration"
    assert "unknown" in components["report_age_hours"]["value_template"]
    assert components["data_stale"]["p"] == "binary_sensor"
    assert components["data_stale"]["device_class"] == "problem"
    assert components["data_stale"]["value_template"] == "{{ 'ON' if value_json.data_stale else 'OFF' }}"
    assert components["problem"]["json_attributes_topic"] == DIAGNOSTICS_TOPIC


class FakeClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.calls: list[tuple] = []
        self.publishes: list[tuple[str, str, int, bool]] = []
        self.subscriptions: list[tuple[str, int]] = []

    def username_pw_set(self, username: str, password: str) -> None:
        self.calls.append(("username_pw_set", username, password))

    def reconnect_delay_set(self, min_delay: int, max_delay: int) -> None:
        self.calls.append(("reconnect_delay_set", min_delay, max_delay))

    def will_set(self, topic: str, payload: str, qos: int, retain: bool) -> None:
        self.calls.append(("will_set", topic, payload, qos, retain))

    def connect_async(self, host: str, port: int, keepalive: int) -> None:
        self.calls.append(("connect_async", host, port, keepalive))

    def loop_start(self) -> None:
        self.calls.append(("loop_start",))

    def loop_stop(self) -> None:
        self.calls.append(("loop_stop",))

    def disconnect(self) -> None:
        self.calls.append(("disconnect",))

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscriptions.append((topic, qos))

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> object:
        self.publishes.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=0)


def make_publisher(fake: FakeClient, initial: MetricsSnapshot | None = None) -> MqttPublisher:
    settings = MqttSettings("core-mosquitto", 1883, "service-user", "service-pass")
    return MqttPublisher(settings, "0.1.0", lambda: initial or snapshot(), client_factory=lambda *a, **k: fake)


def test_start_sets_lwt_reconnect_credentials_and_async_connect() -> None:
    fake = FakeClient()
    publisher = make_publisher(fake)

    publisher.start()

    assert ("will_set", STATUS_TOPIC, "offline", 1, True) in fake.calls
    assert ("reconnect_delay_set", 1, 60) in fake.calls
    assert ("connect_async", "core-mosquitto", 1883, 60) in fake.calls
    assert ("loop_start",) in fake.calls


def test_connect_publishes_birth_discovery_state_diagnostics_and_subscribes() -> None:
    fake = FakeClient()
    publisher = make_publisher(fake)
    publisher.start()

    assert fake.on_connect is not None
    fake.on_connect(fake, None, None, 0, None)

    assert publisher.is_connected is True
    assert ("homeassistant/status", 1) in fake.subscriptions
    by_topic = {topic: (payload, qos, retain) for topic, payload, qos, retain in fake.publishes}
    assert by_topic[STATUS_TOPIC] == ("online", 1, True)
    assert by_topic[DISCOVERY_TOPIC][1:] == (1, True)
    assert by_topic[STATE_TOPIC][1:] == (1, True)
    assert by_topic[DIAGNOSTICS_TOPIC][1:] == (1, True)
    assert json.loads(by_topic[STATE_TOPIC][0])["messages_latest_period"] == 13


def test_home_assistant_birth_republishes_discovery_and_state() -> None:
    fake = FakeClient()
    publisher = make_publisher(fake)
    publisher.start()
    fake.on_connect(fake, None, None, 0, None)
    fake.publishes.clear()

    assert fake.on_message is not None
    fake.on_message(fake, None, SimpleNamespace(topic="homeassistant/status", payload=b"online"))

    topics = [row[0] for row in fake.publishes]
    assert DISCOVERY_TOPIC in topics
    assert STATE_TOPIC in topics
    assert DIAGNOSTICS_TOPIC in topics


def test_disconnected_snapshot_is_cached_and_republished_on_reconnect() -> None:
    fake = FakeClient()
    publisher = make_publisher(fake)
    newer = replace(snapshot(), messages_latest_period=99)

    publisher.publish_snapshot(newer)
    assert fake.publishes == []

    publisher.start()
    fake.on_connect(fake, None, None, 0, None)
    state_messages = [json.loads(payload) for topic, payload, _, _ in fake.publishes if topic == STATE_TOPIC]
    assert state_messages[-1]["messages_latest_period"] == 99


def test_health_updates_and_stop_are_non_throwing() -> None:
    fake = FakeClient()
    publisher = make_publisher(fake)
    publisher.start()
    fake.on_connect(fake, None, None, 0, None)
    fake.publishes.clear()

    publisher.set_imap_ok(False)
    publisher.set_storage_health(False, "disk failed")
    publisher.stop()

    diagnostics = [json.loads(payload) for topic, payload, _, _ in fake.publishes if topic == DIAGNOSTICS_TOPIC]
    assert diagnostics[-1]["storage_ok"] is False
    assert (STATUS_TOPIC, "offline", 1, True) in fake.publishes
    assert ("loop_stop",) in fake.calls
    assert ("disconnect",) in fake.calls
