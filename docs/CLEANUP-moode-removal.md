# moOde Removal Cleanup — operator + AI runbook

**Status:** _planned cleanup, not yet executed_. Run this only after the
new no-moOde stack has soaked on production hardware for at least a
week with no functional regressions vs. the moOde flow.

This document is the single source of truth for **getting moOde out of
the codebase entirely**. After this cleanup, no source file, deploy
artifact, doc, comment, test, or env var should reference moOde. A
fresh contributor reading the repo cold should never know moOde was
ever part of this project.

The work here is mechanical but extensive — **554 lines across ~70
files mention moOde** at the time of writing. The plan below organizes
those into ~7 commits, in a dependency order that keeps the codebase
buildable + tests passing at each step.

---

## How to use this document

Read top to bottom once for context. Then start at §6 ("The cleanup
plan") and execute commit by commit. After each commit, run the
verification at the end of that commit's section before moving on.

If you're an AI agent picking this up in a fresh context window: the
codebase is at `~/Code/JTS`, branch is `main` (the migration was
already merged). The new stack is shipped and runs on `jts.local`.
Your job is the cleanup, not new feature work.

---

## 1. Why we left moOde

JTS started as a smart-speaker layered on top of [moOde audio](https://moodeaudio.org/),
a Debian-based audio distribution that bundles MPD + shairport-sync
+ librespot + bluez-alsa + a PHP/nginx web UI. The original v1 plan
treated moOde as the foundation: we'd add a CamillaDSP + voice daemon
on top, hijack moOde's `_audioout` ALSA symbol to redirect renderers
into our snd-aloop loopback, and let moOde's web UI handle the user
configuration surface.

This worked, but accumulated friction:

- **moOde's `worker.php` renderer state machine + `cdsp_volume_sync`
  watchdog had a long history of regressions** around session
  transitions (release notes show repeated "FIX: CamillaDSP volume
  restore after renderer ends" etc.). The coordination layer wasn't
  designed for our use case (voice control overlay + multi-account
  Spotify routing).
- **moOde owns `/etc/asound.conf`, `/etc/mpd.conf`, `/var/local/www/`,
  and parts of `/etc/alsa/conf.d/`.** Any moOde update could
  silently rewrite our config. The `zz-` prefix hack on
  `_audioout` was robust against alphabetical conflicts but fragile
  against moOde's own ALSA-config changes between versions.
- **The web UI was unused.** JTS is voice-controlled; the moOde admin
  surface is dead weight.
- **RAM cost was non-trivial.** PHP-FPM + nginx + MiniDLNA + UPnP
  indexer + MPD library cache idled around 300–400 MiB; on a 1 GB
  Pi 5 that's 30–40% of available memory burned on infrastructure
  we don't use.
- **Multi-renderer coexistence** was held together by `worker.php`'s
  start/stop dance, which doesn't extend cleanly to "voice command
  triggers preemption on a specific renderer."

In May 2026 we migrated to a stock Raspberry Pi OS Lite (Trixie,
Debian 13) base, source-built shairport-sync with AirPlay 2 +
nqptp, dropped in go-librespot + bluez-alsa, and replaced moOde's
coordination layer with a small `jasper-mux` daemon. The migration
shipped on the `migrate/no-moode` branch (now merged to `main`).
End-state on `jts.local` (validated 2026-05-07): all three renderers
(AirPlay 2, Spotify Connect, Bluetooth A2DP) work, voice end-to-end
works, total daemon RAM ~76 MB, system idle ~273 MiB used vs.
600–800 MiB on moOde.

---

## 2. The moOde architecture (the part we're deleting)

Audio path on the moOde stack (source files about to be removed):

```
phone → AirPlay/Spotify/BT
     ↓ (handled by moOde-installed daemons)
   shairport-sync / librespot / bluez-alsa / mpd
     ↓ all write to ALSA pcm._audioout
   /etc/alsa/conf.d/_audioout.conf  ← moOde-owned, points at user's DAC
     ↓ overridden by our zz-jts-loopback.conf hijack
   pcm.!_audioout = type plug → hw:Loopback,0,0  ← our hijack
     ↓
   snd-aloop card "Loopback"
     ↓
   pcm.jasper_capture (dsnoop) → CamillaDSP → pcm.jasper_out (dmix on dongle)
```

Coordination path (also being deleted):

```
moOde worker.php loop
  ├── monitors which renderer is active (writes flags to moOde-sqlite3.db)
  ├── stops MPD when AirPlay/Spotify connects
  └── runs cdsp_volume_sync to mirror shairport/librespot volume into
      CamillaDSP main_volume
```

Jasper code that reaches into this:

- `jasper/moode.py:MoodeClient` — polls moOde's REST + SQLite for
  renderer state, dispatches transport commands via
  `/command/?cmd=...`
- `jasper/cli/doctor.py:check_moode_http`, `_read_moode_*` — health
  checks that hit moOde's REST and SQLite
- `jasper/spotify_routing.py`, `jasper/tools/transport.py`,
  `jasper/tools/spotify.py`, `jasper/control/server.py` — all take
  a `moode: MoodeClient` parameter and call methods on it
- `deploy/install.sh` — has a `--backend=moode` branch (the original
  default) that installs the `_audioout` hijack, the shairport
  drop-in to redirect at `_audioout`, and patches moOde's nginx site
- `deploy/alsa/zz-jts-loopback.conf` — the hijack itself
- `deploy/alsa/asoundrc.jasper` — `/root/.asoundrc` template assuming
  the moOde topology (jasper_capture dsnoop fan-out for the AEC
  bridge, jasper_out dmix on the dongle)
- `deploy/systemd/shairport-sync-jts-output.conf` — drop-in that
  forces moOde's shairport-sync.service to write to `_audioout`
- `deploy/nginx-jasper.conf`, `deploy/nginx-jasper-https.conf` —
  reverse-proxy snippet that gets included into moOde's nginx site

---

## 3. The new architecture (what stays)

After cleanup, the stack is:

```
phone → AirPlay 2/Spotify Connect/BT
     ↓
   shairport-sync (source-built, AirPlay 2 + nqptp)
   go-librespot (Spotify Connect, HTTP API on :3678)
   bluez-alsa-aplay (BT A2DP sink, --a2dp-volume)
     ↓ each writes directly to hw:Loopback,0,0
   snd-aloop card "Loopback" (modprobe index=6,7)
     ↓
   plughw:Loopback,1,0 → CamillaDSP → pcm.jasper_out (dmix on dongle) → speakers
                                ↑
                       jasper-voice TTS also writes here
```

Coordination:

```
jasper-mux (1 Hz polling daemon)
  ├── go-librespot HTTP /status
  ├── shairport-sync MPRIS PlaybackStatus over busctl
  └── bluez-alsa list-pcms
  → on transition to playing, pauses the older active source
    (Spotify pause via /player/pause; AirPlay pause via MPRIS;
     Bluetooth no graceful pause — best-effort)
```

Files that own this (these all stay, just lose the moOde-conditional
parts):

- `jasper/renderer.py:DebianBackend` — the only renderer client
  after cleanup
- `jasper/mux.py` — the source-arbiter daemon
- `deploy/debian-stack/*` — currently the source of truth for the new
  stack; these get **promoted** to canonical `deploy/` locations
  during cleanup
- `jasper/cli/doctor.py` — keeps the new debian-stack checks
  (`check_go_librespot_http`, `check_shairport_sync_ap2`,
  `check_nqptp_running`, `check_bluealsa`, `check_jasper_mux`,
  `check_apple_dongle_audio`)

---

## 4. Why this cleanup matters

The user's explicit ask: **"At the end of the process, the codebase
should be beautifully clean, and there should be no vestigial
mentions of moOde. No one should know that we ever used moOde."**

Concretely:

- A new contributor reading `README.md` should learn the project is a
  Pi 5 + Trixie Lite + custom audio stack speaker, not "originally
  on moOde."
- `grep -ri moode .` from the repo root should return zero matches
  (excluding `.git`, log archives, and historical commit messages
  which we deliberately don't rewrite).
- No dead code paths. No `if backend == "moode"` branches. No
  `MoodeClient` class. No `MOODE_BASE_URL` env var.
- Deploy artifacts have one canonical location each. No
  `deploy/debian-stack/` parallel tree.
- Tests don't have `moode = MagicMock()` mocks; mock parameters
  are named `backend` or `renderer` after their actual role.

---

## 5. Prerequisites before cleanup

**Do not start cleanup until ALL of these are true.** This is the
single biggest risk — cleaning up while production is still on
moOde would brick production.

1. **Production hardware runs the new stack.** The "production
   speaker" today is `jasper.local` running moOde + JTS on top.
   Before cleanup, either:
   - Reflash `jasper.local` with Pi OS Lite Trixie, run
     `install.sh`, and verify daily-driver use for ≥1 week, OR
   - Promote `jts.local` to be the production speaker (rename it
     to `jasper.local` or update everyone's expectations) and
     retire the old `jasper.local`.

2. **All four renderer paths verified end-to-end on production:**
   - AirPlay 2 from iPhone plays audibly through speakers
   - Spotify Connect from iPhone (or Mac) plays audibly
   - Bluetooth A2DP plays audibly
   - Voice ("Hey Jarvis, what's the weather?") works

3. **The volume system overhaul has shipped.** See the volume
   handoff prompt that Jasper has — bidirectional source-side
   volume sync is the highest-risk piece of post-migration work.
   Better to land that and let it bake before stripping out
   options.

4. **The XVF mic is on the production Pi.** Voice loop needs the
   mic; AEC bridge depends on the chip being on the 6-channel
   firmware variant.

5. **A Spotify multi-account refresh** (jasper-spotify-auth web
   UI) has been run for each household member against the new Pi.
   Per-account OAuth refresh tokens live in `/var/lib/jasper/spotify/`
   and need to exist before cleanup if you don't want a re-auth
   day on cleanup-merge day.

6. **Production has `JASPER_RENDERER_BACKEND=debian` set explicitly**
   in `/etc/jasper/jasper.env`. The cleanup deletes the env var,
   which means the default behavior changes; if any system was
   silently relying on the default `"moode"`, it would break at
   merge time. Setting `=debian` explicitly first surfaces any
   such system before we delete the var.

7. **You're on a fresh feature branch.** Do not do this on `main`.
   Suggested name: `cleanup/no-moode-vestiges`.

8. **Working tree is clean.** No half-finished dial work, no stash
   you forgot about. `git status` shows nothing.

---

## 6. The cleanup plan

Seven commits, in dependency order. Each has a verification step
that must pass before moving on.

### Commit 1 — promote `deploy/debian-stack/` to canonical locations

This is a pure file-move commit. The `deploy/debian-stack/`
subdirectory was a temporary "the new stack lives here while we
keep the old one alongside" structure. Now we collapse it.

**File moves (use `git mv`):**

| From | To |
|---|---|
| `deploy/debian-stack/etc/asoundrc-jasper.template` | `deploy/alsa/asoundrc.jasper` _(replaces existing)_ |
| `deploy/debian-stack/etc/camilladsp/v1.yml` | `deploy/camilladsp/v1.yml` _(replaces existing)_ |
| `deploy/debian-stack/etc/modprobe.d/snd-aloop.conf` | `deploy/modprobe.d/snd-aloop.conf` _(replaces existing)_ |
| `deploy/debian-stack/etc/shairport-sync.conf` | `deploy/shairport-sync.conf` _(new)_ |
| `deploy/debian-stack/etc/go-librespot/config.yml` | `deploy/go-librespot/config.yml` _(new)_ |
| `deploy/debian-stack/systemd/go-librespot.service` | `deploy/systemd/go-librespot.service` _(new)_ |
| `deploy/debian-stack/systemd/shairport-sync.service` | `deploy/systemd/shairport-sync.service` _(new)_ |
| `deploy/debian-stack/systemd/nqptp.service` | `deploy/systemd/nqptp.service` _(new)_ |
| `deploy/debian-stack/systemd/bt-agent.service` | `deploy/systemd/bt-agent.service` _(new)_ |
| `deploy/debian-stack/systemd/bluealsa-aplay.service.d/jts-output.conf` | `deploy/systemd/bluealsa-aplay.service.d/jts-output.conf` _(new)_ |
| `deploy/debian-stack/systemd/jasper-mux.service` | `deploy/systemd/jasper-mux.service` _(new)_ |
| `deploy/debian-stack/configure-bluez.sh` | `deploy/configure-bluez.sh` _(new)_ |

**File deletions:**

- `deploy/debian-stack/README.md` — content folds into top-level
  `README.md` and `BRINGUP.md` rewrites in commit 6
- `deploy/debian-stack/` directory itself (empty after the moves)

**File deletions of moOde-stack artifacts (these are now superseded):**

- `deploy/alsa/zz-jts-loopback.conf` — the `_audioout` hijack
- `deploy/systemd/shairport-sync-jts-output.conf` — moOde's drop-in
- `deploy/nginx-jasper.conf` — moOde nginx integration
- `deploy/nginx-jasper-https.conf` — _**HOLD this one**_ — see Edge
  Cases §7. If we still want HTTPS for `/spotify` on the new stack,
  this file becomes a standalone nginx site (rewritten to not
  require moOde's nginx). If we defer that to a later commit, leave
  the file in place for now.

**Important — content edits during the move:**

After the file moves, edit each promoted file to remove
"debian-stack" / "no-moOde" / "differs from moOde" framing in the
header comments. The whole point is these are the canonical files
now; they should describe themselves on their own terms, not in
contrast to a moOde version that no longer exists.

Examples:
- `deploy/alsa/asoundrc.jasper` (newly promoted): the existing
  comment says "debian-stack variant. The moOde-stack version uses
  jasper_capture dsnoop." Drop that. Just describe what `jasper_out`
  does without the contrast.
- `deploy/camilladsp/v1.yml`: same — drop the "differs from
  deploy/camilladsp/ moOde version" sentence. Just describe what
  this config does.
- `deploy/modprobe.d/snd-aloop.conf`: drop the "moOde stack uses
  index=0,5; this debian variant uses 6,7" rationale. Just say
  "index=6,7 — clears HDMI which claims 0,1 on Pi OS Lite."

**Commit message:** `deploy: promote debian-stack/ to canonical paths;
delete moOde-only configs`

**Verification:**
- `find deploy -type f` — no `debian-stack/` directory remains;
  no `zz-jts-loopback.conf`; no `shairport-sync-jts-output.conf`;
  no `nginx-jasper.conf`
- `cat deploy/alsa/asoundrc.jasper` — content matches what was at
  `deploy/debian-stack/etc/asoundrc-jasper.template`
- `git log --diff-filter=R --summary HEAD~1..HEAD | grep rename` —
  shows the renames (so git history follows the file)

---

### Commit 2 — delete `jasper/moode.py`; collapse `jasper/renderer.py`

Refactor the renderer module. Currently `renderer.py` has:
- `RendererBackend` Protocol (the abstraction)
- `DebianBackend` class (the new impl)
- `MoodeClient` lazy-imported from `jasper/moode.py`
- `make_backend()` factory that picks based on env var

After cleanup:
- One concrete class: `RendererClient` (renamed from `DebianBackend`).
  No Protocol — there's only one implementation and Python's duck
  typing handles tests fine without a formal interface.
- No factory. Callers just do `RendererClient(...)`.
- `jasper/moode.py` deleted.

**Concrete edits to `jasper/renderer.py`:**

1. Delete the `RendererBackend` Protocol class (lines ~36–55).
2. Rename `class DebianBackend:` to `class RendererClient:`.
   Delete the docstring's "no-moOde stack" framing — describe what
   the class does, not what it isn't.
3. Delete the entire `make_backend()` function (~lines 410–445).
4. Delete the lazy `from .moode import MoodeClient` inside
   `make_backend`.
5. Module docstring: rewrite. Drop "two implementations conform
   to RendererBackend protocol" framing; describe `RendererClient`
   as "renderer state poller + transport dispatcher" or similar.

**Concrete edits to `jasper/voice_daemon.py`:**

```python
# Before:
from .renderer import RendererBackend, make_backend
...
moode: RendererBackend,
...
moode = make_backend(
    moode_base_url=cfg.moode_base_url,
    mpd_host=cfg.mpd_host,
    mpd_port=cfg.mpd_port,
    go_librespot_url=cfg.go_librespot_url,
    backend_name=cfg.renderer_backend,
)

# After:
from .renderer import RendererClient
...
renderer: RendererClient,  # parameter name also gets renamed
...
renderer = RendererClient(
    go_librespot_url=cfg.go_librespot_url,
    mpd_host=cfg.mpd_host,
    mpd_port=cfg.mpd_port,
)
```

Note the **parameter rename `moode → renderer`** — that's a
sibling of this commit, doesn't have to be its own. Local variable
also renamed. Keep the rename consistent across this file.

**Concrete edits to `jasper/control/server.py:_toggle_transport`:**

Same pattern as voice_daemon — replace `make_backend(...)` with
direct `RendererClient(...)` instantiation, rename the local
variable from `moode` to `renderer` or `backend`.

**Delete `jasper/moode.py` entirely.**

**Verification:**
- `grep -rn "MoodeClient\|make_backend\|moode_base_url\|JASPER_RENDERER_BACKEND" jasper/` — zero results
- `python -c "from jasper.renderer import RendererClient"` — imports
  cleanly
- `pytest tests/test_renderer.py` — fails until commit 4 cleans
  up the tests; that's fine. Defer to commit 4. (Or do a partial
  test fix here if you'd rather have green at every step — see
  commit 4 for the full test cleanup.)

**Commit message:** `renderer: delete MoodeClient; collapse to single RendererClient`

---

### Commit 3 — rename `moode` parameter to `renderer` (or `backend`) repo-wide

Mechanical rename across all callers of the old `MoodeClient`/
`RendererBackend`. The variable names were never accurate after
the renderer abstraction landed; this commit makes them honest.

**Files affected** (all just a variable/parameter rename, no
logic change):

- `jasper/spotify_routing.py` — parameter `moode` → `renderer` (or
  `backend`, pick one and stick with it). 6 references.
- `jasper/tools/transport.py` — same. 16 references including
  `make_transport_tools(moode, router)` and
  `make_transport_dispatcher(moode, router)`.
- `jasper/tools/spotify.py` — same. 4 references including
  `make_spotify_tools(router, moode, librespot_name)`.
- `jasper/control/server.py` — already partially done in commit 2;
  finish here. Variable inside `_toggle_transport`.

**Style note:** I'd recommend `renderer` as the new name —
`backend` is overloaded (Gemini API also has "backend" concepts).
`renderer` accurately describes the role: the thing that polls and
controls the music renderer daemons.

**Verification:**
- `grep -rn "\bmoode\b" jasper/` — zero matches (lowercase, word-
  boundary; this is the variable name, not the brand name).
- `pytest tests/test_tools_transport.py tests/test_renderer.py` —
  pre-existing tests still need fixing in commit 4; for now run
  whatever's possible. The control/dial tests that don't touch
  the renderer mock should still pass.

**Commit message:** `refactor: rename moode parameter to renderer across callers`

---

### Commit 4 — test cleanup

The tests reference moOde extensively because they predate the
renderer abstraction. Now that the abstraction is collapsed, tests
should mock `RendererClient` directly with parameters named
`renderer`.

**Files to clean up:**

- `tests/test_renderer.py` (15 moode references) —
  - Delete `test_make_backend_moode_returns_moode_client`
  - Delete `test_make_backend_unknown_name_falls_back_to_moode`
  - Delete `test_make_backend_reads_env_when_no_explicit_name`
    (the env var is gone)
  - Keep all `DebianBackend`/`RendererClient` tests, renaming
    references as needed
  - Update import: `from jasper.renderer import RendererClient`
    instead of `DebianBackend, RendererBackend, make_backend`
  - The `test_protocol_runtime_check` test is moot if we drop the
    Protocol — delete it.

- `tests/test_tools_transport.py` (51 moode references) — bulk
  rename `moode` → `renderer` in mock variables. Test logic is
  unchanged.

- `tests/test_tools_spotify.py` (69 moode references) — same
  bulk rename.

- `tests/test_spotify_routing.py` (32 moode references) — same.

- `tests/test_spotify_router.py` — same.

- `tests/test_config.py` — delete tests for `moode_base_url` and
  `renderer_backend` Config fields (those fields are deleted in
  commit 5).

**Verification:**
- `grep -rn -i "moode" tests/` — zero matches
- `pytest -q` — full test suite green (modulo the pre-existing
  `test_volume_persistence.py::test_partial_update_preserves_other_field`
  failure that's unrelated to this work and predates the migration)

**Commit message:** `tests: rename moode mocks to renderer; drop deleted-API tests`

---

### Commit 5 — Config + env cleanup

`jasper/config.py` carries the legacy fields. Delete them.

**Edits to `jasper/config.py`:**

- Delete `moode_base_url: str` field
- Delete `renderer_backend: str` field
- Delete corresponding `_env(...)` lines in `from_env()`:
  ```python
  moode_base_url=_env("MOODE_BASE_URL", "http://127.0.0.1"),
  renderer_backend=_env("JASPER_RENDERER_BACKEND", "moode"),
  ```
- Change `spotify_device_name` default from `"moode"` to
  `"JTS"` (or whatever go-librespot's `device_name` is set to in
  `deploy/go-librespot/config.yml`). The default in `.env.example`
  changes too.
- Delete the comment block referring to "moOde defaults to 'Moode
  <hostname>'" — outdated.

**Edits to `.env.example`:**

- Delete `MOODE_BASE_URL=http://127.0.0.1` line
- Delete `JASPER_RENDERER_BACKEND=...` line if present
- Change `JASPER_SPOTIFY_DEVICE_NAME=moode` to `=JTS`
- Verify `JASPER_GO_LIBRESPOT_URL=http://127.0.0.1:3678` is present

**Edits to `pyproject.toml`:**

- `description = "Open smart-speaker voice daemon for Pi 5 + moOde + CamillaDSP"`
  → `description = "Voice-controlled smart speaker on Raspberry Pi 5 with CamillaDSP DSP and Gemini Live"`
  (or similar — drop moOde, add the actual stack)

**Verification:**
- `grep -ni moode jasper/config.py .env.example pyproject.toml` —
  zero matches
- `python -c "from jasper.config import Config; cfg = Config.from_env()"`
  — succeeds (with appropriate env vars set)
- `pytest tests/test_config.py` — green

**Commit message:** `config: delete moode_base_url and renderer_backend; refresh defaults`

---

### Commit 6 — `install.sh` simplification + script cleanup

`deploy/install.sh` carries the entire `--backend=moode` branch.
Delete it. Also clean up the small scripts.

**Edits to `deploy/install.sh`:**

- Delete the `--backend=` flag parsing block (lines ~30–40 era).
  No more BACKEND variable.
- Delete every `if [[ "$BACKEND" == "moode" ]]` / `else` branch.
  Keep the body of the debian branch as the one true path.
- Delete the moOde-stack constants near the top (they were already
  unused on debian — verify before deleting).
- Delete `install_self_signed_cert` if HTTPS is deferred, OR
  rewrite it to deploy a standalone nginx site (not patch into
  moOde's). See Edge Cases §7.
- Delete `install_nginx_proxy` entirely if deferred, OR rewrite
  for standalone. The current debian path already says
  `(debian backend — skipping moOde nginx integration; see TODO)`
  — replace that TODO with either a real implementation or just
  delete the function call from `main()`.
- Delete the moOde-version-detection comment near `install_camilladsp`
  ("moOde 10.1.2 ships CamillaDSP 3.0.1 ..."). The version-stop is
  fine, the comment about why is outdated.
- Update header comment: drop the moOde-vs-debian backend
  description; just describe what install.sh does (single path).
- Update file references to point at the promoted paths (commit 1):
  - `${REPO_DIR}/deploy/debian-stack/etc/...` → `${REPO_DIR}/deploy/...`
  - `${REPO_DIR}/deploy/debian-stack/systemd/...` → `${REPO_DIR}/deploy/systemd/...`
  - `${REPO_DIR}/deploy/debian-stack/configure-bluez.sh` →
    `${REPO_DIR}/deploy/configure-bluez.sh`

After this commit, `install.sh` reads top to bottom as a single
deployment story for stock Pi OS Lite — no flags, no branches.

**Edits to `scripts/pi-bundle.sh`:**

- Line 64 references `/var/local/www/footer.txt` (the moOde version
  marker). Delete that line — it's diagnostic output for
  moOde version detection, no longer relevant.

**Edits to `scripts/deploy-pass2.sh`:**

- This script patches moOde's nginx site directly (lines ~48, 56
  reference `/etc/nginx/sites-enabled/moode-http.conf`). Either:
  - Delete the script entirely if its purpose is moOde-specific,
    OR
  - Rewrite for standalone nginx (no moOde site to patch).
  - **Recommendation:** delete the script, rebuild it as part of
    a future "nginx for /spotify on debian" follow-up. It's a
    140-line script that's all moOde-stack glue.

**Edits to `scripts/fetch-pi-logs.sh`** (verify; may not need changes):

- Check for moOde-specific log paths / configs being fetched. If
  any references exist, either drop them or replace with the
  equivalent debian-stack paths (e.g. `/etc/jasper/jasper.env`
  is fine in either; `/var/local/www/db/moode-sqlite3.db` is
  obsolete).

**Verification:**
- `grep -ni moode deploy/install.sh scripts/` — zero matches
- `bash -n deploy/install.sh` — syntax-clean
- Spin up a fresh Trixie Lite Pi, rsync the repo, run
  `sudo bash deploy/install.sh` — should complete without errors
  and produce a working speaker. (Smoke-test on a non-production
  Pi if possible.)

**Commit message:** `install.sh: drop --backend flag; collapse to single Debian path`

---

### Commit 7 — Documentation rewrite

This is the biggest commit by line count but each edit is
straightforward: rewrite everything that mentions moOde, replacing
moOde-flow with the Debian-flow.

**Files to rewrite:**

#### `README.md` (~14 references)

- Header: drop "plus [moOde audio]" link. Replace with "on
  Raspberry Pi OS Lite" or similar.
- "Music streaming via moOde" status bullet → "Music streaming
  (AirPlay 2, Spotify Connect, Bluetooth A2DP)" — drop the via.
- Architecture diagram (the ASCII art): the `moOde renderers
  (MPD/shairport-sync/librespot/bluealsa)` line and the
  `(rewrites pcm._audioout to point at snd-aloop)` annotation —
  rewrite to reflect direct daemons writing to `hw:Loopback,0,0`.
- "No-moOde install option" status bullet — delete (it's not an
  option, it's the only path). Adjust nearby bullets.
- "Repository layout" section — keep as is; references in there
  have already been generic.
- Documentation map at the bottom: keep the structure, drop any
  references to docs that no longer exist.

#### `CLAUDE.md` (~18 references)

- Delete the entire "Renderer backend — moode vs debian" section.
  No more choice.
- `## File ownership` section: rewrite. Currently says "moOde owns
  /etc/asound.conf, /etc/mpd.conf, /var/local/www/." That's no
  longer true. Replace with the new ownership map: install.sh
  owns the files in `/etc/...` it creates; nothing else owns
  anything we care about.
- "Twin file: `AGENTS.md` mirrors this" — keep, update both.
- `worker.php` references in the jasper-mux section — drop
  ("replaces worker.php" → just describe what jasper-mux does).
- Any remaining moOde mentions in scattered comments — sweep.

#### `AGENTS.md` (~17 references)

Mirror of CLAUDE.md edits. Keep the two files in sync.

#### `BRINGUP.md` (~56 references — biggest doc edit)

This document was the moOde flow runbook. It needs a substantial
rewrite, not a sweep. Topics to cover in the new version:

1. Hardware procurement (unchanged — Pi 5 2GB, Apple dongle,
   ReSpeaker XVF3800, optional CrowPanel dial)
2. Flash Pi OS Lite 64-bit Trixie via Raspberry Pi Imager (with
   the OS customization gear icon: SSH key, Wi-Fi, hostname,
   user/password)
3. First boot: SSH in, set up passwordless sudo
4. `apt update && apt full-upgrade`, install base tools (`git`,
   `vim`, etc.)
5. Plug in the dongle (note the Apple-dongle-needs-analog-load
   quirk per `project_apple_dongle_jack_load_required` memory)
6. Plug in the ReSpeaker mic
7. Clone the repo, run `sudo bash deploy/install.sh`
8. Set GEMINI_API_KEY, location, subway, etc. in
   `/etc/jasper/jasper.env`
9. Pair iPhone over Bluetooth (one-time)
10. Set up Spotify OAuth via web flow (one-time per household
    member)
11. Test wake word: "Hey Jarvis, what time is it?"
12. Doctor: `sudo -E /opt/jasper/.venv/bin/jasper-doctor`

Reference the existing "Apple dongle quirk" memory file for the
USB-vs-audio gotcha. Reference the safe-test-volume memory for
volume calibration.

**Approach for the rewrite:** consider deleting the existing
`BRINGUP.md` and writing a fresh one rather than editing in
place — the old structure was organized around moOde's
configuration flow (Phase 1A/1B/2A/2B/3/4 mapped to moOde steps).
The new structure is linear ("flash, install, configure, use").

#### `PLAN.md` (~25 references)

`PLAN.md` is the historical "v1 phased build" document. It's
mostly outdated even before this cleanup — v1 shipped, the moOde
phases are history. Two options:

- **Option A — preserve as historical artifact:** add a header note
  "This document describes the v1 plan as it was when development
  started in early 2026. It references moOde because v1 shipped on
  moOde. The v1.1 migration to a no-moOde stack is documented in
  `docs/CLEANUP-moode-removal.md` (this file). For current
  architecture see README.md." Then do minimal edits below.
- **Option B — rewrite into a forward-looking roadmap:** keep only
  the "what comes after v1" tail section (the v1.1–v9 sequence
  table), drop everything moOde-specific, add the no-moOde
  migration as a completed entry.

**Recommendation:** Option B. The phased-build sections are
historical; the "what comes after" section is the actually-useful
forward-looking roadmap. Cut PLAN.md down to the sequence table
+ updated risks section.

#### `docs/HANDOFF-voice-music-control.md` (~9 references)

This doc describes how voice-driven transport routing works. The
moOde references are about reading renderer state from
`moode-sqlite3.db` and routing transport via moOde's REST. After
cleanup, those code paths are deleted; the doc should describe
the new approach (DBus subscribers, go-librespot HTTP, MPRIS).

Either rewrite in place or delete + rewrite. Recommend rewrite.

#### `docs/HANDOFF-aec.md` (review)

Skim for moOde mentions. The AEC architecture is independent of
the audio backend; mentions are likely just topology references
in the comments. Sweep.

#### `docs/HANDOFF-persistent-live-session.md` (review)

Same — likely just a few references in comments. Sweep.

#### `docs/multi-user-spotify.md`

This describes how multi-account Spotify routing works. The
moOde references are about reading active-renderer state from
moOde's SQLite. With go-librespot HTTP API on the new stack, the
mechanism changes. Rewrite.

#### `docs/audit-pending-followups.md`

Probably stale. Read it; some items will be resolved (the moOde
TODO items), some will be re-categorized for the new stack, some
will move to `PLAN.md` or this cleanup doc. Refresh in place.

#### `docs/aec-chipside-test-briefing.md`, `docs/aec-chipside-final-test-plan.md`

Skim for moOde mentions; topology-related comments only, sweep.

#### `deploy/debian-stack/README.md` — delete

The content (file map, topology, what's NOT in apt) folds into
`README.md` and `BRINGUP.md`. After commit 1's promotion this
file is the last thing in `deploy/debian-stack/`; delete it.

**Verification:**
- `grep -rni "moode\|moOde\|Moode" --exclude-dir=.git --exclude-dir=logs --exclude-dir=.venv .` —
  zero matches **anywhere in the repo**, except possibly in
  `docs/CLEANUP-moode-removal.md` (this file) which legitimately
  documents the removal
- Manual read-through of README.md and BRINGUP.md — they should
  feel like a project that was always on Debian, not a project
  that migrated

**Commit message:** `docs: rewrite README, BRINGUP, PLAN, HANDOFFs to drop moOde framing`

---

## 7. Edge cases and gotchas

### `nginx-jasper-https.conf`

The Spotify OAuth web flow needs HTTPS (Spotify's OAuth refuses
non-loopback HTTP redirect URIs as of 2024). The current
`deploy/nginx-jasper-https.conf` is written assuming nginx is
already deployed (it patches into moOde's nginx). On the new
stack, we need to **also install nginx ourselves** — apt install
nginx, deploy our own site config, restart nginx — for the
Spotify flow to work.

**Decision required:**
- **Option A (defer):** delete `nginx-jasper-https.conf` and
  `nginx-jasper.conf`, drop the `install_nginx_proxy` and
  `install_self_signed_cert` from install.sh. Document
  "Spotify multi-account web flow temporarily unavailable on
  the no-moOde stack; runs `jasper-spotify-auth` CLI instead
  (manual one-account-only flow)." Add to `PLAN.md` as a
  follow-up.
- **Option B (do it now as part of cleanup):** rewrite
  `install_nginx_proxy` to apt-install nginx, deploy our own
  site config (without the include-into-moOde dance), restart
  nginx. Adds maybe 50 lines to install.sh.

The single-account `jasper-spotify-auth` CLI flow still works
either way; multi-account web flow is the casualty of Option A.

**Recommendation:** Option A for cleanup speed; the user already
flagged "configuration web view" as upcoming work in `PLAN.md`,
and a real config web view would supersede the OAuth-only flow
anyway.

### Deleting `jasper/moode.py` if anything still imports it

Before commit 2 deletes the file, verify no leftover imports:

```sh
grep -rn "from jasper.moode\|from .moode\|import moode" jasper/ tests/
```

Should be zero after commit 2's renderer.py edit. If anything
remains, fix it before deleting the file.

### The `RendererBackend` Protocol vs. concrete class

The cleanup recommends dropping the `RendererBackend` Protocol
and making `RendererClient` the single concrete class. If you
prefer to keep the Protocol for documentation/typing reasons —
e.g. you anticipate a future `MockRendererBackend` for testing
— that's defensible. The cost is one extra file/class to
maintain. If you keep it, rename `DebianBackend` → just have
`RendererBackend` be both the Protocol and the concrete class
won't work (name collision). Options:
- `RendererBackend` (Protocol) + `RendererClient` (concrete)
- `Renderer` (Protocol) + `RendererClient` (concrete)
- Delete Protocol; just `RendererClient`

I lean "just `RendererClient`" — Python's duck typing means a
mock object in tests doesn't need to literally inherit anything,
and the Protocol is more typing-system-theatre than substance.

### `pyproject.toml` description

A subtle gotcha: `pyproject.toml`'s `description` field is what
shows up in `pip show jasper-speaker` output. Update it to a
sentence that's accurate **after cleanup**, not a transitional
"... originally on moOde, migrated to..."

### Git history

We're not rewriting git history — old commits will still mention
moOde in their messages and diffs. That's fine and intentional;
git history is the record of how the project got here. The
cleanup target is the **current state of HEAD**, not the
historical record.

### Production Pi config drift

Even after cleanup ships, a production Pi that was provisioned
before cleanup may have leftover files in `/etc/` from the moOde
era (e.g. `/etc/alsa/conf.d/zz-jts-loopback.conf` if it was on
moOde before). Re-running `install.sh` after cleanup **does not**
delete those — install.sh is additive, not destructive.

If the production Pi was provisioned correctly under the new
stack from a fresh Trixie flash, this isn't an issue. If the
production Pi was migrated in-place from moOde to debian, do a
manual cleanup pass on the production filesystem after the
software cleanup ships:

```sh
# On the production Pi:
sudo rm -f /etc/alsa/conf.d/zz-jts-loopback.conf
sudo rm -f /etc/systemd/system/shairport-sync.service.d/jts-output.conf
# (any other vestigial files install.sh's old branches created)
sudo systemctl daemon-reload
sudo systemctl restart shairport-sync
```

Document this pass in the commit message of commit 7 or as a
release-notes entry.

---

## 8. Out of scope for this cleanup

Things that are **NOT** moOde cleanup, even though they're
related:

- **Volume system overhaul** (the Paradigm B/C work). Separate
  effort, separate handoff doc. Don't mix into cleanup commits.
- **Configuration web view** (location, subway, default startup
  volume). PLAN.md flagged as upcoming work. Out of scope here.
- **Standalone nginx for HTTPS** (deferred per §7).
- **Renaming `jasper.local` to something else** or moving to a
  different production Pi. Operator-side decision, not codebase
  cleanup.
- **AEC bridge re-enable on the new stack.** Currently disabled
  by default; the asoundrc on debian doesn't include the
  `jasper_capture` dsnoop block needed for AEC. When AEC bridge
  is re-enabled, that block goes back into asoundrc (matching
  what the old moOde-stack version had). That's its own follow-up.
- **Mechanical rename `make_transport_dispatcher` parameters**
  beyond the `moode → renderer` rename — if you want a deeper
  refactor of the transport tools, do it as a separate PR.

---

## 9. Final verification — how to know you're done

After all 7 commits land, run these checks. They should all
pass.

### Code-side

```sh
cd ~/Code/JTS

# 1. Zero references to moode/moOde/Moode anywhere except
# this cleanup doc and git history.
grep -rni "moode\|moOde\|Moode" \
    --exclude-dir=.git \
    --exclude-dir=logs \
    --exclude-dir=.venv \
    --exclude-dir=__pycache__ \
    --exclude="CLEANUP-moode-removal.md" \
    .
# Expected: no output

# 2. No moOde-specific files
test ! -f jasper/moode.py
test ! -f deploy/alsa/zz-jts-loopback.conf
test ! -f deploy/systemd/shairport-sync-jts-output.conf
test ! -d deploy/debian-stack

# 3. Tests pass
.venv/bin/pytest -q
# Expected: green (modulo the unrelated test_volume_persistence
# pre-existing failure)

# 4. install.sh is single-path
grep -c "BACKEND" deploy/install.sh
# Expected: 0

# 5. install.sh syntax-clean
bash -n deploy/install.sh && echo OK

# 6. Config no longer has moode fields
grep -E "moode_base_url|renderer_backend" jasper/config.py
# Expected: no output

# 7. .env.example clean
grep -i moode .env.example
# Expected: no output
```

### Docs-side

Read top to bottom:
- `README.md` — does it describe the project as a Pi 5 Debian
  Trixie speaker, with no "originally on moOde"?
- `CLAUDE.md` — no "Renderer backend" section choosing between
  moode/debian?
- `BRINGUP.md` — does it walk a fresh operator from blank SD card
  to working speaker without ever mentioning moOde?

Hand the cold repo to a fresh contributor (or fresh Claude
context) and ask "what's this project built on?" The answer
should be "Pi 5 + Debian Trixie + custom audio stack +
Gemini Live." Not "moOde."

### Production-side

After merging cleanup to main:
- Re-run `install.sh` on the production Pi (idempotent; should
  be a no-op for the most part)
- Verify all four paths still work: AirPlay 2, Spotify Connect,
  Bluetooth, voice
- Verify `jasper-doctor` is green (modulo expected fails like
  XVF mic if not in 6-channel firmware)

### Branch hygiene

After merge:
- Delete the local `cleanup/no-moode-vestiges` branch
- Delete the remote `origin/cleanup/no-moode-vestiges` branch
- Delete the now-meaningless `migrate/no-moode` branch (both
  local and remote — the migration is complete and cleanup
  is in main)

```sh
git branch -d cleanup/no-moode-vestiges
git push origin --delete cleanup/no-moode-vestiges
git branch -d migrate/no-moode
git push origin --delete migrate/no-moode
```

`main` is now the single canonical branch. The codebase no
longer has any awareness that moOde existed.

---

## 10. Estimated effort

Per the file counts and per-commit complexity:

| Commit | Effort | Notes |
|---|---|---|
| 1. Promote debian-stack/ + delete moOde-only configs | 30 min | Mostly mechanical; `git mv` + minor comment edits |
| 2. Delete moode.py; collapse renderer.py | 1 h | Code refactor; some test breakage held for #4 |
| 3. Rename `moode` parameter | 30 min | Find/replace across ~6 files |
| 4. Test cleanup | 1.5 h | Bulk rename + delete obsolete tests |
| 5. Config + env cleanup | 30 min | Small; verify nothing breaks |
| 6. install.sh + scripts | 1 h | Significant install.sh rewrite |
| 7. Documentation rewrite | 3 h | Big — BRINGUP.md is a from-scratch rewrite |
| **Total** | **~8 h** | One focused day |

Plus ~2 hours of soak-testing on the production Pi after merge
to verify nothing regressed.

---

## 11. If something goes wrong

The cleanup commits are organized so each one is independently
revertable. If commit 3 introduces a bug, `git revert <hash>`
gets you back without losing commits 1–2.

The biggest revert risk is commit 6 (install.sh). If a fresh
install fails after the rewrite, the easiest recovery is:
- `git revert HEAD` to undo the install.sh changes
- Continue using the pre-cleanup install.sh until the rewrite is
  fixed
- The production Pi already running doesn't need reinstall; it's
  not affected

For commit 7 (docs rewrite), there's no functional risk — bad
docs don't break runtime. Reviewers can flag prose issues
post-merge.

The cleanup is **not** a destructive operation in the sense of
losing data: the production Pi keeps running across the cleanup
merge, the dev branches are preserved in git history, and every
moOde file we delete is recoverable from `git show HEAD~1:<path>`
if needed. The "destructive" aspect is purely about the codebase
visual state, which is the whole point.

---

_End of cleanup runbook. Update this file in place if the
cleanup plan changes, and delete it after the cleanup ships
(or move it to `docs/historical/` if you want to keep it as a
record of the migration)._
