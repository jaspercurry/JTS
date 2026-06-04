# JTS — Staff Engineering Review for Google OSS Ownership

> **Status: historical review.** Snapshot from 2026-05-26 / PR #335.
> **Superseded by [REVIEW-2026-06-04-deep-dive.md](REVIEW-2026-06-04-deep-dive.md)**
> (the current assessment — this snapshot predates the
> `active_speaker`/`output_topology`/DAC8x subsystem, the supply-chain
> provenance rework, the `http_security` guard, and the canonical-UI
> migration). Preserved as a point-in-time OSS-readiness assessment, not
> current operational truth. Current documentation rules live in
> [AGENTS.md#documentation-paradigm](../AGENTS.md#documentation-paradigm);
> the repo doc atlas lives in
> [README.md#documentation-map](../README.md#documentation-map).
>
> **Refresh against main (2026-05-26, `6d6ff52`).** Main now has an
> Apache-2.0 [LICENSE](../LICENSE), [CONTRIBUTING.md](../CONTRIBUTING.md),
> [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md), issue/PR templates,
> pytest CI, many wizard CSRF helpers, and [CLAUDE.md](../CLAUDE.md) as a
> thin import shim. This update intentionally leaves the original review
> body intact; treat stale claims below as review history. Current
> open work is tracked in the living
> [OSS readiness top five](OSS-READINESS-TOP-FIVE.md), with attribution
> details in [LICENSE-third-party.md](../LICENSE-third-party.md).

*A hypothetical assessment, written as if a Google staff software engineer
were evaluating this repository for the company to take on open-source
ownership. Synthesized from a multi-agent code review covering architecture,
build & deploy, tests, docs, Python code quality, OSS readiness, security,
AEC pipeline, embedded firmware, the voice provider abstraction, the tool
system, observability, the web wizard surface, supply chain, and
performance.*

---

## TL;DR — should Google take this on?

**Yes, conditionally.** This is the most well-engineered solo smart-speaker
project I've reviewed. The architectural bones are right (defensible
process boundaries, post-incident-tuned restart semantics, a Protocol-shaped
seam for swappable voice providers, telemetry as a feedback loop on AEC
quality). Documentation discipline is exceptional. Test discipline is real
(1,514 hardware-free tests, bug-driven regression scenarios).

But it is **not currently legally open source** — there is no `LICENSE`
file — and the OSS plumbing, security posture, and supply-chain hygiene are
all hobby-grade. **Inherit + invest 8–12 engineer-weeks** to bring it to
"Google-grade OSS." **Do not rewrite.** Most needed work is consolidation,
not redesign.

---

## Ratings across dimensions

| Dimension | Grade | Justification |
|---|---|---|
| Architecture | **A−** | Right seams (provider abstraction, transit registry, AEC reconciler), defensible transport choices (UDP localhost vs snd-aloop). Hot spots: 3,456-line `voice_daemon.py`, 14k LOC web wizard sprawl, NYC/XVF/Apple-dongle hardcoding. |
| Python code quality | **B** | Above-median style consistency without enforcement, near-universal type hints used as docs, excellent comment discipline. Dragged down by 525-line `WakeLoop.__init__`, 308-line `_aec_loop`, 265-line `SYSTEM_INSTRUCTION`, ~250 LOC duplicated between OpenAI/Gemini adapters, no enforced lint/format/type. |
| Embedded firmware | **B+** | Dial is production-quality (proper quadrature decoding, pinned-core LVGL with mutex, ES8311 init translated from esp-adf with teaching-quality comments). AMOLED satellite is a well-instrumented Phase 1.2 spike. **No OTA on either.** |
| Documentation | **A** | HANDOFF series is gold standard (`HANDOFF-aec.md`, `HANDOFF-airplay.md`); decision-comments cite primary sources; README's documentation map is the right pattern. Weaknesses: CLAUDE.md/AGENTS.md duplication has already caused URL drift (`/ha/` vs `/homeassistant/`), two HANDOFFs read as letters to a Claude session. |
| Test discipline | **B+** | 1,514 hardware-free pytest functions, mocked via `httpx.MockTransport` + `sys.modules` stubs, paid voice-eval harness with strict cost rails. **Zero CI runs them on PR** — the only workflow is a CLAUDE/AGENTS diff. |
| Build & deploy | **B−** | `install.sh` is 1,385 lines with real idempotency, `set -euo pipefail`, restart policies tuned post-incident. No `--dry-run`, no container-testable subset, hardcoded BOM identifiers (Apple dongle, XVF), 1 of 7 download fetches SHA-pinned. |
| Security | **C** | Honest LAN-trust model, good file perms, no `shell=True` injection paths, consistent HTML escaping. Critical gaps: **no auth on `jasper-control:8780` (anonymous LAN POST can reboot the Pi, change Wi-Fi, restart voice)**, no CSRF protection on any wizard, root-running daemons, plain HTTP for HA tokens / Wi-Fi PSKs / API keys, DNS-rebinding exposure. |
| Performance | **A−** | Clean asyncio hygiene, bounded queues with drop-on-full, `call_soon_threadsafe` correctly placed, post-incident UDP-vs-snd-aloop architecture. Profile-first targets: 200 numpy allocs/sec for AEC telemetry, unbounded `_audio_q` during OpenAI turns. |
| Observability | **A−** | `jasper-doctor` (34 checks, `--json`), `/state` fan-out aggregator with parallel fail-soft probes, SQLite wake-funnel telemetry, persistent journal across watchdog resets, audible cues for user-visible failures. Gaps: no metrics export, partial `event=` adoption, minor API-key leakage in `fetch-pi-logs.sh` redaction. |
| Supply chain | **C−** | Only CamillaDSP is SHA-pinned. Unpinned: `pycamilladsp` (mutable git tag), `raspotify.deb`, `nqptp` master HEAD, `jarvis_v2.onnx`, openWakeWord transitive deps via `--no-deps`, all PIO libs. No lock file. |
| OSS plumbing | **F** | **No `LICENSE`** (legally all-rights-reserved), no `CONTRIBUTING.md`, no copyright headers, vendored code without attribution (xvf_host.py from respeaker, SquareLine LVGL assets), no CI for code, no Dependabot, no DCO/CLA. |
| Maintainability (bus factor) | **C** | Single author with 30k+ words of HANDOFF docs and decision-comments shoring up the bus factor — better than typical, but the wake/turn lifecycle in `WakeLoop` requires holding ~3.5kLOC in your head. |
| Extensibility for contributors | **B** | Excellent for narrow extensions (new tool, new transit provider, new wake model, new OpenAI-Realtime-compatible provider in ~150 LOC). Hostile for cross-cutting changes (new wizard URL, non-XVF mic, non-NYC transit baseline). |

---

## What's genuinely good — the strengths worth preserving

1. **Provider abstraction shape.** `LiveConnection` / `LiveTurn` Protocols
   ([jasper/voice/session.py](../jasper/voice/session.py):9-203) are clean
   enough that **Grok lands in 107 lines**
   ([jasper/voice/grok_session.py](../jasper/voice/grok_session.py)) by
   subclassing the OpenAI adapter. The two heavy adapters (Gemini, OpenAI)
   each pin their wire-format contracts in dedicated tests. The tool
   registry's two-serializer design with per-tool provider gating
   (`@tool(providers={"openai"})`) is the right shape.

2. **Post-incident engineering.** Restart policies, watchdog config, and
   transport choices cite PR numbers and incident dates in the source. The
   UDP-localhost replacement for snd-aloop
   ([docs/HANDOFF-resilience.md](HANDOFF-resilience.md)) is a textbook
   case of "diagnose root cause before fix." The wake-event telemetry
   ([jasper/wake_events.py](../jasper/wake_events.py)) doubles as a
   *feedback loop on AEC quality* — `bridge_config_json` + per-leg peak
   scores let you ask "did changing `JASPER_AEC_NS_LEVEL` help?" against
   real user attempts. Most embedded voice projects can't.

3. **The HANDOFF discipline.** [docs/HANDOFF-aec.md](HANDOFF-aec.md)
   (2,372 lines), [docs/HANDOFF-airplay.md](HANDOFF-airplay.md),
   [docs/HANDOFF-voice-providers.md](HANDOFF-voice-providers.md) — every
   nontrivial subsystem has a "what we tried / why it failed / what
   shipped" doc with primary-source citations (XMOS docs, OpenAI cookbook,
   HA core source). New contributors can ramp in days, not weeks.

4. **Decision comments in code.** `jasper/voice_daemon.py:74-100` cites
   OpenAI's Realtime Prompting Guide and explains *why* conditional rules
   replaced absolute prohibitions. `jasper/audio_io.py:24-77` explains why
   `_log_audio_open_failure` dumps PortAudio/aplay/dmesg context on
   failure. `jasper/cli/aec_bridge.py:1-80` is a mini-design-doc as a
   module docstring.

5. **Telemetry-as-policy.** `jasper-doctor` has **34 named checks** with
   `--json` output; `/state` is a parallel-probed fan-out aggregator with
   per-section fail-soft; audible cues for user-visible failures are
   policy ([CLAUDE.md](../CLAUDE.md): "No silent failure paths").
   Persistent journal across watchdog resets (PR #160) means previous-boot
   logs survive wedges.

6. **Test discipline encoded as culture.** [CLAUDE.md](../CLAUDE.md) makes
   "every new tool ships with a voice-eval regression scenario" and "every
   reported bug becomes a regression scenario *before* the fix" explicit
   policy. Voice-eval cost rails are unusually disciplined: per-scenario
   costs in the README, hard rule against `pytest-repeat`, explicit "if
   you're an LLM agent, refuse to loop on failure" guidance.

7. **The AEC pipeline as a publishable reference.** Reference signal via
   `dsnoop` + `plug:` rate-tolerance + L+R sum + speech-band HPF, near-end
   is chip-processed mic with chip AEC disabled, WebRTC AEC3 at 16 kHz
   mono, UDP localhost transport. This is **the common commodity-speaker
   topology** (external DAC, USB mic) that XMOS's chip AEC actually
   doesn't target, and the failure-mode reasoning is documented end-to-end.

---

## What's not good — issues by severity

### Critical (legal/security blockers before any public release)

1. **No `LICENSE` file.** Default copyright law applies — the repo is
   technically not open source. Pick Apache-2.0 (matches most deps) or MIT.
2. **Vendored code without attribution.** `jasper/xvf/xvf_host.py` (from
   respeaker, Apache-2.0), `firmware/dial/src/CST816D.cpp`,
   `firmware/dial/src/assets/*.c` (SquareLine-generated, **commercial-vs-
   personal license tier unverified**). Needs `LICENSE-third-party/` +
   `NOTICE`.
3. **Anonymous LAN POST controls.** `jasper/control/server.py:1097,1124,
   1130` — `/system/reboot`, `/system/restart/voice`, `/aec/toggle`,
   `/cue/play` are unauthenticated, CSRF-vulnerable, root-effecting. Any
   browser tab on the same LAN can reboot the speaker via cross-origin
   POST.
4. **All daemons run as root.** `jasper-voice`, `jasper-control`,
   `jasper-input`, `jasper-web`. An RCE anywhere is full root.
5. **WiFi PSKs / HA tokens / API keys POSTed over plain HTTP** on the LAN.
   Wizards bind 127.0.0.1 but are nginx-proxied with no auth/TLS.

### High (block confident maintenance + contribution)

6. **No CI for code.** The only GitHub Actions workflow diffs CLAUDE.md
   vs AGENTS.md. 1,514 hardware-free tests are contributor-self-policed.
7. **No enforced lint/format/type.** `ruff` is listed in dev deps but has
   no `[tool.ruff]` config; no mypy, no pre-commit. 356 `# noqa`
   annotations and no enforcement.
8. **`jasper/voice_daemon.py` is 3,456 lines, one file.**
   `WakeLoop.__init__` is 525 lines (lines 1202-1727). Owns wake
   detection, peering, turn lifecycle, watchdog, cues, telemetry,
   mic-mute, manual session, timers, and the 265-line `SYSTEM_INSTRUCTION`
   string literal.
9. **Supply chain is loose.** 1 of 7 install-time fetches is SHA-pinned
   (CamillaDSP). No lock file. `openWakeWord --no-deps` with unpinned
   `requests/tqdm/scipy/scikit-learn`. `jarvis_v2.onnx` fetched with no
   SHA verification — model weights for the wake loop.
10. **No software-only dev path.** A first-time contributor needs Pi 5
    + XVF3800 + Apple dongle + amp + speakers to exercise the end-to-end
    loop. The 1,514 hardware-free tests are reasonable but no mock
    harness exists for the wake-loop state machine.

### Medium (refactor work; not blockers but they bite contributors)

11. **~250 LOC duplication between OpenAI and Gemini adapters** —
    `ConnectionState` enum, `_set_state`, `_maybe_fire_escalation_cue`,
    `_supervisor_loop` scaffold, reconnect-loop boilerplate.
    [HANDOFF-voice-providers.md](HANDOFF-voice-providers.md) defends not
    sharing the loop *body*, which is correct — but the scaffolding
    around the body should still be lifted. Template-method refactor in
    `_supervisor.py`.
12. **14k LOC web wizard surface.** Stdlib `http.server` is defensible
    (socket-activation buys ~60-90 MB Pss back on a 2GB Pi), but
    `_make_handler` / `make_server` / argparse boilerplate is copy-pasted
    in 12 files. `_common.py` should expose a `BaseWizardHandler` +
    `register_wizard()`. `correction_setup.py` has 600+ lines of misfiled
    domain logic.
13. **NYC + XVF3800 + Apple dongle hardcoded** through the transit wizard
    renderer, mic profile, and ALSA card-name detection. A Berlin
    contributor lands non-trivially.
14. **The reconciler is in bash with values duplicated from
    `xvf3800.py`** — 385 lines of shell with `ensure_capture_mixer_open`
    hardcoding channel counts and mixer names that live in Python
    elsewhere. Move to Python or add a sync test.
15. **CLAUDE.md / AGENTS.md duplication has already caused drift**
    (`/ha/` vs `/homeassistant/`). Build step or single-source-with-
    prologue would fix this.

### Low (polish)

16. **Per-frame numpy allocations in AEC telemetry** (4 alloc × 50Hz =
    200/s) — gate behind `% 50 == 0`.
17. **Unbounded `_audio_q` during OpenAI turns** — front-load ~500 KB of
    int16 PCM mid-turn; add `maxsize=128`.
18. **API-key leakage in `fetch-pi-logs.sh`** — only `GEMINI_API_KEY` +
    `SPOTIFY_CLIENT_SECRET` are redacted; `OPENAI_API_KEY`, `XAI_API_KEY`,
    `JASPER_HA_TOKEN`, `JASPER_MTA_BUSTIME_KEY` land cleartext on the
    laptop.
19. **No end-user privacy disclosure for wake-event recording.** 500 MB
    ring of WAVs at `/var/lib/jasper/wake-events/`; only developers see
    this in CLAUDE.md.
20. **Schema generator collapses `list`/`dict`/`Literal` to `"string"`**
    silently (`jasper/tools/__init__.py:158-168`) — works for current
    primitive args, will misroute the moment a tool takes a complex type.

---

## Improvement roadmap

Phased so each phase is shippable. Time estimates assume one Google
engineer at SWE-IV level.

### Phase 0 — Legal + safety (~1 week)

Must land before any public commit beyond what's already public.

- Add `LICENSE` (Apache-2.0 recommended; matches `google-genai`, `openai`,
  libwebrtc-audio-processing).
- Add `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`,
  `LICENSE-third-party/` covering `xvf_host.py` (respeaker Apache-2.0),
  LVGL (MIT), openWakeWord (Apache-2.0), SquareLine assets (**verify or
  regenerate**), libwebrtc binding (BSD-3).
- Apply copyright/SPDX headers to source files.
- Verify SquareLine LVGL assets are redistributable; regenerate from
  permissively-licensed source if not.
- Wake-event privacy disclosure panel in the wizards.

### Phase 1 — Security minimum bar (~1.5 weeks)

- `Origin`/`Host` header validation on every state-changing POST in
  `jasper-control` and wizards (defeats DNS rebinding + cross-origin
  CSRF). Critical for the speaker not making "malicious website rebooted
  my speaker" headlines.
- Move `jasper-web` and `jasper-control` off root via dedicated service
  users + a narrow polkit/sudoers allowlist for the specific `systemctl`
  calls they need.
- Self-signed TLS via mkcert for ALL wizards (not just `/correction/`),
  with a one-time trust prompt during install.
- API key + token redaction extended to all current providers in
  `fetch-pi-logs.sh`.

### Phase 2 — CI + tooling enforcement (~1 week)

- GitHub Actions: pytest (hardware-free subset), ruff, mypy on PR. Branch
  protection on main.
- `[tool.ruff]` config + `.pre-commit-config.yaml`. Format the entire
  codebase once; freeze.
- Shellcheck `scripts/` + `deploy/install.sh` in CI.
- Hashed lockfile via `uv lock` or `pip-compile --generate-hashes`;
  `install.sh` consumes it with `--require-hashes`.
- SHA-pin every download in `install.sh` (raspotify .deb, nqptp clone,
  shairport tag, CamillaGUI, openWakeWord models, `jarvis_v2.onnx`).
  Replace `pycamilladsp@v4.0.0` with `@<commit-sha>`.
- Dependabot config.

### Phase 3 — Refactor pass (~2 weeks)

- Decompose `jasper/voice_daemon.py` into `system_instruction.py` (the
  prompt), `wake_loop.py` (state machine), `tts_volume_tracker.py`,
  `control_socket.py`, `cues_coordinator.py`. The 525-line
  `WakeLoop.__init__` becomes a `WakeLoopConfig` dataclass + 5
  collaborator constructors.
- Lift shared scaffolding into `BaseLiveConnection` in
  `jasper/voice/_supervisor.py`: `ConnectionState` enum, `_set_state`,
  `_maybe_fire_escalation_cue`, `_supervisor_loop` skeleton,
  `_reconnect_with_backoff` skeleton. Keep adapter-specific bodies as
  overridable hooks. Adds `runtime_checkable` Protocol conformance test.
- Extract `BaseWizardHandler` + `register_wizard()` into
  `jasper/web/_common.py`. Move `correction_setup.py`'s domain logic into
  `jasper.correction.*`.
- Move `jasper-aec-reconcile` to Python; share constants with
  `jasper/mics/xvf3800.py`.
- Single-source CLAUDE.md/AGENTS.md via build step or template.

### Phase 4 — Software-only dev path (~2 weeks)

- Mock harness for `WakeLoop` so contributors can drive it from a
  synthetic mic + synthetic provider stream on a laptop.
- `Dockerfile.test` that exercises the Python install path against a
  fake-ALSA layer.
- `install.sh --dry-run` mode that prints what it *would* do without
  touching the host.

### Phase 5 — Generalize for community (~2 weeks)

- Lift NYC assumptions out of `transit_setup.py`'s renderer; per-city
  wizard cards already extensible per `jasper.transit.REGISTRY`, but the
  wizard renderer hardcodes provider IDs.
- Replace ALSA card-name string matching with `JASPER_MIC_CARD` /
  `JASPER_DAC_CARD` overrides; auto-detect as fallback.
- Migrate `jasper/control/server.py`'s ad-hoc UDS JSON protocol to a
  documented schema (gRPC overkill for this; protobuf-text or schema'd
  JSON is plenty).
- Engine `Protocol` in front of `_Aec3Engine` so DTLN-aec / Speex /
  future engines are a config switch, not a file edit.

### Phase 6 — OTA + governance (~1 week)

- ESP32 OTA path (`esp_https_ota`) for both satellites.
- `CODEOWNERS`, issue/PR templates, governance doc, Google CLA or DCO
  bot.
- Move `HANDOFF-volume-control-redesign.md`,
  `HANDOFF-persistent-live-session.md`'s "Read this whole doc first"
  prologue, etc. into `docs/historical/`.

**Total**: ~8.5–10 engineer-weeks for a polished v2. Phases 0–2
(~3.5 weeks) get the repo to "legally and operationally publishable."
Phases 3–6 are quality-of-life that make it a healthy community project.

---

## Final verdict

**Take this on.** The author has done the engineering work that's hard to
replicate: real post-incident learning encoded in restart policies and
decision-comments, a Protocol-shaped seam where Google would have asked
for one, telemetry as a feedback loop on the system's hardest problem
(AEC convergence), and an AEC pipeline that's a publishable reference
implementation for "smart speaker with external DAC."

The work that remains is the work that's easy to do but tedious — OSS
plumbing, CI enforcement, security hardening, supply-chain pins, and one
focused refactor of the wake-loop and adapter scaffolding. It's the kind
of work a Google team can land in a quarter and a solo author would never
quite get to.

The risk is that the author's preferred operating mode is "publish
HANDOFF docs and ship features"; community ownership demands a different
cadence (review queues, breaking-change discipline, deprecation
policies). Worth a conversation about governance expectations before the
handoff.

If you don't take it, **somebody else will fork it within a year** — the
AEC pipeline alone, plus the provider abstraction, plus the wake-
telemetry corpus design, is more reference material than any of the
existing OSS smart-speaker projects (Mycroft, Rhasspy, HA Voice PE) put
together.
