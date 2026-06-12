# Codex work briefs — 2026-06-12 review remediation

Source of truth for findings: `docs/REVIEW-2026-06-12-oss-due-diligence.md`
(on main). Each brief below = one Codex agent session = 1–3 small PRs.

**Lifecycle:** this directory is committed *temporarily* so every agent can
read its brief from main, regardless of where it runs. It is exempt from the
orphan-doc sweep (which globs only root and top-level `docs/*.md`). Delete the
whole directory in a cleanup PR once wave-1 and wave-2 remediation completes.

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

## Wave 2 — run AFTER wave 1 merges, one at a time (ask Claude to expand each
brief when you're ready; they need post-wave-1 reality)

1. **voice_daemon.py** — first a small-fixes PR (track the fire-and-forget
   arbitrate task in a strong-ref set; add a last-resort output-side stall cap
   to `_idle_watchdog`; fix the MUTE-persistence docstring; record the firing
   leg's threshold in `begin_event`), then the seam extractions
   (voice/prompt.py, voice/earcons.py, voice/turn_playback.py,
   voice/daemon_main.py) plus a real test-constructor for WakeLoop so the
   `__new__` fixture idiom and getattr-on-self defensiveness can go.
2. **control/server.py split** — aec_endpoints / uds / state_aggregate /
   volume_ops / dial modules. NOTE: this file is hot (multiroom work landing
   daily); pick a quiet window, expect rebases.
3. **aec_bridge.py** — route all 7 inline emit blocks through `emit_packet`,
   leg-emitter table, derive OUT_PORT defaults from `wake_legs` (or lockstep
   test), move import-time env reads into a BridgeConfig.
4. **sound-profile JS** — `patchActiveSpeaker()` helper (fixes the rehearsal
   state-loss bug), seq-token guards on wizard actions, Promise.allSettled for
   the 9-fetch waterfall, extend the node harness; file split flagged for
   on-device verification.
5. **install.sh** — split the two ~520-line functions along the lib/install
   seam; single `ensure_state_dir` (ends the 0750/0755 flip-flop); de-heredoc
   the model downloads into jasper/model_downloads.py CLI.
6. **outputd stranded-module question** — RE-VERIFY first: the 2026-06-12
   jasper-tts-protocol + outputd tts.rs work may have revived or replaced the
   layer the review called dead. Diagnose, then either delete or de-duplicate
   via the shared crate.
7. **Mechanical noqa strip** (495 vestigial BLE001 markers) — run LAST, when
   no other PRs are open; it's a tree-wide merge-conflict bomb.

## Owner-only actions (Codex cannot do these)

- Merge brief 05's workflow-file PR from the GitHub web UI (OAuth tokens
  without `workflow` scope cannot merge `.github/workflows/*` changes).
- Eyeball the upstream licenses brief 01 collects (5-minute legal sanity pass).
- Flash the dial once to validate the procedural gauge (brief 01) and run the
  camilla park-on-missing-DAC change on hardware (brief 07).
- Decide jarvis_v2 default if its license turns out unstated (brief 01).
- Confirm the actual branch-protection required-check list (brief 03 fixes the
  docs to match `pytest` + `rust`; verify in repo settings).
- Tag v0.1.0 when Phase 0 completes.

## Review loop

As PRs open, hand them to Claude (the coordinator session) for review against
each brief's acceptance criteria + the COAH bar before merge.
