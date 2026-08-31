# DMARC Monitor Hardening Design

## Scope

This change hardens report identity, reclassifies stored data when `known_sources` changes, improves MQTT failure handling, exposes data freshness, introduces real schema migrations, and makes repository verification reproducible. It does not change the IMAP acceptance rule: a source message is accepted only after its parsed batch has been committed to SQLite.

## Report validation and identity

Each aggregate report must contain mapping-valued `report_metadata` and `policy_published` sections. The fields `org_name`, `report_id`, `policy_published.domain`, `begin_date`, and `end_date` must be present and non-empty; timestamps must parse successfully and the end timestamp must not precede the beginning.

The report fingerprint will hash a canonical JSON representation of the complete aggregate report. This prevents separate malformed reports with empty identity fields from collapsing onto one fingerprint and distinguishes reports whose nominal metadata is reused but whose records differ. Exact redeliveries remain idempotent.

Invalid reports fail the whole SQLite transaction and cause the mailbox save callback to return false, preserving the source message for investigation or retry.

## Stored-source reclassification

`known_source` and `known_source_name` remain stored on each aggregate row so metric queries stay simple and fast. On startup, after schema migration and before the first metrics snapshot, all stored rows are re-evaluated against the current ordered `known_sources` rules in one transaction. This guarantees that configuration changes immediately affect both latest-period and rolling metrics.

The reclassification operation reports how many rows changed. It is idempotent and does not rewrite rows whose classification is already current.

## Schema migrations

Database initialization creates the current schema directly for a new database. Existing databases read `meta.schema_version` and apply ordered, transactional migrations until they reach the current version. Unknown newer versions and malformed version values are rejected rather than overwritten.

Schema version 2 establishes the migration framework and ingestion freshness metadata contract. Migration code is structured as an explicit version-to-version registry so later migrations can be added without changing initialization semantics.

## Ingestion freshness

After a batch has committed successfully, including an idempotent duplicate batch, the database records `last_successful_ingestion_ts` in `meta`. A failed persistence attempt must not advance it.

Metrics expose:

- `last_successful_ingestion`: UTC ISO-8601 timestamp or null;
- `report_age_hours`: age of the newest report interval end, rounded to two decimals, or null;
- `data_stale`: true when no successful ingestion exists, no report exists, or the newest report interval ended more than 48 hours ago.

Home Assistant receives one timestamp sensor, one duration sensor, and one problem-class binary sensor for these values. The existing actionable DMARC problem sensor remains based only on `known_fail` and `unknown_pass` in the latest period.

## MQTT reliability

All publisher state shared between the mailbox thread and Paho callback thread is protected by a re-entrant lock. Publish calls inspect Paho's returned reason code; non-success results are logged and mark the publisher disconnected so later snapshots remain cached for reconnect.

`start()` remains idempotent while a client loop is active. If client construction or startup raises synchronously, partial client resources are cleaned up and a later `start()` call performs a fresh attempt. The application periodically asks the publisher to ensure it is started during the existing mailbox retry loop, so a one-off startup failure does not permanently disable MQTT.

MQTT remains downstream of SQLite: MQTT errors never cause a committed report to be rejected.

## Build, CI, and repository hygiene

The Docker test stage sets its own import path and invokes the virtual-environment Python executable directly, making `docker build --target test` followed by the image's default command sufficient to run tests.

`.dockerignore` excludes Git metadata, IDE state, Python caches, test caches, local databases, and Windows alternate-stream artifacts. `.gitignore` excludes the equivalent local artifacts without hiding source or fixtures.

GitHub Actions builds and runs the Docker test target and runs Ruff against application and test code. Ruff is pinned in `requirements-dev.txt`, and its configuration lives in `pyproject.toml`.

## Testing

Tests cover:

- rejection of missing or malformed report identity fields;
- distinct fingerprints for reports with reused metadata but different records;
- idempotence for exact redelivery;
- historical reclassification after rule changes;
- migration from schema version 1, rejection of unknown versions, and new-database initialization;
- freshness timestamps, age calculation, and the 48-hour stale boundary;
- MQTT publish return-code failures, retryable startup, and thread-safe state transitions;
- Home Assistant discovery and payload additions;
- Docker test-stage execution and runtime image build.

All existing tests must continue to pass. Existing user-owned working-tree changes are preserved and excluded from commits made for this work.
