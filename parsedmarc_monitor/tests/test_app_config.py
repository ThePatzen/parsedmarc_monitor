from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path("/addon") if Path("/addon/config.yaml").is_file() else Path(__file__).parents[1]
APP_ROOT = Path("/app") if Path("/app/dmarc_monitor/static/index.html").is_file() else ROOT / "rootfs/app"


def test_addon_declares_home_assistant_ingress_panel() -> None:
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

    assert config["ingress"] is True
    assert config["ingress_port"] == 8099
    assert config["panel_icon"] == "mdi:email-search-outline"
    assert config["panel_title"] == "DMARC Zustellungen"
    assert "ports" not in config


def test_docker_image_copies_application_tree_with_static_assets() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY rootfs/app /app" in dockerfile
    assert "COPY config.yaml Dockerfile requirements.txt /addon/" in dockerfile
    assert (APP_ROOT / "dmarc_monitor/static/index.html").is_file()


def test_web_view_does_not_require_a_web_framework() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    framework_names = ("aiohttp", "django", "fastapi", "flask", "starlette")

    assert not any(name in requirements for name in framework_names)
