# 2026-08-31 tuning-methodology deep research — index and adjudications

> Five owner-run deep-research reports answering Wave 6's research
> assignments (see `docs/tuning-master-plan.md`, ticket 6.9; the fifth —
> documentation architecture — was issued after the first four). Files
> 01–05 are frozen verbatim snapshots; this index records what the
> program adopted, deferred, or rejected from them, and where each
> disposition lives. Dispositions were adjudicated by the conductor
> session and ruled by the owner on 2026-08-31.

| # | Finding (report) | Disposition | Where it lives |
|---|---|---|---|
| 1 | Filter budgets have no published basis; count, phase accumulation, and 64-bit numerics cleared outright (01) | Adopted as reassurance; no code change | operator guide |
| 2 | Flat ±dB tolerance is not audibility-shaped; Olive NBD/SM is the published alternative (01) | Adopted as co-metrics; the band table stays the acceptance lineage | ADR-0202, ticket 6.13 |
| 3 | Single-point 0° fitting risks axis-local artifacts; spatially averaged targets are standard practice (02) | Adopted: pooled horizontal window co-reported; fit-target adoption decided on audit facts, by owner ruling | ADR-0202 |
| 4 | Persistence-across-angle reading thresholds (stable within ±0.5 dB → keep; >~2 dB change or frequency shift → axis-local) (02) | Adopted as reading guidance for the LLM — never a veto | operator guide |
| 5 | FDW genuinely unresolved as an EQ-target transform; valuable as a fixed-gate-disagreement diagnostic (03) | As target: stays not-built. As diagnostic rungs: funded | ADR-0201, ticket 6.10 |
| 6 | The gate ladder is a jackknife-over-window-parameter — novel in audio tooling; quantify per-feature spread (03) | Adopted (6.1 shipped the rungs; spread reading is LLM guidance) | ticket 6.1, operator guide |
| 7 | 1/T is the hard floor; 2.5/T is defensible prudence; the zone between is grey — carry per-feature cycles-in-gate (03) | Adopted as a derived field | ticket 6.11 |
| 8 | A short gate corrupts the Hilbert min-phase reconstruction — EGD must read the longest clean window (03) | Adopted: instrument audit, fix if wrong | ticket 6.11 |
| 9 | The 410 Hz +2.2 dB boost is unsupportable as justified; SBIR-family in-gate reflection is the prime suspect (03) | Overtaken by the incumbent's retirement (ADR-0203); the mechanism map still feeds the recommissioning campaign's priors | ADR-0203, acceptance run |
| 10 | Structure before response; EQ fitted on a misaligned sum is invalid and must be discarded and re-derived (04) | Adopted by owner ruling — the recommissioning campaign | ADR-0203 |
| 11 | Phase-overlay corridor: 60° is convention layered on the established summation math (`20·log10(2·cos(Δφ/2))`; 120° additive boundary, McCarthy) (04) | Adopted as a derived field + guide content | ticket 6.11, operator guide |
| 12 | Sideways-cabinet trick: the horizontal turntable sweeps the vertical plane (04) | Adopted as a verification protocol for the committed alignment | ticket 6.12 |
| 13 | Impedance-sweep-first topology detection (04) | Rejected — the toolbox is microphone-only (standing ruling); the report's acoustic-only corroborations enter the guide as the mic-only lane | ADR-0200, operator guide |
| 14 | Near-field / ground-plane LF truth path with Keele math (03) | Stays parked with bass extension; the math is banked here for when it un-parks | ADR-0192, file 03 |
| 15 | Klippel QC / control-theory safety patterns: apply→verify→rollback, validity gates, checkpoints (04) | Already shipped (pins, auto-restore, integrity class); recorded as external validation, no new machinery | doctrine, operator guide |
| 16 | Reverse-null endorsed as fine-verification; depth targets are convention; level-gap ceiling must be read (04) | Validation of the shipped instrument (whose branch-gap ceiling is stricter than documented practice) | operator guide |
| 17 | Three-tier progressive disclosure; resident context degrades instruction-following measurably (05) | Adopted — the operator surface is tiered (orient verb → on-demand docs → `--help`) | ADR-0204, ticket 6.4 |
| 18 | Per-tool contracts in the tool's own `--help` + teaching error messages — the field's best-measured lever (05) | Adopted; per-tool `.md` documents rejected as a drift surface | ADR-0204, ticket 6.4 |
| 19 | Hand-maintained per-tool manual sections drift; generate the index from the tools' own metadata (05) | Adopted — the runbook menu becomes a generated, regeneration-pinned index | ADR-0204, ticket 6.4 |
| 20 | Skills packaging: preliminary independent evidence shows most skills add ~1%, some hurt (05) | Rejected for the tuning flow; revisit only with this program's own evidence — the campaign stays the eval (ADR-0192) | ADR-0204 |
| 21 | Indirect prompt injection is documented and in-the-wild; tool output is an attack surface (05) | Adopted — tool output is data, never authority (guide honesty rule; the packet's quarantine invariant already held the operator-prose half) | operator guide, ADR-0204 |
