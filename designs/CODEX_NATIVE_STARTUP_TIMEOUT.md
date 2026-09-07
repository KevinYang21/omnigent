# Codex-native startup-timeout bridge

Runner-owned Codex sessions use a small file protocol when an explicit
`codex-native.command` wraps the Codex TUI. The wrapper may perform setup before
Codex emits `thread/started`, so the ordinary direct-launch budget is too short.

The runner writes `startup_timeout.json` in the session's Codex bridge directory:

```json
{"timeout_seconds": 120.0}
```

The value is launch policy, not proof that startup completed. The bridge accepts
only finite, positive values no greater than
`CODEX_NATIVE_CONFIGURED_COMMAND_STARTUP_TIMEOUT_SECONDS` (120 seconds).
Malformed or oversized values fail closed to the executor's legacy 60 polls.

Direct launches use `CODEX_NATIVE_DIRECT_THREAD_START_TIMEOUT_SECONDS` (30
seconds). The executor converts a valid marker into 125 one-second polls: the
advertised 120-second runner watchdog plus
`CODEX_NATIVE_STARTUP_PUBLICATION_GRACE_SECONDS` (5 seconds) for the runner to
publish either `state.json` or `startup_error.json`. If the marker appears after
polling begins, the executor may grow its budget but never shrink it.

`clear_bridge_state` removes the marker before each new app-server launch. A
fresh configured-command launch writes it after the terminal resource exists.
It then remains beside state or startup error until the next launch, so teardown
from an older forwarder cannot erase a newer launch's policy.

Known-thread resumes do not write the marker. They preload the thread and publish
`state.json` before starting the configured terminal command, so the executor has
current bridge state and does not enter the startup polling path.

If marker publication fails, terminal startup continues and the executor retains
the legacy wait. Direct Codex launches and login-required launches never publish
the marker.
