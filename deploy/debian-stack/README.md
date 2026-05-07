# debian-stack/ — moOde-free renderer stack

Validated end-to-end on a fresh Raspberry Pi OS Trixie Lite (64-bit) on
2026-05-06: Spotify Connect, AirPlay 2, and Bluetooth A2DP all writing
into one snd-aloop card, with CamillaDSP bridging the loopback to the
Apple USB-C dongle. Total daemon footprint ~76 MB RAM; system idle
~273 MiB used. moOde is not installed.

This directory is a checkpoint of the configs and units built during
the migration on `migrate/no-moode`. It is **not yet wired into**
`deploy/install.sh` — that's the next step. Once `install.sh` is
adapted with a `--backend=debian` flag, these files become the source
of truth for a one-command install on a blank Trixie box.

## File map

| File here | Installed to | Owned by |
|---|---|---|
| `bin/jasper-librespot-event` | `/usr/local/bin/jasper-librespot-event` | librespot --onevent hook |
| `etc/shairport-sync.conf` | `/etc/shairport-sync.conf` | shairport-sync (source-built) |
| `etc/modprobe.d/snd-aloop.conf` | `/etc/modprobe.d/snd-aloop.conf` | kernel module |
| `etc/asoundrc-jasper.template` | `/root/.asoundrc` (with `__DONGLE_CARD__` substituted) | jasper-camilla + jasper-voice |
| `etc/camilladsp/v1.yml` | `/etc/camilladsp/v1.yml` | jasper-camilla |
| `systemd/librespot.service` | `/etc/systemd/system/librespot.service` | systemd |
| `systemd/shairport-sync.service` | `/etc/systemd/system/shairport-sync.service` | systemd |
| `systemd/nqptp.service` | `/etc/systemd/system/nqptp.service` | systemd |
| `systemd/bt-agent.service` | `/etc/systemd/system/bt-agent.service` | systemd |
| `systemd/bluealsa-aplay.service.d/jts-output.conf` | `/etc/systemd/system/bluealsa-aplay.service.d/jts-output.conf` | systemd drop-in |
| `systemd/jasper-mux.service` | `/etc/systemd/system/jasper-mux.service` | systemd |
| `configure-bluez.sh` | run once during install | sudo |

## Topology

```
iPhone (Spotify) ──┐
iPhone (AirPlay 2) ─┼──► hw:Loopback,0,0  ──► hw:Loopback,1,0  ──► CamillaDSP ──► plughw:CARD=A,DEV=0
iPhone (Bluetooth) ─┘                        (master_gain mixer,                  (Apple USB-C dongle)
                                              flat filters; ducking surface)
```

All three renderers write to the same snd-aloop substream (sub 0).
The `jasper-mux` daemon (this stack's replacement for moOde's
`worker.php`) polls each renderer at 1 Hz and, when a new source
transitions to playing while another is already playing, pauses
the older one — implementing "latest source wins" UX:

- Spotify (librespot): pause via Spotify Web API (`PUT /me/player/pause`)
  using the multi-account router. librespot has no local control HTTP.
- AirPlay (shairport-sync): pause via MPRIS `Pause` over busctl
- Bluetooth (bluez-alsa): no graceful pause API on the receiver
  side — best-effort, brief audio-mixing window until the user
  pauses on their phone.

CamillaDSP captures from `plughw:Loopback,1,0` (which absorbs each
renderer's native rate/format via the plug layer) and writes to
`pcm.jasper_out` — a dmix in `/root/.asoundrc` that fans the
processed music + jasper-voice TTS into one stream before the
Apple USB-C dongle. dmix sums multiple writers sample-wise.

## What's NOT in apt

Only one component requires source-build: **shairport-sync with
AirPlay 2**. The Debian Trixie apt package (`shairport-sync 4.3.7-1`)
is built without `--with-airplay-2` (verified 2026-05-06: features
string contains no `airplay-2` marker, advertises only via
`_raop._tcp` not `_airplay._tcp`). For AP2, source-build from
`mikebrady/shairport-sync` v4.3.7 with these configure flags:

```
./configure --sysconfdir=/etc \
    --with-alsa --with-soxr --with-avahi --with-ssl=openssl \
    --with-systemd --with-airplay-2 \
    --with-metadata --with-dbus-interface --with-mpris-interface
```

Build deps beyond apt's package: `libglib2.0-dev libplist-dev
libsodium-dev libgcrypt20-dev uuid-dev libmbedtls-dev
libavutil-dev libavcodec-dev libavformat-dev libswresample-dev`.

`nqptp` is also source-only (`mikebrady/nqptp`). Both source builds
will be codified in the adapted `install.sh`.

## What's still missing

- **Voice end-to-end on jts.local** — needs the XVF3800 mic
  physically moved over from `jasper.local` (or a duplicate). The
  `jasper-voice` service is installed and enabled; it just won't
  fire wake-word detection without the mic plugged in.
- **AEC bridge on debian** — software AEC works on the moOde stack
  via a `jasper_capture` dsnoop block in /root/.asoundrc. The
  debian asoundrc only defines `jasper_out`; adding `jasper_capture`
  is a one-file edit when the user wants AEC there.
- **Standalone HTTPS for jasper-web** — the moOde install patches
  moOde's nginx site to add `/spotify` reverse-proxying. On debian
  this step is skipped (`install.sh` logs a TODO). Until that's
  built, household members can hit `https://jts.local:8765/spotify`
  directly (jasper-web's bound port + the install.sh-generated
  self-signed cert on port 443 isn't wired yet).
- **Bluetooth graceful pause** — bluez-alsa doesn't expose a
  pause API on the A2DP-sink side. `jasper-mux` logs a no-op when
  asked to preempt BT; phone-side pause is the workaround.
