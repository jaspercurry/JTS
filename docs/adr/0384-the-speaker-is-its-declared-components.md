# ADR-0384: The speaker is its declared components

- **Date:** 2026-09-28
- **Status:** Accepted
- **Context:** [#4990](https://github.com/jaspercurry/JTS/issues/4990) proposed one per-speaker measurement
  profile for the values every measurement reads. On 2026-09-27 the owner found each value already has an
  owner and dropped the profile ([#5737](https://github.com/jaspercurry/JTS/issues/5737) RX-3 TB13). A
  driver's size was declared twice, as a `radiating_diameter_mm` read by role and as a cabinet's effective
  radiating diameter nothing read, so a rear woofer (ADR-0316) could not have its own.
- **Decision:**
  1. There is no per-speaker measurement profile. Speaker facts are the declaration (the design draft);
     room facts are the declared geometry and the seat anchor.
  2. Gate, band and smoothing are per-analysis parameters every answer echoes
     ([ADR-0344](0344-every-round-view-answer-carries-one-envelope.md) §3). Policy constants stay in code.
  3. A declared driver fact (`radiating_diameter_mm`, `driver_class`) resolves by measurement target id
     through `resolve_design_inputs`, the manual row over research. A target whose outputs disagree, or a
     draft with no topology, declares none; a rear output that declares none takes its front's value.
  4. A driver's size is its own `radiating_diameter_mm`, which the sealed single-radiator check reads. A
     cabinet key the reader does not know refuses `unknown_driver_fields` by name and names its fix, as in
     [ADR-0379](0379-a-stored-driver-declaration-in-a-retired-shape-refuses-by-its-field.md) §1.
- **Consequences:** `woofer:rear` has its own trusted band, piston step and beaming prior. A class declared
  only in research reaches speaker-fit's class prior. A stored draft whose cabinet still carries a size
  refuses until its research is imported again or the key is removed at /sound/speaker/; an unknown manual
  cabinet key refuses where it was once dropped (#2902). Rejected: the profile file, and a read-through
  facade over the existing files.
