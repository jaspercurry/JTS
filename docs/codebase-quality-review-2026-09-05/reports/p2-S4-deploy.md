# S4-deploy — laptop → Pi → running speaker, end to end

## A. Verdict

The deploy path is **two well-built halves with an unguarded middle**. The Pi-side install is
genuinely careful: `write_build_manifest` as the last mutation (ADR-0172), a real unit-install
rollback domain, an EXIT trap that always unparks, content-addressed Rust/C/pybind staging that
fixed three separate stale-binary incidents. The laptop-side wrapper is equally careful *when it
runs* — identity TOFU, direction guard, OOM-collateral scan, manifest-advanced proof. The middle
is where it falls apart: **the guards non-negotiable 4 names are conditional on things that have
nothing to do with intent.** `SKIP_INSTALL=1` and the *default* interactive-sudo fallback each
skip both the identity guard and the downgrade guard while `rsync --delete` still lands (both
verified live, §B2). And nothing anywhere gates on health: `run_doctor_summary` prints a red
banner and `return 0`, `surface_system_health` is `|| true`, so a box whose entire audio graph is
down finishes with `==> Done.` and a `status=ok` manifest. The stdlib probe written for "the venv
is broken" is unreachable when the venv is broken (`install.sh:2167` guards `:2173`). Under all
that sits one structural cause the tiles already named — install.sh is forked down the middle by
profile — which is real, and the STEPS-table fix is right (§F1).

## B1. Happy path (full profile, passwordless sudo). `‖` = process boundary.

| # | hop | file:function |
|---|---|---|
| 1 | resolve target | `scripts/_lib.sh:87-124` — `.env.local` → `PI_HOST/PI_USER/JASPER_HOSTNAME`, `JTS_TARGET_FROM` |
| 2 | capture SHA/branch/dirty **before** rsync (.git excluded) | `deploy-to-pi.sh:576-584` |
| 3 | ‖ resolve `$HOME` → `REMOTE_REPO_DIR=~/jts` | `resolve_remote_repo_dir:91` |
| 4 | speaker hostname (IP → remote `hostname -s`) | `resolve_speaker_hostname:123` |
| 5 | gadget-network advisory (non-blocking) | `warn_if_pi_host_on_gadget_network:388` |
| 6 | ‖ **sudo preflight** `sudo -n true` | `preflight_sudo:167` |
| 7 | ‖ **identity guard** — peer_id TOFU vs `.env.local` | `:604-661` → `_lib.sh:verify_or_record_peer_id` |
| 8 | ‖ **direction guard** — `build.txt` vs `SHA_FULL` | `preflight_deploy_direction:225` |
| 9 | ‖ `rsync -az --delete ./ → ~/jts/` | `:682-687` |
| 10 | ‖ airplay-health maintenance marker + EXIT trap | `mark_airplay_health_maintenance:433` |
| 11 | ‖ `sudo JASPER_DEPLOY_SHA/… bash ~/jts/deploy/install.sh` | `:762` |
| 12 | profile resolve + persist marker (**2nd mutation**) | `install.sh:resolve_install_profile:304`, `persist_install_profile:336` |
| 13 | tier preflight, build user, build swap, EXIT trap, service users, park | `install.sh:2288-2296` |
| 14 | ‖ `apt-get update && apt-get install` (network) | `install_deps:837` |
| 15 | ALSA conf.d/modprobe.d + `rmmod/modprobe snd_aloop` + `/etc/asound.conf` | `install_alsa:1267` |
| 16 | ‖ CamillaDSP binary fetch (curl+sha256, first install only) + templates | `install_camilladsp:953` |
| 17 | renderers, boot config, USB data role, wifi tune | `renderers.sh:install_renderers` |
| 18 | **`rsync -a --delete` jasper/ → /opt/jasper**, then venv + `pip -e` | `python-runtime.sh:196,224,241` |
| 19 | outputd/camilla statefiles, secrets/intsecrets reassert | `install.sh:1071-1232` |
| 20 | ‖ Rust: `stage_rust_crate` (`--checksum`, no `-t`) ×7 → `run_contained_build` cargo | `rust-daemons.sh:86,112` |
| 21 | ‖ C ioplug `make plugin` → `/usr/lib/*/alsa-lib/` + provenance record | `ring-platform.sh:94` |
| 22 | units: support files → core-graph rows → **transaction opens** → per-profile units | `systemd-units.sh:1325-1343` |
| 23 | transaction commits (`unset -f install`, `trap - ERR`) | `systemd-units.sh:1704-1709` |
| 24 | `daemon-reload`, enable sockets/units, **park clients** → hw-reconcile → fanin → outputd-ready → camilla → mux → renderers → wizards stop → input → accessory/aec/grouping/coupling reconcile | `systemd-units.sh:1755-1876` |
| 25 | memory/cgroup migrations, journald, avahi, polkit, peering, TLS | `install.sh:2320-2329` |
| 26 | ‖ nginx conf + landing page bake (**system python3 over the checkout**) + assets + `nginx -t` + reload | `install_nginx_site:1772`, `install_management_static_assets:1645`, `web-assets.sh:60` |
| 27 | camillagui, cues regenerate | `install.sh:2047,2024` |
| 28 | **`write_build_manifest`** — last mutation, `status=ok` | `install.sh:1364` |
| 29 | `run_doctor_summary` — **advisory, always returns 0** | `install.sh:2158` |
| 30 | ‖ back on laptop: OOM scan, reboot marker, `build.txt` echo, profile read | `deploy-to-pi.sh:766-833` |
| 31 | ‖ restart jasper-control, jasper-web(+socket), aec-reconcile / grouping-reconcile | `:866-880` |
| 32 | ‖ **gate**: `/system/data.json` 200 via nginx as the real Host (5× retry) | `:963-975` |
| 33 | ‖ **gate**: `/system/` HTML advertises `app.css?v=<SHA>` | `:986-999` |
| 34 | ‖ **gate**: `verify_manifest_advanced` (SHA_FULL + status=ok) | `:497` |
| 35 | ‖ health surface (advisory) + 2nd OOM scan; **gate**: production OOM hit | `surface_system_health:539`, `:1005-1012` |

## B2. Failure branches — does it surface, swallow, or lie?

| hop | failure | surfaced how | verdict |
|---|---|---|---|
| 6 | no passwordless sudo, non-tty | exit 1, Pi untouched | **honest** |
| 6 | no passwordless sudo, tty | falls back to `ssh -tt sudo`; **hops 7, 8, 30, 34, 35 all silently skip** | **swallows** — §C2 |
| 9 | network drop mid-rsync | rsync rc≠0 → `set -e` abort, no message of its own; `~/jts` left partially deleted/updated, no marker | swallows the *state*, surfaces the exit |
| 11 | ssh dies mid-install (255) | dedicated banner "DEPLOY OUTCOME UNKNOWN", exit 255 | **honest, good** |
| 14 | apt-get fails (offline) | `set -e`, abort pre-mutation except the profile marker | honest |
| 16 | curl/sha256 fail | `set -e` abort | honest |
| 18 | pip resolve/build fails | abort — but `/opt/jasper/jasper` **already carries new code with old deps**, manifest still old | **lies by omission** — §C5 |
| 20 | cargo OOM on 1 GB | `run_contained_build` scope is the OOM victim; `build_install_rust_daemon` returns 1 → abort; laptop `report_oom_collateral` names any production victim and fails the deploy | **honest, best-in-tree** |
| 20 | `rust/jasper-fanin/` missing (truncated rsync) | `required=0` → "skipping build", **exit 0** | **swallows** |
| 21 | `make plugin` fails | 3 WARN lines + `revoke_ring_ioplug_provenance`; **returns 0**; stale `.so` stays; manifest advances | honest in transcript, **lies on `/system/`** |
| 22 | any `install` in the transaction fails | ERR trap restores every touched file + `daemon-reload` + abort | **honest** (full profile only — §C7) |
| 24 | unit fails to restart | `\|\| true` / `echo WARN` for fanin, camilla, mux, renderers, input, accessory, identity; only `systemctl enable <core>` is unguarded | **swallows** |
| 24 | `require_outputd_ready` fails | WARN, install continues (deliberate, documented) | honest-ish |
| 26 | `nginx -t` fails | `return 1` → abort — **but the bad conf is already in `sites-enabled/`** | **half-applied** — §C6 |
| 26 | landing-page bake fails | `return 1`, refuses to ship a broken page | **honest, good** |
| 29 | deploy-health / doctor red | banner printed, `return 0`; manifest `status=ok` already written | **lies** — §C3 |
| 29 | venv missing/broken | `return 0` at `:2168` — **no health check of any kind runs** | **lies** — §C4 |
| 32/33/34 | management surface / asset / manifest wrong | banner + exit 1 | **honest, good** |
| 35 | production daemon OOM-killed | banner + exit 1 after all evidence | **honest, good** |
| any | install fails | **nothing is written on the Pi.** 4 `logger -t jasper-install` sites exist, all memory/sandbox/tier; no install-start, step, or failure event | **swallows** — §C15 |

## B3. Guard-bypass matrix (verified live except where noted)

| flag / condition | sudo preflight | identity | direction/downgrade | rsync `--delete` | install | post-deploy gates 32–35 |
|---|---|---|---|---|---|---|
| default (passwordless) | ✓ | ✓ | ✓ | runs | runs | ✓ |
| `SKIP_INSTALL=1` | **skip** | **skip** | **skip** | **runs** | skip | skip |
| `SUDO_INTERACTIVE` (auto fallback) | ✓ | **skip** | **skip** | runs | runs | **skip** |
| `SKIP_RESTART=1` | ✓ | ✓ | ✓ | runs | runs (install still restarts) | **skip — exits at :861** |
| `JTS_ACCEPT_NEW_IDENTITY=1` | ✓ | re-record | ✓ | runs | runs | ✓ |
| `JASPER_DEPLOY_ALLOW_DOWNGRADE=1` | ✓ | ✓ | allow | runs | runs | ✓ |
| `JASPER_ACCEPT_INSTALL_PROFILE_CHANGE=1` | ✓ | ✓ | ✓ | runs | **converts tier** | ✓ |

Verification (scratchpad `S4-deploy/`, stubbed `ssh`/`rsync`, throwaway git repo): identical
downgrade → plain deploy aborts rc=1 with **0 rsync calls**; `SKIP_INSTALL=1` → rsync `--delete`
runs, rc=0. Identical peer_id mismatch → plain deploy aborts, 0 rsync; `SKIP_INSTALL=1` → rsync
runs, rc=0. Forced interactive sudo (pty) → prints "speaker identity: skipped" and "deploy
direction: skipped", then rsyncs and runs install.sh against the **mismatched** peer_id.

## B4. Staleness — does every changed source class ship *and take effect*?

| class | ships | takes effect this deploy | note |
|---|---|---|---|
| `.py` | ✓ `rsync -a --delete` → `/opt/jasper` (`pip -e`) | ✓ on the units restarted at hop 24/31 | daemons not restarted keep old code until they bounce |
| `.rs` | ✓ `--checksum`, no `-t` | ✓ | the mtime trap is solved correctly (`rust-daemons.sh:70-86`) |
| `.c` (ioplug) | ✓ | ✓ **unless the build fails** → old `.so` stays, provenance revoked | doctor row is the only signal; nothing gates |
| `jasper_aec3` C++ | ✓ | ✓ content fingerprint; **`setup.py` deliberately excluded** | disclosed at `python-runtime.sh:413-418` |
| `.service`/`.socket`/`.slice` | ✓ | ✓ `daemon-reload` in both profiles | |
| nginx conf | ✓ | ✓ `nginx -t` + reload | |
| udev rules | ✓ | ✓ `udevadm control --reload-rules` (both profiles) | |
| polkit rules | ✓ | ✓ (polkitd watches the dir) | roster is a hand-copy of `restart_broker` (p1-T24) |
| tmpfiles | ✓ | ✓ `systemd-tmpfiles --create --prefix=` per tree | |
| sysctl | ✓ | ✓ `sysctl --system`, WARN→reboot-required on failure | |
| CamillaDSP templates | ✓ | ✓ then regenerated from live hardware | |
| `deploy/assets/**` | ✓ **additive only** | ✓ for new/changed files | **deleted** pages/JS persist forever unless hand-added to the `rm -rf` list — §C12 |
| `modprobe.d/snd-aloop.conf` | ✓ to disk | ✗ **silently** — `rmmod` EBUSY masked | §C8 |
| `deploy/lib/install/*.sh` | ✓ (all 12) | n/a — 11 have no runtime consumer | §C9 |

## B5. States that roll neither forward nor back

| state | why |
|---|---|
| new `.py` in `/opt/jasper` + old venv deps + old manifest (hop 18 abort) | rsync precedes pip; nothing restores the old tree; any later daemon restart runs new code on old deps, and 5 units carry `StartLimitAction=reboot` |
| bad nginx conf in `sites-enabled/` after a `nginx -t` abort | running nginx holds the old config in memory; the next restart (incl. `jts-recovery.conf` `Restart=always`) takes the whole management surface down — SSH-only recovery |
| `install_profile` marker flipped, tier conversion aborted | marker is the 2nd mutation; every later plain deploy takes the *new* tier's path. Rolling the code back does not roll the marker back |
| one-way state migrations vs a code rollback | `python-runtime.sh:372-384` sed-deletes 12 `jasper.env` keys, `web-assets.sh:68-75` and `install_camilladsp` `rm -f` retired files, `env-migrations.sh` heals modes — a `git checkout <old> && JASPER_DEPLOY_ALLOW_DOWNGRADE=1` restores code but never state |
| streambox unit generation aborted mid-way | no rollback transaction on that profile (§C7) |
| stranded 2 GB build swap | `trap install_exit_cleanup EXIT` is armed *after* `setup_build_swap_if_needed` (§C17) |

## C. Findings

| # | sev | file:line | what | evidence | fix |
|---|---|---|---|---|---|
| 1 | **Blocker** | `deploy-to-pi.sh:602,682,689` | `SKIP_INSTALL=1` skips identity **and** direction guards while `rsync --delete` runs | verified live: same downgrade & same peer_id mismatch abort at rc=1 with 0 rsync calls without the flag, rsync + rc=0 with it. No test names `SKIP_INSTALL`. *(confirms p1-T23 #1; upgraded — the bypass is empirically demonstrated and the write is destructive)* | hoist `preflight_sudo` + identity + `preflight_deploy_direction` above the rsync unconditionally; gate only the `install.sh` invocation |
| 2 | **Blocker** | `deploy-to-pi.sh:167-198, 236-240, 610-613` | the **default** interactive-sudo fallback silently disables identity, direction/downgrade, OOM scan, manifest verification and health — no flag, no opt-in, no `ALLOW_DOWNGRADE` needed | verified live under a pty: prints "identity: skipped" / "direction: skipped", then deploys to a mismatched peer_id. Every direction-guard test pins `SUDO_INTERACTIVE=0` (`test_lib_deploy_direction.py:450,531`); the bypass branch has no behavior pin. **NEW** | capture the manifest/peer_id over a **separate non-tty `ssh -o BatchMode=yes` channel** (only the `sudo` needs the pty), so the guards survive; or refuse to deploy attended unless `JTS_DEPLOY_UNVERIFIED=1` |
| 3 | **Blocker** | `install.sh:2158-2201`; `deploy-to-pi.sh:539-575` | **no health signal gates anything.** `run_doctor_summary` prints the red banner and `return 0`; `surface_system_health` is `\|\| true`. `write_build_manifest` (`status=ok`) already ran at `:2333` | `jasper-deploy-health` returns 1 (`:895`) and `jasper-doctor`'s status is captured into `doctor_status` and then only printed. A box with fanin/outputd/camilla down finishes `==> Done.` and `/system/` shows the new SHA. **NEW (scenario seam)** | make `run_doctor_summary`'s core subset a gate (`return 1`) and let `deploy-to-pi.sh` surface it as a deploy failure; keep the broad rows advisory |
| 4 | **Blocker** | `install.sh:2167-2173` | the stdlib-only probe written for "the venv (and thus jasper-doctor) is broken" is **unreachable when the venv is broken** | `if [[ ! -x /opt/jasper/.venv/bin/jasper-doctor ]]; then return 0; fi` guards the `build_swap_required → jasper-deploy-health` branch below it. Two conflicting rationales for the same tool: its docstring says "1 GB", `docs/DEEP-AUDIT-2026-08-25.md:149` says "venv broken", the code gates on RAM. **NEW** | invert: `-x jasper-doctor` chooses doctor, absence chooses the stdlib probe. Folds into §F3 |
| 5 | **Should-fix** | `python-runtime.sh:196` vs `:224-241` | `rsync -a --delete jasper/ → /opt/jasper` happens **before** venv+pip; a pip failure leaves new source on old deps with the old manifest | ordering is explicit in the file; `set -e` aborts after the rsync. The manifest's honesty guarantee (ADR-0172) covers "which SHA installed cleanly", not "which SHA is on disk". **NEW** | stage to `/opt/jasper.new` + pip there + `mv` (or run pip before the source rsync) so the source tree flips only after deps resolve |
| 6 | **Should-fix** | `install.sh:1789-1794, 1808` (and `:1822-1827,1834` streambox) | the new nginx conf is installed into `sites-enabled/` **before** `nginx -t`; a failed test aborts the install and leaves it there | the running nginx is unaffected until its next restart — and `nginx.service.d/jts-recovery.conf` gives it `Restart=always`, so an OOM bounce during that window makes the management surface unrecoverable without SSH. **NEW** | `nginx -t -c` a temp include (or install to `sites-available` + symlink after the test passes), and restore the prior conf on failure |
| 7 | **Should-fix** | `systemd-units.sh:1302-1323` vs `:1325-1343` | the unit-install rollback domain is **full-profile only**; `install_streambox_systemd_units` never opens a transaction | `install_transaction_dir` / `trap … ERR` / `install() {}` appear only in `install_systemd_units`. A failed `install` mid-streambox leaves a mixed unit generation with no restore. `systemd-analyze verify` is the mirror asymmetry — streambox-only (p1-T24). **NEW** | hoist the transaction open/commit into a wrapper both profiles call; land it together with §F1 |
| 8 | **Should-fix** | `install.sh:1276-1277` | `rmmod snd_aloop 2>/dev/null \|\| true; modprobe snd-aloop \|\| true` — on any box ≥1.2 GB nothing is parked yet, `rmmod` returns EBUSY, and the new `modprobe.d` options **silently never apply**; the comment claims otherwise | `park_low_memory_build_units` returns early unless `build_swap_required` (`systemd-units.sh:947`); `park_audio_clients_for_core_graph_restart` runs ~500 lines later. No test mentions `snd_aloop`. *(confirms p1-T23 #11)* | detect EBUSY → `event=install.aloop_reload_skipped reason=busy` + `_set_reboot_required_reason`; or park first |
| 9 | **Should-fix** | `systemd-units.sh:47-50` | all 12 `deploy/lib/install/*.sh` (5,965 lines) ship to `/usr/local/lib/jasper/install/`; **only `build-sandbox.sh` (400) has a runtime consumer** | `grep -rn usr/local/lib/jasper/install` → 3 hits: the two install lines and `deploy/bin/jasper-contained-build:15`. `build-sandbox.sh` sources no sibling, so 5,565 lines are dead bytes on a 1 GB box. *(confirms p1-T23 #5)* | install `build-sandbox.sh` only (§F2) |
| 10 | **Should-fix** | `env-migrations.sh:22-43,175` | `ensure_state_dir` → `heal_shared_state_modes` → **one `/usr/bin/python3` per call**, from 12 call sites (≤11 per run) | sites: `install.sh:980,1318,1492,2005`, `python-runtime.sh:24,124,449`, `model-staging.sh:28,45`, `env-migrations.sh:455,560`, `renderers.sh:214`. Directly against AGENTS.md/ADR-0226 "no short-lived Python in hot paths; one interpreter per concern". *(confirms p1-T23 #4; **re-graded up** — this is an ADR-0226 breach, not just cost; call-site count is 12, not 11)* | call the heal once from `main()`; leave `ensure_state_dir` as mkdir + chgrp/chmod |
| 11 | **Should-fix** | `install.sh:366-731, 2236-2334`; `python-runtime.sh:395-400` | profile fork by copy: two `main()` lists differing in **6 rows**, 363 lines of hand-written plan prose in two heredocs, and a 202-line test whose only job is keeping them in sync | diffed the two lists: identical modulo `install_{,streambox_}{deps,jasper,systemd_units,nginx_site}` + 3 streambox-only + 2 full-only rows. **New evidence:** those 3 "streambox-only" steps (`reassert_secrets_compartment_perms`, `reassert_intsecrets_…`, `migrate_wifi_guardian`) are **not** absent on full — they run *inside* `install_jasper` at a different altitude and a different point in the order. *(confirms p1-T23 #2; sharpened)* | the STEPS table (§F1) — and hoist those 3 out of `install_jasper` so both profiles share one row |
| 12 | **Should-fix** | `web-assets.sh:60-116`, `install.sh:1719-1720`, `install_camilladsp:1045,1052` | asset/page/config installs are **additive**; removal is a hand-maintained `rm -rf` list with no expiry | `install_web_assets` writes a fresh `.install-manifest` but never prunes `assets_root` of paths absent from it. Deleting `deploy/assets/<page>/` leaves it served by `location /assets/` forever. **NEW (generalizes p1-T23's retirement-list observation)** | prune `assets_root` against the new manifest; that single change retires the whole `rm -rf`/`rm -f` retirement list for assets |
| 13 | **Should-fix** | `deploy/bin/jasper-deploy-health` (900) + `tests/test_deploy_health_script.py` (1,642) | ADR-0233 rule 5 unshipped: `grep -rn '\-\-core' jasper/cli/doctor/ deploy/ scripts/` → 0 | 2,542 lines for a probe that never gates (§C3) and is unreachable in its own stated failure mode (§C4). *(confirms p1-T10)* | §F3 |
| 14 | **Should-fix** | `install.sh:837-868, 870-883` | `apt-get update && apt-get install` runs unconditionally on **every** deploy, unpinned — while the pip toolchain 10 lines away is pinned to the byte (`pip==26.1.2 wheel==0.47.0`) with a written rationale about "silent behavior drift on the highest-blast-radius script in the repo" | same drift class (nginx, rustc, libwebrtc), same script, opposite treatment; also a hard network dependency on every deploy. **NEW** | gate on a package-set fingerprint (skip when unchanged) and pin the versions that matter, or state in one comment why apt is deliberately floating |
| 15 | **Should-fix** | `deploy/` (whole) | a failed install leaves **no record on the Pi**: `logger -t jasper-install` has 4 sites, all memory/sandbox/tier; there is no install-start/step/failure event, and `build.txt` is only written on success | so the laptop transcript is the sole evidence, and hop-30 `report_oom_collateral` is the only thing that reads the Pi after a failure. Against the owner's "observable" goal. **NEW** | one `jasper_install_log <event> <detail>` (which also collapses the 4 spellings p1-T23 #15 names) at start/each main() step/failure, plus `/run/jasper-install/last_failure` |
| 16 | **Nit** | `first-party-runtime.sh` (581); `deploy-to-pi.sh:733-745` | the hand-written 2PC journal cannot be activated through the only sanctioned deploy path: `JASPER_FIRST_PARTY_RUNTIME_BUNDLE` is **absent from the env-forwarding list** | forwarded keys are `JASPER_BUILD_SANDBOX*`, `JASPER_BUILD_SWAP*`, `JASPER_RUST_LOW_MEMORY_BUILD`, `SKIP_RESTART`. `grep` outside its own file/usage/tests → 0. *(confirms p1-T23 #9; **upgraded** — unreachable, not merely unexercised)* | either forward the var (making it a real seam) or delete the recovery machinery per p1-T23 |
| 17 | **Nit** | `install.sh:2292-2296` (and `:2244-2247`) | `trap install_exit_cleanup EXIT` is armed **after** `setup_build_swap_if_needed`; a `mkswap`/`swapon` failure strands a 2 GB `/var/tmp/jasper-build.swap` on the box that just ran out of disk | `BUILD_SWAP_CREATED=1` is set only after `swapon` succeeds (`build-sandbox.sh:173`); `cleanup_build_swap` no-ops without it. Self-heals on the next deploy's `rm -f "${path}"`. **NEW** | move the `trap` two lines up — both trap bodies are already no-ops when nothing was created/parked |
| 18 | **Nit** | `deploy-to-pi.sh:728-731,861-866`; `AGENTS.md:130` | `SKIP_RESTART=1` does not skip restarts — install.sh restarts fanin/camilla/mux/input/wizards and runs 6 reconcilers regardless; the flag skips only the wrapper's own 3, **and all four post-deploy gates** | `grep -rln SKIP_RESTART deploy/` → nothing; the script's own comment says install.sh does not read it. AGENTS.md lists it beside "`SKIP_INSTALL=1` rsync-only" implying symmetry. **NEW** | rename to `SKIP_POST_DEPLOY_RESTART`, or make it exit *after* the gates |
| 19 | **Nit** | `install.sh:1416-1421` | `set_jasper_env_value` is `sed -i` delete + `>>` append — the only non-atomic, non-quoting env writer in the tree, and install.sh never sources `deploy/lib/jasper-env-file.sh`, a lib it *installs* at `systemd-units.sh:35-37` | one caller (`python-runtime.sh:491`, value `"streambox"`), and the marker file is authoritative (`jasper/install_profile.py:159`), so blast radius is small. *(confirms p0-config #5; **re-graded down** to Nit on impact, kept for the "three bash env writers" cost)* | source the lib; delete the function |
| 20 | **Nit** | `rust-daemons.sh:15`, `build-sandbox.sh:109`, `install.sh:214`, `deploy-to-pi.sh:560` | the 1.2 GB low-memory threshold is spelled 4×, once as a bare literal on the **laptop** side — so `JASPER_BUILD_SWAP=1` makes the Pi pick `jasper-deploy-health` while the laptop still picks `jasper-doctor` | plus `/var/lib/jasper/build.txt` spelled in 5 Python modules (`bundles.py:111`, `audio_validation.py:74`, `web/_common.py:161`, `airplay_health.py:328`, `system_metrics.py:1033`) + install.sh + `_lib.sh`. **NEW** | one constant each; §F3 removes the laptop-side branch entirely |

Also confirmed but owned elsewhere, not re-reported: `jasper-web-streambox.service` runs as root
with 4 hardening directives (p1-T24 **Blocker** — confirmed at HEAD, it is on the deploy path for
every streambox); `install_systemd_units` re-inlining the six streambox helpers (p1-T24);
`aec_mode.env` seeded by two writers (p1-T23 #3); 65 source-text assertions across
`test_install_helpers.py` (31), `test_deploy_wiring_guards.py` (24, incl. a regex over the
`SUDO_INTERACTIVE` if-branch at `:516`), `test_install_profile_tiers.py` (10).

Two things that genuinely earn their keep and should not be touched: `install_exit_cleanup`
(`build-sandbox.sh:195-252`) — its `|| true` guards are measured, not defensive; and
`stage_rust_crate`'s `--checksum`-without-`-t` contract plus `RUST_BUILD_CACHE_FORMAT`
(`rust-daemons.sh:70-108`), which is the correct fix for two real stale-binary incidents.

## F. Proposals

**F1 — the STEPS table (p1-T23 #2): correct, and it should carry more than the plan.**
The two `main()` bodies differ in 6 rows out of ~40; the fork is data, not logic. One table
`name | profiles | fn | plan-phrase` deletes both `main()` bodies, both 150–210-line heredocs
(363 lines), and `tests/test_install_plan_covers_main.py` (202) — 565 lines of pure sync tax.
Two amendments: (a) the loop is also where the **rollback transaction** and a per-step
`jasper_install_log` belong, which fixes §C7 and §C15 for free and gives `--dry-run` a real
per-step timing/ordering answer; (b) hoist `reassert_{,int}secrets_compartment_perms` and
`migrate_wifi_guardian` out of `install_jasper` first (§C11) — otherwise the table still lies
about the full profile. Do **not** try to unify `install_{,streambox_}systemd_units` in the same
PR; that is p1-T24's diff and it is bigger than the table.

**F2 — minimal install-lib ship set: `build-sandbox.sh`, and nothing else.**
Verified: `/usr/local/lib/jasper/install/` has exactly one runtime consumer
(`jasper-contained-build:15`), and `build-sandbox.sh` sources no sibling — its only external
call, `unpark_low_memory_build_units`, is already `declare -F`-guarded. Replace the glob at
`systemd-units.sh:48-50` with the single file. The six non-`install/` libs under
`/usr/local/lib/jasper/` all have verified runtime consumers (`jasper-env-file.sh`,
`jasper-alsa-card.sh`, `jasper-asound-render.sh`, `jasper-apple-dongle.sh`,
`jasper-core-graph-park-units.sh`, `jasper-camilla-guard-common.sh`) — keep all six.

**F3 — `jasper-deploy-health` → `jasper-doctor --core`: take the cut, and fix the gate while you are in there.**
The ADR names the three unique rows (required units active, the accessory-reconcile path unit,
the pairing-aware streambox voice check); reading the script, the genuinely additional ones are
the two-sample outputd STATUS progress/xrun delta and the source-intent
expectation+fingerprint-stability pass — both belong in `audio_runtime_outputd.py` and a new
`source_intent` doctor module regardless of this cut. The real work is not the flag but
`jasper/cli/doctor/__init__.py:30-32`, which eagerly imports all 24 roster modules at package
import; `--core` needs a `CORE_MODULES` frozenset and a conditional loop, plus `core: bool` on
`RegisteredCheck`. Then:
- delete `deploy/bin/jasper-deploy-health` (900) and `tests/test_deploy_health_script.py` (1,642);
- delete the `build_swap_required` fork at `install.sh:2171` **and** the hardcoded `1200000`
  branch at `deploy-to-pi.sh:552-570` — the ADR says every box runs the subset (§C20);
- invert `:2167` so a missing venv is the case that *keeps* a stdlib fallback, not the case that
  skips health entirely (§C4). The honest residue is ~40 lines of stdlib `systemctl is-active`,
  not 900;
- make the `--core` exit code a **deploy gate** (§C3) — that is the change that turns all of this
  from a report into a guarantee.

## D. What only hardware/runtime can prove

1. That `rmmod snd_aloop` actually returns EBUSY on a ≥1.2 GB box at that point in the install
   (§C8) — needs a live speaker with renderers up. The *masking* is proven from source.
2. Whether `--core` is actually cheap enough on a 415 MB Zero 2 W after the lazy-import change,
   and whether full `jasper-doctor` really is too heavy there (the premise both the docstring and
   the DEEP-AUDIT assert; nothing in-repo measures it).
3. Whether a bad `sites-enabled/jasper.conf` survives to the next nginx restart in practice
   (§C6) — `nginx -t` failure modes vs `Restart=always` timing.
4. PR #4137's `opt-level=2` on low-memory Rust builds: whether it fits in the contained scope on
   1 GB, and its effect on install wall-clock. Noted as in flight; HEAD is still `opt-level=0`
   (`rust-daemons.sh:61`).
5. Whether the interactive-sudo `ssh -tt` stdout really is uncapturable, or whether a second
   `BatchMode=yes` channel restores the guards (§C2's proposed fix) — the comments assert the
   corruption; a pty test on real hardware would settle whether the fix is that cheap.
6. `sudo -n VAR=… bash install.sh` requires a permissive sudoers (`setenv`/`env_keep`); it
   demonstrably works in production, so a drop-in must exist outside this checkout.

## E. Coverage

**Read fully:** `scripts/deploy-to-pi.sh` (1,014), `deploy/install.sh` (2,343; structurally past
the two plan heredocs), `deploy/lib/install/rust-daemons.sh` (256), `web-assets.sh` (116),
`model-staging.sh` (64); `deploy/bin/jasper-contained-build` (58).
**Read in the parts this flow touches:** `systemd-units.sh` (`install_jasper_support_files`,
`install_local_audio_graph_unit_files`, the three transaction functions,
`install_{,streambox_}systemd_units` heads, the whole `:1740-1877` restart tail,
`reload_audio_recovery_udev_rules_for_install`, `install_nginx_recovery_dropin`);
`python-runtime.sh` (`install_jasper`, `install_streambox_jasper`, `jasper_aec3_*`);
`build-sandbox.sh` (`build_swap_*`, `setup_build_swap_if_needed`, `cleanup_build_swap`,
`install_exit_cleanup`); `env-migrations.sh` (`ensure_state_dir`, `heal_shared_state_modes`);
`ring-platform.sh` (`build_install_jts_ring_ioplug`, provenance); `first-party-runtime.sh`
(activation seam only); `scripts/_lib.sh` (target resolution, peer-id, direction helpers);
`deploy/bin/jasper-deploy-health` (header, `main`, tail); `jasper/cli/doctor/{__init__,_registry}.py`;
`jasper/web/_common.py:158-180`.
**Verified by execution** (scratchpad `S4-deploy/`, stubbed `ssh`/`rsync`, throwaway git repo, no
writes to `/home/user/JTS`): `SKIP_INSTALL=1` guard bypass ×2 (downgrade, identity mismatch),
the passing plain-deploy control for each, and the interactive-sudo bypass under a pty.
**Greps used as evidence** (stated as such): `usr/local/lib/jasper/install` consumers;
`JASPER_FIRST_PARTY_RUNTIME_BUNDLE`; `--core`; `SKIP_INSTALL`/`SKIP_RESTART`/`SUDO_INTERACTIVE`
in `tests/`; `1200000`; `build.txt`; `logger -t jasper-install`; `set_jasper_env_value`;
source-text assertion counts.
**Skipped:** `renderers.sh`, `service-users.sh`, `memory-resilience.sh` bodies (hops 13/17/25 —
p1-T23 owns them and this scenario found no new seam there); all unit-file *contents* (p1-T24);
test bodies beyond assertion shapes; `install_camillagui`, `install_avahi_jasper_control`,
`provision_correction_tls` internals.
