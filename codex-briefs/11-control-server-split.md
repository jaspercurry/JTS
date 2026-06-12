# Brief 11 — control/server.py: fixes, then the five-module split

Mission: review §5.1/§5.5 + the #650-era findings — jasper/control/server.py
is 3,085+ lines mixing five concerns, and carries three real defects. This
file is the HOTTEST in the repo (multiroom lands here weekly): pick a quiet
window, rebase before every push, keep PRs small. Re-locate all sites by
symbol; review line numbers are stale.

Branch: `codex/control-server`. File fence: `jasper/control/` (new modules),
`jasper/control/server.py`, `tests/test_control_*.py`. Do not touch
`jasper/web/` or `jasper/multiroom/`.

## PR 1 — defects (each with a test)

1. **BT lock wedge:** `_source_availability_lock` is held across
   `sources_setup._gather_state`, whose `_bt_state` awaits bluez with no
   timeout — a wedged bluez blocks every `/source/*` worker thread. Wrap the
   probe in `asyncio.wait_for` (bounded, fail-soft to None section) or
   compute outside the lock and swap the cache under it.
2. **Spotify router construction duplicated ~30 lines** (two near-verbatim
   blocks incl. the hardcoded redirect URI). One helper; hoist the
   redirect-URI default to a constant (or import the config default).
3. **No SIGTERM cleanup; dead peering-shutdown path.** `loop.run_forever()`
   never returns so the `daemon.stop()` finally is unreachable; SIGTERM kills
   the thread before the mDNS goodbye. Install a SIGTERM handler
   (`loop.call_soon_threadsafe(loop.stop)` + `server.shutdown()`), or delete
   the misleading finally and document why.
4. **Env read-modify-write without flock:** `_atomic_rewrite_env` claims
   concurrency safety it doesn't have. Brief 08's merged work (#642) added a
   locked read-modify-write helper (check `jasper/atomic_io.py` /
   wake-env PR) — REUSE it for the AEC env writers; soften the docstring to
   what's guaranteed.
5. **Repeated 5-branch except ladders** (six near-identical
   FileNotFoundError/OSError/Timeout→503, Exception→502 blocks) → one
   `_voice_cmd_or_error` helper; pick ONE `_read_json` convention for all
   POST handlers.

## PR 2 (+3 if needed) — the mechanical split

Move, don't rewrite: `control/aec_endpoints.py` (AEC env state + writers),
`control/uds.py` (UDS client helpers), `control/state_aggregate.py`
(`_get_state` + per-section probes), `control/volume_ops.py`,
`control/dial.py` (heartbeat + UDP listener). `server.py` keeps the Handler,
the `_GET_ROUTES`/`_POST_ROUTES` tables, guards, and `main()`. Preserve the
route-table greppability contract — `tests/test_control_client.py`
regex-parses the tables; keep them in server.py and keep the literals
intact. Preserve all `event=` log tokens and the guard-before-route ordering
(its tests must pass unmodified).

Acceptance: `pytest tests/test_control_server.py tests/test_control_aec_state.py
tests/test_control_client.py -q` green with NO behavioral test edits in PR 2
(fixture import paths may change); `ruff check .`; docs-impact note
(doc-map routes this file to resilience + management docs — say what changed
or why no doc impact); split diff is verifiably move-only.
