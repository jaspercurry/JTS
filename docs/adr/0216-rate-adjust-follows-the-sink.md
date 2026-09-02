# ADR-0216: `enable_rate_adjust` follows the sink, not grouping membership

- **Date:** 2026-09-02
- **Status:** Accepted
- **Context:** `jasper.multiroom.member_config.member_camilla_kwargs` spelled
  `enable_rate_adjust=False` into EVERY member shape — leader, follower, solo,
  off, invalid — because two different sinks answer the same way. A `File`/pipe
  sink has no output clock at all (snapclient's sample-stuffing is the synced
  chain's one rate-tracker, inv-5), and since ADR-0100 every other member plays
  into a ring PCM, which is an ioplug alsa-lib reports as card -1, so CamillaDSP
  builds no HCtl and cannot actuate the request. Those are facts about the SINK
  that a member's role happens to correlate with. Worse,
  `fanin_coupling.member_kwargs_are_pipe_sink` then INFERRED "the sink is already
  owned" from that flag, which made it True for every production shape including
  solo. Issue #3556; PR #3562 deferred this proposal.
- **Decision:** `jasper.camilla_config_contract.resolve_enable_rate_adjust`
  answers the field from the playback device / pipe path, and
  `emit_sound_config(enable_rate_adjust=None)` (the new default) takes that
  answer. `member_camilla_kwargs` carries only what grouping genuinely decides:
  the leader's `playback_pipe_path` plus its L/R delays; every other shape
  returns `{}`. `member_kwargs_are_pipe_sink` is deleted — the two callers that
  needed "is this sink already owned?" test `playback_pipe_path` directly.
- **Consequences:** One place answers "can this graph's rate adjuster actuate
  anything", so a future non-ring ALSA sink gets the right answer without a
  member-policy edit, and an explicit `True` alongside a pipe path still fails
  loud at the emitter. Production emits are unchanged on every box as shipped: every
  live path already passed `False` explicitly or through member kwargs. One
  path moves only on a box that declares the narrow ring wire
  (`JASPER_FANIN_RING_WIRE_FORMAT=S16_LE`): a solo `/sound` or `/correction`
  re-emit now takes the coupling's playback half too (the deleted predicate
  dropped it), so its `playback: format` follows the declared wire instead of
  the emitter's `S32_LE` default — the only width the ring ioplug opens there. The golden fixtures flip
  from `true` to `false` because they exercised the emitter's own default, which
  used to be a literal `True` no production caller accepted. The PARKED emitter
  in `jasper.active_speaker.camilla_yaml` is excluded deliberately: it writes
  `enable_rate_adjust: false` as a template literal because its sink is always a
  `File` and it takes no device to resolve from.
  Rejected: keeping the flag in member policy (it made a sink fact look like a
  grouping decision, and every branch had to restate it); and keeping a
  predicate that reads sink ownership off the flag (always True in production,
  so it answered a question it was not asking).
