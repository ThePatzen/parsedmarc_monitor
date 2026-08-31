# Changelog

## 0.1.0

- Initial DMARC aggregate report ingestion via parsedmarc 11.0.0.
- Transactional SQLite persistence with duplicate suppression.
- Known-source classification and latest-period/30-day metrics.
- Home Assistant MQTT device discovery, including data-freshness and stale-data entities.
- Validated report content fingerprints, ordered SQLite schema migrations, and startup reclassification of retained reports when `known_sources` changes.
- MQTT publisher recovery after transient startup or publish failures without affecting committed report ingestion.
