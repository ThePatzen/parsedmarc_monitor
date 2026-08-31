from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .models import DeliveryPage

logger = logging.getLogger(__name__)

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self'; connect-src 'self'; frame-ancestors 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


@dataclass(frozen=True, slots=True)
class DeliveryQuery:
    date_from: str
    date_to: str
    outcome: str = "all"
    search: str = ""
    page: int = 1
    page_size: int = 50


class QueryError(ValueError):
    """Raised when HTTP query parameters cannot form a delivery query."""


def _single_value(query: Mapping[str, list[str]], name: str, default: str | None = None) -> str | None:
    values = query.get(name)
    if values is None:
        return default
    if len(values) != 1:
        raise QueryError(f"{name} must have a single value")
    return values[0]


def _required_value(query: Mapping[str, list[str]], name: str) -> str:
    value = _single_value(query, name)
    if value is None or value == "":
        raise QueryError(f"{name} is required")
    return value


def parse_delivery_query(query: Mapping[str, list[str]]) -> DeliveryQuery:
    date_from = _required_value(query, "date_from")
    date_to = _required_value(query, "date_to")
    outcome = _single_value(query, "outcome", "all")
    search = _single_value(query, "search", "")
    page_text = _single_value(query, "page", "1")
    page_size_text = _single_value(query, "page_size", "50")
    assert outcome is not None and search is not None
    assert page_text is not None and page_size_text is not None

    try:
        parsed_from = date.fromisoformat(date_from)
    except ValueError as exc:
        raise QueryError("date_from must be an ISO date") from exc
    try:
        parsed_to = date.fromisoformat(date_to)
    except ValueError as exc:
        raise QueryError("date_to must be an ISO date") from exc
    if parsed_to < parsed_from:
        raise QueryError("delivery date range is reversed")
    if outcome not in {"all", "passed", "failed"}:
        raise QueryError("delivery outcome must be all, passed, or failed")
    try:
        page = int(page_text)
    except ValueError as exc:
        raise QueryError("delivery page must be an integer") from exc
    if page < 1:
        raise QueryError("delivery page must be positive")
    try:
        page_size = int(page_size_text)
    except ValueError as exc:
        raise QueryError("delivery page size must be an integer") from exc
    if not 1 <= page_size <= 100:
        raise QueryError("delivery page size must be between 1 and 100")
    return DeliveryQuery(date_from, date_to, outcome, search, page, page_size)


def delivery_page_payload(page: DeliveryPage) -> dict[str, object]:
    return {
        "api_version": 1,
        "page": page.page,
        "page_size": page.page_size,
        "total": page.total,
        "items": [asdict(item) for item in page.items],
    }


def create_handler(
    database: Any,
    static_root: Path,
    allowed_clients: frozenset[str] = frozenset({"172.30.32.2"}),
) -> type[BaseHTTPRequestHandler]:
    root = Path(static_root)

    class Handler(BaseHTTPRequestHandler):
        server_version = "dmarc-monitor"

        def _allowed(self) -> bool:
            return self.client_address[0] in allowed_clients

        def _send_json(
            self,
            status: int,
            payload: dict[str, object],
            extra_headers: Mapping[str, str] | None = None,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for name, value in _SECURITY_HEADERS.items():
                self.send_header(name, value)
            if extra_headers:
                for name, value in extra_headers.items():
                    self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, status: int, message: str) -> None:
            self._send_json(status, {"error": message})

        def _send_static(self, path: str) -> None:
            filenames = {"/": ("index.html", "text/html; charset=utf-8"), "/app.css": ("app.css", "text/css; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8")}
            filename, content_type = filenames[path]
            asset = root / filename
            if not asset.is_file():
                self._send_error_json(404, "not found")
                return
            body = asset.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in _SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if not self._allowed():
                self._send_error_json(403, "forbidden")
                return
            try:
                parsed = urlsplit(self.path)
                if parsed.path == "/health":
                    self._send_json(200, {"status": "ok"})
                elif parsed.path == "/api/deliveries":
                    delivery_query = parse_delivery_query(
                        parse_qs(parsed.query, keep_blank_values=True)
                    )
                    page = database.query_deliveries(
                        date_from=delivery_query.date_from,
                        date_to=delivery_query.date_to,
                        outcome=delivery_query.outcome,
                        search=delivery_query.search,
                        page=delivery_query.page,
                        page_size=delivery_query.page_size,
                    )
                    self._send_json(200, delivery_page_payload(page))
                elif parsed.path in {"/", "/app.css", "/app.js"}:
                    self._send_static(parsed.path)
                else:
                    self._send_error_json(404, "not found")
            except QueryError as exc:
                self._send_error_json(400, str(exc))
            except Exception as exc:
                logger.error(
                    "unexpected web request failure type=%s", type(exc).__name__
                )
                self._send_error_json(500, "internal server error")

        def do_POST(self) -> None:  # noqa: N802
            if not self._allowed():
                self._send_error_json(403, "forbidden")
                return
            self._send_json(405, {"error": "method not allowed"}, {"Allow": "GET"})

        def log_message(self, format: str, *args: object) -> None:
            logger.info("%s - %s", self.address_string(), format % args)

    return Handler


class WebServer:
    def __init__(self, database: Any, host: str = "0.0.0.0", port: int = 8099) -> None:
        self.database = database
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("web server is already running")
        handler = create_handler(self.database, Path(__file__).parent / "static")
        server = ThreadingHTTPServer((self.host, self.port), handler)
        thread = Thread(target=server.serve_forever, name="dmarc-web", daemon=True)
        self._server = server
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        self._server = None
        self._thread = None
