# Research Archive: Room Correction / Browser Audio / Preference / FIR (2026-05-27)

> **Status: raw research archive plus synthesis.** User-provided LLM
> research reports preserved for traceability, with one local synthesis
> per topic. Do not treat raw reports or synthesis files as current
> operational truth. Current implementation truth lives in
> [`docs/HANDOFF-correction.md`](../../HANDOFF-correction.md),
> [`docs/HANDOFF-sound-preferences.md`](../../HANDOFF-sound-preferences.md),
> and [`docs/calibration-agent/`](../../calibration-agent/).

## Use Rules

- Use `raw/` for source archaeology and re-review.
- Use `synthesis/` for the distilled 2026-05-27 reading of these
  reports.
- Do not move a raw LLM claim into code, a HANDOFF, or user-facing copy
  without checking primary sources or shipped behavior.
- Preserve raw files verbatim. Add errata, corrections, or updated
  conclusions in synthesis or canonical docs, not inside `raw/`.
- When a detail matters for implementation, prefer the synthesis
  recommendation only if the relevant raw reports agree or the
  disagreement is explicitly documented.

## Synthesis Files

| Topic | Synthesis | Raw inputs |
|---|---|---|
| Mobile browser audio capture reliability | [`synthesis/mobile-browser-audio-reliability.md`](synthesis/mobile-browser-audio-reliability.md) | [`raw/mobile-browser-audio-reliability.md`](raw/mobile-browser-audio-reliability.md) |
| Target curves and preference tuning | [`synthesis/target-curves-preference-tuning.md`](synthesis/target-curves-preference-tuning.md) | [`raw/target-curves-preference-chatgpt.md`](raw/target-curves-preference-chatgpt.md), [`raw/target-curves-preference-claude.md`](raw/target-curves-preference-claude.md) |
| FIR room correction and phase correction | [`synthesis/fir-phase-room-correction.md`](synthesis/fir-phase-room-correction.md) | [`raw/fir-phase-room-correction-chatgpt.md`](raw/fir-phase-room-correction-chatgpt.md), [`raw/fir-phase-room-correction-claude.md`](raw/fir-phase-room-correction-claude.md), [`raw/fir-phase-room-correction-gemini.md`](raw/fir-phase-room-correction-gemini.md) |
| Multi-position correction confidence | [`synthesis/multi-position-room-correction.md`](synthesis/multi-position-room-correction.md) | [`raw/multi-position-room-correction-chatgpt.md`](raw/multi-position-room-correction-chatgpt.md), [`raw/multi-position-room-correction-claude.md`](raw/multi-position-room-correction-claude.md), [`raw/multi-position-room-correction-gemini.md`](raw/multi-position-room-correction-gemini.md) |

## Raw Files

| File | Original local filename | Topic | Provider | Lines | SHA-256 | Distilled into |
|---|---|---|---|---:|---|---|
| [`raw/mobile-browser-audio-reliability.md`](raw/mobile-browser-audio-reliability.md) | `JTS Mobile Browser Microphone Reliability - Research Report.md` | Mobile browser capture reliability | Unknown / single report | 368 | `c2035464a79432f705c615e3090793147971d3fc5e51108b269cf1b4953cc154` | [`synthesis/mobile-browser-audio-reliability.md`](synthesis/mobile-browser-audio-reliability.md) |
| [`raw/target-curves-preference-chatgpt.md`](raw/target-curves-preference-chatgpt.md) | `JTS Target Curves & Preference Tuning - Research Report - Chatgpt.md` | Target curves / preference tuning | ChatGPT | 184 | `ab4ca004554d5e90e3f808f032f11b13010d0828936ffec549a04f6b740948b7` | [`synthesis/target-curves-preference-tuning.md`](synthesis/target-curves-preference-tuning.md) |
| [`raw/target-curves-preference-claude.md`](raw/target-curves-preference-claude.md) | `JTS Target Curves & Preference Tuning - Research Report - Claude.md` | Target curves / preference tuning | Claude | 392 | `8798161da87b3782058efa4a889b65a4749520aa9914c8f4732dd67dac32fb59` | [`synthesis/target-curves-preference-tuning.md`](synthesis/target-curves-preference-tuning.md) |
| [`raw/fir-phase-room-correction-chatgpt.md`](raw/fir-phase-room-correction-chatgpt.md) | `FIR Readiness for Room Correction in JTS (Raspberry Pi 5 + CamillaDSP) - chatgpt.md` | FIR / phase room correction | ChatGPT | 257 | `eda18cfb968173a877efe8e4484568a026b4eba243ddab73f630102e5ed791a2` | [`synthesis/fir-phase-room-correction.md`](synthesis/fir-phase-room-correction.md) |
| [`raw/fir-phase-room-correction-claude.md`](raw/fir-phase-room-correction-claude.md) | `FIR Readiness for Room Correction in JTS (Raspberry Pi 5 + CamillaDSP) - Claude.md` | FIR / phase room correction | Claude | 540 | `2e8c7fddbc36c0341857e444cdaa66a66d7ec0070d6396e03029360194ba1ec3` | [`synthesis/fir-phase-room-correction.md`](synthesis/fir-phase-room-correction.md) |
| [`raw/fir-phase-room-correction-gemini.md`](raw/fir-phase-room-correction-gemini.md) | `FIR Room Correction Research for JTS - gemini.md` | FIR / phase room correction | Gemini | 343 | `20d97ee2290d02f64a484e069cd85586bc3b7a471e604938385aa8cac0963e28` | [`synthesis/fir-phase-room-correction.md`](synthesis/fir-phase-room-correction.md) |
| [`raw/multi-position-room-correction-chatgpt.md`](raw/multi-position-room-correction-chatgpt.md) | `Multi-Position Room-Correction Confidence for JTS -chatgpt.md` | Multi-position correction confidence | ChatGPT | 275 | `22171bd32ea2ecf5c9fc798c9190746f6f207ef249aaf84862956a8e2eabd47c` | [`synthesis/multi-position-room-correction.md`](synthesis/multi-position-room-correction.md) |
| [`raw/multi-position-room-correction-claude.md`](raw/multi-position-room-correction-claude.md) | `Multi-Position Room-Correction Confidence for JTS - claude.md` | Multi-position correction confidence | Claude | 443 | `1cfafa0abe2abbb8305938e27e7a22be99923ec55fae6ca331352c1ca77c8ff3` | [`synthesis/multi-position-room-correction.md`](synthesis/multi-position-room-correction.md) |
| [`raw/multi-position-room-correction-gemini.md`](raw/multi-position-room-correction-gemini.md) | `Multi-Position Room Correction Confidence - gemini.md` | Multi-position correction confidence | Gemini | 312 | `138838aeaebd852b40aa6499ae21246649dd45cad000c35a37173d03bb1b8162` | [`synthesis/multi-position-room-correction.md`](synthesis/multi-position-room-correction.md) |

## Known Risks

- These reports are LLM-generated. They contain useful prior-art
  pointers, but some citations may be incomplete, vendor-marketing
  claims may be over-weighted, and some numeric thresholds are
  engineering proposals rather than peer-reviewed rules.
- Several raw reports include embedded image markers for equations.
  The synthesis restates the relevant plain-language result when the
  equation image was material.
- Some claims concern rapidly changing browser behavior. Re-verify
  mobile browser audio details against current WebKit, Chromium, and
  device behavior before making a user-facing support promise.
- FIR CPU estimates are encouraging but mostly extrapolated. JTS still
  needs on-device Pi 5 1 GB benchmarks before enabling generated FIR
  filters by default.

Last archived: 2026-05-27
