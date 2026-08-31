#!/usr/bin/with-contenv bashio
set -euo pipefail

mkdir -p /data/runtime
chmod 0700 /data/runtime

export DMARC_MQTT_HOST="$(bashio::services mqtt 'host')"
export DMARC_MQTT_PORT="$(bashio::services mqtt 'port')"
export DMARC_MQTT_USERNAME="$(bashio::services mqtt 'username')"
export DMARC_MQTT_PASSWORD="$(bashio::services mqtt 'password')"

exec python3 -m dmarc_monitor.main
