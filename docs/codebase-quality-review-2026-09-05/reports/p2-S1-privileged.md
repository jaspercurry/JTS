# S1 — privileged actions end to end (browser/LLM/CLI → root) @ 2d571e6b8

## A. Verdict

The tree has a **good** mediated boundary (`restart_broker`: SO_PEERCRED, closed verb
vocabulary, unit allowlist, audit line, per-request timeout, derived polkit rule pinned by
a test) and **does not use it where it matters most**. Every *external* client (jasper-web
wizards, mux, input, fan-in, active_speaker, wake-corpus, source_intent) goes through the
broker and, with two exceptions, checks the result. The daemon that *hosts* the broker does
not: **14 direct mutating `systemctl`/`reboot` argv sites live inside `jasper/control/` (plus one
`Popen` of a root shell helper) against 2 brokered ones**, and eleven of them discard the
exit code entirely. Since the WS1 3b-2 user
drop those calls are polkit-mediated, so "discarded exit code" now means "a polkit denial is
invisible" — and there is one live denial today (`jasper-usbsink-volume.service`) plus a
second class nobody has noticed (`systemctl reload avahi-daemon`, granted to neither
`jasper-control` nor `jasper-web`). The authority model is spread over five artefacts
(`MANAGED_UNITS`, `START_ONLY_UNITS`, the two `.rules` files, the `local_sources` registry,
and the per-caller unit literals); only two of the five pairs are drift-pinned, and the one
test that *is* meant to catch "a unit an endpoint touches isn't grantable" is a hand-written
literal set. `server.py:2258`'s claim that jasper-control "is the single mediated systemctl
boundary" is false, and `restart_broker.py:41` says so in the same repo.

## B. Hop lists

### The ten distinct shapes

| # | shape | example route/caller | mediated? | rc checked? |
|---|---|---|---|---|
| A | browser → nginx → `jasper-system-web` (uid jasper-web) → HTTP → jasper-control → **direct `Popen(systemctl)`** → polkit → PID 1 | `/system/restart/{voice,audio}`, `/system/reboot`, `/system/poweroff`, `/system/audio-quality` | no | **no** |
| B | same, but control uses `restart_broker.{manage_units,reset_then_manage}` | `/aec/enhanced-aec/install` (handlers/aec.py:467), `/aec/usb-mic-leg` (:289) | yes | yes → 502 |
| C | browser → nginx → wizard (uid jasper-web) → `manage_units` → UDS → broker → polkit → PID 1 | `/speaker`, `/airplay`, `/wifi/scan`, `/sound` i2s, `/wake-corpus`, `restart_voice_daemon` | yes | mixed |
| D | wizard → env write + broker `start` of a **root oneshot** that performs the un-brokerable verb | `/sources`, `/bluetooth` → `source_intent` (`enable`/`disable`) | yes | **yes, with a fingerprinted completion ack** |
| E | wizard/control → file mailbox → `.path` unit → root worker | `/usb-forensics`, `accessory-reconcile.request` | n/a | via status file |
| F | jasper-control resident supervisor → direct `systemctl`/`reboot` | `shairport_supervisor.restart_shairport`, `system_supervisor.reboot_system` | no | **no** |
| G | non-root sibling daemon → `manage_units` → broker | `mux.py:1902`, `wiim_remote_mic.py:393`, `coupling_reconcile.py:199`, `startup_load.py:181`, `runtime_convergence.py:303`, `output_topology_runtime.py:70` | yes | yes |
| H | root reconciler / sudo CLI → direct `systemctl` | `multiroom/reconcile.py`, `source_intent.py:881`, `accessories/reconcile.py:280`, `jasper/cli/*` (9 sites) | n/a (holds privilege) | yes |
| I | non-root daemon → `systemctl reload avahi-daemon` with **no grant in either polkit rule** | `peering/avahi.py:133` (control), `avahi_service.py:226` ← `web/speaker_setup.py:300` | no | **no** |
| J | jasper-control → `Popen(/usr/local/sbin/jasper-grouping-reconcile-kick)` → `systemctl` | `server.py:1091` | no | no (helper self-journals) |

### Shape A, in full: click "Restart audio" on `/system/`

| # | hop | file:function | boundary |
|---|---|---|---|
| 1 | click | `deploy/assets/system-status/js/actions.js:postAction` | browser |
| 2 | `postControlAction("/system/restart/audio")`, attaches `X-CSRF-Token` + `X-JTS-Token` from `meta[jts-control-token]` | `deploy/assets/shared/js/http.js:103-112` | browser |
| 3 | nginx `location /system/` → `127.0.0.1:8772` | `deploy/nginx-jasper.conf:334` | process |
| 4 | route allowlist, `guard_mutating_request` (Host/Origin/Sec-Fetch), then `proxy_post("/system/restart/audio", headers=forward_control_token_headers(self))` | `jasper/web/system_setup.py:152-197` | process (uid **jasper-web**) |
| 5 | `_guard_management_read`/`_guard_mutating_request` → `_guard_install_profile_route` → `_guard_control_token` (route is in `_TOKEN_GATED_ROUTES`) | `jasper/control/server.py:1664-1946` | process (uid **jasper-control**) |
| 6 | `restart_units=CORE_AUDIO_RESTART_UNITS`; `try_restart_units=LOCAL_SOURCE_AUDIO_REFRESH_UNITS`; parked-follower filter; `log_event(system.action)`; **`subprocess.Popen(["systemctl","try-restart", …])`** | `jasper/control/handlers/system.py:456-514` | — |
| 7 | polkit `org.freedesktop.systemd1.manage-units`, unit-scoped | `deploy/polkit/49-jasper-control.rules:52-108` | kernel/polkitd |
| 8 | PID 1 runs (or refuses) the job | systemd | — |

### Failure branch at every hop

| hop | failure | surfaces as | verdict |
|---|---|---|---|
| 3 | wizard socket-activated, cold start / OOM | nginx 502 | honest |
| 4 | bad Host/Origin | 403 `reject_csrf` | honest |
| 4 | jasper-control unreachable | `proxy_post` → 502 `{"error":"jasper-control unreachable"}` | honest |
| 5 | token file unreadable (EACCES) | `_stored_token()` → `""` → gate **silently OFF** | documented (`control_token.py:112-124`), doctor `check_control_token` reports posture only |
| 5 | wrong/absent token | 403 `control_token_required`; JS prompts once + retries | honest |
| 5 | streambox profile | 404 via `_guard_install_profile_route`… but `/system/restart/audio` IS in `_STREAMBOX_ALLOWED_POST_ROUTES` (server.py:309), so it runs there too | n/a |
| 6 | `Popen` spawn error (`OSError`) | 502 | honest — the **only** failure this endpoint can report |
| 6/7 | **polkit denies** (unit not in allowlist) | `systemctl` exits 1; rc never read | **200 `{"ok": true}` — lies** |
| 7 | rule file missing on host | every unit denied | **200 `{"ok": true}` — lies** |
| 8 | unit not found / masked / failed to start | rc≠0, or a queued job that fails later | **200 `{"ok": true}` — lies** |
| 8 | `restart` blocks past nginx `proxy_read_timeout` | n/a — `Popen` is fire-and-forget | n/a |

Same table applies verbatim to `/system/reboot`, `/system/poweroff`, `/system/restart/voice`,
`/system/audio-quality`'s renderer refresh (handlers/system.py:360), `debug_control.py:75`,
`handlers/aec.py:355`, `aec_endpoints.py:369`, `server.py:1099`, `shairport_supervisor.py:410,417`,
`system_supervisor.py:515`. The correct pattern is 40 lines away in the same file:
`server._run_oneshot_start:707-753` reads the rc and emits `event=<x>_failed`.

## C. Authority matrix — every inconsistency found

| unit | broker allowlist | polkit (jasper-control) | who actually mutates it | verdict |
|---|---|---|---|---|
| `jasper-usbsink-volume.service` | **absent** | **absent** | `handlers/system.py:360,512` `try-restart` from non-root control (via `local_source_audio_refresh_units()`) | **live denial, 200-and-lie** |
| `avahi-daemon` | absent | absent (and absent from `49-jasper-web.rules`) | `peering/avahi.py:133` (control), `avahi_service.py:226` (jasper-web, via `/speaker` rename) | **live denial, zero log** |
| `jasper-camilla-crossover/-recover`, `jasper-snapclient/-snapserver`, `jasper-usb-network-plan`, `jasper-headphone-monitor` | absent | absent | root reconcilers only (`multiroom/reconcile.py`, `source_intent.py`) | correct — outside the client set |
| everything in `MANAGED_UNITS` (25) / `START_ONLY_UNITS` (8) | present | present, byte-equal | broker + 15 direct in-tile sites | grant correct; **usage bypasses it** |
| `manage-unit-files` (enable/disable) | 3 verbs in `_VERB_ARGV`, zero callers | deliberately **not** granted | root `source_intent` only | dead vocabulary (p1-T08 C11 — confirmed) |
| any unit, as root | `_direct_systemctl` validates the **verb but not the unit** (restart_broker.py:659) | n/a | root fallback + root streambox `jasper-web` | allowlist is broker-only, not a property of `manage_units` |

**Drift-pinning coverage:** `MANAGED_UNITS ↔ .rules` — pinned (`tests/test_polkit_jasper_control.py:66`).
`local_source_*_units() ⊆ POLKIT_MANAGE_UNITS` — **not pinned** (this is the gap).
`tests/test_restart_broker.py:131 test_managed_units_cover_every_routed_client_unit` is a
**hand-written literal set**, so it can only catch removals from a list somebody remembered
to update, never a new call site — exactly how `jasper-usbsink-volume` slipped in.

**Do tests pin the denial paths?** No. `tests/test_control_server_system.py:1987-2011` and
`:750-773` install a `fake_popen` returning an object with **no `returncode` attribute** —
proof by construction that production never reads it — and assert only the argv. The web
side is the same: `tests/test_web_common.py:528-585` pins verb + units from a `manage_units`
stub that always returns `{"ok": True}`. The only rc-behaviour pins in the whole boundary are
`tests/test_restart_broker.py:569` (broker-internal), `test_wiim_remote_mic.py:695,980` and
`test_mux_spotify_preempt.py:149`.

## D. Findings

| # | sev | file:line | what | evidence | fix |
|---|---|---|---|---|---|
| 1 | **Blocker** | `control/handlers/system.py:499-514`, `:360` | `/system/restart/audio` and `/system/audio-quality` `try-restart` a unit no grant covers and answer `{"ok":true}` | `set(local_source_audio_refresh_units()) - POLKIT_MANAGE_UNITS == {"jasper-usbsink-volume.service"}` (computed at HEAD); `Popen` rc never read | route through `manage_units`; add `test: local_source_{audio_refresh,park}_units() ⊆ POLKIT_MANAGE_UNITS`. Confirms **p1-T08 C1** |
| 2 | **Blocker** | 11 sites, incl. `handlers/system.py:499-514`, `shairport_supervisor.py:410-421`, `system_supervisor.py:515`, `debug_control.py:75`, `aec_endpoints.py:369`, `handlers/aec.py:355`, `server.py:1099` | a polkit denial / unit-not-found / failed-start is invisible at **every** in-tile mutation site | all use `Popen` or `await proc.wait()` discarding `returncode`; `shairport_supervisor._tick` increments `/state.resilience.shairport.restart_count` *before* the swallow, so `/state` reports a restart that never happened | one call: `restart_broker.manage_units(...)`, which already returns `ok`/`rc` and audits. Confirms **p1-T08 C1/C2** |
| 3 | **Should-fix** | `peering/avahi.py:126-138`, `avahi_service.py:216-238` | `systemctl reload avahi-daemon` is granted to **neither** service user; `check=False`, rc dropped, one site logs nothing at all | `avahi-daemon` appears in neither `.rules`; polkit implicit default for `manage-units` is `auth_admin` → deny for a sessionless daemon | **delete both reloads** — avahi already inotify-watches `/etc/avahi/services` and both docstrings say so ("inotify usually catches changes on its own"). Removes machinery instead of widening a grant. Also collapses a 2× duplicate |
| 4 | **Should-fix** | `control/server.py:2258` | "jasper-control **is the single mediated systemctl boundary**" is false; `restart_broker.py:41` contradicts it in the same package | 14 direct mutating argv sites inside `jasper/control/` vs 2 brokered | delete the sentence, or make it true by doing #2. Confirms **p1-T08 C10** |
| 5 | **Should-fix** | `control/handlers/system.py:428-429` | comment says "jasper-control already runs as root so no sudo needed" | `deploy/systemd/jasper-control.service:User=jasper-control`, `NoNewPrivileges=true` | delete — a wrong comment is worse than none (AGENTS.md) |
| 6 | **Should-fix** | `deploy/systemd/jasper-control.service` (ReadWritePaths) vs `deploy/bin/jasper-render-asound-conf:15,53,58` | `POST /system/audio-quality` writes `/var/lib/jasper-asound/asound.conf` (root:root 0755, install.sh:1332) from a `ProtectSystem=strict` unit whose `ReadWritePaths` do not include it | render is `check=True` → surfaces as 502, so it fails *loudly* — but the dashboard control is likely dead since the 3b-2 drop and nothing tests it | add the path to `ReadWritePaths` + group-`jasper` write, or move the render behind a broker-started oneshot. **Hardware-verifiable (see E)** |
| 7 | **Should-fix** | `web/spotify_setup.py:223,227` | `_restart_voice_daemon` / `_restart_spotify_consumers` call `restart_systemd_units` directly, bypassing `_common.restart_voice_daemon`'s provider-unset and bonded-follower gates | `google_setup.py:136` wraps the gated helper for the identical purpose; `_common.py:645-673` documents both gates as "states where a restart would be WRONG" | delete both; call `restart_voice_daemon()`. Confirms **p1-T16-2** |
| 8 | **Should-fix** | `control/handlers/aec.py:354` | `/aec/threshold` restarts jasper-voice with a bare `Popen`, with **no** parked-follower gate — while `/system/restart/voice` (same file, :445) returns 409 for exactly that state | two gates for one question in one file | `manage_units` + the same 409 |
| 9 | **Should-fix** | `web/_common.py:519-555` | `restart_systemd_units` (~30 call sites via `restart_voice_daemon`) **discards** the `manage_units` result; every wizard save answers 303/200 whatever happened | `manage_units` does log `event=restart_broker.client_error`, so the journal knows and the user does not | return the dict; let savers render "saved, but the restart didn't land" the way `handlers/aec.py:467` already does |
| 10 | **Should-fix** | `deploy/jasper-web-streambox.service` | same `python -m jasper.web` process, root, with 5 hardening directives vs 19 | missing `ProtectKernelTunables/Modules/ControlGroups`, `RestrictNamespaces/SUIDSGID/AddressFamilies`, `LockPersonality`, `SystemCallArchitectures`, `CapabilityBoundingSet`, `SystemCallFilter` — **none of which depend on `User=`** | copy the 11 uid-independent directives across now. **Re-grades p1-T24**: the `User=` deferral itself earns its keep (documented removal condition + `tests/test_systemd_hardening.py:687`); the uid-independent gap does not |
| 11 | **Should-fix** | `tests/test_restart_broker.py:131` | the "every routed client unit is grantable" guard is a hand-maintained literal | it lists 23 units by hand and misses `jasper-usbsink-volume.service` | derive it: assert the union of `local_sources` registry unit tuples + `debug_mode.SUBSYSTEMS[*].unit` + `output_topology_runtime.RECONCILE_UNITS` + `CORE_AUDIO_RESTART_UNITS` ⊆ `POLKIT_MANAGE_UNITS` |
| 12 | **Should-fix** | doctor: `cli/doctor/privsep.py` (reads) vs nothing (writes) | the **read** half of privilege separation has a drift-pinned per-daemon doctor check; the **write/action** half has none | no doctor check probes `/run/jasper-control/restart.sock`, the presence of either `.rules` file, or a daemon's `ReadWritePaths` vs the paths it writes | add `check_restart_broker_reachable` + `check_polkit_rules_installed`; both are cheap and both would have caught #1 and #6 on hardware |
| 13 | Nit | `control/server.py:398-431` vs `:297-313` | `/system/audio-quality` and `/system/usb-latency` restart the same renderers as `/system/restart/audio` but are **not** in `_TOKEN_GATED_ROUTES`, while `/system/restart/audio` is | same blast radius, two authority levels | add both, or drop the third — pick one story |
| 14 | Nit | `web/_common.py:1440-1455` vs `measurement_window.py:236-252` | two contradictory control-token policies: "the wizard never injects the token from disk — the gate stays real" vs a daemon that reads `current_token()` off disk and injects it | both are defensible for their caller; the absolute in the docstring is not | soften the `_common` docstring to "browser-proxied surfaces relay, never inject" |
| 15 | Nit | `control/server.py:984` vs `web/_common.py:698` | two predicates for "is this speaker a parked follower": `effective_follower_leader_addr` vs `effective_local_sources_park_reason` | they diverge in the `role_transition_in_progress` window — the wizard skips a voice restart the dashboard would perform | one helper on `multiroom/effective_role` |
| 16 | Nit | `restart_broker.py:659-664` | `_direct_systemctl` (root fallback) validates the verb but **not** the unit allowlist | so `manage_units`'s allowlist is a property of the *transport*, not the API — and on a root streambox `jasper-web` that transport is optional | check `_unit_allowed_for_verb` in the fallback too; it's two lines and makes the allowlist an API guarantee |
| 17 | Nit | `tests/test_restart_broker.py:205-425,580-880` | ~450 lines and 13 tests exercise `_request_restart_retrying_transient_failures`, a **test-only** helper (no definition outside `tests/`) | grep for the name across `jasper/ deploy/ scripts/` returns nothing | either the retry belongs in `request_restart` (then test it as product) or the harness needs a fixture, not 13 tests |
| 18 | Nit | `web/wifi_setup.py:125` | `JASPER_WIFI_SCAN_REPAIR_UNIT` lets the caller name any unit | harmless (the broker allowlist fail-closes it) but is an unrequested knob | inline the constant |
| 19 | Earns-its-keep | `source_intent.py:712-870` | the **only** privileged path in the tree that proves convergence: outer request lock → env write → fingerprint → broker `start` → fingerprinted completion-status read → `RuntimeError` on mismatch | — | this is the template shapes A and F should aspire to |
| 20 | Earns-its-keep | `control/usb_gadget_forensics.py`, `accessories/reconcile.py` request file + `.path` unit | file-mailbox → root worker: no systemctl from the non-root side at all, `O_NOFOLLOW`, claimed by atomic rename | — | keep; prefer this shape for any *new* privileged action |

## E. What only hardware/runtime can prove

- **#1/#2**: that polkit actually denies `try-restart jasper-usbsink-volume.service` for uid
  `jasper-control` (the rule returns `NOT_HANDLED`; the *implicit* default for
  `manage-units` on this distro build is what makes it a deny). Reproduce:
  `sudo -u jasper-control systemctl try-restart jasper-usbsink-volume.service; echo $?`
  then `curl -X POST …/system/restart/audio` and compare the 200 to the journal.
- **#3**: whether the avahi reload has ever worked post-drop, and whether inotify alone is
  sufficient for the `/speaker` rename advert (`avahi-browse -art | grep name=`).
- **#6**: whether `/system/audio-quality` 502s on a real Pi — `curl -X POST -d
  '{"converter":"samplerate_medium"}' …/system/audio-quality` and check
  `namespace`/`EROFS` in the journal.
- **#12**: that `/run/jasper-control/restart.sock` exists at 0660 group `jasper` and that
  every `BROKER_CLIENT_USERS` uid can connect (traverse on 0750 `/run/jasper-control`).
- **#10**: whether the streambox web unit still starts with the 11 added directives
  (`SystemCallFilter=@system-service` in particular, given the nmcli/busctl children).
- Timing: whether any shape-A restart would exceed nginx's `proxy_read_timeout` if converted
  to a blocking broker call — the fix should keep `no_block=True` and check the *enqueue* rc,
  which is what `_run_oneshot_start` already does.

## F. Coverage

**Read in full:** `jasper/control/restart_broker.py` (775), `control/handlers/system.py:330-540`,
`control/handlers/aec.py:280-495`, `control/debug_control.py:45-100`,
`control/aec_endpoints.py:340-385`, `control/shairport_supervisor.py:320-430`,
`control/system_supervisor.py:495-530`, `control/server.py:{45-62,132-134,287-323,396-472,660-760,984-1000,1080-1140,1660-1760,2248-2262}`,
`control/control_token.py`, `control/usb_gadget_forensics.py`,
`web/system_setup.py`, `web/_common.py:{519-700,1150-1200,1380-1460}`,
`web/speaker_setup.py:{77-130,240-370}`, `web/airplay_setup.py:80-105`,
`web/wifi_setup.py:{123-200,540-700}`, `web/sound_setup.py:395-425`,
`web/sources_setup.py:540-560`, `web/spotify_setup.py:210-230`, `web/_service_state.py`,
`web/_unit_snapshot.py:90-160`, `source_intent.py:{700-980}`,
`avahi_service.py:200-238`, `peering/avahi.py:100-140`, `mux.py:1885-1915`,
`accessories/wiim_remote_mic.py:380-400`, `active_speaker/{startup_load,runtime_convergence}.py` (broker calls),
`output_topology_runtime.py:55-100`, `wake_corpus/bridge_session.py:{1230-1275,2690-2715}`,
`multiroom/reconcile.py:{1030-1370}`, `fanin/latency_mode.py:356-375`, `audio_quality.py:179-220`,
`deploy/polkit/*.rules` (both, in full), `deploy/systemd/jasper-control.service`,
`deploy/jasper-{web,web-streambox,system-web,bluetooth-web,correction-web,chat-web}.service`,
`deploy/systemd/jasper-{accessory-reconcile,usbgadget-forensics,grouping-reconcile-trailing}.{path,service}`,
`deploy/bin/jasper-grouping-reconcile-{kick,trailing}`, `deploy/bin/jasper-render-asound-conf`,
`deploy/assets/system-status/js/actions.js:1-80`, `deploy/assets/shared/js/http.js` (grep),
`tests/test_polkit_jasper_control.py:60-95`, `tests/test_restart_broker.py:95-205`,
`tests/test_control_server_system.py:{750-780,1980-2045}`, `tests/test_web_common.py:528-585`,
`tests/test_systemd_hardening.py:676-700`, `cli/doctor/privsep.py:1-65`.

**Computed at HEAD** (scratchpad/S1-privileged): the polkit↔broker set equality (holds), the
`local_sources` registry ⊄ `POLKIT_MANAGE_UNITS` drift (one unit), a tree-wide `*.service`
literal scan (23 units outside the allowlist, all but two correctly root-only), and a
mutating-vs-read classification of all 74 `systemctl` argv sites (≈34 mutating; 14 in
`jasper/control/`).

**Skipped:** `jasper/cli/*` root CLIs beyond identifying them as shape H (they hold the
privilege by construction and all 9 mutating sites check rc); the BlueZ D-Bus authority path
beyond noting it rides the distro's stock `bluetooth` group policy with no in-repo pin; the
`camillagui*` units; `nginx.service.d`/`ssh.service.d` drop-ins.
