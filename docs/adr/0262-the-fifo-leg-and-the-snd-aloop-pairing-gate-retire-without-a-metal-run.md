# ADR-0262: The FIFO leg and the snd-aloop pairing gate retire without a metal run

- **Date:** 2026-09-09
- **Status:** Accepted
- **Context:** [ADR-0220](0220-the-dac-content-marker-is-served-and-its-contradiction-parks.md)
  closed on a condition: "the FIFO spelling, its park trigger and outputd's FIFO
  reader retire together after a bonded pair plays on metal." A metal run was
  the right bar for retiring a leg that had ever carried audio. This one never
  did. `jasper.multiroom.reconcile.outputd_grouping_env` wrote
  `JASPER_OUTPUTD_DAC_CONTENT_FIFO` EMPTY on every branch, armed and unarmed
  alike, and no emitter in the tree ever produced `CONTENT_BRIDGE=direct` — the
  only bridge under which outputd would have read the FIFO. So no box could
  reach the state a metal run would exercise, and waiting for one waits on
  something that cannot happen.
  [ADR-0186](0186-the-endpoint-gate-stays.md) landmined the reconciler's exit-66
  endpoint gate to the same era's `_OUTPUTD_CAPTURE_BY_PLAYBACK_DEVICE` map,
  whose only key was the retired snd-aloop playback half, and required the two
  to retire in one change. #4503 then moved the reconciler to Python, so the
  gate's stated job — proving a `python3 -m jasper.cli.audio_config` invocation
  imports and returns before the pass commits — is now the pass's own import.
- **Decision:** **The FIFO leg retires on the absence of a writer, not on a
  metal run**, superseding that clause of ADR-0220; the rest of ADR-0220 stands.
  **The endpoint gate and its pairing map retire with it**, superseding
  ADR-0186 under the owner's B4 ruling. Gone in one change: the exit-66 gate and
  its clockless-park fallback, `_OUTPUTD_CAPTURE_BY_PLAYBACK_DEVICE` with the
  four device names it was the last shared reader of,
  `outputd_capture_device_for_playback`, the `outputd-capture-device`
  subcommand, the FIFO env key and its three writes,
  `PARK_GROUPED_DAC_CONTENT_LANE` with its household sentence and doctor
  stand-down, and outputd's unreachable no-source period branch. The one
  retired spelling with a live reader left — `classify_camilla_config_text`
  must keep recognizing the stereo snd-aloop lane, or an unreconciled box reads
  as an advanced config JTS refuses to touch — moves private to that reader.
  `POST_DSP_PLAYBACK_DEVICES` keeps the two rings and the active snd-aloop
  endpoint; with no map left, PAIRING collapses to a single fact — no post-DSP
  playback device has an outputd capture PCM — and `transport_coherence_report`
  keeps sole ownership of what each membership MEANS.
- **Consequences:** The round-trip lane has one spelling, the ring, on both
  ends. `transport_coherence_report`'s verdicts are unchanged: the retired
  snd-aloop stereo pair was the map's one entry and is now simply not a member,
  so it stays error-free, and the two rings keep the dispositions they had.
  Deliberately given up: the #2489 clockless park, which only the exit-66 path
  could trigger. Rejected alternative: keep the gate as a "Python works"
  tripwire — the Python reconciler cannot run at all without the import it was
  proving.
