# ADR-0372: A remote mic is armed only after its adapter runs

- **Date:** 2026-09-26
- **Status:** Accepted. Supersedes (partial)
  [ADR-0225](0225-accessory-bridges-share-one-interpreter.md) where it makes
  the one published env file the only handle on the adapter, and
  [ADR-0217](0217-a-streambox-runs-the-assistant-only-while-a-mic-bearing-remote-is-paired.md)
  decision 2, which ran a streambox's voice while a remote was paired: it
  now runs while a remote is armed.
- **Context:** Under ADR-0225, `accessory-mics.env` was both jasper-input's
  instruction (which adapters to run) and jasper-voice's list of armed
  sources. So the reconciler armed a source before the host that produces it
  had restarted, and a failed host refresh still left the source armed with
  voice converged onto it. The owner's contract for the remote's lifecycle
  (#3346) defines armed as a paired, registered mic remote whose adapter has
  started and passed its readiness check.
- **Decision:**
  1. `jasper-accessory-reconcile` is the single writer of a second file,
     `/var/lib/jasper/accessory-adapters.env`, in the same format, with a
     header naming its writer: the sources whose adapters jasper-input runs.
     jasper-input reads it in Python when it starts; no unit sources it.
  2. Each pass withdraws first, then instructs the host, then arms. A source
     keeps its arming only while its adapter runs untouched. A pass that
     restarts the host therefore withdraws every source before the restart.
     A source enters `accessory-mics.env` only after the restarted host's
     status shows its adapter running and answered by BlueZ, with the remote
     either connected or asleep. That wait is bounded.
  3. A source that fails that check stays unarmed, and voice is not
     converged onto it. The pass fails loudly
     (`accessory_mic.host_refresh_failed`, then a failed oneshot).
  4. Voice converges only when a pass leaves the armed file different. A
     source withdrawn and re-armed within one pass does not bounce voice.
- **Consequences:**
  - **Armed means a verified producer.** Nothing the reconciler arms lacks
    one, so whether a hold can stream depends only on the remote's live
    link, which the adapter publishes.
  - **A dead host is reported, not disarmed.** A host that dies later, seen
    by a pass that does not restart it, is reported by that pass's failure,
    by the doctor, and by voice refusing each hold with the cue. It is not
    withdrawn: that pass has no boot ordering against jasper-input, and a
    boot pass racing the host's start would otherwise disarm the remote
    until the next pairing.
  - **A slower worst-case pass.** The reconciler's worst case grows by the
    verify bound (10 s, still inside the unit's 60 s), and a pairing pass
    waits for the host's cold start.
  - **Upgrade.** The install's reconcile pass writes the plan and restarts
    the host. It re-arms the remote once the adapter answers, without
    restarting voice.
  - **Re-checking.** A source that failed the check is re-checked on the
    next pass: a pairing, a Bluetooth toggle, or boot.
