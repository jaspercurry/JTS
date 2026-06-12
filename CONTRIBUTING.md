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
uv sync
.venv/bin/pytest
```

`uv.lock` is the canonical lockfile for contributor development
environments. `uv sync` installs the default `dev` dependency group, so
the fresh venv has `pytest`, `pytest-asyncio`, and `ruff` without an
extra flag.

If you'd rather not install a new tool, stock pip + venv works too —
just make sure your python is 3.11+:

```sh
python3.11 -m venv .venv     # NOT `python3 -m venv` on macOS — Apple's default is 3.9
source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

That runs the full hardware-free suite — thousands of tests reachable
without a Pi, mic, or speaker. The audio I/O, network calls, and
systemd surfaces are mocked. If it passes here, the change is safe to
deploy to hardware.

The Ubuntu CI path also installs `portaudio19-dev`, then installs
`openwakeword==0.6.0` with `--no-deps` plus the supporting packages it
actually needs (`requests`, `tqdm`, `scikit-learn`). That mirrors the
Pi installer's ONNX-only openWakeWord setup and is useful context if
you're doing audio-adjacent work locally.

Hardware-only work (audio playback, the AEC bridge, the wizards in a
browser) is covered by [BRINGUP.md](BRINGUP.md), which walks from
blank SD card to working speaker.

## How to land a PR

1. **Branch from main**: `git checkout -b your-name/short-description`
2. **Write the change + tests.** Every new voice tool ships with a
   regression scenario under `tests/voice_eval/regression/`. Every
   new subsystem ships with hardware-free pytest coverage.
3. **Run `pytest`** (and `ruff check .` for style).
4. **Push and open a PR** against `main`. Fill in the template.
5. **No direct pushes to main** — even one-line fixes go through PR.

### Branch protection

`main` is protected: the required GitHub Actions checks are `pytest`
(which also runs `ruff check .`) and `rust`. Both **must pass before
any PR can merge**, force-pushes and branch deletion are blocked, and
the rule is enforced for admins too — so nobody, including the
maintainer, can merge into a red `main`. There is no required reviewer,
so you can self-merge your own green PR.

Two operational notes:

- **The required checks are named `pytest` and `rust`.** If you ever
  rename those jobs in `.github/workflows/tests.yml`, update the
  branch-protection rule in the same change, or every merge will block
  on a check that never reports.
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

- **Hardware-free pytest** (`pytest`) — required green before merge.
  No SDK auth or network. Runs the hardware-free suite.
- **Rust audio-daemon gate** (`cargo build --release --locked` and
  `cargo test --locked`) — required green for the `rust/` crates in
  CI, including the production fan-in/outputd daemons and shared
  protocol crate.
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
- Lint with `ruff check .`. Do not run a tree-wide `ruff format` as drive-by
  cleanup; formatting the whole tree is a separate, deliberate PR.
- Match the surrounding style. Don't refactor working code that
  isn't part of your change.
- For larger or riskier changes, use the COAH quality bar in
  [AGENTS.md](AGENTS.md#coah-quality-bar): Clean, Observable,
  Available/resilient, Hardware-safe.
- Web setup pages follow AGENTS.md "Web wizard conventions" — shared
  CSRF helpers, checkbox-based toggles, and no generated inline JS for
  untrusted device/network metadata.
- See AGENTS.md "Behavioral rules for working in this codebase" —
  that's the authoritative style guide for both humans and AI agents.

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

## Where to start

- **First-timer**: read README.md, then pick something from
  [PLAN.md's sequenced roadmap](PLAN.md#sequenced-roadmap). Open an
  issue first to discuss approach before coding.
- **Returning**: scan open PRs and `git log --since="2 weeks ago"`
  for active workstreams. Active subsystems (AEC, mic-quality, USB
  gadget) shouldn't get parallel changes without coordinating.
