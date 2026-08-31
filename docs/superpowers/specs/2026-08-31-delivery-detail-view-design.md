# DMARC Delivery Detail View Design

## Goal

Add a Home Assistant integrated detail view for retained DMARC aggregate delivery groups. The view opens through Home Assistant Ingress, defaults to the last seven calendar days, and supports an inclusive user-selected date range. Failures must be easy to diagnose while successful groups remain inspectable.

DMARC aggregate reports do not identify individual emails. One displayed row therefore represents one aggregate delivery group and shows its `message_count`.

## Scope

The feature includes a read-only Ingress UI, date/outcome/text filters, server-side pagination, expandable diagnostics, documentation, and automated tests. It does not alter ingestion, retention, classification, MQTT identifiers, or MQTT payloads. Editing, deletion, export, and claims about individual messages are out of scope.

## Home Assistant Integration

The add-on declares Ingress support and an internal HTTP port. Home Assistant proxies and authenticates the view, so no host port or separate login is added.

The web server starts beside the mailbox runner and MQTT publisher. They share the `Database` object, whose methods use short-lived SQLite connections. Shutdown stops the HTTP server before process exit. Failure to bind its internal port is a startup failure.

The documentation explains how to open the add-on Web UI. No custom card, HACS dependency, manually exposed URL, or separate dashboard installation is required.

## User Interface

The initial range covers seven calendar dates: today minus six days through today, using the browser's local date. Both bounds are inclusive.

The filter bar provides:

- `Von` and `Bis` date inputs;
- `Alle`, `Fehlerhaft`, and `Erfolgreich` outcomes;
- optional search across source IP, reverse DNS, known source, header-from, and envelope-from;
- an apply action.

The result table shows interval/date, header-from domain, source IP/reverse DNS, known source, message count, and DMARC/SPF/DKIM results. Failed groups are visually prominent. Mixed results order failed groups first, then interval end descending, then stable row ID descending.

Expanding a row shows reporting organization, report ID, policy domain, exact interval, envelope-from, disposition, alignment results, source name, ASN/AS name, country, base domain, and classification. Missing optional values render as an em dash.

The page has loading, empty, validation-error, and server-error states. On narrow screens the row becomes a compact stacked summary with the same detail disclosure.

## Data Query

The existing `reports` and `aggregate_rows` tables contain all required data; no migration is needed.

`Database` gains a read-only query accepting inclusive ISO dates, outcome (`all`, `failed`, `passed`), optional search text, one-based page, and bounded page size. Dates must be real calendar dates and `date_from` must not exceed `date_to`. Page size defaults to 50 and is capped at 100.

The parameterized SQL joins aggregate rows to reports and returns the page plus total matching group count. Outcome uses `dmarc_pass`; message totals never act as row counts. Search is case-insensitive and literal, with SQL wildcard characters escaped.

## HTTP API

The surface is deliberately small:

- `GET /` serves the bundled application;
- `GET /api/deliveries` returns versioned JSON with filters, pagination, and rows;
- `GET /health` returns minimal diagnostic health.

Relative asset and API URLs preserve Home Assistant's Ingress prefix. The read-only API exposes no credentials, raw emails, or unbounded raw reports.

Invalid parameters return HTTP 400 with a concise machine-readable error. Unexpected failures are logged without sensitive configuration and return a generic HTTP 500 response.

SQLite reads retain the existing busy timeout and short connections, allowing them to coexist with ingestion writes in WAL mode. The UI holds no transaction while a user interacts.

The server binds only to the internal add-on interface. Responses specify UTF-8 and conservative security headers. Browser code inserts API values as text rather than HTML.

## Packaging

Use Python's standard-library HTTP server plus bundled static files; no web framework is needed for this read-only UI. Handler, parsing, and lifecycle live in a focused module. Static HTML/CSS/JavaScript lives under the application package and is copied by the existing image build.

The initial UI language is German. Labels stay in a small mapping so English can be added without changing the API.

## Testing

Database tests cover inclusive boundaries, outcomes, literal case-insensitive search, stable failure/newest ordering, totals, pagination, limits, and invalid ranges.

HTTP tests cover HTML/JSON success, relative paths, validation, generic failures, security headers, and absence of secrets.

Frontend contract tests cover required controls, columns, details, states, default seven-day calculation, and safe text rendering. The full existing test suite and lint checks must continue to pass.

## Documentation and Acceptance

Documentation explains opening the add-on Web UI and that rows are aggregate groups rather than individual emails.

The feature is accepted when:

1. the view opens inside Home Assistant through the add-on Web UI;
2. its initial controls cover the last seven calendar days;
3. changing `Von` and `Bis` reloads the inclusive range;
4. all, successful, and failed groups are selectable;
5. failures are prominent and diagnostic fields expand;
6. large result sets use server-side pagination;
7. no external port, login, HACS component, or custom integration is required;
8. ingestion and MQTT remain compatible and all checks pass.
