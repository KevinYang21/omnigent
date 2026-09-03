#!/usr/bin/env bash
set -euo pipefail

plugin_root="${HERDR_PLUGIN_ROOT:-$(cd "$(dirname "$0")" && pwd)}"
repo_root="$(cd "$plugin_root/../.." && pwd)"

if [[ -n "${OMNIGENT_PICKER_PYTHON:-}" ]]; then
  exec "$OMNIGENT_PICKER_PYTHON" "$plugin_root/session_picker.py"
fi

if [[ -x "$repo_root/.venv/bin/python" ]]; then
  exec "$repo_root/.venv/bin/python" "$plugin_root/session_picker.py"
fi

if command -v uv >/dev/null 2>&1 && [[ -f "$repo_root/pyproject.toml" ]]; then
  exec uv run --no-sync --project "$repo_root" python "$plugin_root/session_picker.py"
fi

echo "Omnigent session picker needs the Omnigent Python 3.12 environment." >&2
echo "Set OMNIGENT_PICKER_PYTHON or link this plugin from an Omnigent checkout." >&2
exit 2
