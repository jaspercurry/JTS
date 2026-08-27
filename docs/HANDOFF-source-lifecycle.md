# Handoff: local source lifecycle

> **Status: operational.** Single current-truth owner for the household on/off
> lifecycle of AirPlay, Spotify Connect, Bluetooth, and USB Audio Input:
> persisted intent, runtime convergence, follower-role parking, boot/deploy
> replay, and operator-visible failure states. It does not own source selection
> or audio routing ([audio-paths.md](audio-paths.md)), USB gadget composition
> ([HANDOFF-usb-gadget.md](HANDOFF-usb-gadget.md)), or grouped playback
> ([HANDOFF-multiroom.md](HANDOFF-multiroom.md)). Decisions:
> [ADR-0147](adr/0147-one-source-coordinator-with-three-appliers-no-lifecycle-daemon.md),
> [ADR-0148](adr/0148-every-source-unit-re-reads-canonical-intent-at-its-own-start-boundary.md),
> [ADR-0149](adr/0149-a-blocking-unit-start-client-waits-past-pid-1s-legal-maximum.md).
> Dated JTS4 hardware evidence:
> [historical/source-lifecycle-jts4-evidence-2026-07.md](historical/source-lifecycle-jts4-evidence-2026-07.md).

## The contract

`/var/lib/jasper/source_intent.env` is the single source of truth for what the
household wants enabled. Unit enablement, process state, BlueZ `Powered`,
RF-kill state, and USB gadget shape are derived runtime state.

| Source | Intent key | Shipped default |
|---|---|---|
| AirPlay | `JASPER_SOURCE_INTENT_SHAIRPORT_SYNC_SERVICE` | `enabled` |
| Spotify Connect | `JASPER_SOURCE_INTENT_LIBRESPOT_SERVICE` | `enabled` |
| Bluetooth | `JASPER_BLUETOOTH_SOURCE_INTENT` | `enabled` |
| USB Audio Input | `JASPER_SOURCE_INTENT_JASPER_USBSINK_SERVICE` | `disabled` |

A missing file or key uses that table. Recognized values are exactly `enabled`
or `disabled`. The root reconciler treats the management-group-writable file as
untrusted: it accepts only the four fixed keys derived from
`jasper.local_sources`, caps input at 64 KiB, requires UTF-8, and never accepts
a unit name, command, adapter, or arbitrary operation from it. A malformed value
on a recognized source is fail-closed for that source — the coordinator attempts
its complete safe Off teardown, logs the invalid desired state, and returns
non-zero while other valid sources still converge. An unknown owned key stays a
loud global problem but maps to no source and so authorizes no hardware action.
Full-map readers are strict; the per-source `source_intent_enabled(source)`
reader raises only for that source's malformed value, so an invalid AirPlay
value cannot park valid Spotify or Bluetooth resources.

The user-facing state keeps intent, effective state, and capability separate:

- **desired** — the persisted household choice (the compatibility field
  `enabled` on `/sources/state` means the same thing).
- **effective** — what the speaker can currently provide: `on`, `off`,
  `degraded`, `parked`, or `unavailable`.
- **available** — whether the current hardware/install can provide a future On
  transition, independent of effective state: desired Off with every resource
  withdrawn is `effective=off` even when `available=false`.

An apply failure does not roll desired state back: the POST returns an error,
the UI reads the saved choice back and shows it checked with a degraded reason,
so a Bluetooth switch may truthfully read "set to on, but the Bluetooth radio is
not ready" instead of snapping back to Off.

`parked` and `unavailable` are deliberately different. `parked` means grouping
temporarily denies an otherwise-supported local source and retains derived
enablement for restore. `unavailable` means desired On cannot currently be
provided — for USB, when the hardware resolver assigns the data controller to
output-DAC host mode (or a role change awaits reboot); the coordinator then
preserves household intent, withdraws every derived USB audio resource, and
reports the stable hardware reason without calling the expected state a failed
apply.

## The final start boundary

Every declared source-owned unit has an
`ExecCondition=/opt/jasper/.venv/bin/jasper-local-source-allowed --source <id>`
that re-reads current grouping role and canonical household intent immediately
before `ExecStart`. Off or follower parking cleanly skips the start even when
unit enablement or a maintenance snapshot is stale; malformed intent fails
closed with `event=local_sources.guard_intent_failed`. The `<id>` vocabulary
comes from the local-source registry, and a contract test derives every source
resource — plus optional Bluetooth accessory adapter services (currently the
WiiM remote mic) from the accessory registry — so a new declaration cannot ship
without its matching gate. Rationale, the privileged-boundary hardening, and
the deliberate exceptions (`jasper-mux.service`, `jasper-usbgadget.service`) are
in [ADR-0148](adr/0148-every-source-unit-re-reads-canonical-intent-at-its-own-start-boundary.md).

The intent file and the three adjacent update/request/reconcile locks stay
`root:jasper 0660` below the non-world-traversable state directory; install
heals all four to that mode.

## One coordinator, three appliers

Both `/sources/` and `/bluetooth/` call
`jasper.source_intent.request_source_intent`, which atomically updates one fixed
key and then synchronously asks the restart broker to start
`jasper-source-intent-reconcile.service`. The root oneshot reads all four
intents and converges each source independently. A request-only advisory lock
covers the whole write-plus-apply transaction across both web processes so
concurrent toggles cannot overtake each other; the reconciler holds a separate
advisory lock so boot, deploy, systemd, and direct invocations cannot apply
different snapshots at once.

Each completed pass atomically publishes a root-owned
`/run/jasper-source-intent/status.json` with the exact intent-file fingerprint,
a monotonic completion timestamp, and every source's
desired/effective/result/reason outcome. A request accepts only a fresh matching
fingerprint plus its exact target outcome; a start that joined an older
activation triggers one fresh pass. An aggregate sibling failure does not poison
a target that explicitly succeeded; missing, malformed, stale, or mismatched
status fails loudly.

Timeouts are derived, never chosen —
[ADR-0149](adr/0149-a-blocking-unit-start-client-waits-past-pid-1s-legal-maximum.md)
holds the rule and the ladder (2693 s coordinator, 2703 s broker wait,
`proxy_read_timeout 5600s` on both surfaces, 120 s for every other broker verb).
USB and Bluetooth select their concrete ordered appliers first; any remaining
lifecycle declaration with `intent_unit is not None` uses the ordinary systemd
mechanism.

### 1. Ordinary systemd: declared intent units

The coordinator mirrors desired state to every source-owned runtime unit's
enablement, then starts them only when desired and local sources are allowed.
Off means disabled and stopped; a bonded follower keeps desired enablement but
stops the runtime, so unpairing can restore the household choice.

Before the first intentional desired-On start, the applier probes all runtime
units owned by that source and clears and verifies any stale failure/start-limit
latch. This preflight includes required companions such as NQPTP, because
starting the intent unit may ask systemd to bring them up before their later
explicit verification turn. A healthy active unit receives neither
`reset-failed` nor another start; reset or start failures stay bounded, fail the
owning source loudly, and do not block the other sources.

The two shipped ordinary declarations are AirPlay — intent unit
`shairport-sync.service`, with companion timing daemon `nqptp.service` following
the same intent through the local-source resource registry — and Spotify, whose
sole runtime/intent unit is `librespot.service`.

### 2. Bluetooth: control plane, radio, and resource units

`bluetooth.service` (`bluetoothd`) is shared control-plane infrastructure, not
the source intent unit: turning Bluetooth Off never disables or stops it, so the
management UI can still reach the control plane and turn the source back on. The
source-owned resource group is `bluealsa.service`, `bluealsa-aplay.service`, and
`bt-agent.service`, and desired state is mirrored to all three so boot cannot
resurrect a household-disabled Bluetooth source; runtime role restore also
returns through this coordinator.

- **On:** ensure `bluetooth.service` is active, wait a bounded interval for the
  kernel RF-kill radio, RF-unblock Bluetooth, retry BlueZ `Powered=true` while
  the adapter settles, then start bluealsa, bluealsa-aplay, and the pairing
  agent.
- **Off:** stop those resource units in reverse order, set BlueZ
  `Powered=false`, then soft-block Bluetooth with `rfkill`; the three resource
  units remain disabled across reboot.
- **Parked follower:** keep desired enablement, stop the three resource units,
  and do not introduce a new RF-kill block. Role parking suppresses local
  playback/advertising; it does not rewrite household intent or invent radio
  policy grouping cannot undo.

Every mutation is followed by an observation. Off teardown is fail-closed and
aggregating: it attempts every resource stop, the BlueZ power-down, accessory
parking, and the final RF-kill even if an earlier step failed. A hard-blocked
radio, a failed D-Bus property write, or enablement/activity that does not
converge makes only Bluetooth degraded and returns a non-zero result.

Optional Bluetooth accessories stay modular: after the source/radio transition
the coordinator starts the fixed `jasper-accessory-reconcile.service` owner,
which combines Bluetooth intent with role permission and then enables or parks
its own declared adapter services — the source coordinator never learns their
unit names. Two serialized starts guarantee one accessory pass began after the
latest intent even if voice startup already had that oneshot activating.

### 3. USB Audio Input: ordered, idempotent recompose

`jasper-usbsink.service` is the derived lifecycle/enablement readiness mirror
consumed by gadget composition; it is not a second intent store and cannot
authorize audio when canonical intent or role denies it.
`jasper-usbgadget.service` owns the composite descriptor; NCM management is
independent of USB Audio Input intent but exists only when the resolved hardware
role supports a gadget.

On enable, the coordinator writes unit enablement first, starts the fan-in
USB reconciler while UAC2 is still absent so the direct lane is armed and
waiting, recomposes the gadget only when the UAC2 card is absent, starts the
process-free USB readiness marker, then proves both `/proc/asound/UAC2Gadget`
and a present `idle`/`capturing` direct lane. On disable or role park, it stops
and disables the audio lifecycle first, recomposes only when the UAC2 card is
present, proves the card disappeared, then always invokes that reconciler to
verify that no persisted direct lane survived. It is idempotent, so this
bounded verification does not imply an audio-graph restart when its derived plan
is already correct. That order prevents an advertised audio device with no ready
consumer. If an On transition fails, cleanup preserves desired On but withdraws
derived enablement, UAC2, and direct capture; stopping the composite gadget is
the last-resort fail-closed state if UAC2 cannot be withdrawn.

Before either sequence, the USB applier reads the reconciled hardware role. When
USB audio hardware is unavailable it disables/stops the readiness marker,
withdraws UAC2, and disarms direct capture: desired On returns
`effective=unavailable` with the resolver reason, desired Off returns
`effective=off` once that withdrawal is proven, and the Sources surface
independently reports `available=false` plus the hardware reason. In a stable
host or unsupported role it also stops the entire composite gadget. One bounded
deployment grace exists: while a Zero-class controller is still actively
peripheral but a host-role change is pending reboot, the applier keeps or
restores NCM-only composition so a deployment using that link can finish, with
strict audio availability false and UAC2 withdrawn; after reboot activates host
mode, the next reconcile stops the gadget normally. The applier never changes
saved source intent or the USB controller role — only the hardware
installer/reconciler owns that boot decision.

Runtime capture health does not participate in this lifecycle boundary. Fan-in
locally reopens a stale direct UAC2 handle and exports health/reopen telemetry,
but only this coordinator may translate canonical intent, effective role, and
hardware availability into a host-visible UAC2 change — so an audio-path
recovery attempt cannot silently disable both the USB output and the exported
microphone.

The operation is idempotent: an unrelated source toggle does not re-enumerate
USB. The fan-in USB reconciler may receive a bounded convergence request, but it
restarts fan-in only when the derived plan actually changed; an unchanged
CamillaDSP confirm uses the emitted-YAML equality fast path and reloads only
when real drift exists. The NCM
function remains available while audio is Off or parked **when the board is
gadget-capable**. Composition, network
addressing, and gadget teardown details stay in
[HANDOFF-usb-gadget.md](HANDOFF-usb-gadget.md); the USB audio data plane stays
in [HANDOFF-usbsink.md](HANDOFF-usbsink.md).

The `/system/` USB-forensics repair is a maintenance restart of that same
composite owner, not a second lifecycle writer: bring-up re-reads the canonical
intent/role gates, and `PartOf=jasper-usbgadget.service` makes the readiness
marker re-prove the resulting UAC2 card.

Malformed or unreadable USB intent also fails closed at that boundary.
The reconciler treats USB authorization as false, writes the ordinary
explicit-disabled fan-in combo plan, completes its ordered restart when needed,
then emits `result=auto_usb_intent_fail_closed` and returns nonzero. Thus a stale
previously armed direct-capture lane cannot survive the same malformed value
that parked the source; unrelated source state remains untouched.

## Role parking

Household intent and runtime permission are different inputs: a valid bonded
follower may not advertise or play local sources. The grouping reconciler first
lands its role/data plane, then synchronously invokes the canonical source
coordinator, which stops each source resource group without disabling household
intent — USB is disarmed and recomposed so its audio function disappears while
its management network remains. `/sources/` and `/bluetooth/` report
`effective=parked`, disable their local radio/source controls, and reject
mutations with HTTP 409 while the follower role is active.

`grouping.env` remains the household's requested bond even when safety checks
refuse it and the box lands solo. The grouping reconciler records the landed
local-source permission in the root-owned
`/var/lib/jasper-grouping/effective-role.json`, bound to both a fingerprint of
the exact parsed request and the current Linux boot ID. Its `StateDirectory` is
persistent so a prior deny survives an interrupted transition or reboot, while
boot freshness prevents a prior-boot grant from enabling sources. A missing,
malformed, mismatched, unwritable, or untrusted status never grants a follower
local sources.

Both transition directions are fail-safe: grouping publishes the deny first,
completes the role/DSP plan, and publishes the matching same-boot grant only
after every derived file, DSP restore/apply, role unit, and refresh has landed.
Any failed step leaves sources parked for a later retry, and a landed follower
receiving a new solo/leader request reports `role_transition_in_progress` from
its stale `local_sources_allowed=false` fact until that sequence completes. Only
then does grouping invoke the source coordinator.

The source coordinator, every unit `ExecCondition`, the management UI, local
volume forwarding, and the AirPlay supervisor all consume this same
effective-role fact. Deploy health independently cross-checks `grouping.env`
against the landed Snapcast units; the saved bond request and its
`blocked_reason` stay visible so the user can repair and retry it.

On unpair, grouping invokes the same source coordinator, which re-reads
persisted desired state and restores only allowed sources. It owns USB's
arm-direct → recompose-UAC2 → start-liveness order and Bluetooth's radio,
runtime-unit, and accessory-owner order. Grouping never iterates the source
registry or invokes accessory/USB owners directly. If a source pass was
already activating, grouping joins it without interruption and then runs one
fresh pass; its own success is withheld until source convergence completes.
AirPlay latency changes use `systemctl try-restart` so grouping can refresh an
active receiver without resurrecting a household-disabled one; wedge recovery
uses ordinary `restart` to recover a fully dead desired-On receiver, with the
final ExecCondition making a concurrent Off/park win. The full grouped-playback
order and UI contract remain in [HANDOFF-multiroom.md](HANDOFF-multiroom.md).

## Triggers and recovery

- **User action:** `/sources/` handles all four sources; `/bluetooth/`'s Power
  switch writes the same Bluetooth intent. Pairing mode and scanning stay
  adapter-local, gated by effective radio power and role permission.
- **Boot:** `jasper-source-intent-reconcile.service` is enabled for both install
  profiles, wanted by `multi-user.target`, ordered after
  `systemd-rfkill.service` and `hciuart.service`, and bounded by
  `TimeoutStartSec=2693`. It has no `Restart=` loop (deploy, boot, role changes,
  and the next toggle are the bounded replay points) and no ordering pull on
  `bluetooth.service` — the Bluetooth applier starts the control plane when On
  requires it.
- **Deploy:** both profile installers refresh only renderers that were already
  active, then invoke the same coordinator directly as root with `--reason
  install --invalidate-status-before`. Install removes the prior completion fact
  before waiting, drains the canonical reconcile lock for at most 2698 seconds,
  and removes the fact again under that lock before applying. Timeout warns and
  leaves no acceptable acknowledgement, so deploy health fails closed rather
  than certifying stale state. Install never enables an Off source as a
  temporary baseline; a failed source warns and install continues so the web UI
  stays available, and boot or the next toggle retries.
- **Role change:** grouping changes no source state itself and never writes the
  intent file — see Role parking above.
- **Maintenance restore:** correction and the one-time librespot OAuth claim may
  remember what they temporarily stopped, but that snapshot only decides whether
  to *request* restoration; the final unit gate decides whether it happens.

## Observability and failure model

```sh
journalctl -u jasper-source-intent-reconcile.service -b --no-pager
```

Stable events:

- `event=source.intent_requested`, `event=source.intent_write_failed`, and
  `event=source.intent_apply_failed` at the request boundary;
- `event=source.intent_sibling_failure` when the requested target succeeded but
  another source made the aggregate pass fail. It carries `failed_siblings`
  naming the declared source(s) that failed the same pass, so the warning cannot
  be read as a failure of the toggle the household pressed;
  `failed_siblings=null` means the aggregate failed with no failing source to
  name (a rejected intent key, or an unpublishable completion fact);
- `event=source_intent.begin` with the trigger reason;
- one `event=source.reconcile` per source with `desired`, `effective`, `result`,
  and a bounded failure `reason`;
- `event=source_intent.reconciled` with applied/failure counts;
- `event=source_intent.status_write_failed` when the root completion fact could
  not be published; and
- `event=source_intent.read_failed`, `event=source_intent.rejected_unit`, or
  `event=source_intent.bad_value` for an unreadable or untrusted intent file.

For a malformed recognized value, the matching `event=source.reconcile` uses
`desired=invalid result=failed reason=invalid_intent_fail_closed`; its
`effective=off` confirms the safe teardown landed without disguising invalid
persisted state as a valid household Off choice.

Failures isolate per source — one broken adapter does not stop the others
converging — but any failure makes the oneshot exit non-zero. `/sources/state`,
`/bluetooth/state`, the `/system/` audio cards, and `jasper-doctor` distinguish
intentional Off from degraded runtime state: the Bluetooth surfaces compare
RF-kill, BlueZ power, and required resource units, and doctor validates both
desired-On radio readiness and Off-but-active drift. The system audio-health
surface derives Off drift from each source's parked units, not its desired-On
health dependencies — USB's management gadget may remain active while USB Audio
Input is Off on gadget-capable hardware, and only an active USB audio/volume
resource is Off drift.

The low-memory deploy probe parses the complete fixed four-source contract
(including USB's shipped Off default), rejects unknown keys in the owned intent
namespace, requires all four units to match intent, validates Bluetooth radio
state, and validates USB's UAC2 card plus direct fan-in lane (`present` and
`idle`/`capturing`) for On with both absent for Off. A confirmed bonded follower
switches those checks to parked expectations;
[HANDOFF-install-update-transaction.md](HANDOFF-install-update-transaction.md)
owns the complete deploy gate.

## JTS4 validation checklist

Run after deploying a lifecycle change. Use the browser surfaces as the primary
result and the shell only to explain a mismatch.

1. Open `http://jts4.local/sources/` and `http://jts4.local/bluetooth/`. Both
   Bluetooth switches show the same desired state, and pairing mode/Scan are
   disabled while effective power is Off.
2. Turn Bluetooth Off. Both `/state` surfaces report `desired=false`,
   `effective=off`; `bluetooth.service` stays available; bluealsa,
   bluealsa-aplay, and bt-agent are inactive and disabled; `rfkill list
   bluetooth` reports a soft block.
3. Reboot. The switches stay Off, the three resource units stay
   inactive/disabled, and the boot journal shows four successful
   `source.reconcile` events.
4. Turn Bluetooth On. Desired/effective become `true`/`on`, RF-kill is clear,
   BlueZ reports `Powered: yes`, the three resource units are enabled and
   active, and a scan/pair proves the control plane survived the cycle.
5. With JTS4's USB output DAC selected, USB Audio Input reports
   `available=false` with the shared-port-reserved reason, no UAC2/NCM gadget is
   composed, and the saved desired value is not rewritten (saved Off →
   `effective=off`; saved On → `effective=unavailable`). A Zero with a
   registered I²S DAC is the separate positive gadget-mode target.
6. Redeploy once with Bluetooth Off and once On, then run
   `sudo deploy/bin/jasper-deploy-health` from the deployed checkout: the
   persisted state is accepted in both cases, not repaired to a package default.

Last verified: 2026-08-26 (intent keys and shipped defaults rechecked against
`jasper/local_sources/registry.py`; the event vocabulary against
`jasper/source_intent.py` and `jasper/local_sources/guard.py`; the timeout
ladder against `jasper/control/restart_broker.py`,
`deploy/systemd/jasper-source-intent-reconcile.service`, and the nginx confs).
Prior 2026-08-04 (desired-On failed/start-limit recovery ordering, healthy-unit
no-op behavior, bounded reset/start failures). Prior 2026-07-15 (JTS4 hardware
evidence — now in
[historical/source-lifecycle-jts4-evidence-2026-07.md](historical/source-lifecycle-jts4-evidence-2026-07.md)).
