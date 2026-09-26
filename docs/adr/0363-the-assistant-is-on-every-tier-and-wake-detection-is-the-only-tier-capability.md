# ADR-0363: The assistant is on every tier; wake detection is the only tier capability

- **Date:** 2026-09-25
- **Status:** Accepted. Supersedes (partial)
  [ADR-0217](0217-a-streambox-runs-the-assistant-only-while-a-mic-bearing-remote-is-paired.md)
  §1; §§2–5 stand, and where they say a profile granting `ASSISTANT` they now
  mean every profile.
- **Context:** ADR-0217 §1 had the streambox grant `ASSISTANT` and not
  `WAKE_DETECTION`. The full tier already granted `ASSISTANT`, so from then on
  both tiers held it, and an unreadable install marker falls back to
  streambox, which holds it too. The gate could never close. It still guarded
  nine nav rows, six jasper-web wizards, four jasper-control routes and the
  accessory reconciler's voice ownership, and the install-time bake shipped a
  `voice_brain` key that was always `true`.
- **Decision:** `Capability.ASSISTANT` is deleted. The assistant is available
  on every tier, ungated: its wizards, `/session/start`, `/session/end`,
  `/cue/play` and `/system/restart/voice`. `WAKE_DETECTION` is the only
  capability a tier grants; the full tier has it and the streambox does not,
  because the Zero 2 W lacks the headroom for always-on wake inference
  (ADR-0217, Context). The baked capability map is the grant table, one key
  per capability, so its only key is `wake_detection`. The Wake corpus row
  gates on it too, since its wizard is served only where wake detection is
  granted; the separate `developer_tools` key, which was true exactly on the
  full tier, goes.
- **Consequences:** A streambox still runs the voice daemon only while a
  mic-bearing remote is paired: the accessory reconciler owns it wherever wake
  detection is absent (ADR-0217 §2). A future tier that should not offer the
  assistant needs a new capability and a new ADR. Rejected: keeping
  `ASSISTANT` for that tier in advance, because a gate no tier can close is a
  check that never runs.
