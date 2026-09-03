# Omnigent Sessions for Herdr

This Herdr plugin turns a bounded set of Omnigent sessions into native Herdr
Spaces. Stored history remains virtual in a searchable popup, so a large
Omnigent account does not create a PTY and shell for every session.

Press `Ctrl-B`, then `u` to open the picker. Selecting a session focuses its
existing Space or lazily creates one whose root pane runs `omnigent open`.
Ordinary Herdr Spaces and Omnigent-backed Spaces can be freely interleaved,
split, resized, reordered, and closed.

## Why this is a plugin

Herdr 0.8.2 provides the required extension points: popup panes, actions and
keybindings, workspace creation/focus/rename, and workspace metadata. No Herdr
fork is required. The responsibility split is:

- Herdr owns terminal layout, persistence, and input.
- The plugin owns discovery, filtering, lazy Space materialization, and the
  Omnigent-to-Herdr binding cache.
- `omnigent open` owns runner attachment, wake-up, recovery, and waiting for an
  unavailable host.

The plugin never archives or deletes an Omnigent session and never
automatically closes a Herdr Space.

## Requirements

- Herdr 0.8.2 or newer on Linux or macOS.
- Python 3.12 or newer in an Omnigent development environment.
- An Omnigent executable containing the recovery-aware `omnigent open`
  command.
- Access to the configured Omnigent server and, for new sessions, an online
  host on this machine.

## Install from this checkout

Create the Omnigent environment, link the plugin, and seed its configuration:

```sh
cd ~/git/omnigent-herdr
uv sync --frozen

herdr plugin link "$PWD/integrations/herdr"
herdr plugin config-dir omnigent.sessions
cp integrations/herdr/config.example.json \
  "$(herdr plugin config-dir omnigent.sessions)/config.json"
```

Edit the generated `config.json`. `omnigent` should be an absolute path so a
new Herdr pane does not depend on shell PATH initialization:

```json
{
  "server": "http://127.0.0.1:55777",
  "omnigent": "/Users/you/git/omnigent-herdr/.venv/bin/omnigent",
  "max_sessions": 200,
  "search_debounce_ms": 300
}
```

If a config already exists, keep it instead of copying the example. Omit
`server` to use normal Omnigent CLI server resolution. Set
`OMNIGENT_PICKER_PYTHON` to an explicit Python executable when the plugin is
not linked from an Omnigent checkout with a `.venv`.

Add the picker keybinding to `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+u"
type = "plugin_action"
command = "omnigent.sessions.open-picker"
description = "open Omnigent sessions"
```

Apply a keybinding change to a running server with:

```sh
herdr server reload-config
```

Relinking the same plugin id updates its source path without deleting plugin
config or state. Python and manifest changes are picked up the next time the
popup opens; Herdr does not need to restart.

## Picker workflow

The popup paints its last persisted catalog immediately, then refreshes it in
the background. Typing filters cached results without blanking the list while
a debounced server-side title/content search runs.

- Up/Down or `Ctrl-P` moves the selection.
- Tab/Shift-Tab cycles All, Active, and Pinned.
- Enter focuses or lazily loads the selected session.
- `Ctrl-R` refreshes.
- `Ctrl-N` starts a new Omnigent session.
- Escape closes the popup.

`OPEN` rows already have a Herdr Space; `LOAD` rows remain virtual until they
are selected. Pinned and project names are shown as Omnigent metadata; pin and
project mutation are not yet exposed as picker actions.

`Ctrl-N` opens a cached, searchable native-agent chooser and prefers Codex
when available. Enter advances to the initial-message editor, `Ctrl-J` inserts
a newline, Enter creates and submits, and Escape returns to agent selection
without losing the draft. The invoking pane's directory becomes the new
session workspace.

Creation, first-message delivery, and Space opening are separate failure
boundaries. The plugin creates once and sends once. It never automatically
resends after an ambiguous transport timeout; it can still open the created
session for inspection. A failed Space open retries only the open operation.

The first nonblank prompt line is a provisional Space name. A pane-scoped
supervisor then follows generated or manually edited Omnigent titles and
renames that exact Herdr Space while its TUI is alive.

## Working-set behavior

Herdr's sidebar is backed by real workspaces, and every real workspace owns a
PTY and root shell. Consequently, unloaded Omnigent sessions cannot appear as
sidebar rows for free. The plugin uses two layers:

1. The popup is the complete, bounded discovery catalog.
2. The sidebar contains only sessions explicitly loaded by the user, plus
   normal Herdr workspaces.

Closing an Omnigent Space is local UI state: it does not archive or stop the
Omnigent session, and the synchronizer will not immediately recreate it.
Select it from the picker to load it again.

Catalog snapshots are scoped to the Herdr session and Omnigent server, expire
after seven days, and live under Herdr's private plugin state directory. Agent
snapshots expire after 30 days. Search snippets and workspace paths are not
persisted in the catalog cache.

## Configuration

`config.json` accepts:

| Field | Default | Purpose |
| --- | --- | --- |
| `server` | Omnigent CLI resolution | Server URL used for discovery and title sync. |
| `omnigent` | `OMNIGENT_BIN` or `omnigent` | Executable launched by a Space. Prefer an absolute path. |
| `max_sessions` | `200` | Maximum discovery page size, from 1 to 1000. |
| `search_debounce_ms` | `300` | Server search debounce, from 0 to 5000 ms. |
| `fallback_cwd` | Invoking pane directory | Directory used when a stored workspace is unavailable locally. |
| `state_file` | Herdr plugin state | Optional binding-state override. |
| `catalog_cache_file` | Herdr plugin state | Optional session-cache override. |
| `agent_cache_file` | Herdr plugin state | Optional agent-cache override. |

For a second Herdr session or server, caches and bindings are automatically
separated by the Herdr socket and configured Omnigent origin.

## Optional status rows

The bridge records Omnigent working, waiting, idle, and attention state using
Herdr's native agent status and metadata. To show the exact values in sidebar
rows:

```toml
[ui.sidebar.spaces]
rows = [
  ["state_icon", "workspace"],
  ["$omnigent_status", "$omnigent_agent"],
]
```

## Development checks

Run the focused tests and lint from the repository root:

```sh
uv run --no-sync python -m unittest discover \
  -s integrations/herdr -p 'test_*.py'
uv run --no-sync ruff check integrations/herdr
uv run --no-sync ruff format --check integrations/herdr
```

Previewing a direct projection is also useful when debugging bindings:

```sh
uv run --no-sync python integrations/herdr/session_space_sync.py \
  --herdr "$(command -v herdr)" \
  --server http://127.0.0.1:55777 \
  --omnigent "$PWD/.venv/bin/omnigent" \
  --dry-run
```

The remote access and multi-user control boundaries are documented in
[REMOTE_COLLABORATION.md](REMOTE_COLLABORATION.md).
