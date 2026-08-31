# DMARC Monitor Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make report ingestion identity-safe, configuration-aware, freshness-observable, MQTT-resilient, migration-safe, and reproducibly verified.

**Architecture:** SQLite remains the durable acceptance boundary. Database initialization owns ordered schema migration and source reclassification, metrics derive freshness from persisted report and ingestion timestamps, and MQTT remains a failure-tolerant projection protected across its caller and callback threads.

**Tech Stack:** Python 3.14, SQLite, parsedmarc 11, Paho MQTT 2, pytest 9, Ruff, Docker, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-31-hardening-design.md`

## Global Constraints

- Preserve the rule that only a successful SQLite commit accepts a mailbox batch.
- MQTT failures must never reject a committed report.
- Existing user-owned staged Zone.Identifier deletions and untracked IDE files must not enter implementation commits.
- Data is stale when no successful ingestion exists, no report exists, or newest report end is more than 48 hours old.
- Follow red-green-refactor for every production behavior change.
- Run tests through the Docker test image targeting Python 3.14.

---

### Task 1: Reproducible test and lint pipeline

**Files:**
- Create: `.gitignore`
- Create: `.dockerignore`
- Create: `.github/workflows/ci.yml`
- Create: `pyproject.toml`
- Modify: `Dockerfile`
- Modify: `requirements-dev.txt`

**Interfaces:**
- Produces: Docker test image whose default command collects `dmarc_monitor`; `ruff check rootfs/app tests`.

- [ ] **Step 1: Capture the failing Docker test behavior**

Run: `docker build --target test -t parsedmarc-monitor-test:red . && docker run --rm parsedmarc-monitor-test:red`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'dmarc_monitor'`.

- [ ] **Step 2: Correct the test-stage environment and command**

Add to the test stage:

```dockerfile
ENV PYTHONPATH="/app"
CMD ["/opt/venv/bin/python3", "-m", "pytest", "/tests", "-q"]
```

Pin `ruff` in `requirements-dev.txt`, configure Python 3.14 and focused rule sets in `pyproject.toml`, and add ignores for Git, IDE, cache, database, coverage, and `*:Zone.Identifier` artifacts. CI must build/run the test target and execute Ruff inside a deterministic Python 3.14 environment.

- [ ] **Step 3: Verify the build and lint contract**

Run: `docker build --target test -t parsedmarc-monitor-test:task1 . && docker run --rm parsedmarc-monitor-test:task1`

Expected: PASS, 45 existing tests.

Run: `docker run --rm --entrypoint /opt/venv/bin/ruff parsedmarc-monitor-test:task1 check /app /tests`

Expected: PASS after correcting only relevant lint findings.

- [ ] **Step 4: Commit only Task 1 files**

Commit message: `build: make test and lint pipeline reproducible`.

### Task 2: Versioned schema initialization and migration

**Files:**
- Modify: `rootfs/app/dmarc_monitor/db.py`
- Modify: `tests/test_db.py`

**Interfaces:**
- Produces: `CURRENT_SCHEMA_VERSION = 2`; `Database.initialize() -> None` that creates current schema or migrates versions in order.

- [ ] **Step 1: Write failing migration tests**

Add tests that manually create a version-1 database, call `initialize()`, and assert schema version `2`; add malformed (`"abc"`) and future (`"999"`) schema-version cases that raise `RuntimeError`. Update the new-database assertion to expect `2`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run inside the task-1 image with the workspace mounted: `pytest /workspace/tests/test_db.py -q`.

Expected: FAIL because initialization currently overwrites every version with `1`.

- [ ] **Step 3: Implement ordered migrations**

Split schema creation into a current-schema script and introduce:

```python
CURRENT_SCHEMA_VERSION = 2
Migration = Callable[[sqlite3.Connection], None]
MIGRATIONS: dict[int, Migration] = {1: _migrate_v1_to_v2}
```

Within one initialization transaction, detect whether `meta` exists, create current schema for a new database, otherwise parse and validate the stored version and apply every migration in order. Never overwrite a future or malformed version.

- [ ] **Step 4: Verify GREEN and regression safety**

Run: `pytest /workspace/tests/test_db.py -q`.
Run: `pytest /workspace/tests -q`.

Expected: all pass.

- [ ] **Step 5: Commit Task 2**

Commit message: `feat: add ordered database migrations`.

### Task 3: Validated report identity and historical reclassification

**Files:**
- Modify: `rootfs/app/dmarc_monitor/db.py`
- Modify: `rootfs/app/dmarc_monitor/main.py`
- Modify: `tests/test_db.py`
- Modify: `tests/test_integration.py`

**Interfaces:**
- Produces: `Database.reclassify_sources(rules: Sequence[KnownSourceRule]) -> int`.
- Consumes: existing `match_known_source(source_ip, reverse_dns, rules) -> str | None`.

- [ ] **Step 1: Write failing report-validation and fingerprint tests**

Test missing/non-mapping metadata and policy, empty required identity strings, unparsable timestamps, and end-before-begin. Test two reports with identical identity metadata but different records insert separately, while exact redelivery remains idempotent.

- [ ] **Step 2: Verify validation tests fail for the expected behavior**

Run: `pytest /workspace/tests/test_db.py -q`.

Expected: at least reused-metadata content is incorrectly treated as a duplicate and missing fields are accepted too far into persistence.

- [ ] **Step 3: Implement validation and content identity**

Create private helpers that require mappings and non-empty strings, validate both interval timestamps, and hash canonical JSON of the complete report with sorted keys and compact separators. Call validation before inserting a report. Preserve transaction rollback on any invalid member of a batch.

- [ ] **Step 4: Write and verify failing reclassification tests**

Persist rows with no rules, call the wished-for `reclassify_sources()` with a matching CIDR, and assert counts move from `unknown_pass` to `known_pass`, changed count is correct, and a second call returns zero. Add a main lifecycle assertion that reclassification occurs before maintenance and the initial metrics build.

- [ ] **Step 5: Implement transactional reclassification**

Read row id, source IP, reverse DNS, and current stored classification; calculate current rule matches; update only changed rows in a single transaction and return the number changed. Invoke it immediately after `initialize()`.

- [ ] **Step 6: Verify Task 3**

Run: `pytest /workspace/tests/test_db.py /workspace/tests/test_integration.py -q`.
Run: `pytest /workspace/tests -q`.

Expected: all pass.

- [ ] **Step 7: Commit Task 3**

Commit message: `feat: validate report identity and reclassify history`.

### Task 4: Persisted ingestion freshness and metrics

**Files:**
- Modify: `rootfs/app/dmarc_monitor/models.py`
- Modify: `rootfs/app/dmarc_monitor/db.py`
- Modify: `rootfs/app/dmarc_monitor/metrics.py`
- Modify: `tests/test_db.py`
- Modify: `tests/test_metrics.py`
- Modify: `tests/test_mailbox.py`

**Interfaces:**
- Produces: `Database.latest_report_end_ts() -> int | None`.
- Produces: `Database.last_successful_ingestion() -> str | None`.
- Extends `MetricsSnapshot` with `last_successful_ingestion: str | None`, `report_age_hours: float | None`, and `data_stale: bool`.

- [ ] **Step 1: Write failing persistence tests**

Assert successful new and duplicate batches set `last_successful_ingestion_ts` to the supplied `received_at`; an invalid batch rolls back without advancing it. Assert the newest report end timestamp is queryable.

- [ ] **Step 2: Verify RED**

Run: `pytest /workspace/tests/test_db.py -q`.

Expected: missing freshness accessors/metadata.

- [ ] **Step 3: Persist freshness atomically**

Upsert `last_successful_ingestion_ts` within the same transaction as batch persistence, after all reports validate. Store an integer epoch and expose a UTC `YYYY-MM-DDTHH:MM:SSZ` accessor.

- [ ] **Step 4: Write failing metric-boundary tests**

At exactly 48 hours, assert `data_stale is False`; one second later assert true. Assert no report or no successful ingestion is stale and nullable values are returned appropriately. Assert age is rounded to two decimals.

- [ ] **Step 5: Implement freshness metrics**

Calculate age against `latest_report_end_ts()` using the normalized UTC `now`. Populate the extended immutable snapshot without changing the existing problem calculation.

- [ ] **Step 6: Verify Task 4**

Run: `pytest /workspace/tests/test_db.py /workspace/tests/test_metrics.py /workspace/tests/test_mailbox.py -q`.
Run: `pytest /workspace/tests -q`.

Expected: all pass.

- [ ] **Step 7: Commit Task 4**

Commit message: `feat: expose persisted ingestion freshness`.

### Task 5: Home Assistant freshness entities

**Files:**
- Modify: `rootfs/app/dmarc_monitor/mqtt.py`
- Modify: `tests/test_mqtt.py`
- Modify: `DOCS.md`
- Modify: `translations/de.yaml`
- Modify: `translations/en.yaml`

**Interfaces:**
- Consumes: the three new `MetricsSnapshot` freshness fields.
- Produces: state keys with the same names and three discovery components with stable unique IDs.

- [ ] **Step 1: Extend snapshot fixtures and write failing payload tests**

Assert state contains all three freshness values. Assert discovery contains a timestamp sensor, an hours duration sensor, and a `problem` device-class binary sensor using `data_stale`.

- [ ] **Step 2: Verify RED**

Run: `pytest /workspace/tests/test_mqtt.py -q`.

Expected: missing state keys and components.

- [ ] **Step 3: Implement payloads and discovery**

Add nullable templates for the timestamp and number values, add a duration sensor with unit `h`, and add a binary sensor whose ON/OFF template is driven by `data_stale`. Update documentation/entity counts and both translations.

- [ ] **Step 4: Verify Task 5**

Run: `pytest /workspace/tests/test_mqtt.py -q`.
Run: `pytest /workspace/tests -q`.

Expected: all pass.

- [ ] **Step 5: Commit Task 5**

Commit message: `feat: publish data freshness to Home Assistant`.

### Task 6: MQTT publish failures, concurrency, and startup recovery

**Files:**
- Modify: `rootfs/app/dmarc_monitor/mqtt.py`
- Modify: `rootfs/app/dmarc_monitor/mailbox.py`
- Modify: `tests/test_mqtt.py`
- Modify: `tests/test_mailbox.py`

**Interfaces:**
- Produces: `MqttPublisher.ensure_started() -> None` as a non-throwing retry entry point.
- Preserves: `publish_snapshot()`, `set_imap_ok()`, and `set_storage_health()` never propagate broker errors to ingestion.

- [ ] **Step 1: Write failing publish-return-code tests**

Make `FakeClient.publish()` return nonzero `rc`; assert the publisher becomes disconnected and the newest snapshot remains cached for a later reconnect. Add a factory that fails once, then succeeds, and assert a second start attempt creates a working client.

- [ ] **Step 2: Verify RED**

Run: `pytest /workspace/tests/test_mqtt.py -q`.

Expected: nonzero return code is ignored and retry behavior is incomplete.

- [ ] **Step 3: Implement locked state and return-code handling**

Create `threading.RLock` in the publisher. Guard reads/writes of client, connected, started, latest snapshot, and health. Make safe publish return a boolean, accept Paho success code zero, log other codes, and atomically mark disconnected. Clean up partial startup without leaving `_started` true.

- [ ] **Step 4: Write failing mailbox recovery test**

Use a publisher recording `ensure_started()` and assert each outer mailbox-loop attempt asks MQTT to recover before connecting IMAP, without changing save-callback acceptance.

- [ ] **Step 5: Implement recovery hook**

Call `publisher.ensure_started()` at the top of each mailbox-loop iteration. Keep it non-throwing and idempotent.

- [ ] **Step 6: Verify Task 6**

Run: `pytest /workspace/tests/test_mqtt.py /workspace/tests/test_mailbox.py -q`.
Run: `pytest /workspace/tests -q`.

Expected: all pass.

- [ ] **Step 7: Commit Task 6**

Commit message: `fix: recover MQTT publishing failures`.

### Task 7: Final documentation and release verification

**Files:**
- Modify: `README.md`
- Modify: `DOCS.md`
- Modify: `CHANGELOG.md` if user-facing changes are not already recorded.

**Interfaces:**
- Documents: validation behavior, reclassification semantics, freshness threshold/entities, migration behavior, and MQTT recovery.

- [ ] **Step 1: Reconcile documentation against the implemented interfaces**

Update entity totals and operational explanations. Do not document planned names that differ from the tested implementation.

- [ ] **Step 2: Run complete verification**

Run: `docker build --target test -t parsedmarc-monitor-test:final .`
Run: `docker run --rm parsedmarc-monitor-test:final`
Run: `docker run --rm --entrypoint /opt/venv/bin/ruff parsedmarc-monitor-test:final check /app /tests`
Run: `docker build --target runtime -t parsedmarc-monitor:final .`

Expected: every command exits zero with no test or lint failures.

- [ ] **Step 3: Inspect final scope**

Run: `git diff --check HEAD~7..HEAD`.
Run: `git status --short`.
Run: `git log --oneline --decorate -8`.

Confirm the implementation commits exclude pre-existing user-owned files and every approved item maps to a completed task.

- [ ] **Step 4: Commit documentation if changed after prior tasks**

Commit message: `docs: document monitor hardening`.

