# Brief 04 — LAN trust model: document it, pin the floors, close the GET leak

Mission: `docs/REVIEW-2026-06-12-oss-due-diligence.md` §4.5. The LAN-trust
posture is defensible for a home appliance but must be *stated*, and two
cheap floors are missing.

Branch: `codex/lan-trust`. File fence: `SECURITY.md`,
`deploy/configure-bluez.sh`, `jasper/web/*` (read-path guard only),
`jasper/http_security.py`, `jasper/web/_common.py` (new helper only — brief
09 deletes a legacy block elsewhere in this file; different regions, merges
cleanly), plus tests.

## PR 1 — SECURITY.md threat-model section (docs only)

Add a "Threat model — what network position gets you" section stating
explicitly, with endpoint specifics:

- Any LAN device can, without authentication: set volume, toggle the privacy
  mic-mute (`POST :8780/mic/mute`), change AEC profiles, reboot/power off
  (`/system/reboot`, `/system/poweroff`), and rewire multiroom bonds
  (`/grouping/set`). Browser-origin attacks are blocked (Host/Origin/
  Sec-Fetch-Site guards + CSRF); raw LAN clients are trusted by design —
  the same posture as the dial, HA, and shortcuts integrations.
- Setup wizards submit API keys / HA token / WiFi PSK over plain HTTP on the
  LAN (nginx serves HTTP; only /correction/ is HTTPS). State the
  guest-VLAN/rogue-AP implication and the mitigation (run setup on a trusted
  network; keys are stored 0600 server-side).
- Bluetooth: pairing uses Just-Works auto-accept *inside an explicit
  300-second pairing window* opened from /bluetooth/ (matches Echo/Sonos
  norms); at rest the speaker is non-discoverable and non-pairable (PR 2
  makes that a config floor, not a runtime best-effort).
- Peering/multiroom control messages are unauthenticated multicast on the LAN
  (spoofable by design today); name it and the planned HMAC follow-up.
- Future-work line: an opt-in shared-token for power/mic mutations on 8780 is
  under consideration — document only, do not implement.

Keep SECURITY.md's existing honest tone; it already discloses the wizard-GET
gap — update that paragraph when PR 3 closes it.

## PR 2 — Bluetooth `Pairable=false` at-rest floor

- `deploy/configure-bluez.sh` (~lines 53-55) pins `Discoverable=false` but
  never bare `Pairable=false`; today non-pairability-at-rest depends on the
  agent's best-effort runtime `set_discoverable(False)` rollback. Add
  `Pairable=false` to the emitted main.conf block so the floor holds even if
  the agent crashes mid-window.
- Verify the pairing window still works: `jasper/bluetooth/adapter.py`
  `set_discoverable` must already set `Pairable=true` at window-open (read the
  code; if it only toggles Discoverable, fix it to toggle both, window-scoped).
- Add the missing config-parse test (the review found the "not pairable at
  rest" promise untested): assert configure-bluez.sh's emitted config contains
  both `Discoverable=false` and `Pairable=false` — house style is a
  source-parse test next to `tests/test_bt_agent_systemd.py`.

## PR 3 — host-guard the wizard read path (DNS-rebinding read leak)

- `jasper/http_security.py` has `management_read_allowed`, but only
  jasper-control calls it; no `jasper/web/*_setup.py` GET handler checks Host,
  so a DNS-rebinding page can read masked-secret wizard pages, SSIDs, etc.
- Implement once in the shared layer: a `guard_read_request(handler)` helper
  in `jasper/web/_common.py` that applies `management_read_allowed` (same
  allowlist + Avahi-rename heuristic semantics as the mutating guard) and
  returns 403 with the same friendly body the control server uses. Wire it at
  the top of every wizard's `do_GET` — follow the existing
  route-check-then-guard ordering convention so unknown paths still 404
  without revealing guard state.
- Extend `tests/test_web_wizard_conventions.py` with an AST sweep asserting
  every wizard `do_GET` calls the new guard (mirror the existing do_POST
  chokepoint test), plus behavioral tests: evil-Host GET → 403, normal-Host
  GET → 200, on at least two wizards. Check `/state`-polling pages (wifi)
  still work — their fetches carry the real Host.
- Update SECURITY.md's known-gap paragraph to "closed as of <PR>".

## Acceptance

- `pytest tests/test_web_wizard_conventions.py tests/test_http_security.py
  tests/test_bt_agent_systemd.py -q` green (plus the new tests); `ruff check .`
  clean; shellcheck passes on configure-bluez.sh (`bash -n` + severity=warning,
  matching CI).
