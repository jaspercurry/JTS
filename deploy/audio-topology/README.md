# `deploy/audio-topology/`

Configuration variants for the JTS audio chain. The active variant
is selected by `deploy/bin/jasper-audio-topology` (installed at
`/usr/local/sbin/jasper-audio-topology`) and pinned via
`/var/lib/jasper/audio_topology.env`.

Two topologies today:

| Mode | Default? | Renderer path | DSP capture | Saves |
|---|---|---|---|---|
| `dmix` | yes | renderers → `pcm.jasper_renderer_in` → userspace dmix → `hw:Loopback,0,0` | dsnoop on `hw:Loopback,1,0` | (baseline) |
| `fanin` | no — opt-in | renderers → per-renderer substream alias → `hw:Loopback,0,0..3` | dsnoop on `hw:Loopback,1,7` (jasper-fanin's summed output) | ~85 ms latency vs dmix |

The `dmix` variant is the existing topology (`deploy/alsa/asoundrc.jasper`
is its asoundrc; it's installed at `/etc/asound.conf` by `install.sh`).
The `fanin` variant lives in this directory as overlay templates that
`jasper-audio-topology` installs on top when switching modes.

## Files

```
deploy/audio-topology/
  README.md                          ← this file
  fanin/
    asound.conf.template             ← `pcm.jasper_renderer_mix` removed,
                                       capture dsnoop shifts to substream 7
```

## Why one variant lives here, not the other

The `dmix` variant is the deployable default and is the source of
truth for `/etc/asound.conf` on every fresh install — it lives at
`deploy/alsa/asoundrc.jasper` alongside everything else `install.sh`
copies into `/etc/`. The `fanin` variant is the *alternate* topology;
keeping it under `audio-topology/` separates "default state" from
"overlay state" cleanly so a reader doesn't have to wonder which
file represents the live behavior.

When `jasper-audio-topology fanin` runs, it:
1. Backs up the current `/etc/asound.conf` (= dmix variant after
   install) to `/etc/asound.conf.dmix-mode-backup`.
2. Renders `fanin/asound.conf.template` (substituting
   `__DONGLE_CARD__`) and installs at `/etc/asound.conf`.

When `jasper-audio-topology dmix` runs, it restores the backup.

## Adding a future topology

If a third variant ever ships (e.g., a PipeWire migration, an
alternate-hardware topology), add it as another subdirectory here
with the same asoundrc-template shape and extend the topology
switch script to handle the new mode. The contract is small:
templates use `__DONGLE_CARD__` for the dongle name; everything
else is hard-coded.

See [`docs/HANDOFF-fan-in-daemon.md`](../../docs/HANDOFF-fan-in-daemon.md)
for the Tier 2A fan-in design and migration plan.
