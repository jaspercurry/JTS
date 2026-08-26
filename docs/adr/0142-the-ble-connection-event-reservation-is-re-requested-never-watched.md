# ADR-0142: The BLE connection-event reservation is re-requested, never watched

- **Date:** 2026-08-26
- **Status:** Accepted (ratified on the 2026-08-06 jts4 measurements; recorded
  here when HANDOFF-hotplug-resilience.md was trimmed to its operational spine)

## Context

The WiiM Remote 2's microphone needs 62.5 GATT notifications/second and each
takes about 6 Link Layer PDUs, but BlueZ hardcodes the connection-event length
to 0. On the Pi Zero 2 W's BCM43436 that default admits roughly one PDU per
connection event, so the mic runs at about a quarter of realtime (measured on
jts4: 196/190 packets against 794/805 with a reservation in force).

The reservation is a single `HCI_LE_Connection_Update` on the live link, and
it **lives on the connection** — lost on every disconnect. Worse,
`hci_le_conn_update()` hardcodes `min_ce_len = max_ce_len = 0` and is the
function BlueZ calls to service a *peripheral*-initiated parameter update, so
a remote asking for its own (typically slower) parameters silently overwrites
the reservation mid-connection, with no log line anywhere.

## Decision

**The adapter re-requests the reservation once per connection, immediately
after `char.call_start_notify()`, through jasper-control's restart broker; a
silent mid-connection revert is measured, not defended against.**

- Per-connection, not unit ordering: the reservation cannot be expressed as a
  dependency because it does not outlive the link. The helper unit is
  `StartLimitIntervalSec=0` so a flapping link cannot rate-limit it to `failed`.
- Every step fails soft. A broker that is unreachable, a missing unit, a
  rejected request, or a controller that never confirms all leave audio
  flowing at the starved rate with a WARNING in the journal — the mic is
  degraded, never dead.
- **Only the automatic re-request is declined.** The measurement half ships:
  each stream segment (in practice one push-to-talk hold) logs its delivery
  rate, so an operator can see a starved link.

## Consequences

- Realtime delivery now also depends on `jasper-control` being up, because the
  broker socket is the non-root adapter's only route to the root helper. That
  dependency is worth knowing when reading a slow-mic report.
- Two re-arm implementations were priced and both declined:
  - *Watch the HCI event stream* for the parameter update. HCI has no read path
    for live connection parameters, so this means a btmon-style monitor
    subscription — **a new resident process** on a 415 MB Pi Zero 2 W.
  - *Watch the packet rate* and re-request on collapse. Not a residency cost
    (the adapter already counts packets) and the discriminator is clean
    (~62.5/s holding, ~15/s starved, 0 idle), but it costs a rate window,
    starved-versus-idle logic, and debounce interacting with the helper's
    start path.
- Declined for the same reason: neither is worth carrying to defend against
  something this remote was **measured not to do** — the reservation survived
  ~4 minutes and multiple idle→active cycles on jts4 (2026-08-06). If a
  slow-mic report ever survives every other check with an `applied` event in
  the journal, this is the remaining explanation: capture `btmon` across an
  idle→active cycle, look for a peripheral-initiated parameter update, and
  reopen with that evidence. The packet-rate watcher is the cheaper build.
- The measurement half needs no rate window because a hold boundary already
  exists — the 250 ms inter-packet gap that resets the stream.
