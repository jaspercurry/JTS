# ADR-0382: A tweeter's cap is declared or derived, else the plan refuses by name

- **Date:** 2026-09-28
- **Status:** Accepted. Supersedes (partial)
  [ADR-0227](0227-owner-rulings-the-prose-pass-surfaced.md) §9: the class default is no longer a
  high-frequency driver's fallback on the program path. Its two invariants and its named residual
  stand.
- **Context:** On the program path a high-frequency driver that declares no level limit takes its
  cap from a low-frequency sibling's cap less the declared sensitivity delta (ADR-0227 §9). When a
  sensitivity was missing, the path logged `excitation_ceiling_derivation_skipped` and played the
  tweeter at the −65 dBFS class default, a figure sized for a naked tone with no protective
  high-pass, so it measured tens of decibels under its sibling for no visible reason. A stored limit
  equal to that default was also read as "no limit", the seed of a retired contract; jts3 stores
  none ([#2913](https://github.com/jaspercurry/JTS/issues/2913),
  [#5928](https://github.com/jaspercurry/JTS/issues/5928) TB12).
- **Decision:**
  1. On the program path a high-frequency driver's cap is its declared `max_effective_peak_dbfs`,
     else the sensitivity-delta derivation. When the tweeter, or every low-frequency output,
     declares no sensitivity, the plan refuses before it plays: `driver_sensitivity_undeclared`,
     naming the roles, with the next action "Declare this driver's sensitivity". Every other driver
     keeps its declared cap or its class default; a full-range driver's is −65 dBFS.
  2. A stored value equal to the class default is a declaration.
  3. Anchoring stays per role: an output that declares no sensitivity takes its role's, and a role
     whose outputs disagree reads as undeclared.
  4. No cross-check against the preset's `sensitivity_db`
     ([#2765](https://github.com/jaspercurry/JTS/issues/2765)): a commissioned speaker's preset
     copies it from the same declaration. A swapped pair shows as a tweeter cap near full scale.
- **Consequences:** No tweeter falls back to the class default on the program path; a missing
  sensitivity is fixed at /sound/speaker/, where the refusal points. A stored seed plays at the
  −65 dBFS it holds, which is quieter than its old derived cap unless the woofer's own cap is under
  −65 dBFS plus the sensitivity delta. Session volume, admission and every ADR-0365 probe keep
  reading one resolver. Rejected: keeping the class default as the fallback, which is what hid the
  missing declaration.
