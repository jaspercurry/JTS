# Barge-in build prompts — per-window execution plan

> **Status: execution artifact (current).** This is the *execution layer* for
> building robust barge-in: the step sequencing plus copy-paste prompts, one per
> fresh agent context window. The **spec** is
> [HANDOFF-barge-in.md](HANDOFF-barge-in.md) (current state, blockers, the
> provider-pack capability seam, the numbered implementation plan); this file
> just tells you how to drive it. Each prompt is self-contained for a fresh
> window and points the agent back at the spec. **Retire this file once barge-in
> has shipped** (through step 7, default-on). Run each window on **Opus 4.8**.

The numbering here matches the "Implementation plan" table in
[HANDOFF-barge-in.md](HANDOFF-barge-in.md).

## Part A — How the work breaks up

Eight steps; only **five are agent-buildable** (1–5). 6–8 are human/hardware/deferred.

| # | Step | What it does | Files (lang) | Depends on | Agent-buildable? | Size |
|---|------|--------------|--------------|------------|------------------|------|
| **1** | Playout ledger → fan-in ack | Make the production TTS flush ack report the real DAC-clock `audio_played_ms` (today the fan-in hardcodes `0`); the true ledger already lives in `jasper-outputd` | `rust/jasper-fanin`, `rust/jasper-outputd` (Rust) | — | yes | M |
| **2** | Core spine + drain-tail | Keep mic live during TTS, local-VAD gate on the AEC leg sets the interrupt event, cover the `wait_drained` window, wire the threshold — behind a default-OFF flag | `voice_daemon.py`, `turn_playback.py`, `config.py` (Py) | — | yes | L |
| **3** | Capability seam | Add `cancel_response` / `truncate_assistant_audio` / `supports_provider_vad` to the Protocol + no-op defaults + capability declaration on the `PROVIDERS` registry | `session.py`, `catalog.py`, 3 adapters (Py) | — | yes | M |
| **4** | OpenAI pack | `response.cancel` + `conversation.item.truncate(ledger ms)` with the no-op-if-`0` guard; the reference pack | `openai_session.py` (Py) | 1, 2, 3 | yes | M |
| **5** | Gemini pack | Obey `interrupted` + drive flush from the local gate; **no** client truncate; resolve the `NO_INTERRUPTION` decision | `gemini_session.py` (Py) | 2, 3 | yes | S–M |
| **6** | Grok verify | Inherit the OpenAI pack; gate truncate best-effort; one **paid** eval trial | `grok_session.py` + tests | 4 | code tiny; needs a **paid trial (you run it)** | S |
| **7** | AEC threshold + default-on | On-Pi capture of TTS-bleed vs real-barge distributions; set threshold; doctor check; flip defaults per profile | measurement + `jasper-doctor` | 2 | no — **hardware (you + the Pi)** | M |
| **8** | Bonded/multiroom barge-in | Member-local TTS flush + the `:9891` reference path in grouped mode | multiroom + outputd | 7 | deferred follow-up | M |

### Sequencing (waves)

- **Wave 1 — parallel-safe (file-disjoint):** **1, 2, 3.** Rust fan-in vs Python
  daemon vs `session.py`+adapters don't overlap, so three windows can run at
  once with minimal conflict.
- **Wave 2 — after 1+2+3 merged:** **4** and **5** (parallel with each other —
  different adapter files).
- **Wave 3:** **6** (after 4; needs your paid trial), **7** (after 2; needs the Pi).
- **Later:** **8** (deferred).

### The key property

Steps 1–6 all ship **default-OFF and additive** — `main` stays shippable after
each merge, and with the flag off the speaker behaves exactly as today. The
*felt* barge-in ("talk over it, it stops") comes alive the moment **Step 2** is
enabled (Step 7), independent of the packs; the packs (4/5) add the
model-context correctness on top.

### Recommended one-at-a-time order

**2 → 3 → 1 → 4 → 5 → (6, 7).** (2 is the keystone and lets you flip the flag in
a dev test to *feel* it; 3 is the seam; 1 is needed before 4; 4 is the primary
provider; then Gemini; then verify/hardware.) Order within {1,2,3} is free —
they're independent.

### Coordination across windows

- One **branch per step** (ideally a separate git **worktree** if you run
  windows concurrently), PR to `main`, rebase before merge.
- Each agent **builds + self-tests + opens a PR, then stops** — it does *not*
  merge and does *not* run a review.
- When a PR is "ready," hand it to a reviewer (the maintainer runs the
  COAH adversarial-review prompt, Opus, read-only) before merge.

## Part B — The prompts (one per window)

Each is self-contained for a fresh context window. Run each in a Claude Code
session set to **Opus 4.8**, rooted in the JTS repo.

### Step 2 — Core spine + drain-tail (do this first)

```
You are a staff-level engineer in the JTS repo (a Raspberry Pi smart speaker:
Python daemons + Rust audio crates). FRESH context — read before coding:
- AGENTS.md (operational rules: surgical changes, test discipline, no silent
  failure paths, AEC topology is fixed, deploy path)
- README.md (architecture)
- docs/HANDOFF-barge-in.md — THIS IS YOUR SPEC. You are implementing PR-2 of its
  "Implementation plan" table. Read the whole doc, especially "The blockers",
  "The provider-pack capability seam", "Config & observability", and "Risks".
- docs/HANDOFF-speaker-output-reference.md + docs/HANDOFF-voice-providers.md
  (the canonical barge-in contract — do not restate it, follow it)

TASK: Add full-duplex barge-in DETECTION + local TTS flush during assistant
playback, behind a per-provider feature flag that DEFAULTS OFF. This is the
provider-agnostic spine; it must NOT do any provider truncate/cancel (that's
later PRs). It delivers the felt experience: user talks over the assistant ->
local TTS stops.

Implement:
1. In jasper/voice_daemon.py, _handle_session_frame early-returns once
   self._input_ended is set (drops mic during playback). Add a branch (e.g.
   _handle_playback_frame) reached when _input_ended is set AND barge-in is
   enabled for the active provider: run local Silero VAD (jasper/vad.py) on the
   AEC-cleaned mic frame; on a sustained speech run >= JASPER_VAD_BARGE_IN_THRESHOLD
   (mirror the existing wake-tail arming constants), set the turn's interrupt
   event (the wait_for_interrupt path that turn_playback._play_responses awaits).
   Detection MUST run INLINE in the frame handler — NOT as a WakeLoop._bg_task:
   _handle_session_frame ends the turn when any _bg_tasks entry completes
   (`if any(t.done() for t in self._bg_tasks): await self._end_turn()`).
2. Fix the drain-tail gap: in jasper/voice/turn_playback.py, _play_responses
   races the interrupt only inside the `async for` chunk loop; extend the race
   to also cover the `await tts.wait_drained()` window (the most common barge-in
   moment for burst-delivery providers).
3. Wire JASPER_VAD_BARGE_IN_THRESHOLD (Config.vad_barge_in_threshold) — currently
   dead config — into the gate. Make its .env.example comment match what the
   code now does.
4. Feature flag: per-provider barge-in enable, DEFAULT OFF. Read it via a fresh
   SSOT reader (jasper/voice/provider_state.py style), NOT a Config.from_env
   cache that a non-restarted daemon (jasper-control/state) would read stale.
5. Feed the AEC-cleaned mic leg ONLY (the leg the live session already uses).
   Never the AEC-OFF leg. This is leg selection, NOT an AEC topology change.

SAFETY / OBSERVABILITY:
- Flag OFF => byte-identical current behavior. Add a pinning test asserting a
  frame arriving after _input_ended is dropped exactly as today.
- event=barge.detected (leg, silero, sustained_ms) via jasper.log_event;
  event=barge.flush_failed (WARN) if the flush errors, then fall through to
  normal turn end (no silent failure).
- Runtime self-interrupt-loop guard: if the gate would trip but no AEC reference
  is available (e.g. direct_mic profile), hard-disable for the session + WARN
  rather than loop.

TESTS (hardware-free pytest, same PR): flag-off pinning test; flag-on +
synthetic high-Silero frames during a fake playback turn => interrupt event
fires and _play_responses flushes, INCLUDING during the drain-tail window.
Run scripts/test-fast, then scripts/test-merge.

DELIVERABLE: feature branch, PR to main (do NOT merge), tests green. Report what
you changed and state the on-Pi validation gap plainly (false-barge from TTS
bleed cannot be validated off-device — that's a later hardware step). Do NOT
enable barge-in by default. Do NOT run any adversarial review. Flag any
uncertainty instead of guessing.
```

### Step 3 — Capability seam

```
You are a staff-level engineer in the JTS repo (Pi smart speaker). FRESH context
— read first: AGENTS.md (esp. the config-ownership doctrine and provider-registry
pattern), README.md, docs/HANDOFF-barge-in.md (YOUR SPEC: implement PR-3 of the
"Implementation plan" table; read "The provider-pack capability seam" section),
and docs/HANDOFF-voice-providers.md (the canonical "Provider Interruption
Contract" — the capability interface is defined there).

TASK: Add the provider-pack capability seam to the voice provider interface,
with safe no-op defaults so behavior is UNCHANGED until later PRs implement the
packs.

Implement:
1. In jasper/voice/session.py, extend the LiveConnection/LiveTurn Protocol with:
   - cancel_response(reason): explicit local/manual cancel of an in-progress
     response.
   - truncate_assistant_audio(provider_item_id, audio_played_ms): align the
     provider's conversation history to what the listener actually heard.
   - supports_provider_vad(): distinct from barge-in support.
   Document each. Keep wait_for_interrupt()/clear_interrupted() as the
   daemon-facing event.
2. Adapters MUST tolerate a missing provider_item_id (Gemini has none; OpenAI
   emits one — see _last_assistant_item_id in openai_session.py).
3. Add a capability DECLARATION to each entry in the PROVIDERS registry
   (jasper/voice/catalog.py) describing its reconciliation kind (e.g.
   needs_client_truncate / server_self_truncates / inherits) so packs branch
   declaratively, not via provider-name if/elif. This is the "pack" metadata
   home, following the transit-registry pattern.
4. Provide no-op / behavior-neutral default implementations in all three
   adapters (openai/gemini/grok) so THIS PR changes no runtime behavior.

CONSTRAINTS: behavior-neutral here (packs fill these in PR-4/5). Do NOT put
per-provider config into the central Config dataclass (AGENTS.md config-ownership
rule).

TESTS (same PR): a session-contract test asserting all three adapters satisfy
the extended Protocol; a test asserting each registry entry has a valid
capability declaration. Run scripts/test-fast, then scripts/test-merge.

DELIVERABLE: feature branch, PR to main (do NOT merge), tests green, report. Do
NOT run any adversarial review. Flag uncertainty instead of guessing.
```

### Step 1 — Playout ledger → fan-in ack

```
You are a staff-level Rust+systems engineer in the JTS repo (Pi smart speaker;
Rust audio crates under rust/). FRESH context — read first: AGENTS.md, README.md,
docs/HANDOFF-speaker-output-reference.md (YOUR SPEC — read "Robust Barge-In
Contract" and the playout-ledger / fan-in-flush sections; note the explicit
statement that the fan-in TTS ack currently reports max_audio_played_ms=0 and
the final playout-ledger slice still needs wiring), and docs/HANDOFF-barge-in.md
(implement PR-1 of its "Implementation plan" table; see blocker #3).

TASK: Make the production TTS flush acknowledgement report the REAL DAC-clock
audio_played_ms instead of 0, so barge-in can truncate provider context by what
actually reached the speaker (not bytes received).

Context to verify by reading the code:
- The fan-in FLUSH_SYNC ack currently hardcodes "max_audio_played_ms":0 and
  "events":[] (look in rust/jasper-fanin/src/tts.rs).
- The honest per-segment ledger (audio_played_ms from the output clock + DAC
  delay) already exists in rust/jasper-outputd (look in its tts.rs).
- Python consumes ack["max_audio_played_ms"], ack["flushed_frames"],
  ack["events"] (jasper/audio_io.py).

DESIGN FORK TO RESOLVE (and flag for review): the fan-in sits BEFORE CamillaDSP;
the true played-ms ledger lives in jasper-outputd AFTER the chain. Determine the
minimal correct way to surface the final playout ledger's audio_played_ms in the
active TTS flush ack (e.g. fan-in querying/receiving it from outputd, or routing
the flush through the path that owns the ledger), per the #532 contract. Pick the
smallest design that satisfies the contract; DOCUMENT the options you considered
and why you chose one, and call it out explicitly in your report for review.

CONSTRAINTS: surgical. Keep the flush EPOCH semantics intact. Keep the ack JSON
shape backward-compatible. Bound any new accounting (no unbounded growth — JTS
runs for months on a 1 GB Pi). Do NOT change audio output behavior, CamillaDSP
config, volume ceilings, the outputd final-output graph, or the fan-in lane
topology.

TESTS: Rust unit test(s) proving the ack reports a nonzero max_audio_played_ms
within one output period of the real played duration for a flush mid-segment
(this is the "Required Tests" barge-in criterion in
HANDOFF-speaker-output-reference.md). Run: cargo fmt --all -- --check, cargo
clippy with -D warnings, and cargo test for the affected crates. Hardware-free.

DELIVERABLE: feature branch, PR to main (do NOT merge), Rust checks green.
Report the design fork you resolved and any on-Pi validation gap. Do NOT run any
adversarial review. Flag uncertainty instead of guessing.
```

### Step 4 — OpenAI pack (after 1, 2, 3 are merged)

```
You are a staff-level engineer in the JTS repo (Pi smart speaker). FRESH context
— read first: AGENTS.md, README.md, docs/HANDOFF-barge-in.md (YOUR SPEC:
implement PR-4, "OpenAI pack", of the "Implementation plan" table — and read the
OpenAI bullet under "Provider packs"), and docs/HANDOFF-voice-providers.md
("Provider Interruption Contract": OpenAI over WebSocket — the server NEVER
auto-truncates; the client must stop playback, measure played ms, and truncate).
PREREQUISITES already merged: PR-1 (real played-ms in the flush ack), PR-2
(barge-in spine sets the interrupt event), PR-3 (capability seam).

TASK: Implement the OpenAI barge-in pack — the reference pack.

Implement in jasper/voice/openai_session.py (+ wiring in the daemon barge-in
path from PR-2):
1. cancel_response(reason) -> existing _cancel_response() (sends response.cancel).
   GUARD: response.cancel errors if there is no active response — only send when
   a response is in progress.
2. truncate_assistant_audio(provider_item_id, audio_played_ms) ->
   conversation.item.truncate {item_id, content_index:0, audio_end_ms}. The
   scaffolding _last_assistant_item_id is already captured; the truncate is
   currently only a comment. audio_end_ms = the played-ms from the PR-1 ledger.
   CRITICAL GUARD (from review): truncate MUST no-op + WARN if the ledger ack
   reports max_audio_played_ms == 0 — NEVER truncate on bytes-received; an
   out-of-range audio_end_ms errors server-side and desyncs context.
3. On a local barge-in for an OpenAI session (PR-2 path): after the local flush,
   call cancel_response then truncate_assistant_audio with the ledger ms.
4. send_audio currently no-ops after _committed. Only relax this for the
   forward-during-playback case IF you choose server-VAD corroboration — and
   keep the _released/_turn_lost guards. Prefer the MINIMAL change: local-VAD +
   cancel/truncate is sufficient for correctness, so you likely do NOT need to
   relax send_audio at all. Flag the decision.
5. supports_provider_vad() -> true.

CONSTRAINTS: default OFF (inherit the PR-2 flag). Surgical. event=barge.truncate
logged with the audio_end_ms used.

TESTS (hardware-free pytest): the audio_end_ms computation; the no-op-if-0 guard;
the cancel-when-no-active-response guard. Also ADD (but do NOT run) a
tests/voice_eval/regression scenario "interrupt mid-TTS (OpenAI)" — voice_eval is
PAID; writing it is required, running it is not (note the cost in your report).
Run scripts/test-fast, then scripts/test-merge.

DELIVERABLE: feature branch, PR to main (do NOT merge), hardware-free tests
green. Report + state the paid-eval / on-device validation gap. Do NOT enable
barge-in by default. Do NOT run any adversarial review. Flag uncertainty.
```

### Step 5 — Gemini pack (after 2, 3 are merged)

```
You are a staff-level engineer in the JTS repo (Pi smart speaker). FRESH context
— read first: AGENTS.md, README.md, docs/HANDOFF-barge-in.md (YOUR SPEC:
implement PR-5, "Gemini pack" — read the Gemini bullet under "Provider packs"),
docs/HANDOFF-voice-providers.md ("Provider Interruption Contract": Gemini's
server self-truncates; the client just obeys `interrupted` and flushes; there is
NO client truncate call), and docs/HANDOFF-persistent-live-session.md (why
NO_INTERRUPTION + manual VAD is the current deliberate choice). PREREQUISITES
already merged: PR-2 (spine), PR-3 (capability seam).

TASK: Implement the Gemini barge-in pack.

Implement in jasper/voice/gemini_session.py:
1. truncate_assistant_audio -> documented NO-OP (Gemini self-truncates server
   side; no item-id truncate exists). cancel_response -> no-op / minimal (server
   self-stops on interrupt).
2. The pack's real job: obey server_content.interrupted (already parsed — it sets
   _interrupt_event) AND set the interrupt event from the LOCAL gate (PR-2) so
   JTS flushes its own TTS regardless (Gemini doesn't know JTS's DAC queue depth).
3. Resolve the NO_INTERRUPTION + automatic_activity_detection.disabled choice in
   _build_config, behind the barge-in flag. Pick ONE and document the rationale:
   (a) keep manual-VAD and drive the flush purely from the local gate
   (RECOMMENDED — single interruption authority, no double-VAD), or (b) enable
   interruptible ActivityHandling. Default behavior with the flag OFF must be
   unchanged.
4. Gotcha: an interrupted Gemini turn sends NO generation_complete (it goes
   interrupted -> turn_complete). Ensure the turn-end logic does not hang waiting
   for generation_complete.
5. supports_provider_vad() per your chosen design.

CONSTRAINTS: default OFF. Surgical. Keep the flag-off path byte-identical.

TESTS (hardware-free pytest): the interrupted-without-generation_complete
turn-end path; the local gate sets the event and the pack stays no-op on
truncate. ADD (but do NOT run) a tests/voice_eval/regression scenario "interrupt
mid-TTS (Gemini)" — voice_eval is PAID (note cost). Run scripts/test-fast then
scripts/test-merge.

DELIVERABLE: feature branch, PR to main (do NOT merge), tests green. Report +
on-device gap. Do NOT enable by default. Do NOT run any adversarial review. Flag
uncertainty.
```

### Steps 6–8 — not agent-build prompts yet

- **Step 6 (Grok verify):** the code is "inherit the OpenAI pack; keep
  `conversation.item.truncate` gated best-effort/tolerant-of-error" — tiny. Its
  real content is a **paid Grok trial (you run it, ~$0.05/min)** confirming
  `response.cancel`/`truncate` behave on the xAI endpoint and that
  `conversation.item.done` absence doesn't strand any state machine. Write the
  small code prompt + the trial checklist once Step 4 is merged.
- **Step 7 (AEC threshold + default-on):** **hardware** — capture TTS-bleed vs
  real-barge Silero distributions on the Pi, per AEC profile, set the threshold
  from data, then flip defaults per profile. No agent can do this (it needs the
  Pi and a human listening). Write an on-device measurement runbook when ready.
- **Step 8 (Bonded/multiroom):** deferred follow-up; prompt it later.

---

Last verified: 2026-06-21 (sequencing + prompts authored against the barge-in
plan in HANDOFF-barge-in.md; this is an execution artifact, retire when barge-in
ships).
