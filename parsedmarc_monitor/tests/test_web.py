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
