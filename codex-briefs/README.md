# Codex work briefs — 2026-06-12 review remediation

Source of truth for findings: `docs/REVIEW-2026-06-12-oss-due-diligence.md`
(on main). Each brief below = one Codex agent session = 1–3 small PRs.

**Lifecycle:** this directory was committed temporarily so every agent could
read its brief from main, regardless of where it ran. It is now mostly a status
ledger and review-archaeology bundle. It remains exempt from the orphan-doc
sweep (which globs only root and top-level `docs/*.md`). Delete the whole
directory in a final cleanup PR once the partial items below are closed or moved
into canonical living docs.

## Ground rules for every agent (paste happens via the brief files; AGENTS.md
covers the rest and Codex loads it automatically)

- Branch: `codex/<brief-slug>`. Rebase on `origin/main` before push AND before
  merge — main moves multiple PRs/hour.
- Small, independently-mergeable PRs. Never bundle two briefs.
- Local preflight before every push: `ruff check .` + the targeted pytest files
  named in the brief. Never run `tests/voice_eval/` (paid), never deploy,
  never touch a Pi.
- Stay inside the brief's file fence. If a fix seems to require an out-of-fence
  edit, stop and note it in the PR description instead.
- Every behavior change lands with a test in the same PR (repo rule: pin
  promises with tests). Every PR body includes the documentation-impact note.
- Before opening each PR, run the self-review prompt in
  `codex-briefs/REVIEW-PROMPT.md` against your session's work and fix what it
  finds. The reviewer applies the same prompt, so passing it honestly first
  saves a round-trip.
- Findings cite `main @ 6772b81a`; line numbers may have drifted. Re-locate by
  symbol/grep, and re-verify each claim against current code before fixing
  (several subsystems — fanin/outputd TTS — changed on 2026-06-12).

## Wave 1 — run in parallel (disjoint file fences)

| Brief | Theme | Fence (primary files) |
|---|---|---|
| 01-licensing-redistribution | Replace/clear uncleared vendored code+assets | firmware/dial/*, LICENSE-third-party.md, NOTICE, jasper/xvf/, jasper/aec_engines/dtln_models.py |
| 02-privacy | PRIVACY.md + transcript/payload logging + mute gaps | PRIVACY.md (new), jasper/voice/openai_session.py, jasper/tools/__init__.py, jasper/cli/wake_enroll.py, jasper/web/google_setup.py, jasper/web/wake_corpus_setup.py |
| 03-contributor-front-door | Working quick start, dep markers, broken CLI | CONTRIBUTING.md, pyproject.toml, uv.lock (new), jasper/cli/spotify_auth.py, new tests |
| 04-lan-trust-security | SECURITY.md threat model, BT pairable floor, wizard GET guard | SECURITY.md, deploy/configure-bluez.sh, jasper/web/* (GET guard only), jasper/http_security.py |
| 05-ci-and-repo-mechanics | Workflow hardening + CODEOWNERS/CHANGELOG | .github/workflows/tests.yml, .github/CODEOWNERS (new), CHANGELOG.md (new) |
| 06-doc-drift | The verified stale-doc list | AGENTS.md, README.md, BRINGUP.md, QUICKSTART.md, docs/HANDOFF-aec.md, docs/HANDOFF-xvf3800.md — NOT CONTRIBUTING/SECURITY |
| 07-defects-resilience | Duck-stuck, shairport gate, DAC reboot, sshd probe, epoch gate | jasper/control/*, rust/jasper-fanin + jasper-outputd (re-diagnose first), deploy/systemd/jasper-camilla.service |
| 08-defects-misc | Small confirmed Python defects | jasper/config.py, jasper/control/server.py (two functions only), deploy/bin/jasper-bootloop-guard + siblings, tests/voice_eval/harness.py+tts.py, jasper/web/wake_setup.py |
| 09-dead-code-python | Delete legacy web primitives + dead voice session layer | jasper/web/_common.py (legacy block only), jasper/voice/session.py, jasper/voice/gemini_session.py, jasper/wake.py, their tests |

Known benign overlaps (git merges cleanly, different regions): 02 and 06 both
add a line to README; 04 and 09 both touch `_common.py` (04 adds a guard
helper near the top, 09 deletes the legacy block). Rebase order doesn't matter.

## Wave 1 status — COMPLETE 2026-06-12

All nine briefs filed, reviewed (22-agent merge-gate pass + adversarial
verification), reworked where needed, and merged: 17 PRs on main, plus the
lan-trust stack (652/653/654), the two resilience reworks (647/650), and the
XVF rewrite (656) in final re-review/sign-off at the time of writing.

## Wave 2 status — verified 2026-06-19

Do not assign these briefs blindly from the original sequencing notes. Most of
wave 2 has landed on main; use this table to route only the remaining work.

| Brief | Current status | Evidence / remaining work |
|---|---|---|
| 10-voice-daemon | Landed; residual hotspot | Defect coverage and protocol tests landed. Prompt text, earcons, turn playback, and daemon entrypoint code now live in `jasper/voice/prompt.py`, `jasper/voice/earcons.py`, `jasper/voice/turn_playback.py`, and `jasper/voice/daemon_main.py`; `WakeLoop.for_tests` exists. `jasper/voice_daemon.py` is still large, so future cleanup belongs in the OSS hotspot register rather than this brief. |
| 11-control-server-split | Landed; residual hotspot | `jasper/control/aec_endpoints.py`, `uds.py`, `state_aggregate.py`, `volume_ops.py`, and `dial.py` exist, with route tables still centralized in `server.py`. Future endpoint churn should keep shrinking `server.py` locally. |
| 12-aec-bridge-emit-legs | Landed | `BridgeConfig`, wake-leg-derived default ports, shared `emit_packet`, `LegEmitter`, and regression tests are present. |
| 13-sound-profile-js | Partial | The rehearsal state-loss fix, sequence guards, `patchActiveSpeaker`, and `active-speaker-ui.js` extraction landed. `main.js` is still a large module and no per-probe `Promise.allSettled` degradation was found, so active-speaker JS cleanup remains live work. |
| 14-install-sh-split | Landed; residual installer debt | Model staging moved into `jasper/model_downloads.py`, and installer helpers now live under `deploy/lib/install/`. `deploy/install.sh` remains large, so future work is incremental installer-hotspot cleanup. |
| 15-outputd-loudness-extract | Landed | The shared loudness engine lives in `rust/jasper-tts-protocol/src/loudness.rs`; fan-in and outputd keep compatibility shims. |
| 16-improv-dedup | Landed | Shared Improv onboarding modules exist in `jasper/cli/_improv.py` and `jasper/cli/_esp32_onboard.py`; dial and satellite CLIs are thin wrappers. |
| 17-supply-chain-mirrors | Landed; narrower supply-chain gaps remain | JTS-owned release-asset mirrors, `deploy/provenance.toml`, `uv.lock`, and `deploy/constraints-pi.txt` are committed. Remaining Python hash/mirror, apt snapshot, and PlatformIO depth work lives in `docs/HANDOFF-supply-chain.md`. |

**Mechanical noqa strip** (about 610 vestigial `BLE001` markers as of
2026-06-19) — still queued LAST, when no other PRs are open; it is a
tree-wide merge-conflict bomb.

## Owner-only / hardware actions

The old wave-1 owner-only list is superseded. Current hardware-sensitive
follow-ups live in canonical docs:

- AEC/chip-AEC verification and post-AEC voice UX gates:
  `docs/audit-pending-followups.md` and `docs/HANDOFF-aec.md`.
- Supply-chain maintenance: `docs/HANDOFF-supply-chain.md`.
- Release tagging: `CHANGELOG.md` notes that maintainers tag v0.1.0 at OSS
  launch.
- Branch protection/check names are repository settings; `CONTRIBUTING.md`
  documents the current contributor-facing expectations.

## Review loop

For any remaining live brief cleanup PR, hand it to Claude (the coordinator
session) for review against the original acceptance criteria + the COAH bar
before merge. Both sides use `codex-briefs/REVIEW-PROMPT.md`: the working
agent runs it as a self-review before opening the PR; the coordinator runs it
again as the merge gate.
