# Brief 15 — Extract the shared loudness engine (the outputd "dead layer" is alive)

Mission UPDATE — read this first: the 2026-06-12 review (§5.2, at commit
6772b81a) recommended deleting ~2k LOC of "stranded" outputd modules. A
re-diagnosis on 2026-06-12 evening (post jasper-tts-protocol refactor, PRs
~#624-#628) found that layer is now **fully live**: OutputCore + ledger +
loudness + mixer + reference are called from outputd's bonded-member TTS path
(main.rs run_alsa TTS branch) and the solo path keeps the reference fanout.
**Do NOT delete anything.** The remaining debt is duplication: outputd's
`loudness.rs` (~562 lines) and fanin's (~512) are near-verbatim twins of the
K-weighted loudness engine, while the wire protocol + AssistantProfile/
SegmentKind already moved to the shared `jasper-tts-protocol` crate.

Priority: cleanup-grade (lowest in wave 2). The twins WILL drift — this repo
has a proven clone-then-drift failure mode — but nothing is broken today.

Branch: `codex/loudness-extract`. File fence: `rust/jasper-tts-protocol/**`,
`rust/jasper-fanin/src/loudness.rs` (+ its importers in that crate),
`rust/jasper-outputd/src/loudness.rs` (+ its importers), the crates'
Cargo.toml/lock, `tests/test_wire_contracts.py` +
`tests/test_rust_runtime_panic_freedom.py` only if module paths force it.

## One PR

1. Diff the two loudness.rs files first and characterize the delta (they
   were "near-verbatim" — find what genuinely differs and whether it's
   playout-model-specific or drift). Anything playout-specific stays in the
   daemon; the K-weighted filter, state machine, gain-decision logic, and
   `AssistantGainDecision`/`AssistantLoudnessConfig` types move into
   `jasper-tts-protocol` (e.g. a `loudness` module).
2. Swap both daemons to import the shared engine. Behavior must be
   bit-identical: port both crates' existing loudness unit tests to the
   shared module, and add one cross-daemon parity test (same input sequence →
   same gain decisions) so future drift is structurally impossible.
3. Keep `cargo test --locked` green in all three crates (CI builds them; the
   new rust-cache makes this cheap). Run the panic-freedom ratchet and wire
   contracts pytest files — update allowlist paths only if modules moved.
4. Coordination: multiroom/dumb-follower work (#661 era) touches outputd
   weekly — rebase before push; if `git log --oneline -5 -- rust/` shows
   active churn in these files the same day, hold and note it in the PR.

Acceptance: cargo tests green ×3 crates; parity test exists and passes;
`pytest tests/test_wire_contracts.py tests/test_rust_runtime_panic_freedom.py -q`
green; PR body includes the before/after LOC and the characterized delta
between the former twins; docs-impact: HANDOFF-fan-in-daemon +
HANDOFF-speaker-output-reference route here — one-line note that the loudness
engine is now shared.
