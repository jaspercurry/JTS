# ADR-0105: Each speaker derives its own USB /30 from its CPU serial

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

The USB management network (`ncm.usb0`) originally reused Raspberry Pi OS's
rescue-gadget address, `10.12.194.1`, fleet-wide: every JTS speaker answered
on the same device address over its own USB link. That is safe with one
speaker attached and unsafe with two. With both plugged into one Mac, macOS
could resolve `jts3.local` to the shared address and route it through the USB
interface belonging to a *different* speaker; the management-host guard then
correctly rejected a request that had reached the wrong box. Distinct
hostnames (mDNS) and distinct MACs (already derived per Pi) do not
disambiguate overlapping IP routes — the route table has one entry for that
prefix and picks one interface.

## Decision

**Each speaker derives its own IPv4 /30 for `usb0` from its immutable CPU
serial**, under the versioned `cpu-serial-sha256-v1` plan owned by
`jasper/usb_network.py` — the only derivation, validation, rendering, and
attestation surface. The Pi takes the first usable address and the attached
computer the second. The allocation space is `10.64.0.0/10` (1,048,576
possible /30s), so a household collision is negligible without a registry or
any operator-visible setting. Only the derived /30 is installed as a route,
`never-default=true`, with no gateway and no DHCP router/DNS option.

The plan is persisted to a root-owned artifact and **re-attested against the
current Pi at boot**, so a cloned or moved SD card cannot reuse another Pi's
subnet.

mDNS stays the canonical UX: `jts.local` resolves over the USB link because
Avahi advertises on all multicast interfaces. The derived address is a
diagnostic fallback surfaced by `/state` and doctor, not an address a user
memorises.

## Consequences

- Several household speakers can be attached to one computer at once, each
  reachable at its own hostname over its own link.
- JTS no longer promises the rescue gadget's raw `10.12.194.1`. Anything that
  hard-coded it must read `/state.usb_network.desired_address` instead.
- The /10 is private space that can overlap an enterprise or VPN range. The
  installed route is four addresses wide and never-default, which bounds the
  blast radius but does not eliminate it.
- Upgrades from the legacy generation cannot flip live: if `usb0` is up on a
  different address, promotion is deferred to the next boot so an install
  running over that very link cannot strand itself. Both projections
  (NetworkManager keyfile and dnsmasq conf) are published together under one
  owner lock, and a partially written pair is repaired by the next successful
  plan run before the gadget may expose `usb0`.
- Rejected: a per-speaker operator setting or a wizard field. The serial is
  already the stable per-box identity used for the gadget serial number and
  MAC derivation; adding a knob would add a way to configure a collision.
