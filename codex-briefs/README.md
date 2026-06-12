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

## Wave 2 — briefs 10-17 (expanded, ready to assign)

| Brief | Theme | Sequencing notes |
|---|---|---|
| 10-voice-daemon | Defect PR, then the seam extractions | HOT file — rebase before every push |
| 11-control-server-split | Defect PR, then the five-module split | HOTTEST file (multiroom) — pick a quiet window |
| 12-aec-bridge-emit-legs | One emit path, leg table, BridgeConfig | Independent; wire-neutral required |
| 13-sound-profile-js | rehearsal state-loss bug + guards, then split | Independent; EQ half moves verbatim or not at all |
| 14-install-sh-split | God-function split + de-heredoc models | Land AFTER brief 17 (both touch install.sh) |
| 15-outputd-loudness-extract | Shared loudness engine (NOT deletion — layer is live) | Lowest priority; coordinate with multiroom churn |
| 16-improv-dedup | Shared Improv module for dial/satellite CLIs | Independent |
| 17-supply-chain-mirrors | Mirror commit archives as release assets | Needs network + gh release perms; BEFORE brief 14 |

Run 12/13/16/17 freely in parallel; 10 and 11 are fine in parallel with each
other (disjoint files) but expect rebases; 14 after 17; 15 whenever quiet.

**Mechanical noqa strip** (495 vestigial BLE001 markers) — still queued LAST,
when no other PRs are open; it's a tree-wide merge-conflict bomb.

## Owner-only actions (updated after wave 1)

- Bless #656's provenance position (rewrite-with-reference from XMOS docs,
  not strict clean-room) so the XVF rewrite can merge.
- Hardware checks queued: flash the dial once (procedural gauge, #634); one
  Pi boot-check of XVF chip control after #656 deploys; unplug/replug test
  for the outputd DAC-park change (#650) after it merges; full
  `deploy-to-pi.sh` after brief 14 lands.
- Confirm the live branch-protection required-check list matches the docs
  (`pytest` + `rust`).
- Tag v0.1.0 when Phase 0 closes (licensing + privacy + LAN-trust docs all
  merged).

## Review loop

As PRs open, hand them to Claude (the coordinator session) for review against
each brief's acceptance criteria + the COAH bar before merge. Both sides use
`codex-briefs/REVIEW-PROMPT.md`: the working agent runs it as a self-review
before opening the PR; the coordinator runs it again as the merge gate.
