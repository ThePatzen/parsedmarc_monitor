# DMARC Monitor

DMARC Monitor reads aggregate DMARC reports from a dedicated IMAP mailbox, parses them with `parsedmarc`, stores normalized data in a local SQLite database, and creates a **DMARC Monitor** device in Home Assistant through MQTT Discovery.

You need:

- a dedicated mailbox such as `dmarc@example.at` that receives DMARC aggregate reports,
- access to that mailbox over IMAP/IMAPS,
- the Home Assistant Mosquitto/MQTT service.

The App automatically obtains its MQTT service credentials from Home Assistant. No MQTT username/password is entered in the App configuration.

Parsed report metadata is retained locally in `/data/dmarc.sqlite3`; the default retention is **90 days**. Source report emails are moved from the configured report folder to the configured archive folder only after SQLite accepts the parsed batch.

See the **Documentation** tab for the full option and entity reference.
