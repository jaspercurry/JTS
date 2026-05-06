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
| `etc/go-librespot/config.yml` | `/etc/go-librespot/config.yml` | go-librespot |
| `etc/shairport-sync.conf` | `/etc/shairport-sync.conf` | shairport-sync (source-built) |
| `etc/modprobe.d/snd-aloop.conf` | `/etc/modprobe.d/snd-aloop.conf` | kernel module |
| `etc/camilladsp/v1.yml` | `/etc/camilladsp/v1.yml` | jasper-camilla |
| `systemd/go-librespot.service` | `/etc/systemd/system/go-librespot.service` | systemd |
| `systemd/shairport-sync.service` | `/etc/systemd/system/shairport-sync.service` | systemd |
| `systemd/nqptp.service` | `/etc/systemd/system/nqptp.service` | systemd |
| `systemd/bt-agent.service` | `/etc/systemd/system/bt-agent.service` | systemd |
| `systemd/bluealsa-aplay.service.d/jts-output.conf` | `/etc/systemd/system/bluealsa-aplay.service.d/jts-output.conf` | systemd drop-in |
| `configure-bluez.sh` | run once during install | sudo |

## Topology

```
iPhone (Spotify) ──┐
iPhone (AirPlay 2) ─┼──► hw:Loopback,0,0  ──► hw:Loopback,1,0  ──► CamillaDSP ──► plughw:CARD=A,DEV=0
iPhone (Bluetooth) ─┘                        (master_gain mixer,                  (Apple USB-C dongle)
                                              flat filters; ducking surface)
```

All three renderers write to the same snd-aloop substream (sub 0). They
serialize via "first writer wins" — the previous renderer's
`session_timeout` releases the device after idle and the next can grab
it. Multi-renderer concurrent playback is not yet designed; that's a
follow-up architectural decision (separate substreams vs dmix vs
session-timeout-only).

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

- jasper-voice not yet installed on the new Pi (next phase)
- `jasper/moode.py` still references moOde-specific REST and SQLite
  (refactor to `RendererBackend` ABC pending)
- Multi-renderer concurrent-playback architecture
- jasper-doctor checks for the new daemons
