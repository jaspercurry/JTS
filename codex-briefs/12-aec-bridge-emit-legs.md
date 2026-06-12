# Brief 12 — aec_bridge.py: one emit path, a leg table, config at startup

Mission: review §5.1/§5.4 + the AEC dive — `_aec_loop` is a ~975-line
function with seven inline copies of its own `emit_packet` helper, a third
unguarded copy of the UDP port map, and import-time env capture that forces
test gymnastics. All changes must be wire-neutral: same bytes to the same
ports in the same order.

Branch: `codex/aec-bridge-emit`. File fence: `jasper/cli/aec_bridge.py`,
`tests/test_aec_bridge_stall.py` (+ new tests), `tests/test_wake_legs.py`
(only if adding the lockstep pin). Do NOT touch deploy/bin/jasper-aec-reconcile
or jasper/wake_legs.py semantics.

## PR 1 — route every leg through `emit_packet`

The helper exists (~line 1691 at review time) but ref/dtln/usb_raw/
usb_webrtc/usb_dtln and two more re-inline the identical
extend/sendto/BlockingIOError/del pattern. Introduce a small `LegEmitter`
dataclass (sock, dest, batch buffer, stats key, optional engine token) and a
list the loop iterates. ~100 lines deleted, one stats path. Per-leg failure
isolation must be preserved exactly (a dead DTLN leg disables only itself —
its test pins this). Acceptance: the leg-emission tests pass unmodified;
add one test asserting every configured leg routes through the shared path
(e.g. a counting stub on emit_packet).

## PR 2 — kill the third port-map copy

`OUT_PORT_*` literals (~lines 179-248) duplicate `jasper/wake_legs.py`
REGISTRY ports, guarded only by comments. Either derive the defaults from
`wake_legs.by_token(...).udp_port` (preferred; the module already imports
wake_legs — verify import cost is acceptable for the bridge daemon) or add a
lockstep test diffing the bridge literals against the registry (the
reconciler already has `tests/test_reconciler_constants_match_python.py` as
the template). Also fix the two misplaced/stale strings: the non-docstring
after `global` (~878) and the mid-function string still describing the
retired Loopback/silence-fallback behavior (~1330-1348) — replace with a
true docstring describing UDP + carry-forward.

## PR 3 — BridgeConfig at startup + warn-flood debounce

1. Module-level env capture (~116-134, 162-248: sweep config, MIC_DEVICE,
   ports frozen at import, logging at import) → a `BridgeConfig` dataclass
   built in `main()`. Then simplify the `importlib.reload` gymnastics the
   stall tests document (~test file lines 76-84) into plain construction.
2. Apply the existing 1 s aggregation pattern (ref threads already do it) to
   the two unbounded warn paths: `chip_aec_primary_missing` (~1950-1954) and
   "mic queue full" (~1103) — both can emit ~50/s during a wedge.
3. OPTIONAL (only if measurement-safe): cache the `resample_poly` filter
   design across 20 ms ref chunks (taps are recomputed per chunk). Output
   bytes must be IDENTICAL — verify with a fixture comparing before/after
   output on a recorded chunk sequence; if not byte-identical, drop this item
   and note why.

Acceptance: `pytest tests/test_aec_bridge_stall.py tests/test_wake_legs.py -q`
green; `ruff check .`; PR bodies state the wire-neutrality argument and what
proves it; docs-impact: HANDOFF-aec routes here — likely no-impact note since
behavior is unchanged, but say so explicitly.
