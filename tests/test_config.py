from __future__ import annotations

import json
from pathlib import Path

import pytest

from dmarc_monitor.config import load_mqtt_settings, load_settings


def write_options(tmp_path: Path, **overrides: object) -> Path:
    options: dict[str, object] = {
        "imap_host": "imap.example.at",
        "imap_port": 993,
        "imap_ssl": True,
        "imap_skip_certificate_verification": False,
        "imap_username": "dmarc@example.at",
        "imap_password": "secret-value",
        "reports_folder": "INBOX",
        "archive_folder": "Archive",
        "check_timeout": 60,
        "retention_days": 90,
        "known_sources": [],
        "log_level": "info",
    }
    options.update(overrides)
    path = tmp_path / "options.json"
    path.write_text(json.dumps(options), encoding="utf-8")
    return path


def test_load_settings_uses_expected_values(tmp_path: Path) -> None:
    settings = load_settings(write_options(tmp_path))

    assert settings.imap_host == "imap.example.at"
    assert settings.imap_port == 993
    assert settings.imap_ssl is True
    assert settings.imap_skip_certificate_verification is False
    assert settings.check_timeout == 60
    assert settings.retention_days == 90
    assert settings.known_sources == ()
    assert settings.log_level == "info"


def test_load_settings_normalizes_known_source(tmp_path: Path) -> None:
    path = write_options(
        tmp_path,
        known_sources=[{
            "name": "Mail relay",
            "cidr": "203.0.113.10",
            "rdns_suffix": ".Example.AT.",
        }],
    )

    settings = load_settings(path)

    rule = settings.known_sources[0]
    assert str(rule.network) == "203.0.113.10/32"
    assert rule.rdns_suffix == "example.at"


def test_rejects_rule_without_matcher(tmp_path: Path) -> None:
    path = write_options(
        tmp_path,
        known_sources=[{"name": "Broken", "cidr": "", "rdns_suffix": ""}],
    )

    with pytest.raises(ValueError, match=r"known_sources\[0\].*(cidr|rdns_suffix)"):
        load_settings(path)


def test_rejects_invalid_cidr_without_echoing_password(tmp_path: Path) -> None:
    path = write_options(
        tmp_path,
        known_sources=[{"name": "Broken", "cidr": "999.2.3.4/24", "rdns_suffix": ""}],
    )

    with pytest.raises(ValueError) as exc:
        load_settings(path)
    assert "secret-value" not in str(exc.value)


@pytest.mark.parametrize("field", ["imap_host", "imap_username", "imap_password", "reports_folder", "archive_folder"])
def test_rejects_missing_required_strings_without_secret_value(tmp_path: Path, field: str) -> None:
    path = write_options(tmp_path, **{field: ""})

    with pytest.raises(ValueError, match=field) as exc:
        load_settings(path)
    assert "secret-value" not in str(exc.value)


def test_rejects_invalid_bounds_and_log_level(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="imap_port"):
        load_settings(write_options(tmp_path, imap_port=70000))
    with pytest.raises(ValueError, match="check_timeout"):
        load_settings(write_options(tmp_path, check_timeout=9))
    with pytest.raises(ValueError, match="retention_days"):
        load_settings(write_options(tmp_path, retention_days=0))
    with pytest.raises(ValueError, match="log_level"):
        load_settings(write_options(tmp_path, log_level="trace"))


def test_loads_mqtt_from_environment() -> None:
    mqtt = load_mqtt_settings({
        "DMARC_MQTT_HOST": "core-mosquitto",
        "DMARC_MQTT_PORT": "1883",
        "DMARC_MQTT_USERNAME": "service-user",
        "DMARC_MQTT_PASSWORD": "service-pass",
    })
    assert mqtt.host == "core-mosquitto"
    assert mqtt.port == 1883
    assert mqtt.username == "service-user"
    assert mqtt.password == "service-pass"


def test_mqtt_environment_requires_all_values_and_valid_port() -> None:
    with pytest.raises(ValueError, match="DMARC_MQTT_PASSWORD"):
        load_mqtt_settings({
            "DMARC_MQTT_HOST": "core-mosquitto",
            "DMARC_MQTT_PORT": "1883",
            "DMARC_MQTT_USERNAME": "service-user",
        })
    with pytest.raises(ValueError, match="DMARC_MQTT_PORT"):
        load_mqtt_settings({
            "DMARC_MQTT_HOST": "core-mosquitto",
            "DMARC_MQTT_PORT": "70000",
            "DMARC_MQTT_USERNAME": "service-user",
            "DMARC_MQTT_PASSWORD": "service-pass",
        })
