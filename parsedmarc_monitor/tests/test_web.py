from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict
from http.client import RemoteDisconnected
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import parse_qs

import pytest

import dmarc_monitor.web as web_module
from dmarc_monitor.models import DeliveryDetail, DeliveryPage
from dmarc_monitor.web import (
    DeliveryQuery,
    QueryError,
    create_handler,
    delivery_page_payload,
    parse_delivery_query,
    WebServer,
)


def test_parse_delivery_query_defaults() -> None:
    query = parse_delivery_query(
        parse_qs("date_from=2026-08-24&date_to=2026-08-30")
    )

    assert query == DeliveryQuery("2026-08-24", "2026-08-30", "all", "", 1, 50)


def test_parse_delivery_query_parses_scalar_values() -> None:
    query = parse_delivery_query(
        parse_qs(
            "date_from=2026-08-24&date_to=2026-08-30&outcome=failed"
            "&search=mail.example&page=2&page_size=25"
        )
    )

    assert query == DeliveryQuery(
        "2026-08-24", "2026-08-30", "failed", "mail.example", 2, 25
    )


@pytest.mark.parametrize("parameter", ["date_from", "date_to", "outcome", "search", "page", "page_size"])
def test_parse_delivery_query_rejects_duplicate_parameter(parameter: str) -> None:
    values = {
        "date_from": ["2026-08-24"],
        "date_to": ["2026-08-30"],
        "outcome": ["all"],
        "search": [""],
        "page": ["1"],
        "page_size": ["50"],
    }
    values[parameter].append(values[parameter][0])

    with pytest.raises(QueryError, match="single value"):
        parse_delivery_query(values)


@pytest.mark.parametrize("missing", ["date_from", "date_to"])
def test_parse_delivery_query_requires_dates(missing: str) -> None:
    values = {"date_from": ["2026-08-24"], "date_to": ["2026-08-30"]}
    del values[missing]

    with pytest.raises(QueryError, match=missing):
        parse_delivery_query(values)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"outcome": ["other"]}, "outcome"),
        ({"page": ["zero"]}, "page"),
        ({"page": ["0"]}, "page"),
        ({"page_size": ["101"]}, "page size"),
        ({"page_size": ["0"]}, "page size"),
        ({"date_from": ["not-a-date"]}, "date"),
    ],
)
def test_parse_delivery_query_rejects_database_invalid_values(values: dict[str, list[str]], message: str) -> None:
    query_values = {
        "date_from": ["2026-08-24"],
        "date_to": ["2026-08-30"],
        "outcome": ["all"],
        "search": [""],
        "page": ["1"],
        "page_size": ["50"],
    }
    query_values.update(values)

    with pytest.raises(QueryError, match=message):
        parse_delivery_query(query_values)


def test_parse_delivery_query_rejects_reversed_date_range() -> None:
    with pytest.raises(QueryError, match="reversed"):
        parse_delivery_query(
            {"date_from": ["2026-08-30"], "date_to": ["2026-08-24"]}
        )


def test_delivery_query_is_immutable() -> None:
    query = DeliveryQuery("2026-08-24", "2026-08-30", "all", "", 1, 50)

    with pytest.raises(AttributeError):
        query.page = 2  # type: ignore[misc]


def sample_item() -> DeliveryDetail:
    return DeliveryDetail(
        id=7,
        report_date="2026-08-29",
        interval_begin="2026-08-29T00:00:00Z",
        interval_end="2026-08-29T23:59:59Z",
        reporting_org="München receiver",
        report_id="report-1",
        policy_domain="example.org",
        source_ip="203.0.113.10",
        source_reverse_dns="mail.example.org",
        source_base_domain="example.org",
        source_name="Mail source",
        source_asn=64500,
        source_as_name="Example ASN",
        source_country="AT",
        known_source_name="Primary",
        classification="known_fail",
        message_count=3,
        header_from="example.org",
        envelope_from="bounce@example.org",
        disposition="quarantine",
        dkim_result="fail",
        spf_result="pass",
        dkim_aligned=False,
        spf_aligned=True,
        dmarc_pass=False,
        report_org_email="reports@receiver.example",
        report_org_extra_contact_info="https://receiver.example/help",
        report_generator="Receiver Engine",
        report_errors=("minor issue",),
        xml_schema="draft",
        xml_namespace="urn:example:dmarc",
        timespan_requires_normalization=False,
        original_timespan_seconds=86400,
        policy_adkim="r",
        policy_aspf="s",
        policy_p="quarantine",
        policy_sp="none",
        policy_pct="100",
        policy_fo="0",
        policy_np="reject",
        policy_testing=None,
        policy_discovery_method="dmarc",
        source_type="cloud",
        source_as_domain="example.org",
        envelope_to="example.org",
        policy_override_reasons=(
            {"type": "local-policy", "comment": "test"},
        ),
        dkim_auth_results=(
            {
                "domain": "example.org",
                "selector": "selector1",
                "result": "fail",
                "human_result": "invalid signature",
            },
        ),
        spf_auth_results=(
            {
                "domain": "bounce.example.org",
                "scope": "mfrom",
                "result": "pass",
                "human_result": None,
            },
        ),
        normalized_timespan=False,
    )


def test_delivery_page_payload_serializes_dataclasses() -> None:
    page = DeliveryPage((sample_item(),), total=3, page=2, page_size=1)

    assert delivery_page_payload(page) == {
        "api_version": 1,
        "page": 2,
        "page_size": 1,
        "total": 3,
        "items": [asdict(sample_item())],
    }


class RecordingDatabase:
    def __init__(self) -> None:
        self.calls: list[DeliveryQuery] = []

    def query_deliveries(self, **kwargs):
        self.calls.append(DeliveryQuery(**kwargs))
        return DeliveryPage((sample_item(),), total=1, page=kwargs["page"], page_size=kwargs["page_size"])


class FailingDatabase:
    def query_deliveries(self, **kwargs):
        raise RuntimeError("database secret should not be returned")


class ValueErrorDatabase:
    def query_deliveries(self, **kwargs):
        raise ValueError("database password=sentinel")


def running_server(database, tmp_path: Path, allowed_clients: frozenset[str] | None = None):
    handler = create_handler(
        database,
        tmp_path,
        allowed_clients=frozenset({"127.0.0.1"}) if allowed_clients is None else allowed_clients,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(server: ThreadingHTTPServer, path: str, method: str = "GET"):
    url = f"http://127.0.0.1:{server.server_port}{path}"
    request_obj = Request(url, method=method)
    try:
        response = urlopen(request_obj, timeout=5)
    except HTTPError as exc:
        return exc
    except RemoteDisconnected:
        pytest.fail("server disconnected before returning an HTTP response")
    return response


def response_json(response) -> dict:
    return json.loads(response.read().decode("utf-8"))


SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


STATIC_ROOT = Path(web_module.__file__).with_name("static")


def test_http_serves_frontend_assets_with_expected_content_types_and_cache_policy() -> None:
    for server in running_server(RecordingDatabase(), STATIC_ROOT):
        expected = {
            "/": "text/html; charset=utf-8",
            "/app.css": "text/css; charset=utf-8",
            "/app.js": "text/javascript; charset=utf-8",
        }
        for path, content_type in expected.items():
            response = request(server, path)
            assert response.status == 200
            assert response.headers["Content-Type"] == content_type
            assert response.read()
            if path == "/":
                assert response.headers["Cache-Control"] == "no-store"
            else:
                assert response.headers["Cache-Control"] == "public, max-age=3600"


def test_frontend_contract_uses_semantic_controls_and_relative_resources() -> None:
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    for marker in (
        'id="date-from"',
        'id="date-to"',
        'id="outcome"',
        'id="search"',
        'id="apply-filters"',
        '<table',
        'id="pagination"',
        'id="loading"',
        'id="empty"',
        'id="error"',
        'href="./app.css"',
        'src="./app.js"',
    ):
        assert marker in html
    assert "<details" in html
    assert "https://" not in html


def test_frontend_contract_renders_api_values_as_safe_text() -> None:
    javascript = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    assert "textContent" in javascript
    assert "createTextNode" in javascript
    assert "./api/deliveries" in javascript
    assert "innerHTML" not in javascript
    assert "insertAdjacentHTML" not in javascript
    assert "http://" not in javascript
    assert "https://" not in javascript


def test_frontend_contract_has_mobile_failure_and_focus_styles() -> None:
    css = (STATIC_ROOT / "app.css").read_text(encoding="utf-8")
    assert "@media (max-width: 700px)" in css
    assert ".delivery-row.failed" in css
    assert ":focus-visible" in css
    assert "details" in css


def test_frontend_contract_renders_extended_report_and_authentication_details() -> None:
    javascript = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    for marker in (
        "report_org_email",
        "report_org_extra_contact_info",
        "report_generator",
        "report_errors",
        "policy_adkim",
        "policy_aspf",
        "policy_p",
        "policy_sp",
        "policy_pct",
        "policy_fo",
        "policy_np",
        "policy_testing",
        "policy_discovery_method",
        "source_type",
        "source_as_domain",
        "envelope_to",
        "policy_override_reasons",
        "dkim_auth_results",
        "spf_auth_results",
        "createElement(\"ul\")",
    ):
        assert marker in javascript

    css = (STATIC_ROOT / "app.css").read_text(encoding="utf-8")
    assert ".detail-section" in css


def test_http_deliveries_returns_utf8_json_and_security_headers() -> None:
    database = RecordingDatabase()
    for server in running_server(database, Path(__file__).parent):
        response = request(
            server,
            "/api/deliveries?date_from=2026-08-24&date_to=2026-08-30&page=2&page_size=25",
        )
        assert response.status == 200
        assert response.headers["Content-Type"] == "application/json; charset=utf-8"
        assert {name: response.headers[name] for name in SECURITY_HEADERS} == SECURITY_HEADERS
        payload = response_json(response)
        assert payload["api_version"] == 1
        assert payload["items"][0]["reporting_org"] == "München receiver"
        assert database.calls == [DeliveryQuery("2026-08-24", "2026-08-30", "all", "", 2, 25)]


def test_http_health_returns_minimal_json() -> None:
    for server in running_server(RecordingDatabase(), Path(__file__).parent):
        response = request(server, "/health")
        assert response.status == 200
        assert response_json(response) == {"status": "ok"}
        assert {name: response.headers[name] for name in SECURITY_HEADERS} == SECURITY_HEADERS


def test_http_rejects_unknown_routes_and_post() -> None:
    for server in running_server(RecordingDatabase(), Path(__file__).parent):
        unknown = request(server, "/not-a-route")
        assert unknown.status == 404
        assert unknown.headers["Content-Type"] == "application/json; charset=utf-8"
        post = request(server, "/health", method="POST")
        assert post.status == 405
        assert post.headers["Allow"] == "GET"


def test_http_returns_400_for_invalid_query() -> None:
    for server in running_server(RecordingDatabase(), Path(__file__).parent):
        response = request(server, "/api/deliveries?date_from=not-a-date&date_to=2026-08-30")
        assert response.status == 400
        assert "date" in response_json(response)["error"]


def test_http_returns_generic_500_without_exception_text() -> None:
    for server in running_server(FailingDatabase(), Path(__file__).parent):
        response = request(server, "/api/deliveries?date_from=2026-08-24&date_to=2026-08-30")
        body = response.read().decode("utf-8")
        assert response.status == 500
        assert "database secret" not in body
        assert json.loads(body) == {"error": "internal server error"}


def test_http_sanitizes_unexpected_value_error_and_logs_no_exception_text(caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger="dmarc_monitor.web"):
        for server in running_server(ValueErrorDatabase(), Path(__file__).parent):
            response = request(
                server,
                "/api/deliveries?date_from=2026-08-24&date_to=2026-08-30",
            )
            body = response.read().decode("utf-8")
            assert response.status == 500
            assert json.loads(body) == {"error": "internal server error"}
    assert "sentinel" not in caplog.text


def test_http_denies_clients_not_in_allowlist() -> None:
    for server in running_server(RecordingDatabase(), Path(__file__).parent, allowed_clients=frozenset()):
        response = request(server, "/health")
        assert response.status == 403
        assert response_json(response) == {"error": "forbidden"}


def test_web_server_lifecycle_is_threaded_and_idempotent_on_stop() -> None:
    web_server = WebServer(RecordingDatabase(), host="127.0.0.1", port=0)
    web_server.stop()
    web_server.start()
    try:
        assert web_server._thread is not None
        assert web_server._thread.daemon is True
        with pytest.raises(RuntimeError, match="already running"):
            web_server.start()
    finally:
        web_server.stop()
        web_server.stop()


def test_web_server_cleans_up_when_thread_start_fails(monkeypatch) -> None:
    class FakeServer:
        instances: list["FakeServer"] = []

        def __init__(self, address, handler) -> None:
            del address, handler
            self.closed = False
            self.shutdown_called = False
            self.instances.append(self)

        def server_close(self) -> None:
            self.closed = True

        def serve_forever(self) -> None:
            pass

        def shutdown(self) -> None:
            self.shutdown_called = True
            raise AssertionError("shutdown must not run before serve_forever starts")

    class FailingThread:
        daemon = True

        def __init__(self, target, name: str, daemon: bool) -> None:
            del target, name, daemon

        def start(self) -> None:
            raise RuntimeError("thread start failed")

    monkeypatch.setattr(web_module, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(web_module, "Thread", FailingThread)

    web_server = WebServer(RecordingDatabase(), host="127.0.0.1", port=0)
    with pytest.raises(RuntimeError, match="thread start failed"):
        web_server.start()

    server = FakeServer.instances[-1]
    assert server.closed is True
    assert server.shutdown_called is False
    assert web_server._server is None
    assert web_server._thread is None
    web_server.stop()
