from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Callable

from .models import MetricsSnapshot, MqttSettings

LOGGER = logging.getLogger(__name__)

DISCOVERY_TOPIC = "homeassistant/device/dmarc_monitor/config"
STATUS_TOPIC = "dmarc_monitor/status"
STATE_TOPIC = "dmarc_monitor/state"
DIAGNOSTICS_TOPIC = "dmarc_monitor/diagnostics"
HA_STATUS_TOPIC = "homeassistant/status"


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    imap_ok: bool | None = None
    storage_ok: bool = True
    last_storage_error: str | None = None


def build_state_payload(snapshot: MetricsSnapshot) -> dict[str, object]:
    return {
        "latest_report_date": snapshot.latest_report_date,
        "messages_latest_period": snapshot.messages_latest_period,
        "pass_latest_period": snapshot.pass_latest_period,
        "fail_latest_period": snapshot.fail_latest_period,
        "pass_rate_latest_period": snapshot.pass_rate_latest_period,
        "messages_30d": snapshot.messages_30d,
        "pass_rate_30d": snapshot.pass_rate_30d,
        "known_fail_30d": snapshot.known_fail_30d,
        "unknown_pass_30d": snapshot.unknown_pass_30d,
        "unknown_fail_30d": snapshot.unknown_fail_30d,
        "last_report": snapshot.last_report,
        "last_reporting_org": snapshot.last_reporting_org,
        "problem": snapshot.problem,
    }


def build_diagnostics_payload(snapshot: MetricsSnapshot, health: RuntimeHealth) -> dict[str, object]:
    error = health.last_storage_error
    return {
        "problem_sources": [asdict(source) for source in snapshot.problem_sources[:5]],
        "imap_ok": health.imap_ok,
        "storage_ok": health.storage_ok,
        "last_storage_error": error[:300] if error else None,
    }


def _sensor_component(
    key: str,
    name: str,
    entity_id: str,
    *,
    unit: str | None = None,
    device_class: str | None = None,
    nullable_number: bool = False,
) -> dict[str, object]:
    value_template = f"{{{{ value_json.{key} }}}}"
    if nullable_number:
        value_template = f"{{{{ value_json.{key} if value_json.{key} is not none else 'unknown' }}}}"
    component: dict[str, object] = {
        "p": "sensor",
        "name": name,
        "unique_id": f"ha_dmarc_monitor_{key}",
        "default_entity_id": entity_id,
        "value_template": value_template,
    }
    if unit:
        component["unit_of_measurement"] = unit
    if device_class:
        component["device_class"] = device_class
    return component


def build_discovery_payload(app_version: str) -> dict[str, object]:
    components: dict[str, dict[str, object]] = {
        "latest_report_date": _sensor_component(
            "latest_report_date", "Latest report date", "sensor.dmarc_latest_report_date"
        ),
        "messages_latest_period": _sensor_component(
            "messages_latest_period", "Messages latest period", "sensor.dmarc_messages_latest_period"
        ),
        "pass_latest_period": _sensor_component(
            "pass_latest_period", "Passed latest period", "sensor.dmarc_pass_latest_period"
        ),
        "fail_latest_period": _sensor_component(
            "fail_latest_period", "Failed latest period", "sensor.dmarc_fail_latest_period"
        ),
        "pass_rate_latest_period": _sensor_component(
            "pass_rate_latest_period",
            "Pass rate latest period",
            "sensor.dmarc_pass_rate_latest_period",
            unit="%",
            nullable_number=True,
        ),
        "messages_30d": _sensor_component("messages_30d", "Messages 30d", "sensor.dmarc_messages_30d"),
        "pass_rate_30d": _sensor_component(
            "pass_rate_30d", "Pass rate 30d", "sensor.dmarc_pass_rate_30d", unit="%", nullable_number=True
        ),
        "known_fail_30d": _sensor_component(
            "known_fail_30d", "Known source failures 30d", "sensor.dmarc_known_fail_30d"
        ),
        "unknown_pass_30d": _sensor_component(
            "unknown_pass_30d", "Unknown source passes 30d", "sensor.dmarc_unknown_pass_30d"
        ),
        "unknown_fail_30d": _sensor_component(
            "unknown_fail_30d", "Unknown source failures 30d", "sensor.dmarc_unknown_fail_30d"
        ),
        "last_report": _sensor_component(
            "last_report", "Last report", "sensor.dmarc_last_report", device_class="timestamp"
        ),
        "last_reporting_org": _sensor_component(
            "last_reporting_org", "Last reporting organization", "sensor.dmarc_last_reporting_org"
        ),
        "problem": {
            "p": "binary_sensor",
            "name": "Problem",
            "unique_id": "ha_dmarc_monitor_problem",
            "default_entity_id": "binary_sensor.dmarc_problem",
            "value_template": "{{ 'ON' if value_json.problem else 'OFF' }}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "json_attributes_topic": DIAGNOSTICS_TOPIC,
        },
    }
    return {
        "dev": {
            "ids": ["ha_dmarc_monitor"],
            "name": "DMARC Monitor",
            "mf": "Custom",
            "mdl": "parsedmarc Monitor",
            "sw": app_version,
        },
        "o": {"name": "HA DMARC Monitor", "sw": app_version},
        "state_topic": STATE_TOPIC,
        "availability_topic": STATUS_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
        "qos": 1,
        "cmps": components,
    }


def _default_client_factory() -> object:
    import paho.mqtt.client as mqtt

    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ha_dmarc_monitor")


class MqttPublisher:
    def __init__(
        self,
        settings: MqttSettings,
        app_version: str,
        snapshot_provider: Callable[[], MetricsSnapshot],
        client_factory: Callable[..., object] | None = None,
    ) -> None:
        self.settings = settings
        self.app_version = app_version
        self.snapshot_provider = snapshot_provider
        self.client_factory = client_factory
        self._client: object | None = None
        self._connected = False
        self._started = False
        self._latest_snapshot: MetricsSnapshot | None = None
        self._health = RuntimeHealth()

    @property
    def is_connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        if self._started:
            return
        try:
            factory = self.client_factory
            client = factory() if factory is not None else _default_client_factory()
            self._client = client
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message
            client.username_pw_set(self.settings.username, self.settings.password)
            client.reconnect_delay_set(min_delay=1, max_delay=60)
            client.will_set(STATUS_TOPIC, payload="offline", qos=1, retain=True)
            client.connect_async(self.settings.host, self.settings.port, keepalive=60)
            client.loop_start()
            self._started = True
        except Exception:
            LOGGER.exception("Unable to start MQTT publisher")
            self._client = None
            self._connected = False

    def stop(self) -> None:
        client = self._client
        self._connected = False
        if client is None:
            return
        try:
            client.publish(STATUS_TOPIC, "offline", qos=1, retain=True)
        except Exception:
            LOGGER.exception("Unable to publish MQTT offline state")
        try:
            client.loop_stop()
        except Exception:
            LOGGER.exception("Unable to stop MQTT loop")
        try:
            client.disconnect()
        except Exception:
            LOGGER.exception("Unable to disconnect MQTT client")
        self._started = False

    def publish_snapshot(self, snapshot: MetricsSnapshot) -> None:
        self._latest_snapshot = snapshot
        if not self._connected:
            return
        self._publish_snapshot_payloads(snapshot)

    def set_imap_ok(self, value: bool) -> None:
        self._health = RuntimeHealth(value, self._health.storage_ok, self._health.last_storage_error)
        self._publish_health_if_connected()

    def set_storage_health(self, ok: bool, error: str | None = None) -> None:
        self._health = RuntimeHealth(self._health.imap_ok, ok, None if ok else error)
        self._publish_health_if_connected()

    def _snapshot(self) -> MetricsSnapshot | None:
        if self._latest_snapshot is not None:
            return self._latest_snapshot
        try:
            self._latest_snapshot = self.snapshot_provider()
        except Exception:
            LOGGER.exception("Unable to build MQTT snapshot")
            return None
        return self._latest_snapshot

    def _safe_publish(self, topic: str, payload: str, *, retain: bool = True) -> None:
        client = self._client
        if client is None:
            return
        try:
            client.publish(topic, payload, qos=1, retain=retain)
        except Exception:
            LOGGER.exception("Unable to publish MQTT topic %s", topic)

    def _publish_json(self, topic: str, payload: dict[str, object]) -> None:
        self._safe_publish(topic, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def _publish_snapshot_payloads(self, snapshot: MetricsSnapshot) -> None:
        self._publish_json(STATE_TOPIC, build_state_payload(snapshot))
        self._publish_json(DIAGNOSTICS_TOPIC, build_diagnostics_payload(snapshot, self._health))

    def _publish_health_if_connected(self) -> None:
        if not self._connected:
            return
        snapshot = self._snapshot()
        if snapshot is not None:
            self._publish_json(DIAGNOSTICS_TOPIC, build_diagnostics_payload(snapshot, self._health))

    def _republish_all(self) -> None:
        try:
            self._publish_json(DISCOVERY_TOPIC, build_discovery_payload(self.app_version))
            snapshot = self._snapshot()
            if snapshot is not None:
                self._publish_snapshot_payloads(snapshot)
        except Exception:
            LOGGER.exception("Unable to republish MQTT discovery/state")

    def _on_connect(self, client: object, userdata: object, flags: object, reason_code: object, properties: object) -> None:
        if getattr(reason_code, "is_failure", False) or (isinstance(reason_code, int) and reason_code != 0):
            self._connected = False
            LOGGER.warning("MQTT connection rejected: %s", reason_code)
            return
        self._connected = True
        try:
            client.subscribe(HA_STATUS_TOPIC, qos=1)
        except Exception:
            LOGGER.exception("Unable to subscribe to Home Assistant MQTT status")
        self._safe_publish(STATUS_TOPIC, "online")
        self._republish_all()

    def _on_disconnect(self, client: object, userdata: object, disconnect_flags: object, reason_code: object, properties: object) -> None:
        self._connected = False

    def _on_message(self, client: object, userdata: object, message: object) -> None:
        try:
            topic = getattr(message, "topic", "")
            payload = getattr(message, "payload", b"")
            if topic == HA_STATUS_TOPIC and bytes(payload).decode("utf-8", errors="replace").strip().lower() == "online":
                self._republish_all()
        except Exception:
            LOGGER.exception("Unable to process Home Assistant MQTT birth message")
