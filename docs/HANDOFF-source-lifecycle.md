# Handoff: local source lifecycle

> **Status: operational.** This is the single current-truth owner for the
> household on/off lifecycle of AirPlay, Spotify Connect, Bluetooth, and USB
> Audio Input: persisted intent, runtime convergence, follower-role parking,
> boot/deploy replay, and operator-visible failure states. It does not own
> source selection or audio routing (see [audio-paths.md](audio-paths.md)), USB
> gadget composition (see [HANDOFF-usb-gadget.md](HANDOFF-usb-gadget.md)), or
> grouped playback (see [HANDOFF-multiroom.md](HANDOFF-multiroom.md)).

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

A missing file or key uses that table. Recognized values are exactly
`enabled` or `disabled`. The root reconciler treats the management-group-writable file
as untrusted: it accepts only the four fixed keys derived from
`jasper.local_sources`, caps input at 64 KiB, requires UTF-8, and never accepts
a unit name, command, adapter, or arbitrary operation from the file. A
malformed value on a recognized source is fail-closed for that source: the
coordinator attempts its complete safe Off teardown, logs the invalid desired
state, and returns non-zero while other valid sources still converge. An
unknown owned key remains a loud global problem but maps to no source and
therefore authorizes no hardware action. Full-map readers remain strict; the
per-source `source_intent_enabled(source)` reader raises only for that source's
malformed value, so an invalid AirPlay value cannot park valid Spotify or
Bluetooth resources.

The user-facing state keeps intent, effective state, and capability separate:

- **desired** is the persisted household choice. The compatibility field
  `enabled` on `/sources/state` means the same thing.
- **effective** is what the speaker can currently provide: `on`, `off`,
  `degraded`, `parked`, or `unavailable`.
- **available** says whether the current hardware/install can provide a future
  On transition. It is independent of effective state: desired Off with every
  resource withdrawn is `effective=off` even when `available=false`.

An apply failure does not roll desired state back. The POST returns an error,
but the UI reads the saved choice back and shows it checked with a degraded
reason. This is why a Bluetooth switch may truthfully read “set to on, but the
Bluetooth radio is not ready” instead of snapping back to Off.

`parked` and `unavailable` are deliberately different. `parked` means grouping
temporarily denies an otherwise-supported local source and retains derived
enablement for restore. `unavailable` means desired On cannot currently be
provided. For USB, that happens when the hardware resolver assigns the data
controller to output-DAC host mode (or a role change awaits reboot): the
coordinator preserves household intent but disables/stops every derived USB
audio resource and reports the stable hardware reason without treating the
expected state as a failed apply. If the saved choice is already Off and those
resources are withdrawn, effective remains `off`; the independent availability
field and reason still explain why On cannot be selected.

## The final start boundary

The coordinator is the normal lifecycle writer, but it is not the only thing
that can ask systemd to start a unit: boot, dependency pulls, operator commands,
room-correction cleanup, and the librespot OAuth claim can all issue a start or
restart. Therefore every declared source-owned unit has an
`ExecCondition=/opt/jasper/.venv/bin/jasper-local-source-allowed --source <id>`.
The fixed `<id>` vocabulary comes from the local-source registry. Immediately
before `ExecStart`, the guard re-reads both current grouping role and canonical
household intent. Off or follower parking cleanly skips the start even when
unit enablement or a maintenance snapshot is stale. Malformed/unreadable source
intent fails closed and emits `event=local_sources.guard_intent_failed`; it
never falls back to a shipped On default at this security boundary. The
USB guard also requires the shared `usb_data_role.gadget_available` capability,
so stale unit enablement or a manual start cannot bypass output ownership of a
Zero's shared OTG port. The composite gadget's separate privileged boundary
uses `management_transport_available`: this keeps NCM-only service during the
named pending-host/current-peripheral deploy grace, never UAC2. The
intent file remains `root:jasper 0660` below the non-world-traversable state
directory. Renderer users are not added to the intent-writer group. Each final
guard instead crosses a narrow privileged boundary with a fixed
`/usr/bin/env -i` argv and fixed `PATH`; its unit first unsets native-loader
injection variables (`LD_PRELOAD`, `LD_LIBRARY_PATH`, `LD_AUDIT`, and
`GLIBC_TUNABLES`) before `/usr/bin/env` itself starts. Python therefore sees no
renderer-controlled environment while root can read the fixed file. The three
adjacent update/request/reconcile locks also remain `root:jasper 0660`.
The units do not order themselves after or require the source coordinator:
that coordinator starts the units, so such an edge would deadlock. The final
gate reads the atomic intent file directly and is safe at boot.

The contract test derives every source resource from
`jasper/local_sources/registry.py`, and derives optional Bluetooth adapter
services (currently the WiiM remote mic) from the accessory registry, so a new
declaration cannot ship without the matching source gate. Shared
`jasper-mux.service` retains the generic role-only guard because it has no one
source intent. `jasper-usbgadget.service` also cannot carry a whole-unit source
gate: its NCM management network must survive USB Audio Off and follower
parking. Instead, both `jasper-usbgadget-wanted` and `jasper-usbgadget-up` ask
the same source-aware `--source usbsink` guard before composing `uac2.usb0`,
then require the coordinator-derived `jasper-usbsink.service` enablement as a
readiness mirror. Canonical intent plus role remains the authority: Off or
follower parking dominates stale enabled state. Conversely, desired-On with a
failed/stale disabled mirror suppresses UAC2 instead of advertising an audio
device with no ready consumer. Derived enablement is never interpreted as
household preference.

## One coordinator, three concrete mechanisms

Both `/sources/` and `/bluetooth/` call
`jasper.source_intent.request_source_intent`. It atomically updates one fixed
key, then synchronously asks the restart broker to start
`jasper-source-intent-reconcile.service`. The root oneshot runs
`jasper-source-intent-reconcile`, reads all four intents, and converges each
source independently. A request-only advisory lock covers the complete
write-plus-apply transaction across both web processes, so concurrent toggles
cannot overtake each other. Each completed pass atomically publishes a
root-owned `/run/jasper-source-intent/status.json` with the exact intent-file
fingerprint, a monotonic completion timestamp, and every source's
desired/effective/result/reason outcome. A request normally makes one bounded
synchronous start and accepts only a fresh matching fingerprint plus its exact
target outcome. If that start joined an older activation, the stale
acknowledgement triggers one fresh pass. An aggregate sibling failure does not
poison a target that explicitly succeeded; missing, malformed, stale, or
mismatched status fails loudly. Each exact blocking `start` of this singleton
helper may wait up to 793 seconds, just beyond the
coordinator's finite 783-second systemd ceiling. Every other broker unit/verb
and every `--no-block` shape retains the 120-second hard ceiling; the broker
derives this exception from the validated request rather than trusting the
client's requested number alone. The coordinator's 783-second ceiling has an
explicit complete budget for enablement/activity probes and actions, bounded
failed-state probes and `reset-failed` actions for desired-Off units,
BlueZ/RF-kill work, USB direct-lane settling and failed-On cleanup,
Bluetooth's two 65-second accessory barriers, USB's 125-second coupling
barrier, and a final process margin. Every source-owned unit declares finite
`TimeoutStartSec` and `TimeoutStopSec`; its blocking client waits one second
longer than the relevant service ceiling, or one second longer than their sum
for `restart`, so the client cannot time out while PID 1 may legally continue
the job. AirPlay gets a 30-second start ceiling for its root pre-start renderer,
and its client also includes the required NQPTP cold-start ceiling plus margin
(the old generic 15-second client was observed false-timing-out on JTS4), while
USB gets 40 seconds because `jasper-usbsink-wait-card` may validly consume 30
seconds before the oneshot readiness marker becomes active (exited). Owner starts retain their larger
65/125-second clients, deliberately beyond the target oneshots' 60/120-second
bounds. The reconciler itself has a separate advisory lock so boot, deploy,
systemd, and direct invocations cannot apply different snapshots concurrently.
Install heals the intent file and all three shared locks to `root:jasper 0660`;
the lock primitive does not follow symlinks and does not require a
group writer to chmod an already-correct root-owned lock.

The HTTP envelope matches the same synchronous contract. Both `/sources/` and
`/bluetooth/` use `proxy_read_timeout 1700s` in the full and streambox nginx
configs: the stale-join case can require two broker calls at 793 seconds plus
the broker client's five-second response margin, with bounded handler/readback
slack. The generous path timeout does not make any child
unbounded; the 783-second coordinator and 60/120-second owner limits remain the
enforcement points.

There is deliberately no resident lifecycle daemon and no plugin API. The
smallest durable design is one coordinator with three concrete appliers:

### 1. Ordinary systemd: declared intent units

The coordinator mirrors desired state to every source-owned runtime unit's
enablement, then starts them only when desired and local sources are allowed.
Off means disabled and stopped. A bonded follower keeps desired enablement but
stops the runtime, so unpairing can restore the household choice.

USB and Bluetooth select their concrete ordered appliers first. Any remaining
lifecycle declaration with `intent_unit is not None` uses this ordinary
systemd mechanism; there is no second `{AIRPLAY, SPOTIFY}` dispatch list to
maintain. This is a small declaration-based branch, not a plugin framework.
The two shipped ordinary declarations are AirPlay and Spotify.

AirPlay's intent unit is `shairport-sync.service`; its companion timing daemon
`nqptp.service` follows the same intent through the local-source resource
registry. Spotify's sole runtime/intent unit is `librespot.service`.

### 2. Bluetooth: control plane, radio, and resource units

`bluetooth.service` (`bluetoothd`) is shared control-plane infrastructure, not
the source intent unit. Turning Bluetooth Off never disables or stops it; the
management UI can still reach the control plane and later turn the source back
on.

The source-owned resource group is `bluealsa.service`,
`bluealsa-aplay.service`, and `bt-agent.service`. Desired state is mirrored to
all three units so boot cannot resurrect a household-disabled Bluetooth source;
runtime role restore also returns through this coordinator.

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
converge makes only Bluetooth degraded and returns a non-zero coordinator
result.

Optional Bluetooth accessories remain modular: after the source/radio
transition, the coordinator starts the fixed
`jasper-accessory-reconcile.service` owner. That reconciler combines Bluetooth
intent with role permission, then enables or parks its own declared adapter
services. The source coordinator never learns their unit names. Two serialized
starts guarantee one accessory pass began after the latest intent even if
voice startup already had the accessory oneshot activating.

### 3. USB Audio Input: ordered, idempotent recompose

`jasper-usbsink.service` is the derived lifecycle/enablement readiness mirror
consumed by gadget composition; it is not a second intent store and cannot
authorize audio when canonical intent or role denies it.
`jasper-usbgadget.service` owns the composite descriptor; NCM management is
independent of USB Audio Input intent but exists only when the resolved
hardware role supports a gadget.

On enable, the coordinator writes unit enablement first, starts the fan-in
coupling owner while UAC2 is still absent so the direct lane is armed and
waiting, recomposes the gadget only when the UAC2 card is absent, starts the
process-free USB readiness marker, then proves both
`/proc/asound/UAC2Gadget` and a present `idle`/`capturing` direct lane. On
disable or role park, it stops and disables the audio lifecycle first,
recomposes only when the UAC2 card is present, proves the card disappeared,
then always invokes the coupling owner to verify that no persisted direct lane
survived. The owner is idempotent, so this bounded verification does not imply
an audio-graph restart when its derived plan is already correct.
That order prevents an advertised audio device with no ready consumer. If an
On transition fails, cleanup preserves desired On but withdraws derived
enablement, UAC2, and direct capture; stopping the composite gadget is the
last-resort fail-closed state if UAC2 cannot be withdrawn.

Before either ordinary On/Off sequence, the USB applier reads the reconciled
hardware role. When USB audio hardware is unavailable it disables/stops the
readiness marker, withdraws UAC2, and disarms direct capture. Desired On
returns `effective=unavailable` with the resolver reason; desired Off returns
`effective=off` once that withdrawal is proven. In both cases the Sources
surface independently reports `available=false` and the hardware reason. In a
stable host or unsupported role, the applier also stops the entire composite
gadget. There is one bounded deployment grace: while a Zero-class controller
is still actively peripheral but a host-role change is pending reboot,
management transport remains available. The applier keeps or restores NCM-only
composition so a deployment using that link can finish, but strict audio
availability remains false and UAC2 stays withdrawn. After reboot activates
host mode, the next reconcile stops the gadget normally. The applier never
changes saved source intent or the USB controller role; only the hardware
installer/reconciler owns that boot decision.

The runtime combo-health fallback uses the same ownership boundary. After it
records the fallback marker, it calls the source coordinator's narrow USB
withdrawal phase while fan-in's direct consumer still exists. The coordinator
stops/disables the derived USB readiness mirror and recomposes to NCM-only;
only after that succeeds does the watcher disarm direct capture. A failed
recompose stops the composite gadget fail-closed and leaves direct capture
armed for a later retry rather than advertising consumerless UAC2.

The operation is idempotent: an unrelated source toggle does not re-enumerate
USB. The coupling owner may receive a bounded convergence request, but it
restarts fan-in only when the derived plan actually changed; an unchanged
CamillaDSP confirm uses the emitted-YAML equality fast path and reloads only
when real drift exists. The NCM
function remains available while audio is Off or parked **when the board is
gadget-capable**. Composition, network
addressing, and gadget teardown details stay in
[HANDOFF-usb-gadget.md](HANDOFF-usb-gadget.md); the USB audio data plane stays
in [HANDOFF-usbsink.md](HANDOFF-usbsink.md).

Malformed or unreadable USB intent also fails closed at the coupling boundary.
The coupling owner treats USB authorization as false, writes the ordinary
explicit-disabled fan-in combo plan, completes its ordered restart when needed,
then emits `result=auto_usb_intent_fail_closed` and returns nonzero. Thus a stale
previously armed direct-capture lane cannot survive the same malformed value
that parked the source; unrelated source state and the current valid ring or
loopback coupling remain untouched (a separately invalid/removed coupling still
fails safe to loopback).

## Role parking

Household intent and runtime permission are different inputs. A valid bonded
follower is not allowed to advertise or play local sources. The grouping
reconciler first lands its grouping role/data plane, then synchronously invokes
the canonical source coordinator. That one owner stops each source resource
group without disabling household intent; USB is disarmed and recomposed so its
audio function disappears while its management network remains. The
`/sources/` and `/bluetooth/` report `effective=parked`, disable their local
radio/source controls, and reject mutations with HTTP 409 while the follower
role is active.

`grouping.env` remains the household's requested bond even when safety checks
refuse it and the box lands solo. The grouping reconciler records the landed
local-source permission in the root-owned
`/var/lib/jasper-grouping/effective-role.json`, bound to both a fingerprint of
the exact parsed request and the current Linux boot ID. The dedicated
`StateDirectory` is persistent so a prior local-source deny (from either an
active or dumb follower) survives an interrupted transition or reboot; boot freshness still prevents a prior-boot
grant from enabling sources. A missing, malformed, mismatched, unwritable, or
untrusted status never grants a requested follower local sources. During a refused-follower transition
grouping first publishes a deny, completes the solo DSP restore plus role-unit
plan, and only then publishes the matching same-boot solo grant; any failed step
leaves sources parked for a later retry. The reverse transition is equally
fail-safe: when a landed follower receives a new solo/leader request, its stale
`local_sources_allowed=false` fact returns `role_transition_in_progress`;
grouping publishes a
deny for the new request before touching the role data plane and publishes the
grant only after the derived files, DSP restore/apply, role units, and refreshes
land. Only then does it invoke the source coordinator. The source coordinator,
every unit `ExecCondition`, management UI, local volume forwarding, and AirPlay
supervisor consume this same effective-role fact. Deploy health independently
cross-checks `grouping.env` against the landed Snapcast units. The saved bond
request and its `blocked_reason` remain visible so the user can repair and retry
it.

On unpair, grouping invokes the same source coordinator, which re-reads
persisted desired state and restores only allowed sources. It owns USB's
arm-direct → recompose-UAC2 → start-liveness order and Bluetooth's radio,
runtime-unit, and accessory-owner order. Grouping never iterates the source
registry or invokes accessory/coupling owners directly. If a source pass was
already activating, grouping joins it without interruption and then runs one
fresh pass; its own success is withheld until source convergence completes.
Every source unit still rechecks current intent and role at its final
ExecCondition boundary. AirPlay latency changes use `systemctl try-restart`, so
grouping can refresh an active receiver without resurrecting a household-
disabled one. AirPlay wedge recovery uses ordinary `restart` so it can recover a
fully dead desired-On receiver; the same final ExecCondition makes concurrent
Off/park win. The full
grouped-playback order and UI contract remain in
[HANDOFF-multiroom.md](HANDOFF-multiroom.md).

## Triggers and recovery

- **User action:** `/sources/` handles all four sources; `/bluetooth/`'s Power
  switch writes the same Bluetooth intent. Pairing mode and scanning remain
  adapter-local operations gated by effective radio power and role permission.
- **Boot:** `jasper-source-intent-reconcile.service` is enabled for both install
  profiles, wanted by `multi-user.target`, ordered after
  `systemd-rfkill.service` and `hciuart.service`, and bounded by
  `TimeoutStartSec=783`. It deliberately has no automatic `Restart=` loop;
  deploy, boot, role changes, and the next user toggle are explicit bounded
  replay points. It has no ordering pull on
  `bluetooth.service`; the Bluetooth applier starts the control plane when On
  requires it.
- **Deploy:** both profile installers refresh only renderers that were already
  active, then invoke the same coordinator directly as root with `--reason
  install --invalidate-status-before`. Install removes the prior completion
  fact before waiting, drains the canonical reconcile lock for at most 788
  seconds, and removes the fact again under that lock before applying. The
  outer 793-second process ceiling deliberately leaves little room for a fresh
  pass after a worst-case older pass: timeout warns and leaves no acceptable
  acknowledgement, so deploy health fails closed rather than certifying stale
  state. Install never starts or enables an Off source as a temporary baseline.
  A failed source warns and install continues so the web UI and diagnostics
  remain available; boot or the next toggle retries.
- **Role change:** grouping changes no source state itself and never changes the
  intent file. After its role plan, it drains any older source pass without
  interrupting it, runs a provably fresh canonical source pass, and waits for
  that pass to park/restore sources plus their accessory/coupling subowners.
- **Maintenance restore:** correction and the one-time librespot OAuth claim
  may remember what they temporarily stopped, but that snapshot only decides
  whether to request restoration. The final source-aware unit gate decides
  whether the current household intent and role still permit a start.

## Observability and failure model

The useful journal is:

```sh
journalctl -u jasper-source-intent-reconcile.service -b --no-pager
```

Stable events are:

- `event=source.intent_requested`, `event=source.intent_write_failed`, and
  `event=source.intent_apply_failed` at the request boundary;
- `event=source.intent_sibling_failure` when the requested target succeeded but
  another source made the aggregate pass fail;
- `event=source_intent.begin` with the trigger reason;
- one `event=source.reconcile` per source with `desired`, `effective`,
  `result`, and a bounded failure `reason`;
- `event=source_intent.reconciled` with applied/failure counts; and
- `event=source_intent.status_write_failed` when the root completion fact could
  not be published; and
- `event=source_intent.read_failed`, `event=source_intent.rejected_unit`, or
  `event=source_intent.bad_value` for an unreadable or untrusted intent file.

For a malformed recognized value, the matching `event=source.reconcile` uses
`desired=invalid result=failed reason=invalid_intent_fail_closed`; its
`effective=off` confirms the safe teardown landed without disguising invalid
persisted state as a valid household Off choice.

Failures are isolated per source: one broken adapter does not prevent the
other valid sources from converging, but any failure makes the oneshot exit
non-zero. `/sources/state`, `/bluetooth/state`, the `/system/` audio cards, and
`jasper-doctor` distinguish intentional Off from degraded runtime state. The
Bluetooth surfaces compare RF-kill, BlueZ power, and required resource units;
doctor validates both desired-On radio readiness and Off-but-active drift. The
system audio-health surface derives Off drift from each source's parked units,
not its desired-On health dependencies. In particular, USB's management
gadget may remain active while USB Audio Input is Off on gadget-capable
hardware; only an
active USB audio/volume resource is Off drift. The
low-memory deploy probe parses the complete fixed four-source contract,
including USB Audio Input's shipped Off default, and rejects unknown keys in
the owned intent namespace. It requires AirPlay, Spotify, Bluetooth, and the
USB lifecycle unit to match intent; validates Bluetooth radio state; and
validates USB's UAC2 card plus direct fan-in lane (`present` and
`idle`/`capturing`) for On, with both absent for Off. A confirmed bonded
follower switches those checks to parked expectations. See
[HANDOFF-install-update-transaction.md](HANDOFF-install-update-transaction.md)
for the complete deploy gate.

## JTS4 validation checklist

Run this after deploying the change to JTS4. Use the browser surfaces as the
primary result and the shell only to explain a mismatch.

1. Open `http://jts4.local/sources/` and `http://jts4.local/bluetooth/`.
   Confirm both Bluetooth switches show the same desired state and pairing
   mode/Scan are disabled while effective power is Off.
2. Turn Bluetooth Off. Confirm `/sources/state` and `/bluetooth/state` report
   `desired=false`, `effective=off`; `bluetooth.service` remains available;
   bluealsa, bluealsa-aplay, and bt-agent are inactive and disabled; and
   `rfkill list bluetooth` reports a soft block.
3. Reboot. Confirm the switches remain Off and the three resource units remain
   inactive/disabled. Check the boot journal above for four successful
   `source.reconcile` events.
4. Turn Bluetooth On. Confirm desired/effective become `true`/`on`, RF-kill is
   clear, BlueZ reports `Powered: yes`, and the three resource units are
   enabled and active. Scan/pair a device to prove the control plane still
   works after the Off→On cycle.
5. With JTS4's USB output DAC selected, confirm USB Audio Input reports
   `available=false`, the reason says the shared port is reserved for output,
   no UAC2/NCM gadget is composed, and the saved desired value is not silently
   rewritten. Saved Off must report `effective=off`; saved On must report
   `effective=unavailable`. A Zero configured with a registered I²S DAC is the
   separate positive gadget-mode validation target.
6. Redeploy once with Bluetooth Off and once with it On. Run
   `sudo deploy/bin/jasper-deploy-health` from the deployed checkout and
   confirm the persisted state is accepted in both cases rather than repaired
   back to a package default.

### Historical hardware evidence — JTS4, 2026-07-14 (pre-role reboot)

The streambox-profile JTS4 passed the Bluetooth portion of this checklist
through the real CSRF-protected `/sources/set`, `/bluetooth/power`, and
`/bluetooth/scan` HTTP surfaces. Off produced matching desired/effective state
on both pages, left `bluetooth.service` active, disabled and stopped all three
resource units, set BlueZ `Powered: no`, and soft-blocked `hci0`. Those states
survived reboot. The first boot pass hit the prior AirPlay client timeout; an
explicit supported reconcile retry then converged all four sources without
resurrecting Bluetooth. The timeout contract has since been widened and pinned
to AirPlay's service plus required NQPTP transaction, but that exact boot path
still needs a final JTS4 replay. On restored RF-kill,
`Powered: yes`, all resource units, and a successful scan start/stop. The
shared intent and three lock files landed `root:jasper 0660`, and both web
service owners wrote successfully. Supported deploys in both On and Off states
preserved the saved intent; the low-memory probe certified the Bluetooth radio
and units in both states. At that pre-role-reboot snapshot JTS4 had no
observable configured output DAC, so its unrelated outputd fake-backend
advisory remained. No physical pairing target or USB UAC2 host was available;
pairing and USB re-enumeration remained explicit gaps at that point.

### Current USB/output evidence — JTS4, 2026-07-15

After the role reboot, JTS4 (Zero 2 W) resolved an active `host` data role and
detected its Apple USB-C output DAC. The output-hardware artifact reported the
registered Apple profile ready, outputd used the ALSA backend, Bluetooth was
enabled, USB Audio Input was intentionally unavailable, and strict deploy
health completed with 0 failures / 0 warnings. This validates the Zero
USB-output negative gadget path and output recovery; it does **not** validate
UAC2 or positive gadget mode, which still requires a registered I²S-output
Zero or a board with separate host ports.

Last verified: 2026-07-15 (desired-Off/effective-Off semantics with independent
USB availability, JTS4 active-host Apple-DAC recovery, and the final guard
rechecked separately from follower parking; fingerprinted per-source
completion acknowledgement, source-aware final start boundary, desired-Off
failed-unit reset and timeout budget, declared accessory coverage,
correction/claim restore races, and USB authorization-plus-readiness
composition gate rechecked; the dated 2026-07-14 pre-reboot evidence is
retained above as history.)
