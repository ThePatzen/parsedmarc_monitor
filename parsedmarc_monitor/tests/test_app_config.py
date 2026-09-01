from __future__ import annotations

from pathlib import Path
import runpy

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
    assert "RUN apk add --no-cache nodejs" in dockerfile
    runtime_stage = dockerfile.split("FROM ${BUILD_FROM} AS runtime", 1)[1]
    assert "nodejs" not in runtime_stage
    assert (APP_ROOT / "dmarc_monitor/static/index.html").is_file()


def test_web_view_does_not_require_a_web_framework() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    framework_names = ("aiohttp", "django", "fastapi", "flask", "starlette")

    assert not any(name in requirements for name in framework_names)


def test_release_metadata_and_delivery_view_documentation_are_consistent() -> None:
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    version = runpy.run_path(
        APP_ROOT / "dmarc_monitor/__init__.py",
        run_name="dmarc_monitor_release_metadata",
    )["__version__"]
    docs = (ROOT / "DOCS.md").read_text(encoding="utf-8")

    assert config["version"] == "0.3.0"
    assert version == "0.3.0"
    for phrase in (
        "Web UI",
        "letzten sieben",
        "Von",
        "Bis",
        "Fehlerhaft",
        "Erfolgreich",
        "Nachrichtenanzahl",
        "aggregierte Gruppen und keine einzelnen E-Mails",
    ):
        assert phrase in docs


def test_docs_define_browser_local_calendar_default_range() -> None:
    docs = " ".join((ROOT / "DOCS.md").read_text(encoding="utf-8").split())

    assert (
        "browser's local calendar date: it covers today plus the preceding six "
        "local calendar dates, inclusive"
    ) in docs


def test_docs_describe_delivery_order_and_summary_vs_expanded_fields() -> None:
    docs = " ".join((ROOT / "DOCS.md").read_text(encoding="utf-8").split())
    normalized = docs.lower()

    assert (
        "failed groups are ordered first, then by interval end descending, "
        "then stable row id descending"
    ) in normalized
    assert "each summary row shows" in normalized
    assert "expand a row" in normalized
    assert "largest message count used as tie-breakers" not in normalized
