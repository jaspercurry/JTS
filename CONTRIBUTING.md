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

```sh
git clone https://github.com/jaspercurry/JTS.git
cd JTS
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

That's ~1000 tests across 93 files in under a minute. Everything
reachable without a Pi, mic, or speaker — the audio I/O, network
calls, and systemd surfaces are all mocked. If it passes here, the
change is safe to deploy to hardware.

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

## Tests

- **Hardware-free pytest** (`pytest`) — required green before merge.
  No SDK auth or network. >1000 tests across 93 files.
- **Voice-eval suite** (`pytest tests/voice_eval/regression/`) —
  opens **paid** real-time LLM sessions (~$0.075/scenario on Gemini,
  ~$0.60 on OpenAI). Don't run on every PR; nightly at most with
  an explicit budget. See AGENTS.md "Voice-eval cost discipline."
- **Hardware tests** — `sudo /opt/jasper/.venv/bin/jasper-doctor` on
  the Pi after a deploy.

## Code style

- Python 3.11+ (Pi runs 3.13).
- Format with `ruff format`; lint with `ruff check`. Both are dev deps.
- Match the surrounding style. Don't refactor working code that
  isn't part of your change.
- See AGENTS.md "Behavioral rules for working in this codebase" —
  that's the authoritative style guide for both humans and AI agents.

## Documentation

The repo has ~30 markdown files in a layered structure:

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
  [docs/HANDOFF-aec.md](docs/HANDOFF-aec.md) and AGENTS.md
  "AEC bridge — reconciler toggle."
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

- **First-timer**: read README.md, then pick something off
  [PLAN.md](PLAN.md) "What comes after v1." Open an issue first to
  discuss approach before coding.
- **Returning**: scan open PRs and `git log --since="2 weeks ago"`
  for active workstreams. Active subsystems (AEC, mic-quality, USB
  gadget) shouldn't get parallel changes without coordinating.
