# Changelog

## 0.4.0

- Added extended aggregate-report details for published policy settings,
  report diagnostics, source metadata, policy overrides, and individual
  DKIM/SPF authentication results.
- Added a schema migration that preserves existing reports while exposing the
  additional details in the Web UI.

## 0.3.0

- Added an ingress-served Home Assistant Web UI for delivery detail groups.
- Added inclusive date-range, outcome, and sender search filters with
  failure-first ordering and pagination.
- Added expandable report, source, authentication, classification, and
  message-count details; rows represent aggregate groups rather than
  individual emails.

## 0.2.0

- Added Home Assistant data-freshness and stale-data entities.
- Added validated report content fingerprints with backward-compatible v1 migration.
- Added ordered SQLite schema migrations and startup reclassification when `known_sources` changes.
- Hardened MQTT startup, reconnect, publish-error, and concurrent retained-state recovery.
- Added reproducible Python 3.14 Docker tests, Ruff checks, and CI.

## 0.1.0

- Initial DMARC aggregate report ingestion via parsedmarc 11.0.0.
- Transactional SQLite persistence with duplicate suppression.
- Known-source classification and latest-period/30-day metrics.
- Home Assistant MQTT device discovery.
