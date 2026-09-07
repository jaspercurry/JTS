# AI agent guide for JTS

> Canonical for every agent (Claude Code, Codex, anyone else). CLAUDE.md is a
> thin shim that imports this file — edit here, never there. Keep this file
> under ~220 lines: rules stack badly, and every line is paid on every request.
> Rationale lives in [docs/adr/](docs/adr/) and git history, not here.

## What this project is

JTS is a **hobbyist smart speaker** on Raspberry Pi (Python + Rust + C + a
little JS). One owner, no other users, not yet a product. Development is done
by AI agents with the owner directing. Deploys and rollbacks are cheap and
spare Pis exist, so the default posture is: **add the least new machinery
that works, ship it, verify on hardware, and iterate** — except for the
non-negotiables below, which are always production-grade. Scope is measured
by what a change *adds*, never by lines touched: a deletion or consolidation
that preserves behavior is a small change no matter how many lines it
removes, and right-sizing refactors are welcome work, not scope creep. Big
removals still land as reviewable, single-concern PRs.

Read [README.md](README.md) for architecture and repo layout. The Pi has 1 GB
of RAM; respect its budget (bounded loops, no heavy analysis on-device — use
`bash scripts/pi-run-diagnostic.sh -- <cmd>` for anything experimental).

## Non-negotiables (closed list — nothing else is "safety")

1. **Hearing:** `devices.volume_limit` stays `0.0` in every CamillaDSP config
   and `CamillaController.set_volume_db` clamps positive writes. The
   commissioning SPL stop stays. Never weaken either.
2. **Hardware damage:** never call `SAVE_CONFIGURATION` on the XVF3800
   (brick hazard); respect declared driver caps in DSP/measurement paths.
3. **Secrets:** keys/PSKs/tokens live only in their compartment files
   (`/var/lib/jasper-secrets/`, `/var/lib/jasper-intsecrets/`, wizard-owned
   `/var/lib/jasper/*.env`) and never appear in logs, `/state`, doctor
   output, or code.
4. **Deploy integrity:** `bash scripts/deploy-to-pi.sh` is the only deploy
   path. Never hand-roll rsync+install; never bypass the identity or
   direction guards (deliberate overrides: `JTS_ACCEPT_NEW_IDENTITY=1`,
   `JASPER_DEPLOY_ALLOW_DOWNGRADE=1`).
5. **Renderer ALSA devices** must resolve as the unit's real `User=`:
   `sudo -n -u $USER env LC_ALL=C timeout <N> aplay -q -s <F> -D $DEVICE -c 2
   -r 48000 -f S16_LE /dev/zero`; exit 0 only. N, F: renderers.py constants.
6. **No silent deafness:** a new code path that prevents wake response must
   play a cue (`jasper/cues/registry.py`).
7. **Paid tests:** `tests/voice_eval/` opens paid realtime-LLM sessions.
   Never loop or auto-retry them; state estimated cost before running.
8. **`main` is protected:** CI green before merge; never merge a red `main`.

A gate claiming "safety" that is not on this list is a nanny — demote it.

## Defaults (concrete, not mood — these replace the old doctrine)

- **Leave every file you touch smaller than you found it** unless the feature
  genuinely grew. Delete dead code you find in scope (verify no caller first:
  registries, `pyproject.toml` entry points, systemd `ExecStart`, `deploy/bin`,
  udev, CI, `importlib`/`getattr`).
- **Tests:** pin externally observable behavior, one altitude per behavior.
  Never assert on source text or log/error prose — assert types, codes, and
  structured fields. Prefer one parametrized/property test over an example
  cluster. A bug fix gets one behavior pin, not a new test file. Delete a
  test when its subject moves. Non-negotiable paths (list above) get heavy
  tests; everything else gets tests where behavior can actually break.
- **Comments:** only non-derivable constraints (units, ranges, timing,
  hardware quirks) and `why`-pointers (`See ADR-NNN`, an issue, a doc). No
  narration of what code does, no history, no dates/PR numbers, no text
  addressed to a reviewer. When you can't verify a comment against the code,
  delete it — a wrong comment misleads agents more than a missing one.
- **Guards:** do not defend hypotheticals. After a real incident: fix
  forward and add observability (`event=` log, `/state`, doctor). Permanent
  machinery needs a non-negotiable tie or a recurrence; any new guard ships
  with a removal condition or expiry noted beside it. A passed
  commissioning/validation proof stays valid until something observably
  breaks: upstream changes demote it to disclosed-stale, never to a park —
  parking on unproven-ness is reserved for the non-negotiables (ADR-0101).
- **Docs:** decisions go to `docs/adr/` (append-only, dated, one decision per
  file; supersede, never edit). The HANDOFF doc corpus was deleted for good
  (ruling 13, ADR-0199; the live bass-extension plan is exempt, ADR-0229) — do
  not recreate that tier; a subsystem fact gets re-derived at HEAD, not parked
  in a new handoff. Do not restate here, in README, or in code what another
  file owns.
- **Duplication:** before writing a helper, constant vocabulary, or module,
  grep for the existing one and extend or consume it. Two implementations of
  one concern in reach: converge them or open an issue — never add a third.
- **Config:** no new `JASPER_*` knob unless the owner asked for the toggle or
  hardware genuinely varies. Pattern choice (typed `Config` vs plugin
  self-parse vs reconciler-owned env) per
  [docs/extensibility.md](docs/extensibility.md).
- **Evidence first:** for any bug, fetch logs
  (`bash scripts/fetch-pi-logs.sh`, `curl -s http://jts.local:8780/state`)
  and name the failing line/transition before proposing a fix. Verify at the
  user's surface (the URL, the daemon's `/state`), not upstream config.
- **Constrained hardware:** the Pi Zero 2 W (415 MB) is a supported target.
  Push, don't pull; no short-lived Python in hot or restart paths; one
  interpreter per concern. See ADR-0226.

## Map

- `jasper/` — product Python. Notable: `voice_daemon.py` (wake→LLM loop),
  `mux.py` (source arbitration), `volume_coordinator.py`, `camilla.py`
  (DSP control), `output_topology.py`; packages: `voice/` (providers),
  `tools/` (LLM tool packs), `web/` (wizards; shared primitives in
  `web/_common.py`), `control/` (jasper-control daemon), `fanin/`,
  `multiroom/`, `transit/`, `cues/`, `cli/` (incl. `cli/doctor/`).
- `jasper/active_speaker/`, `jasper/audio_measurement/`, `jasper/correction/`
  — the speaker tuning/measurement program (own doctrine:
  [docs/measurement-loop-doctrine.md](docs/measurement-loop-doctrine.md)).
- `rust/` — jasper-fanin (mixer), jasper-outputd (final output owner),
  shared crates. `c/jts-ring-ioplug` — ALSA shared-memory ring plugin.
- `deploy/` — `install.sh` + `lib/install/`, systemd units, nginx confs,
  web assets (`deploy/assets/`, design system in `app.css`).
  `scripts/` — laptop-side operator tools. `experiments/usb-turntable` is
  production (turntable-driven speaker measurement) despite the path.
- Audio path: renderers → snd-aloop/ring → jasper-fanin → CamillaDSP →
  jasper-outputd → DAC ([docs/audio-paths.md](docs/audio-paths.md)).
- **Single-writer env files** under `/var/lib/jasper/` (wizard- or
  reconciler-owned; the writer is named in each file's header). Never move
  their keys into `/etc/jasper/jasper.env`; long-lived daemons re-read these
  files fresh — never cache wizard-owned values from `os.environ`.
- Laptop state: `.env.local` (`PI_HOST`, `PI_USER`, `JASPER_HOSTNAME`) +
  `CLAUDE.local.md`, written by `scripts/onboard.sh` / `scripts/use`. One
  checkout per Pi.
- Deep dives, only when touching that subsystem:
  [docs/extensibility.md](docs/extensibility.md).

## Build, test, deploy

- Iterating: `scripts/test-fast`. Before merge: `scripts/test-merge`
  (runs the lint-imports layers contract + mypy + full hardware-free
  pytest). Trust only the final `==> <lane>: N passed` sentinel line —
  a piped/truncated run lies.
- Deploy: `bash scripts/deploy-to-pi.sh` (flags: `SKIP_INSTALL=1` rsync-only,
  `SKIP_RESTART=1`). Verify: `http://jts.local/system/` shows the new SHA;
  `sudo /opt/jasper/.venv/bin/jasper-doctor` on the Pi. Runtime Python lives
  in `/opt/jasper/`, not the rsync checkout — edits aren't live until install
  re-copies.
- CI lanes and branch protection: [CONTRIBUTING.md](CONTRIBUTING.md).

## PRs and coordination

- Start every task: `git fetch origin` and confirm
  `git merge-base --is-ancestor origin/main HEAD`; rebase if behind. Fetch
  again before pushing — multiple agents work this repo concurrently.
- Keep PRs small (target < 400 changed lines) and single-concern. Run
  `scripts/test-fast` before pushing. After pushing, confirm the remote ref
  advanced.
- A PR adding/widening a tree-scanning check: validate against the merge
  result (`git merge-tree --write-tree origin/main HEAD`), not your branch.
- Never `git stash` in shared worktrees; remove agent worktrees when done
  (`git worktree remove` + `prune`); never remove one with unpushed work.

## Review policy (tiered — replaces the mandatory adversarial gate)

- **Default (most changes):** one pass of the built-in `/code-review`
  (medium effort). The owner triages findings — each gets fixed or an
  explicit wontfix; there is no zero-findings requirement. Run `/simplify`
  occasionally after a feature lands.
- **Non-negotiable tier** (a diff touching the clamps above, DSP math on the
  output path, secrets handling, or `deploy/install.sh`): also run
  [/adversarial-review](.claude/commands/adversarial-review.md) and fix its
  blockers before merge.
- Docs, mechanical cleanups, test-only changes: author judgment plus a
  sanity look. No panels, no re-review rounds.

## Memory

Behavioral baseline (all agents, all projects): the owner's global ruleset,
[jaspercurry/claude-rules](https://github.com/jaspercurry/claude-rules).
Its spirit is folded into the Defaults above — do not restate it here.

What agents must remember lives in, in order: this file (operational),
`docs/adr/` (decisions and their why), git history and PR descriptions
(what changed and when). The HANDOFF doc corpus that used to sit at the end
of that list is gone (ruling 13, ADR-0199) — a subsystem fact not covered by
the three sources above gets re-derived at HEAD, never trusted from a
handoff. Inline code prose is not a memory store.

---

*This file replaced the 3,500-line doctrine on 2026-08-26 —
[ADR-0001](docs/adr/0001-operating-model-reset.md) records why, what it
supersedes, and the evidence. The old text lives in git history.*
