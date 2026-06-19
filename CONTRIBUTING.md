# Contributing to JTS

JTS is a personal smart-speaker project that runs on a Raspberry Pi 5.
It's open-sourced so others can fork it, learn from it, or contribute
back. This guide is the on-ramp for **first-time contributors** —
humans or AI coding agents.

If you're extending the codebase rather than just reading it, the
authoritative operational guide is [AGENTS.md](AGENTS.md) (Claude
users: same content via [CLAUDE.md](CLAUDE.md)). That file covers
file-ownership rules, the deploy path, the wizards, and the
hardware-specific footguns. This file is just "how do I land my
first PR."

## Quick start (laptop, no hardware required)

Recommended path is [uv](https://docs.astral.sh/uv/) — it reads
`requires-python` from `pyproject.toml` and refuses to build a venv
on the wrong Python. (Plain `python -m venv` silently accepts
whatever python you invoked it with, which on macOS defaults to
Apple's 3.9 — produces a broken venv that fails with confusing
errors deep in `jasper/peering/`.)

```sh
git clone https://github.com/jaspercurry/JTS.git
cd JTS
uv sync --extra full --extra streambox
scripts/test-fast
```

`uv.lock` is the canonical lockfile for contributor development
environments. The `--extra full --extra streambox` flags pull in the
runtime packages the hardware-free suite imports (`numpy`, `httpx`,
`scipy`, `spotipy`, …) alongside the default `dev` group
(`pytest`/`pytest-asyncio`/`pytest-xdist`/`ruff`/`mypy`). A bare `uv sync`
installs only the `dev` group, so pytest would fail collection with
missing-module errors — the extras carry the code under test. (uv 0.11 has
no `default-extras` setting to fold these into a bare sync, so the flags
are explicit; a regression test pins this command.) `scripts/test-fast` is
the normal local iteration lane: it runs lint, last-failed tests, a
changed-file pytest selection, and a small always-on guard set.

If you'd rather not install a new tool, stock pip + venv works too —
just make sure your python is 3.11+:

```sh
python3.11 -m venv .venv     # NOT `python3 -m venv` on macOS — Apple's default is 3.9
source .venv/bin/activate
pip install -e '.[full,dev]'
scripts/test-fast
```

That runs the fast local lane without a Pi, mic, or speaker. The audio
I/O, network calls, and systemd surfaces are mocked in the default suite.
Before publishing substantial work, run `scripts/test-merge`; that mirrors
the required hardware-free pytest lane and runs the suite in four
pytest-xdist workers. The `pytest` CI job also runs `ruff check .` and the
lenient, baselined `mypy` gate before the suite.

The Ubuntu CI path also installs `portaudio19-dev`, then replays the
committed lock with
`uv sync --locked --extra full --extra dev --group openwakeword-onnx`.
That group lock-covers the ONNX-only openWakeWord helper packages
(`requests`, `tqdm`, `scikit-learn`). After the exact sync, CI installs
only `openwakeword==0.6.0` itself with `--no-deps`, mirroring the Pi
installer's ONNX-only setup.

Hardware-only work (audio playback, the AEC bridge, the wizards in a
browser) is covered by [BRINGUP.md](BRINGUP.md), which walks from
blank SD card to working speaker.

## How to land a PR

1. **Branch from main**: `git checkout -b your-name/short-description`
2. **Write the change + tests.** Every new voice tool ships with a
   regression scenario under `tests/voice_eval/regression/`. Every
   new subsystem ships with hardware-free pytest coverage.
3. **Run the local test lane**: `scripts/test-fast`. For non-trivial
   work, also run the full Python merge lane: `scripts/test-merge`.
   Use `ruff check .` and `mypy` for explicit static-check spot runs.
4. **Push and open a PR** against `main`. Fill in the template.
5. **No direct pushes to main** — even one-line fixes go through PR.

### Branch protection

`main` is protected: the required GitHub Actions checks are `pytest`
and `rust`. Both **must pass before any PR can merge**, force-pushes
and branch deletion are blocked, and the rule is enforced for admins too
— so nobody, including the maintainer, can merge into a red `main`.
The `rust` job is path-aware on PRs: it exits green quickly for
unrelated changes, but runs full Cargo when Rust or Rust deploy/CI
surfaces change and always runs on `main` pushes. There is no required
reviewer, so you can self-merge your own green PR.

Two operational notes:

- **The required checks are named `pytest` and `rust`.** The visible
  `pytest` check is an aggregate over the Python-version matrix
  (`pytest / py3.11`, `pytest / py3.12`, `pytest / py3.13`) and keeps the
  branch-protection context stable. If you ever rename the aggregate
  `pytest` job or the `rust` job in `.github/workflows/tests.yml`,
  update the branch-protection rule in the same change, or every merge
  will block on a check that never reports.
- **Emergency override.** If CI is wedged or GitHub Actions is down and a
  fix genuinely cannot wait, an admin can temporarily lift protection at
  `Settings → Branches → main`, merge, and re-enable it immediately — or
  reapply the rule via the API:

  ```sh
  gh api -X PUT repos/<owner>/<repo>/branches/main/protection \
    -H "Accept: application/vnd.github+json" --input - <<'JSON'
  {"required_status_checks":{"strict":false,"contexts":["pytest","rust"]},
   "enforce_admins":true,"required_pull_request_reviews":null,
   "restrictions":null,"allow_force_pushes":false,"allow_deletions":false,
   "required_conversation_resolution":true}
  JSON
  ```

## Tests

- **Fast local lane** (`scripts/test-fast`) — default for humans and AI
  agents while iterating. Runs lint, last-failed tests, changed-file
  pytest selection, and always-on guard tests.
- **Python merge lane** (`scripts/test-merge`) — required green before
  merge through the `pytest` CI job. No SDK auth or network. Runs the
  hardware-free suite in parallel and excludes paid `tests/voice_eval`.
  CI runs this lane on Python 3.11, 3.12, and 3.13; the aggregate
  `pytest` check fails unless every versioned matrix leg passes.
- **Python static checks** (`ruff check .` and `mypy`) — run once in the
  Python 3.13 matrix leg before the test suite. mypy starts permissive
  and baselined so existing type debt does not block day-one adoption,
  but new unbaselined errors fail the job.
- **Rust audio-daemon gate** (`cargo build --release --locked` and
  `cargo test --locked`) — required green through the `rust` CI job
  when Rust-relevant surfaces change, and on every `main` push. Covers
  the production fan-in/outputd daemons and shared protocol crate.
- **Shell entry-point gate** (`bash -n` plus `shellcheck
  --severity=warning`) — CI parses and lints the installer, deploy
  helpers, and shell operator scripts that can mutate a live speaker.
- **Supply-chain provenance** (`python3 scripts/check-provenance.py`) —
  required when touching install/build fetches, firmware dependency
  declarations, wake/DTLN model registries, or Python direct URL
  dependencies. See [docs/HANDOFF-supply-chain.md](docs/HANDOFF-supply-chain.md).
- **Optional ESP32 firmware build check**
  (`scripts/check-firmware-builds.sh`) — run when touching
  `firmware/`, PlatformIO pins, or accessory onboarding. This is
  explicit instead of always-on CI because most PRs do not affect
  optional dial/satellite hardware and PlatformIO is a large download.
- **Voice-eval suite** (`pytest tests/voice_eval/regression/`) —
  opens **paid** real-time LLM sessions (~$0.075/scenario on Gemini,
  ~$0.60 on OpenAI). Don't run on every PR; nightly at most with
  an explicit budget. See AGENTS.md "Voice-eval cost discipline."
- **Hardware tests** — `sudo /opt/jasper/.venv/bin/jasper-doctor` on
  the Pi after a deploy.

## Code style

- Python 3.11+ (Pi runs 3.13).
- Lint with `ruff check .` and type-check with `mypy`. Do not run a
  tree-wide `ruff format` as drive-by cleanup; formatting the whole tree is a
  separate, deliberate PR.
- Match the surrounding style. Don't refactor working code that
  isn't part of your change.
- For larger or riskier changes, use the COAH quality bar in
  [AGENTS.md](AGENTS.md#coah-quality-bar): Clean, Observable,
  Available/resilient, Hardware-safe.
- Web setup pages follow AGENTS.md "Web wizard conventions" — shared
  CSRF helpers, checkbox-based toggles, and no generated inline JS for
  untrusted device/network metadata.
- See the [Agent behavior baseline](AGENTS.md#agent-behavior-baseline)
  in AGENTS.md — that's the authoritative style guide for both humans
  and AI agents.

## Documentation

The repo has a large Markdown corpus in a layered structure:

- **[README.md](README.md)** — architecture, hardware, where things
  live. Read first.
- **[AGENTS.md](AGENTS.md)** — operational rules for AI agents (and
  the de-facto reference for human contributors).
  [CLAUDE.md](CLAUDE.md) is the same content via @-import.
- **[BRINGUP.md](BRINGUP.md)** — flash a fresh Pi to working speaker.
- **[PLAN.md](PLAN.md)** — roadmap.
- **[docs/HANDOFF-*.md](docs/)** — one deep-dive per subsystem (AEC,
  AirPlay, mic, voice providers, transit, etc.).

**If you touch a subsystem, scan its HANDOFF first.** Those docs
capture hardware-specific footguns that aren't obvious from the code.
If you find something stale, fix it inline in the same PR.

## Working on a sensitive subsystem

A few subsystems have explicit design constraints that aren't
obvious from the code alone. If you're proposing changes here,
**read the HANDOFF first** — it captures decisions that have
already been made and what's NOT a reviewable trade-off.

- **AEC / mic pipeline.** Engine swaps and tuning parameters are
  reviewable; architectural changes (PipeWire fanout, hardware
  AEC retry, custom XVF firmware) are not. Mic capture is
  consumed by ML (openWakeWord + speech LLMs), never humans —
  optimize for ASR accuracy, not naturalness. See
  [docs/HANDOFF-aec.md](docs/HANDOFF-aec.md) and
  [AGENTS.md "AEC bridge — input profile and reconciler"](AGENTS.md#aec-bridge--input-profile-and-reconciler).
- **Voice provider abstraction.** New providers go through the
  `LiveConnection` / `LiveTurn` protocol; don't add
  provider-specific branches outside `jasper/voice/`. See
  [docs/HANDOFF-voice-providers.md](docs/HANDOFF-voice-providers.md).
- **The XVF3800 mic chip.** **Never call `SAVE_CONFIGURATION`**
  — documented brick hazard on certain firmware versions. See
  [docs/HANDOFF-xvf3800.md](docs/HANDOFF-xvf3800.md).
- **Voice prompting.** `SYSTEM_INSTRUCTION` and tool docstrings
  are sensitive to model-specific RLHF biases. Read
  [docs/HANDOFF-prompting.md](docs/HANDOFF-prompting.md) before
  rewording.

## Reporting bugs / suggesting features

Use the templates in `.github/ISSUE_TEMPLATE/`. The bug template asks
for hardware, environment, and the relevant `jasper-doctor` output —
"the voice doesn't work" is hard to help with; "wake fires but
session ends with 0 input_tokens" is actionable.

## Code of conduct

This project follows the [Contributor Covenant 2.1](CODE_OF_CONDUCT.md).
Be kind, assume good faith, and remember this is someone's hobby
project that others are trying to build on top of.

## License

Apache 2.0. See [LICENSE](LICENSE). By contributing you agree your
contributions will be licensed under the same.

First-party JTS source is Apache-2.0. New files should carry a short
SPDX license line when practical (`# SPDX-License-Identifier:
Apache-2.0`, or the language's matching comment style). Do not add JTS
SPDX headers to vendored, generated, model, data, or third-party files:
those need an explicit entry in [LICENSE-third-party.md](LICENSE-third-party.md)
or, if REUSE is enabled later, a scoped `REUSE.toml` annotation instead.

## Where to start

- **First-timer**: read README.md, then pick something from
  [PLAN.md's sequenced roadmap](PLAN.md#sequenced-roadmap). Open an
  issue first to discuss approach before coding.
- **Returning**: scan open PRs and `git log --since="2 weeks ago"`
  for active workstreams. Active subsystems (AEC, mic-quality, USB
  gadget) shouldn't get parallel changes without coordinating.
