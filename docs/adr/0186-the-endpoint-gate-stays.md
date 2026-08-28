# ADR-0186: The endpoint gate stays

- **Date:** 2026-08-27
- **Status:** Accepted

## Context

The right-sizing campaign's audit considered `deploy/bin/jasper-audio-hardware-reconcile`'s
outputd-endpoint check a candidate for removal: the ring model gives outputd no
capture PCM to pair with a playback device (a ring reads its FILE sink
directly), so the pairing the check resolves is not one the current audio
path needs answered for its own sake.

The check's shape: `resolve_outputd_capture_device()`
(`jasper-audio-hardware-reconcile:458`) shells out to `python3 -m
jasper.cli.audio_config outputd-capture-device --playback-device
outputd_content_playback`, which calls
`jasper.camilla_config_contract.outputd_capture_device_for_playback`. That
function looks `RETIRED_ALOOP_PLAYBACK_DEVICE` ("outputd_content_playback")
up in `_OUTPUTD_CAPTURE_BY_PLAYBACK_DEVICE`, a **one-entry map** holding only
the retired snd-aloop pair — every ring device is deliberately absent, since
a ring has no outputd capture half to pair. On every fleet box today the key
matches and the lookup succeeds, returning `DEFAULT_OUTPUTD_CAPTURE_DEVICE`
("outputd_content_capture"), a value the reconciler never reads again once
it has checked that the lookup was non-empty. If the CLI fails or the lookup
misses, `outputd_endpoint_contract_failed_exit()` (line 607) logs
`outputd_endpoint_contract_failed`, either parks the box into a
preserved-env `backend=fake` idle state
(`park_preserved_env_if_clockless()` — the `outputd_env_clockless_park`
path, issue #2489) when the box otherwise looks fully configured, or leaves
it as-is, and then unconditionally `exit 66`s.

So the check's value today is not the string it resolves — nothing consumes
it — but the act of resolving it: it is the one point in the reconcile
where a `python3 -m jasper.cli.audio_config` invocation must actually
import `jasper`, load `camilla_config_contract`, and return successfully
before the reconcile proceeds. A broken Python environment, a missing
module, or a corrupted config-contract import surfaces here, loud, at
boot/udev time, instead of silently downstream. It is also the sole trigger
of the #2489 clockless park — the mechanism that idles a box safely instead
of spinning it through a restart loop when the endpoint contract cannot
resolve.

## Decision

**Keep the gate.** `resolve_outputd_capture_device` →
`outputd_endpoint_contract_failed_exit` → `exit 66` stays in
`jasper-audio-hardware-reconcile`, unchanged, as the reconcile's only
fail-loud "Python works" tripwire and the sole trigger of the #2489
clockless park. The resolved capture-device value stays deliberately
unused past the truthy check.

**The one-entry map and the gate are landmined together.**
`_OUTPUTD_CAPTURE_BY_PLAYBACK_DEVICE` in `jasper/camilla_config_contract.py`
is not independently trimmable: its only key is
`RETIRED_ALOOP_PLAYBACK_DEVICE`, the exact constant the gate feeds in as
`DEFAULT_OUTPUTD_PLAYBACK_DEVICE`. Removing the entry while the gate stands
turns today's universal hit into a universal miss — every box parks or
exits 66 on every reconcile. **The gate and the map entry retire together
in one change, or neither retires at all.**

## Consequences

The reconcile keeps a code path whose resolved value has no downstream
reader — a shape that reads as dead weight to a line-count audit but is
doing exactly one job on purpose: proving the Python side of the reconcile
still runs before anything commits. Rejected alternative: delete the check
as unused-value dead code, which the audit's line-count lens favored; ruled
out because it would remove the only mechanism that turns a broken Python
environment into a loud, parked failure instead of a silent one, with
nothing proposed to replace it.

A future removal is not foreclosed. If the ring model ever needs a real
reason to resolve an outputd capture pairing, or a different fail-loud
Python check replaces this one, that is a new decision superseding this
one, and it must retire the map entry in the same change. Until then, the
map entry and the gate move together, never one alone.
