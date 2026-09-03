# Remote sessions and collaboration

The architecture can support remote work without moving the Herdr layer onto
the Omnigent host. A Herdr Space can run a local `omnigent` client while the
session's runner and native TUI remain on another machine. Omnigent already
proxies the terminal WebSocket through its server/runner tunnel.

There are two distinct cases:

1. **Your session on a remote host:** interactive terminal attachment works
   today when the client can authenticate to the Omnigent server.
2. **Another user's shared session:** transcript collaboration works today,
   but the current Herdr plugin does not yet select the correct collaborator
   mode. It always invokes the owner-oriented `omnigent open` path.

## What Omnigent supports today

| Capability | Minimum access | Current behavior |
| --- | --- | --- |
| Discover and read a shared session | Read | `GET /v1/sessions` is filtered to sessions accessible by the authenticated caller; snapshots and the SSE stream require read access. |
| See collaborators | Read | The session SSE stream publishes join, leave, and idle presence for viewers across the session tree. |
| Send attributed messages | Edit | `POST /v1/sessions/{id}/events` requires edit access and derives `created_by` from the authenticated caller. Multiple clients can co-drive through the transcript. |
| Watch a native terminal | Read | The terminal WebSocket accepts `?read_only=true`; input is dropped by both the server and runner. The browser already uses this mode. |
| Type directly into a native TUI | Owner | A writable terminal attach requires owner access. Raw PTY bytes cannot carry a collaborator identity, so non-owner input would otherwise be recorded as the owner. |
| Recover a stopped runner | Edit | `retry_session` is sent through the session event endpoint. Recovery is single-flighted per session. |
| Recover an offline host | Depends on host | A live external host can relaunch its runner. A resumable managed host can be reprovisioned under the same host identity. An offline external laptop must reconnect; the session is not migrated to another user's machine. |

The access levels are read (1), edit (2), manage (3), and owner (4). A server
can globally disable sharing, cap grants at read-only, prohibit public links,
or prohibit sharing sessions rooted at a home/root directory. The session
owner grant is protected from replacement or revocation.

Relevant implementation boundaries are:

- `omnigent/server/routes/sessions/routes_core.py` for permission-filtered
  discovery, snapshots, watch updates, and per-viewer metadata.
- `omnigent/server/routes/sessions/routes_events.py` for edit-authorized,
  attributed input and single-flight recovery.
- `omnigent/server/routes/terminal_attach.py` for proxied terminal attachment,
  read-only enforcement, and owner-only writable input.
- `omnigent/cli.py` for `attach` and the recovery-aware `open` command.

## What the Herdr plugin does today

The plugin accepts a remote `server` URL and reuses the normal Omnigent CLI
credential chain. Discovery therefore includes owned and shared sessions that
the caller can read. Selecting any row currently launches:

```text
omnigent open <session-id> --server <server-url>
```

That is exactly right for an owned session. It recovers the existing runner,
waits for the original external host when necessary, and attaches the native
TUI over the terminal WebSocket. The process remains local to the Herdr pane;
the agent process and files remain on the bound Omnigent host.

For a non-owner shared session, the current behavior is incomplete:

- A read-only collaborator cannot send the recovery event.
- An edit/manage collaborator can send transcript input and request recovery,
  but cannot open a writable raw terminal.
- The CLI has no recovery-aware collaborator command or read-only native
  terminal flag, so the plugin cannot yet make a deliberate mode choice.

The picker should expose that distinction instead of treating an authorization
failure as a disconnected session.

## Recommended next slice

Add the authenticated viewer's `permission_level` to the plugin record and
materialize one of three explicit Space modes:

| Picker mode | Access | Space command | Interaction |
| --- | --- | --- | --- |
| Open | Owner | `omnigent open` | Full native TUI control and recovery. |
| Join | Edit/manage | New recovery-aware transcript command | Read terminal output, send attributed chat messages, resolve only authorized actions. |
| View | Read | New read-only command | Transcript plus read-only terminal when it is live; no wake or input. |

`Join` should build on the existing `run_attach` transcript client and the
`retry_session` primitive, not synthesize terminal keystrokes. `View` should
reuse session snapshot/SSE and request terminal attachment with
`read_only=true`. Both commands should be first-class Omnigent primitives so
Herdr remains a thin presentation plugin rather than duplicating auth,
recovery, and permission logic.

This slice is comparatively small: the server already has the ACL, presence,
attribution, terminal proxy, and recovery machinery. Most work is CLI UX,
surfacing `permission_level` in the bridge, choosing a launcher, and adding a
mode badge to the picker.

## If simultaneous native control is desired

Multiple writable clients can technically feed one tmux-backed PTY when they
authenticate as the owner, but there is no control arbitration. Keystrokes can
interleave, clients can fight over terminal size, and approvals or elicitation
answers have no reliable raw-input attribution. This should not be presented
as safe collaboration.

A production multi-user control design needs an explicit terminal-control
lease:

- one controller identity at a time;
- request, accept, revoke, disconnect expiry, and an owner override;
- server-side enforcement on every input frame;
- a single resize authority while the lease is held;
- presence and controller state shown in both Omnigent and Herdr;
- audit events for control changes and a clear rule for approvals.

That is a larger security and protocol feature. Transcript co-driving covers
most collaboration use cases with correct attribution and should ship first.

## Herdr's SSH remote mode

Herdr itself also supports `herdr --remote <ssh-host>`. That attaches to one
Herdr runtime on a Linux/macOS host and is useful when the whole terminal
workspace should live beside the code. It is complementary, not a replacement
for Omnigent sharing:

- Herdr remote uses SSH/OS access rather than Omnigent session grants.
- Collaborators attached to the same Herdr runtime share layout and terminal
  input without Omnigent identity-level control arbitration.
- A local Herdr runtime plus the Omnigent plugin gives each user a personal
  layout while Omnigent remains the shared, permissioned session plane.

For team collaboration, the last model is the safer default.
