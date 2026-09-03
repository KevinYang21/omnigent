#!/usr/bin/env bash
set -euo pipefail

herdr_bin="${HERDR_BIN_PATH:-herdr}"
plugin_id="${HERDR_PLUGIN_ID:-omnigent.sessions}"

exec "$herdr_bin" plugin pane open \
  --plugin "$plugin_id" \
  --entrypoint session-picker \
  --focus
