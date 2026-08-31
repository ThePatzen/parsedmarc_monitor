# DMARC Delivery Detail View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only Home Assistant Ingress view that lists retained DMARC aggregate delivery groups, defaults to seven calendar days, supports inclusive date and outcome filtering, and exposes expandable diagnostics.

**Architecture:** Add a parameterized, paginated read model to `Database`; expose it through a small standard-library HTTP server; and serve bundled German HTML/CSS/JavaScript through Home Assistant Ingress. Start and stop the HTTP server beside the existing mailbox and MQTT components without changing ingestion or MQTT contracts.

**Tech Stack:** Python 3.14, SQLite, `http.server`, vanilla HTML/CSS/JavaScript, Home Assistant App Ingress, pytest, Ruff, Docker.

**Spec:** `parsedmarc_monitor/docs/superpowers/specs/2026-08-31-delivery-detail-view-design.md`

## Global Constraints

- A displayed row is one DMARC aggregate group, never an individual email; always expose `message_count`.
- The initial browser range is today minus six days through today, inclusive.
- API page size defaults to 50 and must never exceed 100.
- Keep all existing MQTT topics, entity IDs, payloads, ingestion behavior, retention, and schema version compatible.
- Add no web framework, HACS dependency, custom integration, host port, or separate authentication.
- In production, accept web requests only from the Home Assistant Ingress proxy address `172.30.32.2`.
- Render report-controlled values as text, never as HTML.
- Use German UI labels; keep the API language-neutral.

---

### Task 1: Paginated delivery read model

**Files:**
- Modify: `parsedmarc_monitor/rootfs/app/dmarc_monitor/models.py`
- Modify: `parsedmarc_monitor/rootfs/app/dmarc_monitor/db.py`
- Test: `parsedmarc_monitor/tests/test_db.py`

**Interfaces:**
- Consumes: existing `reports` and `aggregate_rows` schema.
- Produces: `DeliveryDetail`, `DeliveryPage`, and `Database.query_deliveries(date_from: str, date_to: str, outcome: str = "all", search: str = "", page: int = 1, page_size: int = 50) -> DeliveryPage`.

- [ ] **Step 1: Add failing tests for inclusive range, returned fields, and ordering**

Append tests that persist reports on multiple dates, then assert the boundary dates are included, out-of-range dates are excluded, failures precede passes, and equal outcomes use `interval_end_ts DESC, id DESC`. Assert a returned item exposes all report, source, identity, result, alignment, and classification fields.

```python
result = db.query_deliveries("2026-08-24", "2026-08-30")
assert result.total == 2
assert [row.dmarc_pass for row in result.items] == [False, True]
assert result.items[0].message_count == 3
assert result.items[0].reporting_org == "receiver.example"
assert result.items[0].report_id == "fixture-report-1"
assert result.items[0].header_from == "example.org"
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `cd parsedmarc_monitor && python3 -m pytest tests/test_db.py -k query_deliveries -v`

Expected: FAIL because `Database.query_deliveries` and the delivery result types do not exist.

- [ ] **Step 3: Define immutable delivery result types**

Add to `models.py`:

```python
@dataclass(frozen=True, slots=True)
class DeliveryDetail:
    id: int
    report_date: str
    interval_begin: str
    interval_end: str
    reporting_org: str
    report_id: str
    policy_domain: str
    source_ip: str
    source_reverse_dns: str | None
    source_base_domain: str | None
    source_name: str | None
    source_asn: int | None
    source_as_name: str | None
    source_country: str | None
    known_source_name: str | None
    classification: str
    message_count: int
    header_from: str
    envelope_from: str | None
    disposition: str | None
    dkim_result: str | None
    spf_result: str | None
    dkim_aligned: bool
    spf_aligned: bool
    dmarc_pass: bool

@dataclass(frozen=True, slots=True)
class DeliveryPage:
    items: tuple[DeliveryDetail, ...]
    total: int
    page: int
    page_size: int
```

Use UTC `YYYY-MM-DDTHH:MM:SSZ` strings for interval fields.

- [ ] **Step 4: Implement the minimal parameterized join and mapping**

Validate `date.fromisoformat`, ordered bounds, outcome membership, positive page, and page size `1..100`. Build one shared parameterized `WHERE` clause for count and page queries. Map `known_source` and `dmarc_pass` to exactly `known_pass`, `known_fail`, `unknown_pass`, or `unknown_fail`; do not reuse the anomaly-only `ProblemSource` classification.

Use ordering:

```sql
ORDER BY a.dmarc_pass ASC, a.interval_end_ts DESC, a.id DESC
LIMIT ? OFFSET ?
```

Escape `\`, `%`, and `_` in search input and use `LIKE ? ESCAPE '\' COLLATE NOCASE` across the five specified fields.

- [ ] **Step 5: Verify core query tests GREEN**

Run: `cd parsedmarc_monitor && python3 -m pytest tests/test_db.py -k query_deliveries -v`

Expected: PASS.

- [ ] **Step 6: Add failing filter, literal search, pagination, and validation tests**

Add parameterized tests for `passed`, `failed`, invalid outcome, invalid/misaligned dates, page zero, page size 101, `%` and `_` literal search, case-insensitive matching of every supported field, total independent of page size, and an empty page beyond the end.

```python
page = db.query_deliveries(
    "2026-08-24", "2026-08-30", outcome="failed", page=2, page_size=1
)
assert page.total == 2
assert page.page == 2
assert len(page.items) == 1
```

- [ ] **Step 7: Run new tests RED, then implement minimal filter helpers**

Run the focused command from Step 5 before implementation. Confirm each new case fails for missing validation/filter behavior, then add `_delivery_where(...)` and `_delivery_detail(...)` private helpers and rerun until all focused tests pass.

- [ ] **Step 8: Run the database suite and commit**

Run: `cd parsedmarc_monitor && python3 -m pytest tests/test_db.py -v`

Expected: all database tests PASS.

```bash
git add parsedmarc_monitor/rootfs/app/dmarc_monitor/models.py parsedmarc_monitor/rootfs/app/dmarc_monitor/db.py parsedmarc_monitor/tests/test_db.py
git commit -m "feat: query delivery detail groups"
```

### Task 2: Read-only Ingress HTTP API

**Files:**
- Create: `parsedmarc_monitor/rootfs/app/dmarc_monitor/web.py`
- Create: `parsedmarc_monitor/tests/test_web.py`

**Interfaces:**
- Consumes: `Database.query_deliveries(...) -> DeliveryPage` from Task 1.
- Produces: `DeliveryQuery`, `parse_delivery_query(query: Mapping[str, list[str]]) -> DeliveryQuery`, `create_handler(database: Database, static_root: Path, allowed_clients: frozenset[str] = frozenset({"172.30.32.2"})) -> type[BaseHTTPRequestHandler]`, and `WebServer(database: Database, host: str = "0.0.0.0", port: int = 8099)` with `start()` and `stop()`.

- [ ] **Step 1: Write failing query parser tests**

Assert exact defaults (`all`, empty search, page 1, page size 50), scalar parsing, duplicate parameter rejection, missing dates, and forwarding of database validation as a concise `QueryError`.

```python
query = parse_delivery_query(parse_qs(
    "date_from=2026-08-24&date_to=2026-08-30&outcome=failed&page=2&page_size=25"
))
assert query == DeliveryQuery("2026-08-24", "2026-08-30", "failed", "", 2, 25)
```

- [ ] **Step 2: Run parser tests RED**

Run: `cd parsedmarc_monitor && python3 -m pytest tests/test_web.py -k query -v`

Expected: FAIL because `dmarc_monitor.web` does not exist.

- [ ] **Step 3: Implement query parsing and JSON serialization**

Create immutable `DeliveryQuery`, a `QueryError(ValueError)`, strict single-value extraction, and `delivery_page_payload(page: DeliveryPage) -> dict[str, object]`. The payload must be:

```python
{
    "api_version": 1,
    "page": page.page,
    "page_size": page.page_size,
    "total": page.total,
    "items": [asdict(item) for item in page.items],
}
```

- [ ] **Step 4: Add failing live HTTP tests**

Start `ThreadingHTTPServer(("127.0.0.1", 0), handler)` with `allowed_clients=frozenset({"127.0.0.1"})`. Test `/api/deliveries`, `/health`, unknown routes, POST rejection, invalid query 400, database exception 500 without exception text, UTF-8 JSON, security headers, and denial when the peer is absent from the allowlist.

Required security headers:

```python
{
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
```

- [ ] **Step 5: Run HTTP tests RED**

Run: `cd parsedmarc_monitor && python3 -m pytest tests/test_web.py -k http -v`

Expected: FAIL because the handler and routes are not implemented.

- [ ] **Step 6: Implement the minimal handler and lifecycle wrapper**

Use `BaseHTTPRequestHandler`, `ThreadingHTTPServer`, `urlsplit`, and `parse_qs`. Return `application/json; charset=utf-8`; log unexpected errors through the module logger; never place exception text in a 500 body. `WebServer.start()` constructs the server and starts one daemon thread; `stop()` calls `shutdown()`, `server_close()`, and joins the thread. Reject a second `start()` and make `stop()` safe before or after normal shutdown.

- [ ] **Step 7: Verify the web module and commit**

Run: `cd parsedmarc_monitor && python3 -m pytest tests/test_web.py -v`

Expected: all web tests PASS.

```bash
git add parsedmarc_monitor/rootfs/app/dmarc_monitor/web.py parsedmarc_monitor/tests/test_web.py
git commit -m "feat: add read-only delivery API"
```

### Task 3: Responsive German delivery view

**Files:**
- Create: `parsedmarc_monitor/rootfs/app/dmarc_monitor/static/index.html`
- Create: `parsedmarc_monitor/rootfs/app/dmarc_monitor/static/app.css`
- Create: `parsedmarc_monitor/rootfs/app/dmarc_monitor/static/app.js`
- Modify: `parsedmarc_monitor/rootfs/app/dmarc_monitor/web.py`
- Modify: `parsedmarc_monitor/tests/test_web.py`

**Interfaces:**
- Consumes: relative `./api/deliveries` JSON API from Task 2.
- Produces: `GET /`, `/app.css`, and `/app.js`; browser functions `localIsoDate(date)`, `defaultRange(now)`, `buildQuery(filters)`, `renderRows(items)`, `renderPagination(payload)`, and `loadDeliveries()`.

- [ ] **Step 1: Add failing static and frontend-contract tests**

Assert the handler serves all three files with correct content types and paths remain relative. Assert HTML contains `date-from`, `date-to`, `outcome`, `search`, `apply-filters`, result table, pagination, loading, empty, and error regions. Assert JavaScript contains `textContent`/`createTextNode`, uses `./api/deliveries`, and contains no `innerHTML`, `insertAdjacentHTML`, or remote asset URL.

- [ ] **Step 2: Run contract tests RED**

Run: `cd parsedmarc_monitor && python3 -m pytest tests/test_web.py -k "static or frontend" -v`

Expected: FAIL because static assets and routes do not exist.

- [ ] **Step 3: Implement semantic HTML and the seven-day default**

Use native date inputs, select, search input, button, table, `<details>`, and accessible status regions. Implement the local-date calculation without UTC conversion:

```javascript
function defaultRange(now = new Date()) {
  const end = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const start = new Date(end);
  start.setDate(start.getDate() - 6);
  return { dateFrom: localIsoDate(start), dateTo: localIsoDate(end) };
}
```

Build every report-controlled cell with `document.createElement` plus `textContent`. Represent missing optional values as `—` and use translated visible labels from one `STRINGS.de` object.

- [ ] **Step 4: Implement filtering, states, details, and pagination**

Apply filters on submit, reset to page 1, disable controls while loading, show the empty state for zero items, and keep the prior filters visible on errors. Add Previous/Next controls from `total`, `page`, and `page_size`. Render failed rows with a dedicated class and order exactly as returned by the API.

- [ ] **Step 5: Add responsive styling and failure emphasis**

Use only local CSS, Home Assistant-compatible system colors, visible focus outlines, status pills that do not rely on color alone, and a breakpoint at `700px` that turns each table row into a labeled stacked card while preserving `<details>`.

- [ ] **Step 6: Serve static files safely**

Map only the three explicit routes to fixed `Path` objects under `static_root`; do not translate arbitrary URL paths into filesystem paths. Allow cacheable static assets but retain `no-store` for HTML and JSON.

- [ ] **Step 7: Run web tests and commit**

Run: `cd parsedmarc_monitor && python3 -m pytest tests/test_web.py -v`

Expected: all web and frontend-contract tests PASS.

```bash
git add parsedmarc_monitor/rootfs/app/dmarc_monitor/static parsedmarc_monitor/rootfs/app/dmarc_monitor/web.py parsedmarc_monitor/tests/test_web.py
git commit -m "feat: add delivery detail interface"
```

### Task 4: Home Assistant Ingress and process lifecycle

**Files:**
- Modify: `parsedmarc_monitor/config.yaml`
- Modify: `parsedmarc_monitor/rootfs/app/dmarc_monitor/main.py`
- Modify: `parsedmarc_monitor/tests/test_integration.py`
- Create: `parsedmarc_monitor/tests/test_app_config.py`

**Interfaces:**
- Consumes: `WebServer(database, host="0.0.0.0", port=8099)` from Task 2.
- Produces: one Ingress panel on port 8099 and coordinated HTTP/MQTT shutdown.

- [ ] **Step 1: Add failing config contract tests**

Parse `config.yaml` and assert:

```python
assert config["ingress"] is True
assert config["ingress_port"] == 8099
assert config["panel_icon"] == "mdi:email-search-outline"
assert config["panel_title"] == "DMARC Zustellungen"
assert "ports" not in config
```

Also assert the Dockerfile copies `rootfs/app`, which includes static assets, and does not add a framework dependency.

- [ ] **Step 2: Run config tests RED, then add Ingress keys**

Run: `cd parsedmarc_monitor && python3 -m pytest tests/test_app_config.py -v`

Expected before change: FAIL on missing Ingress keys. Add exactly the four keys above; keep `panel_admin` at its secure default (`true`) and do not declare a host port.

- [ ] **Step 3: Add failing lifecycle tests**

Extend the existing fake lifecycle test with `FakeWebServer`. Assert startup ordering after database maintenance and before the blocking mailbox runner, and assert `web.stop()` runs in `finally` before `publisher.stop()`. Add a failure case where web startup raises and publisher/web cleanup still occurs.

```python
assert calls == [
    "settings", "mqtt_settings", "reclassify", "maintenance",
    "web_start", "metrics", "runner", "web_stop", "mqtt_stop",
]
```

- [ ] **Step 4: Run lifecycle tests RED**

Run: `cd parsedmarc_monitor && python3 -m pytest tests/test_integration.py -k lifecycle -v`

Expected: FAIL because `main` does not construct `WebServer`.

- [ ] **Step 5: Integrate `WebServer` minimally**

Import `WebServer`, initialize `web_server: WebServer | None = None`, construct it after database initialization/maintenance, call `start()`, then retain existing publisher and runner behavior. In `finally`, stop the web server if constructed, then stop the publisher. Keep sanitized top-level error logging and return code 1 unchanged.

- [ ] **Step 6: Verify config, lifecycle, and full tests; commit**

Run:

```bash
cd parsedmarc_monitor
python3 -m pytest tests/test_app_config.py tests/test_integration.py -v
python3 -m pytest -q
```

Expected: all tests PASS.

```bash
git add parsedmarc_monitor/config.yaml parsedmarc_monitor/rootfs/app/dmarc_monitor/main.py parsedmarc_monitor/tests/test_app_config.py parsedmarc_monitor/tests/test_integration.py
git commit -m "feat: expose delivery view through ingress"
```

### Task 5: Documentation, release metadata, and final verification

**Files:**
- Modify: `README.md`
- Modify: `parsedmarc_monitor/DOCS.md`
- Modify: `parsedmarc_monitor/CHANGELOG.md`
- Modify: `parsedmarc_monitor/config.yaml`
- Modify: `parsedmarc_monitor/rootfs/app/dmarc_monitor/__init__.py`
- Modify: `parsedmarc_monitor/tests/test_app_config.py`

**Interfaces:**
- Consumes: completed query, API, UI, and Ingress behavior.
- Produces: user instructions and consistent release version `0.3.0`.

- [ ] **Step 1: Add failing release consistency assertions**

Extend `test_app_config.py` to load `__version__` and assert both it and `config.yaml` equal `0.3.0`. Assert `DOCS.md` contains `Web UI`, `letzten sieben`, `Von`, `Bis`, `Fehlerhaft`, `Erfolgreich`, `Nachrichtenanzahl`, and an explicit statement that rows are aggregated groups rather than individual emails.

- [ ] **Step 2: Run release tests RED**

Run: `cd parsedmarc_monitor && python3 -m pytest tests/test_app_config.py -v`

Expected: FAIL because the version is 0.2.0 and delivery-view documentation is absent.

- [ ] **Step 3: Update documentation and version**

Add a concise Root README feature note. In `DOCS.md`, document opening the App's `Web UI`, default seven-day range, inclusive dates, outcome/search filters, failure-first ordering, expansion fields, and pagination. Add a `0.3.0` changelog section and update both version sources to `0.3.0`.

- [ ] **Step 4: Run release tests GREEN**

Run: `cd parsedmarc_monitor && python3 -m pytest tests/test_app_config.py -v`

Expected: PASS.

- [ ] **Step 5: Run fresh full verification**

From the repository root run:

```bash
docker build --target test -t parsedmarc-monitor-test:delivery-view ./parsedmarc_monitor
docker run --rm parsedmarc-monitor-test:delivery-view
docker run --rm --entrypoint /opt/venv/bin/ruff parsedmarc-monitor-test:delivery-view --config /pyproject.toml check /app /tests
docker build --target runtime -t parsedmarc-monitor-runtime:delivery-view ./parsedmarc_monitor
git diff --check
```

Expected: test image and runtime image build successfully, the complete pytest suite reports zero failures, Ruff reports `All checks passed!`, and `git diff --check` prints nothing.

- [ ] **Step 6: Review the acceptance criteria manually**

Confirm every spec criterion maps to evidence: Ingress keys/config test; seven-day JavaScript contract; inclusive database tests; outcome tests; prominent failure CSS and expandable details contract; pagination tests; no ports/framework; unchanged MQTT suite.

- [ ] **Step 7: Commit release documentation**

```bash
git add README.md parsedmarc_monitor/DOCS.md parsedmarc_monitor/CHANGELOG.md parsedmarc_monitor/config.yaml parsedmarc_monitor/rootfs/app/dmarc_monitor/__init__.py parsedmarc_monitor/tests/test_app_config.py
git commit -m "docs: release delivery detail view"
```
