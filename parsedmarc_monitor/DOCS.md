# DMARC Monitor documentation

## Overview

DMARC Monitor turns DMARC aggregate (`rua`) report emails into Home Assistant entities. It is designed for operators who already run their own mail domain and want visibility into legitimate senders, authentication failures, and spoofing activity without operating Elasticsearch/OpenSearch.

Data flow:

```text
DMARC reporting organizations
          |
          v
Dedicated IMAP mailbox
          |
          v
      parsedmarc
          |
          | save_callback
          v
    SQLite transaction
          |
          +---- success ----> archive source mail
          |
          v
  Metrics + MQTT Discovery
          |
          v
    Home Assistant
```

The SQLite commit is the acceptance gate. MQTT is not part of that gate: if the broker is temporarily offline, reports are still durably stored and the source email can be archived after the database commit. State is republished when MQTT reconnects or Home Assistant announces its MQTT birth message.

## Mailbox recommendation

Create a dedicated mailbox such as `dmarc@example.at` and point the domain's DMARC aggregate-report destination (`rua`) to that address. Keeping DMARC reports separate from normal mail makes archiving, permissions, and retention predictable.

Use IMAPS (`imap_ssl: true`, normally port `993`) with normal certificate validation. `imap_skip_certificate_verification: true` should only be used to diagnose or temporarily work around a private PKI problem because it disables server-certificate verification.

> **Python 3.14 / unencrypted IMAP note:** parsedmarc currently tracks an upstream IMAPClient issue affecting `ssl = False` on Python 3.14. IMAPS is the supported and recommended configuration for this App.

## App options

| Option | Default | Meaning |
| --- | --- | --- |
| `imap_host` | required | Hostname or IP address of the IMAP server. |
| `imap_port` | `993` | IMAP server port. Use `993` for normal IMAPS. |
| `imap_ssl` | `true` | Establish an encrypted SSL/TLS IMAP connection. |
| `imap_skip_certificate_verification` | `false` | Disable IMAP certificate validation. Reduces security and is not recommended. |
| `imap_username` | required | Username for the dedicated DMARC mailbox. |
| `imap_password` | required | Password for the dedicated DMARC mailbox. |
| `reports_folder` | `INBOX` | Folder scanned initially and then watched for reports. |
| `archive_folder` | `Archive` | Destination folder for source mails after successful database persistence. |
| `check_timeout` | `60` | IMAP operation/watch timeout in seconds. Allowed range: 10–3600. |
| `retention_days` | `90` | Number of days kept in the App-local SQLite database. Allowed range: 1–3650. |
| `known_sources` | `[]` | Rules identifying legitimate sending infrastructure by CIDR and/or reverse-DNS suffix. |
| `log_level` | `info` | One of `debug`, `info`, `warning`, `error`. |

### `known_sources`

Each rule needs a `name` and at least one matcher. If both `cidr` and `rdns_suffix` are present, either matcher is sufficient. Rules are evaluated in order and the first matching rule supplies the source name.

CIDR example:

```yaml
known_sources:
  - name: Company mail gateway
    cidr: 203.0.113.0/24
```

Reverse-DNS example:

```yaml
known_sources:
  - name: Newsletter provider
    rdns_suffix: mail.provider.example
```

Combined example:

```yaml
known_sources:
  - name: Microsoft 365 outbound
    cidr: 192.0.2.0/24
    rdns_suffix: protection.outlook.com
  - name: Company MX
    cidr: 2001:db8:1200::/48
```

Use the real ranges/suffixes documented for your sending infrastructure. Do not add a source merely because it appears in a report; verify that it legitimately sends mail for your domain first.

## Classification model

Every aggregate row receives one of four classifications:

| Classification | Meaning | Typical interpretation |
| --- | --- | --- |
| `known_pass` | Source matches `known_sources` and DMARC passes. | Expected legitimate mail. |
| `known_fail` | Source matches `known_sources` but DMARC fails. | Likely SPF/DKIM/alignment/configuration problem on legitimate infrastructure. |
| `unknown_pass` | Source is not known but DMARC passes. | Investigate: legitimate infrastructure may be missing from `known_sources`, or an unexpected sender can authenticate as the domain. |
| `unknown_fail` | Source is unknown and DMARC fails. | Commonly spoofing/abuse that DMARC is meant to detect/reject. |

`binary_sensor.dmarc_problem` turns on when the **latest report period** contains at least one `known_fail` or `unknown_pass`. Ordinary `unknown_fail` spoofing does not by itself turn on the problem sensor.

## Why the entities say `latest_period` instead of `today`

DMARC aggregate reports describe a reporting interval chosen by the receiving organization and normally arrive after that interval has ended. A report received today may therefore describe yesterday or another completed period. Calling those counts “today” would be misleading.

The App uses the latest report date present in SQLite for the `latest_period` sensors and separately calculates rolling 30-day counts from report dates.

## Home Assistant entities

MQTT Discovery creates one **DMARC Monitor** device with these sixteen entities:

| Entity | Meaning |
| --- | --- |
| `sensor.dmarc_latest_report_date` | Newest report date represented in SQLite. |
| `sensor.dmarc_messages_latest_period` | Total messages in the newest report period. |
| `sensor.dmarc_pass_latest_period` | DMARC-passing messages in the newest report period. |
| `sensor.dmarc_fail_latest_period` | DMARC-failing messages in the newest report period. |
| `sensor.dmarc_pass_rate_latest_period` | Weighted DMARC pass percentage for the newest report period. |
| `sensor.dmarc_messages_30d` | Total messages across the rolling 30-day report-date window. |
| `sensor.dmarc_pass_rate_30d` | Weighted DMARC pass percentage across the rolling 30-day window. |
| `sensor.dmarc_known_fail_30d` | Messages from known sources that failed DMARC in the rolling 30-day window. |
| `sensor.dmarc_unknown_pass_30d` | Messages from unknown sources that passed DMARC in the rolling 30-day window. |
| `sensor.dmarc_unknown_fail_30d` | Messages from unknown sources that failed DMARC in the rolling 30-day window. |
| `sensor.dmarc_last_report` | Timestamp of the most recently represented report. |
| `sensor.dmarc_last_reporting_org` | Organization that issued the most recent stored report. |
| `sensor.dmarc_last_successful_ingestion` | Timestamp when the App last successfully persisted a report batch. |
| `sensor.dmarc_report_age_hours` | Age in hours of the newest report's reporting-interval end; its Home Assistant state is `unknown` when no report end is available. |
| `binary_sensor.dmarc_data_stale` | On when there is no successful ingestion, no report end time, or the newest report ended more than 48 hours ago. |
| `binary_sensor.dmarc_problem` | On for actionable latest-period `known_fail`/`unknown_pass` conditions. |

`binary_sensor.dmarc_data_stale` turns on when no successful ingestion has been recorded, no report end is available, or the newest reporting interval ended **more than** 48 hours ago. At exactly 48 hours it remains off. `sensor.dmarc_last_successful_ingestion` is a UTC timestamp, and `sensor.dmarc_report_age_hours` is a Home Assistant duration sensor in hours.

The problem binary sensor exposes bounded diagnostic attributes including up to five problem sources plus `imap_ok`, `storage_ok`, and the last bounded storage error.

## Delivery detail Web UI

Open the App's **Web UI** button in Home Assistant to inspect the stored
delivery details. By default, the view loads the last seven calendar dates
(die **letzten sieben** Kalendertage), including today. The date fields **Von**
and **Bis** are inclusive, so a row whose report date equals either boundary is
included.
The initial range is computed from the browser's local calendar date: it covers
today plus the preceding six local calendar dates, inclusive. Auf Deutsch: Der
Anfangszeitraum richtet sich nach dem lokalen Kalenderdatum des Browsers und
umfasst heute sowie die sechs unmittelbar davorliegenden lokalen Kalendertage;
beide Grenzen sind eingeschlossen.

Use the outcome filter to show all rows, **Fehlerhaft** rows, or **Erfolgreich**
rows. The search field matches sender IP, reverse DNS, known-source name,
Header-From, and Envelope-From.
Failed groups are ordered first, then by interval end descending, then stable
row ID descending. Each summary row shows the report date and interval,
`Header-From`, source IP and reverse DNS, known-source name,
`Nachrichtenanzahl`, and DMARC/SPF/DKIM results. Expand a row to inspect the
reporting organization, report ID, policy domain, exact interval, Envelope-From,
disposition, alignment results, source name, ASN/AS name, country, base domain,
and source classification. Pagination keeps larger result sets manageable; use
the previous/next controls to move through the matching rows.

Rows are **aggregierte Gruppen und keine einzelnen E-Mails**: one row is a
summary for a source and reporting interval, and its message count represents
the number of messages in that aggregate group.

## Existing reports on first start

At startup the App first scans the configured `reports_folder` before entering continuous watch mode. Existing DMARC reports in `INBOX` are therefore imported on the first successful connection.

The ingestion flow uses a parsedmarc `save_callback`:

1. parsedmarc downloads and parses a batch;
2. DMARC Monitor starts a SQLite transaction;
3. the normalized report and rows are committed;
4. the callback returns success;
5. parsedmarc can then perform its normal move to `archive_folder`.

If SQLite persistence fails, the callback returns failure. The App configures a very high unsaved-retry limit (`2147483647`) so a temporary storage problem does not normally cause parsedmarc to give up and archive/delete an unpersisted source message.

Before a batch is accepted, every aggregate report must provide mapping-valued `report_metadata` and `policy_published` sections, non-empty organization, report-ID, and policy-domain values, parseable begin/end timestamps, and an end timestamp that is not earlier than its begin timestamp. An invalid report rolls back the whole batch, so the source message remains available for retry or investigation.

Duplicate report deliveries/retries are suppressed by a stable SHA-256 fingerprint of the canonical complete report content. Exact redeliveries therefore do not double the statistics, while reports that reuse the same identity metadata but have different contents remain distinct.

## Startup reclassification and database migrations

On startup, the App initializes the SQLite schema and applies ordered transactional migrations to the current schema version. A new database is created at the current version; an existing database is migrated in order. Missing, malformed, or newer-than-supported schema versions are rejected rather than overwritten.

Immediately after initialization and before daily maintenance or the first metrics snapshot, stored aggregate rows are re-evaluated using the current ordered `known_sources` rules. This makes configuration changes take effect for both latest-period and rolling metrics without re-importing reports. Rows that are already correctly classified are not rewritten.

## Storage and retention

The database is stored at:

```text
/data/dmarc.sqlite3
```

`/data` is the private writable data area of the Home Assistant App and is included in App backups. The database file is created with private file permissions. Daily maintenance removes reports whose reporting interval ended before the configured `retention_days` cutoff.

Stored fields contain normalized DMARC metadata such as report organization/domain, source IP, reverse DNS, message count, DMARC alignment/result, classification, and known-source name. IMAP passwords and MQTT credentials are **not** database columns.

## MQTT behavior

The App declares `mqtt:need` in the Home Assistant App manifest. At runtime it asks the Supervisor service API for the broker host, port, username, and password. You do not configure MQTT credentials in the App options.

Discovery/state topics:

```text
homeassistant/device/dmarc_monitor/config
dmarc_monitor/status
dmarc_monitor/state
dmarc_monitor/diagnostics
```

Discovery, state, diagnostics, and availability are retained with QoS 1. A synchronous MQTT client construction or startup failure is retried by `ensure_started()` during mailbox retry cycles. After an established client goes offline or a publish fails, the most recent SQLite-derived snapshot remains cached; Paho reconnect and its `on_connect` callback republish it, as does the Home Assistant MQTT `online` birth message. MQTT errors never reject a batch that SQLite has committed.

## Local installation on Home Assistant OS

Until this App is published in a public custom repository, install it as a Local App:

1. Copy the complete `parsedmarc_monitor` directory to `/addons/parsedmarc_monitor` on Home Assistant OS. Samba, SSH, or Filebrowser can be used to place the directory there.
2. Open **Settings → Apps → App Store** and refresh/reload Local Apps.
3. Open **DMARC Monitor** and build/install it.
4. Configure the IMAP options and, optionally, `known_sources`.
5. Start the App. `boot: auto` makes it start automatically on subsequent host boots. Existing stored reports are reclassified against the configured rules at this point.
6. Check the App logs and then **Settings → Devices & services → MQTT** for the **DMARC Monitor** device and its sixteen entities, including the freshness and data-stale indicators.

### Later: custom repository

Once this project is published at a real Git repository URL, that URL can be added as a custom App repository and normal repository-based installs/updates can be used. This v0.3.0 package intentionally does not claim a repository URL that does not yet exist.

## Troubleshooting

### IMAP connection/TLS failure

- Verify `imap_host`, `imap_port`, username, and password.
- Prefer `imap_ssl: true` with port 993.
- Verify the server certificate chain and hostname before considering `imap_skip_certificate_verification`.
- If `imap_skip_certificate_verification: true` is required for a private CA, install/fix the trust chain instead where possible.
- The App stays alive on IMAP failures and retries with exponential delays starting at 5 seconds and capped at 300 seconds.

### `imap_ssl: false` does not work

Use IMAPS. The target image uses Python 3.14, and parsedmarc currently tracks an upstream IMAPClient issue for unencrypted `ssl=False` on Python 3.14.

### Invalid CIDR

A malformed `known_sources[].cidr` is rejected at startup. Use IPv4 or IPv6 CIDR syntax, for example `203.0.113.0/24` or `2001:db8::/32`.

### SQLite/storage error

When the database cannot accept a parsed batch, `storage_ok` becomes false and the save callback returns false. The source message is therefore not normally moved to the archive. Fix storage/filesystem availability; the mailbox runner will be able to process the report again. Do not delete the source message merely to clear the error.

### MQTT unavailable

MQTT is downstream of SQLite. A broker outage does not invalidate an already committed report. The App caches/rebuilds state and republishes discovery/state after MQTT reconnects. Confirm that the Mosquitto App/service is running and the Home Assistant MQTT integration is loaded.

### DMARC problem sensor is on

Inspect the binary sensor attributes and distinguish:

- `known_fail`: fix SPF, DKIM, or DMARC alignment on a legitimate sender;
- `unknown_pass`: verify whether this is a legitimate sender missing from `known_sources`; if not, investigate how it obtained aligned SPF/DKIM authorization.

`unknown_fail` counts are still useful for observing spoofing volume but do not alone make the actionable problem sensor turn on.

## Privacy

DMARC aggregate reports contain operational metadata about mail flows. This App keeps normalized source IP/domain/report metadata in local SQLite and publishes only bounded summary/diagnostic data to the local MQTT broker. It does not persist IMAP or MQTT credentials in SQLite, and it does not store raw email bodies as part of the database schema.
