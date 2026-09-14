# ADR-0313: The CamillaController refuses a graph that breaks the hearing ceiling

- **Date:** 2026-09-14
- **Status:** Accepted
- **Context:** Non-negotiable 1 says `devices.volume_limit` stays at or below
  0 dB in every CamillaDSP config, because CamillaDSP defaults the main
  fader's maximum to +50 dB when the key is absent and its own `--check`
  accepts a positive one. Until now the only enforcement was
  `dsp_apply._volume_limit_safety_error`, reached solely through
  `validate_camilla_config` inside `apply_dsp_config`'s file transaction.
  `CamillaController` has three graph-write doors that never asked:
  `set_active_config_raw` (raw upload, eight product call sites),
  `set_config_file_path` (three call sites outside that transaction), and
  `patch_config`. A graph without a ceiling could install through any of them.
- **Decision:** The rule is one function —
  `camilla_config_contract.check_volume_limit`, raising
  `VolumeLimitViolation` with a `code` of `volume_limit_missing`,
  `volume_limit_positive` or `patch_touches_devices` — and the controller,
  the last Python hop before the daemon, asks it at every door before the
  graph mutation, the way `_coerce_main_volume_db` already clamps every
  fader write. `set_config_file_path` reads the file it is about to install;
  an unreadable path is disclosed (`camilla.graph_admission_unreadable`,
  WARNING) and proceeds, because CamillaDSP's own load fails loudly there and
  a read race must not invent a verdict. `patch_config` refuses any patch
  carrying a top-level `devices` key: a patch may write parameters of
  running filters, never the block the ceiling lives in. A refusal logs
  `camilla.graph_refused` at ERROR with its source and code, returns `False`
  to a `best_effort` caller and otherwise raises — a `ValueError`, so the
  `except CamillaUnavailable` sites do not swallow it.
  `dsp_apply._volume_limit_safety_error` now consumes the same function, so
  the apply gate and the doors cannot drift.
  The three doors stay three: a file path moves the durable rollback anchor,
  a raw upload installs a graph without moving it, and a patch writes running
  parameters — collapsing them would change product behavior, not remove
  duplication.
- **Consequences:** Every emitter must carry `devices.volume_limit`; a graph
  that omits it now fails at the boundary instead of installing silently and
  raising the fader ceiling. An ambiguous config (duplicate `devices` or
  `volume_limit` keys) fails closed as missing. The doctor's own
  `devices.volume_limit` readers stay independent and detective; converging
  them on this function is follow-up work (#5063), not part of this decision.
