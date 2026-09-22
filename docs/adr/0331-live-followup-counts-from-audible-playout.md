# ADR-0331: Live follow-up counts from audible playout

- **Date:** 2026-09-22
- **Status:** Accepted; refines [ADR-0320](0320-live-hangup-is-one-silence-window.md).
- **Context:** A replay of the local speech detector and Live watchdog closed
  a call despite high-confidence speech starting 160 ms before its deadline.
  Speech needs 200 ms to qualify. Live's 800 ms quiet-output bridge also moved
  the follow-up clock after audible speech had ended.
- **Decision:** One `SpeechActivity` owns local speech-run state and utterance
  timing. A new run holds the watchdog for the confirmation period plus one
  polling interval, at most 450 ms from onset. A subthreshold frame clears
  that hold; missing frames or a run below the peak requirement cannot extend
  it indefinitely. Live marks quiet bridge chunks separately from audible
  output. Playback records the estimated drain of the last accepted audible
  chunk; the two-second follow-up window starts there, without adding bridge
  silence. Physical playback must still drain before closure. Keep the
  separate five-second answer wait and bounded backend waits from
  [ADR-0321](0321-live-first-answer-wait-is-separate-from-followup.md).
- **Consequences:** New speech can qualify across the hang-up boundary. Quiet
  padding still prevents playback gaps, but no longer adds to the follow-up
  wait. A pause longer than the follow-up window within a Live answer can
  still close it: the API has no spoken-answer completion event. Echo rejection
  still depends on the input chain. Endpointed providers and push-to-talk keep
  their existing lifecycles under
  [ADR-0292](0292-followup-windows-are-provider-owned.md).
