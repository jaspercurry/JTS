# ADR-0266: Fan-in publishes only evidence that has a reader

- **Date:** 2026-09-09
- **Status:** Accepted. Supersedes (partial)
  [ADR-0254](0254-runtime-buffers-are-bounded-and-drop-and-count.md): its
  decision item 2, the fan-in xrun channel and its `XrunSink`. Items 1
  (playout queue) and 3 (TTS servers) stand unchanged, and so does the
  bounded drop-and-count shape item 2 established — the impulse tap's
  channel still carries `EVENT_CHANNEL_CAPACITY` and `send_drop_counted`,
  which is what `mixer.rs` cites ADR-0254 for. Supersedes (partial)
  [ADR-0205](0205-the-airplay-offset-ledger-is-four-terms-not-three.md):
  `ring_a_frames`' first-choice term only. The four-term ledger, its
  remaining tiers and every other term stand.
- **Context:** ADR-0254 bounded fan-in's xrun channel without asking whether
  anything read what it carried. Nothing did: the `fanin-xrun-writer` thread
  wrote a JSONL artifact under `JASPER_FANIN_XRUN_LOG_PATH` that no doctor
  check, no `/state` reader and no operator tool has ever opened, and its
  `xrun_events_dropped` gauge could only report the loss of lines with no
  reader. The same shape held on the output side: since ADR-0100 left
  CamillaDSP reached over SHM with no playback PCM, `output.xrun_count`,
  `output.snd_pcm_delay_frames` and `output.snd_pcm_delay_ms` have been
  pinned at 0/null on every box, and ADR-0205 still named the ALSA-delay
  field as `ring_a_frames`' first choice.
- **Decision:** Fan-in's STATUS and its off-thread writers carry only fields
  a reader consumes on the shipped topology. The xrun channel, its writer
  thread, `XrunSink`/`XrunEvent`/`XrunSource`, the
  `JASPER_FANIN_XRUN_LOG_PATH` knob and the `xrun_events_dropped` key are
  deleted, and so are the three pinned `output.*` keys. Fan-in's xrun
  evidence is `inputs[].xrun_count`, `inputs[].last_xrun_age_ms` (a stale
  count must read differently from a live one) and the `event=fanin.xrun`
  journal line. Ring A's live latency term is
  `output.ring.occupancy × output.period_frames`, ADR-0205's second tier,
  which is now its first.
- **Consequences:** An xrun storm leaves counts, recency and journal lines
  rather than a JSONL file, and the mixer thread loses a channel send at each
  xrun site instead of gaining one. What this gives up: per-event forensic
  records with fields the journal line does not carry, which nothing had
  claimed. Removal condition for that trade — a reader that genuinely needs
  per-event xrun detail earns the sink back, sized to that reader.
  *Rejected:* keeping the bounded channel until a reader appeared (an unread
  buffer's bound protects nothing), and keeping the `output.*` keys as
  documented-null (a key that can only be null teaches every consumer to
  branch on it).
