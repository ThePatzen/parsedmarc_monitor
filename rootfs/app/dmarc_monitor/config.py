from __future__ import annotations

import json
from ipaddress import ip_network
from pathlib import Path
from typing import Mapping

from .models import KnownSourceRule, MqttSettings, Settings

VALID_LOG_LEVELS = {"debug", "info", "warning", "error"}


def _required_string(data: Mapping[str, object], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _required_bool(data: Mapping[str, object], field: str) -> bool:
    value = data.get(field)
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _required_int(data: Mapping[str, object], field: str, *, minimum: int, maximum: int | None = None) -> int:
    value = data.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f"{minimum}..{maximum}" if maximum is not None else f">={minimum}"
        raise ValueError(f"{field} must be {bounds}")
    return value


def _known_sources(value: object) -> tuple[KnownSourceRule, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("known_sources must be a list")

    rules: list[KnownSourceRule] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"known_sources[{index}] must be an object")
        name_value = item.get("name")
        if not isinstance(name_value, str) or not name_value.strip():
            raise ValueError(f"known_sources[{index}].name must be non-empty")
        name = name_value.strip()

        cidr_value = item.get("cidr", "")
        suffix_value = item.get("rdns_suffix", "")
        if cidr_value is None:
            cidr_value = ""
        if suffix_value is None:
            suffix_value = ""
        if not isinstance(cidr_value, str):
            raise ValueError(f"known_sources[{index}].cidr must be a string")
        if not isinstance(suffix_value, str):
            raise ValueError(f"known_sources[{index}].rdns_suffix must be a string")

        cidr = cidr_value.strip()
        suffix = suffix_value.strip().lower().strip(".")
        if not cidr and not suffix:
            raise ValueError(f"known_sources[{index}] requires cidr or rdns_suffix")

        network = None
        if cidr:
            try:
                network = ip_network(cidr, strict=False)
            except ValueError as exc:
                raise ValueError(f"known_sources[{index}].cidr is invalid") from exc

        rules.append(KnownSourceRule(name=name, network=network, rdns_suffix=suffix or None))
    return tuple(rules)


def load_settings(path: Path) -> Settings:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read options from {path}") from exc
    if not isinstance(data, dict):
        raise ValueError("options root must be an object")

    log_level = _required_string(data, "log_level").lower()
    if log_level not in VALID_LOG_LEVELS:
        raise ValueError("log_level must be one of debug, info, warning, error")

    return Settings(
        imap_host=_required_string(data, "imap_host"),
        imap_port=_required_int(data, "imap_port", minimum=1, maximum=65535),
        imap_ssl=_required_bool(data, "imap_ssl"),
        imap_skip_certificate_verification=_required_bool(data, "imap_skip_certificate_verification"),
        imap_username=_required_string(data, "imap_username"),
        imap_password=_required_string(data, "imap_password"),
        reports_folder=_required_string(data, "reports_folder"),
        archive_folder=_required_string(data, "archive_folder"),
        check_timeout=_required_int(data, "check_timeout", minimum=10),
        retention_days=_required_int(data, "retention_days", minimum=1),
        known_sources=_known_sources(data.get("known_sources", [])),
        log_level=log_level,
    )


def load_mqtt_settings(env: Mapping[str, str]) -> MqttSettings:
    def required(name: str) -> str:
        value = env.get(name)
        if value is None or not value.strip():
            raise ValueError(f"{name} must be set")
        return value.strip()

    host = required("DMARC_MQTT_HOST")
    port_text = required("DMARC_MQTT_PORT")
    username = required("DMARC_MQTT_USERNAME")
    password = required("DMARC_MQTT_PASSWORD")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("DMARC_MQTT_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("DMARC_MQTT_PORT must be between 1 and 65535")
    return MqttSettings(host=host, port=port, username=username, password=password)
