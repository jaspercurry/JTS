# Research Archive: Calibration Agent / Active Speaker DSP (2026-05-25)

> **Status: raw research archive.** Verbatim local LLM research
> reports preserved for traceability. Do not treat these files as
> current operational truth. Current distilled guidance lives in
> [`docs/HANDOFF-correction.md`](../../HANDOFF-correction.md),
> [`docs/HANDOFF-calibration-agent.md`](../../HANDOFF-calibration-agent.md),
> [`docs/HANDOFF-active-speaker-dsp.md`](../../HANDOFF-active-speaker-dsp.md),
> and [`docs/calibration-agent/`](../../calibration-agent/).

## Use Rules

- Use these reports for source archaeology: what informed the
  2026-05-25 corpus and handoffs.
- Do not cite raw claims as JTS guidance unless the claim is
  distilled into a canonical doc.
- Preserve report bodies verbatim. Add errata, verification notes, or
  source corrections here or in canonical docs, not inside `raw/`.
- When a detail matters for implementation, verify it against primary
  sources or shipped code before moving it into a HANDOFF.

## Files

| File | Original local filename | Topic | Lines | SHA-256 | Distilled into |
|---|---|---|---:|---|---|
| [`raw/room-correction-science-and-agent-foundation.md`](raw/room-correction-science-and-agent-foundation.md) | `deep-research-report (1).md` | Room correction / FIR / calibration-agent research | 184 | `02ba8bee4ef983a970b7698e4086937f87fa7064c860574d94b2678d5e6fe8e1` | [`HANDOFF-calibration-agent.md`](../../HANDOFF-calibration-agent.md), [`docs/calibration-agent/`](../../calibration-agent/) |
| [`raw/fir-target-curves-and-preference-eq.md`](raw/fir-target-curves-and-preference-eq.md) | `compass_artifact_wf-e95b8be7-ea52-4e2b-aa62-ac20fe5a2ce1_text_markdown.md` | FIR / target curves / preference EQ | 432 | `6d0314542b6215ce85d961ba5c60caeba46d5d634121f5c79bcd3b1acff9f362` | [`HANDOFF-calibration-agent.md`](../../HANDOFF-calibration-agent.md), [`docs/calibration-agent/`](../../calibration-agent/) |
| [`raw/fir-room-correction-implementation-blueprint.md`](raw/fir-room-correction-implementation-blueprint.md) | `JTS FIR Room Correction Research.md` | FIR DSP architecture | 327 | `91548d6dc50e51c745d11de3d9d54d0bbe5a5deeffb0bff59e5ebbfedda8529d` | [`HANDOFF-calibration-agent.md`](../../HANDOFF-calibration-agent.md), [`docs/calibration-agent/`](../../calibration-agent/) |
| [`raw/active-speaker-dsp-commissioning-architecture.md`](raw/active-speaker-dsp-commissioning-architecture.md) | `DIY DSP Speaker Commissioning Research.md` | Active two-way DSP commissioning | 369 | `edd7c9b230a16f1908a07622e0b5b3a5d12e1b0b02227c5b868b1f5006c00a76` | [`HANDOFF-active-speaker-dsp.md`](../../HANDOFF-active-speaker-dsp.md) |
| [`raw/active-crossover-measurement-workflow.md`](raw/active-crossover-measurement-workflow.md) | `deep-research-report (3).md` | Active crossover workflow | 96 | `1bd2f7109d6889e6d32962a2e537c4ca827054698eca2718d32723294386ee54` | [`HANDOFF-active-speaker-dsp.md`](../../HANDOFF-active-speaker-dsp.md) |
| [`raw/jts-two-way-camilladsp-commissioning-plan.md`](raw/jts-two-way-camilladsp-commissioning-plan.md) | `compass_artifact_wf-49865d5e-0039-4341-bdd2-0d4cd4826800_text_markdown.md` | JTS-specific two-way proposal | 264 | `7bdadb2bb45e07b44d26d8ea75cca09f12decb3041ac9f4f537f6ba9a42fdfd2` | [`HANDOFF-active-speaker-dsp.md`](../../HANDOFF-active-speaker-dsp.md) |

## Known Risks

- Raw LLM claims may be wrong or overconfident.
- Some citations use non-portable `turn...` citation markers from
  model sessions.
- Some reports include embedded equation-image markers.
- No obvious secrets or API keys were found in a quick scan.
- Total size is small: about 294 KB / 1,672 lines.

Last archived: 2026-05-25
