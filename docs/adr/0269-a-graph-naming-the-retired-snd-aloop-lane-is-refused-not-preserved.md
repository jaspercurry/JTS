# ADR-0269: A graph naming the retired snd-aloop lane is refused, not preserved

- **Date:** 2026-09-09
- **Status:** Accepted (amends [ADR-0262](0262-the-fifo-leg-and-the-snd-aloop-pairing-gate-retire-without-a-metal-run.md))
- **Context:** ADR-0262 kept one retired spelling alive in production:
  `classify_camilla_config_text` recognised `outputd_content_playback` so an
  unreconciled box would not read its own pre-ADR-0100 graph as an advanced
  config JTS refuses to touch. Tracing the install order shows no box can
  reach the classifier with that graph. The only JTS-written graph that named
  the lane without a `# Source:` marker was the static seed
  `deploy/camilladsp/outputd-cutover.yml`, retired in #3172; every emitter
  since writes a source marker that classifies before the device name is
  consulted. `deploy/install.sh` runs `jasper-sound render-flat-cutover`, the
  single writer of `/etc/camilladsp/outputd-cutover.yml`, one INSTALL_STEPS
  row before `runtime-safe-graph` classifies anything, and the boot reconciler
  and `jasper-output-topology-reset` call the same writer.
- **Decision:** The retired name leaves `classify_camilla_config_text`;
  `jts_outputd_stereo` means Ring B only. A graph naming the retired lane
  classifies `unknown_custom`, so `converge_boot_statefile` selects the freshly
  rendered flat or parked graph instead of preserving bytes that name a PCM
  with no ALSA definition. `runtime_contract`'s duplicate of
  `ACTIVE_OUTPUTD_PLAYBACK_DEVICE` is deleted; endpoint refusal is membership
  in `OUTPUTD_LEGAL_ENDPOINT_DEVICES`, pinned once for any non-member.
- **Consequences:** No retired lane spelling survives in production code. The
  refuse-by-name pins collapse to one parametrised non-member pin. If a box
  ever reports `unknown_custom_camilla_config` on a graph JTS itself emitted,
  that report is the evidence to bring back, not a reason to widen the set.
