# HANDOFF — AirPlay glitch troubleshooting guide

If you're hearing glitches on the AirPlay receiver, **start here**. This
document is the canonical entry point for diagnosing audio artifacts on the
shairport-sync path. It bundles:

- A symptom → likely-cause decision flow (the first 200 lines)
- Concrete diagnostic recipes (commands to run, what to look for)
- Per-pattern playbooks for every known failure mode, with confirmed fixes
- A first-principles reference (the audio chain, the four clocks at play,
  source-cited mechanism details, the diagnostic experiments we've run)
- An escalation ladder for unknown failure modes

The current outputd path is **green** as of 2026-05-28 in live Pi lab
validation — synced mode was already glitch-free on the Apple USB-C
dongle with:
- [PR #83](https://github.com/jaspercurry/JTS/pull/83)'s shairport
  `resync_threshold=0.2` fix (Pattern B);
- CamillaDSP `target_level: 2048` on the dsnoop path (Pattern A2);
- The Tier 2A fan-in topology
  ([docs/HANDOFF-fan-in-daemon.md](HANDOFF-fan-in-daemon.md))
  replacing the userspace dmix (Pattern A3);
- `JASPER_FANIN_INPUT_BUFFER_FRAMES=4096` to absorb 802.11 A-MPDU
  WiFi-burst delivery;
- Shairport's derived `audio_backend_latency_offset_in_seconds` to
  keep AirPlay video/multi-room timing honest after CamillaDSP's
  target buffer, jasper-fanin's output buffer, jasper-outputd's
  optional content bridge, and jasper-outputd's direct-DAC buffer.

With the current outputd values, the rendered offset is
`-0.149333` (CamillaDSP buffer + fan-in output buffer + outputd DAC
buffer only; no renderer-side dmix because fanin replaces it; no
content-bridge term because packaged outputd defaults to direct mode).

Mux preemption now uses shairport-sync's MPRIS `Stop` when AirPlay
loses the audible lane to Spotify, Bluetooth, or USB sink. Voice
transport "pause AirPlay" still uses MPRIS `Pause`; source arbitration
uses `Stop` so the sender session does not linger as hidden active
AirPlay while another renderer owns the fan-in gate.

If you're hearing artifacts, something has changed (active
correction profile, DAC swap, software update, network change,
hardware fault, topology flipped). This doc helps you find what.

---

## Quick triage — is it actually AirPlay?

JTS handles four music sources (AirPlay, Spotify Connect, Bluetooth A2DP,
and USB sink). A "music glitches" report could be any of them. Verify
the source first:

```sh
# On the Pi (or via SSH from laptop)
curl -s http://localhost:8780/state | jq .active_source
# Expected: "airplay" (or "spotify", "bluealsa", or "usbsink")
```

If `active_source` is `spotify` or `bluealsa`, the glitch is in that
renderer's chain — this doc may still help (the snd-aloop + CamillaDSP
mechanism applies to all three), but the diagnostic recipes below
emphasize AirPlay-specific log signatures. For Spotify Connect issues
see also [HANDOFF-voice-music-control.md](HANDOFF-voice-music-control.md).

Also check for sidecar churn before blaming AirPlay. `jasper-voice` and
`jasper-aec-bridge` are not in the AirPlay music path, but a watchdog
restart loop can perturb scheduling and make music diagnostics noisy:

```sh
systemctl is-active jasper-camilla shairport-sync jasper-voice jasper-aec-bridge
```

If voice/AEC is flapping, pause that investigation separately and get a
clean music-only window before changing shairport or CamillaDSP knobs.

---

## Pattern match: what does your glitch sound like?

Find the row that best matches what you're hearing and jump to that
section. If none fit, follow the "[unknown pattern](#unknown-pattern--data-capture-recipe)"
guide.

| Symptom (audible) | Symptom (in logs) | Likely cause | Section |
|---|---|---|---|
| Glitches every ~5–15 s, broken audio | CamillaDSP `Capture read short` floods + `Prepare playback after buffer underrun` every ~5 s | rate_adjust + AsyncSinc oscillating | [Pattern A](#pattern-a--camilladsp-rate_adjust--asyncsinc-oscillation) |
| Periodic small tears, shairport clean | CamillaDSP `Prepare playback after buffer underrun`, no `Capture read short` flood | CamillaDSP playback target too shallow for dsnoop/dmix path | [Pattern A2](#pattern-a2--camilladsp-playback-buffer-too-shallow) |
| Occasional clicks/discontinuities during steady-state playback (esp. AirPlay), no obvious pattern in shairport stats | shairport `Dropping out of date packet ... Lead time is 0.115-0.120 seconds` at ~5-15 s intervals, tight lead-time cluster, 1:1 with "Player: packets out of sequence" warnings | Dmix-induced player-thread timing slip × WiFi A-MPDU burst delivery (fixed by topology cutover to fanin) | [Pattern A3](#pattern-a3--dmix-induced-burst-head-drops-the-wifi-aggregation-interaction) |
| Glitches every ~60 s, brief tear then quiet | shairport `Large positive sync error +50ms` → `alsa underrun` → `Large negative sync error -485ms` | shairport `resync_threshold` misfire on snd-aloop fill (NOT actual DAC drift) | [Pattern B](#pattern-b--shairport-resync_threshold-misfire-the-classic) |
| Similar to B but interval varies; appeared after DAC swap | Same as B; possibly with shorter or irregular intervals | DAC crystal drift exceeded continuous-correction headroom (~2500 ppm) | [Pattern C](#pattern-c--dac-swap-drift-exceeds-continuous-correction-headroom) |
| Random / non-periodic, sometimes worse on busy Pi or at certain times | shairport log mostly clean except occasional events; possibly `Broken pipe`; WiFi RX errors increment | Network, sender, WiFi, or CPU contention | [Pattern D](#pattern-d--non-periodic-glitches-network--sender--cpu) |
| No audio at all from JTS, but speaker is "discoverable" | shairport active but never accepts session; AP2 wedge log | shairport AP2 wedge / nqptp issue | [Pattern E](#pattern-e--no-audio-airplay-cant-connect-or-wedged) |
| No audio + speaker missing from picker | Services down | Service failure — not a sync issue | [Pattern F](#pattern-f--speaker-not-discoverable) |

---

## Step 1: Confirm the audible symptom

Listen for ~60 seconds and time the glitches. Three useful data points:

- **Interval** — every 5 s? every 60 s? non-periodic?
- **Character** — brief tear/click? drop to silence for ~250 ms? continuous static?
- **Variation** — same across all music? worse during loud passages? worse when WiFi is busy?

Combine that with the pattern table above to narrow which section to read first.

## Step 2: Capture the data

Two paths — fast log scan (works for known patterns) and full polling
diagnostic (when you need to confirm the mechanism).

### Fast log scan (10 s)

```sh
# Last 5 minutes of shairport-sync events
sudo journalctl -u shairport-sync --since "5 minutes ago" -o short-iso \
  | grep -E "Large positive|Large negative|underrun|Broken pipe|recovering"

# Last 5 minutes of CamillaDSP events
sudo journalctl -u jasper-camilla --since "5 minutes ago" -o cat \
  | grep -E "Capture read|underrun|warn|error"

# Quick counts
echo "shairport sync errors: $(sudo journalctl -u shairport-sync --since '5 minutes ago' -o cat | grep -c 'Large positive')"
echo "camilla short reads:   $(sudo journalctl -u jasper-camilla --since '5 minutes ago' -o cat | grep -c 'Capture read')"
echo "camilla underruns:     $(sudo journalctl -u jasper-camilla --since '5 minutes ago' -o cat | grep -c 'Prepare playback after buffer underrun')"

# Active Camilla config + live playback-buffer snapshot
sudo cat /var/lib/camilladsp/outputd-statefile.yml
ACTIVE=$(sudo awk '/config_path:/ {print $2}' /var/lib/camilladsp/outputd-statefile.yml)
sudo grep -nE 'target_level|enable_rate_adjust|resampler|device:' "$ACTIVE"
/opt/jasper/.venv/bin/python -c 'from camilladsp import CamillaClient; c=CamillaClient("127.0.0.1",1234); c.connect(); print("buffer", c.query("GetBufferLevel"), "rate_adjust", c.query("GetRateAdjust"), "capture_rate", c.query("GetCaptureRate")); c.disconnect()'
```

**Expected counts in a healthy state** (5 min sample, music playing):

```
shairport sync errors:  0
camilla short reads:    0
camilla underruns:      0
```

Anything non-zero needs diagnosis.

Decision rule:
- shairport `Dropping out of date packet` events with tightly clustered lead times (~0.115-0.120 s) AND `Player: packets out of sequence` warnings at the same 1:1 cadence → **Pattern A3** first. Verify the deployed fan-in wiring: `/etc/asound.conf` should have `pcm.jasper_capture` on `hw:Loopback,1,7`, `shairport_substream` in `/etc/shairport-sync.conf`, and `jasper-fanin.service` active.
- shairport `Large positive`/`Large negative` events non-zero → Pattern B/C/D first.
- Camilla short reads non-zero → Pattern A first.
- Camilla underruns non-zero while shairport and short reads are zero → Pattern A2 first.
- fanin xrun events (`event=fanin.xrun source=input label=airplay`) — Pattern A3's companion failure mode. Confirm `JASPER_FANIN_INPUT_BUFFER_FRAMES=4096` is in effect (`sudo journalctl -u jasper-fanin --no-pager | grep 'input.opened' | head -1` should show `buffer_frames=4096`).
- Active config under `/var/lib/camilladsp/configs/` → inspect it; room-correction profiles can persist stale settings even when `/etc/camilladsp/outputd-cutover.yml` is clean.

To distinguish Pattern A3 from generic late-packet events (e.g. transient WiFi), look at the lead-time distribution:

```sh
sudo journalctl -u shairport-sync --since '10 minutes ago' -o cat \
  | grep "Dropping out of date packet" \
  | awk -F'Lead time is ' '{print $2}' | awk '{print $1}' \
  | sort | uniq -c | sort -rn | head -10
```

A tight cluster (most events within ~3 ms of each other, around 0.118 s) — combined with 1:1 "Player: packets out of sequence" warnings — is Pattern A3 (dmix-induced burst-head drops). A wide spread (50-500 ms varied) is network/sender (Pattern D).

### System dashboard readout

`/system/` has an AirPlay card backed by
[`jasper/control/airplay_health.py`](../jasper/control/airplay_health.py).
It is a recent-health view, not a full diagnostics runner:

- Fan-in `STATUS` is sampled every 5 s over UDS with a short timeout.
  The card uses `airplay.frames_read` deltas for "currently receiving
  frames", `airplay.xrun_count` deltas for AirPlay input recovery
  events, output `xrun_count` deltas for downstream pressure, the
  configured fan-in buffers, and watchdog progress age.
- The same card includes the outputd final-output snapshot from
  `/run/jasper-outputd/control.sock` plus `jasper-outputd.service`
  cgroup memory from the system sampler, so outputd RAM drift is visible
  next to the AirPlay/output chain it affects. Outputd xrun counters
  are labelled content/DAC and include last-xrun age plus
  uptime-normalized rate when non-zero.
- Shairport and CamillaDSP journals are scanned incrementally every
  30 s and classified into the same patterns this document uses:
  packet drops / packet order, large sync corrections, shairport ALSA
  underruns, material Camilla capture short reads, and Camilla playback
  underruns. The dashboard ignores tiny recovered Camilla partial reads
  (for example 1016-1023 frames returned for a 1024-frame request):
  CamillaDSP immediately loops to fill the rest of the chunk, and these
  sub-1% partials appear on the healthy plug/dsnoop/rate-adjust path
  without shairport, fan-in, outputd, or playback underruns. Use the
  fast log scan above when you need raw Camilla journal counts.
- MPRIS and CamillaDSP live probes run at the slower 30 s cadence.
  Camilla context includes buffer level, rate adjust, active config
  basename, and the active config's target/chunk values when the YAML is
  readable. These are useful context, not the hot-path truth source.
- History is in-memory only: 10 s buckets for 30 min plus a small
  recent-event ring. There is no database and no dashboard-poll-time
  journal scan. The socket-activated `jasper-system-web` process only
  formats the already-digested `/system/snapshot` payload.
- The canonical deploy wrapper writes a bounded maintenance marker at
  `/run/jasper-airplay-health-suppress-until`. While it is active, the
  sampler still reads current fan-in / MPRIS / Camilla state, but it
  does not count fan-in xrun deltas or shairport/Camilla journal
  recovery lines into the recent AirPlay-health buckets. This keeps
  deploy-induced restarts from polluting reliability data without
  hiding the post-deploy live state.

Status meanings:

| Status | Meaning |
|---|---|
| `ok` | AirPlay is actively streaming (shairport reports playing), fan-in is receiving frames, and the 5 m/30 m windows have no AirPlay-path recovery events. |
| `inactive` | shairport reports nothing streaming (MPRIS `PlaybackStatus` not playing). The airplay input lane free-runs ~48 kHz of *silence* whenever the pipeline is up — even with no sender connected — so idle is decided by `PlaybackStatus`, **not** the frame rate. Idle-pipeline artifacts (benign Camilla short reads, content EAGAIN) do not raise a warning here. |
| `watch` | While AirPlay is actively streaming, non-fatal evidence appeared — usually material Camilla short reads or older 30 m shairport/fan-in events. Treat it as "keep listening and correlate," not "change config immediately." Idle short reads stay `inactive`; they do not escalate. |
| `issue` | Recent recovery event in the last 5 m, fan-in input buffer below 4096, stale fan-in watchdog, shairport sync/drop/underrun event, fan-in xrun, Camilla playback underrun, or shairport reports playing while fan-in is not receiving frames. |
| `unknown` | The sampler cannot read fan-in state, `/system/snapshot` caught an AirPlay-health sampler failure, shairport `PlaybackStatus` is unavailable, or the sampler is still waiting for its first fan-in frame-rate baseline after startup. If it persists beyond one sample interval, check `jasper-fanin.service` and the control socket before interpreting higher-level AirPlay symptoms. |

Use the card for "is anything happening right now / recently?" If it
shows `watch` or `issue`, use the fast scan above or the full polling
diagnostic below to prove the mechanism before changing shairport,
CamillaDSP, WiFi, or buffer settings.

### Full polling diagnostic (5-min run, ~5 min wall time)

Use this when log signatures are inconclusive or you want to characterize
Pattern B/C behavior empirically. Polls the loopback ring fill at 2 Hz
alongside shairport's journal. It does **not** measure CamillaDSP's
playback buffer; for Pattern A2 use `GetBufferLevel` via the CamillaDSP
websocket command in the fast scan above.

```sh
ssh pi@jts.local '
LOG=/tmp/loopback_fill.log
JOURNAL_LOG=/tmp/shairport_journal.log
> $LOG; > $JOURNAL_LOG

# Start shairport journal capture in background
sudo journalctl -fu shairport-sync --since "now" -o short-iso > $JOURNAL_LOG 2>&1 &
JPID=$!

# Poll the active loopback substream every 0.5s for 600 samples
SUB=/proc/asound/Loopback/pcm0p/sub0/status
for i in $(seq 1 600); do
  T=$(date +%s.%N)
  HW=""; AP=""
  while IFS= read -r line; do
    case "$line" in
      "hw_ptr"*)   HW=$(echo "$line" | awk "{print \$3}") ;;
      "appl_ptr"*) AP=$(echo "$line" | awk "{print \$3}") ;;
    esac
  done < $SUB
  if [ -n "$HW" ] && [ -n "$AP" ]; then
    echo "$T $((AP - HW))" >> $LOG
  fi
  sleep 0.5
done
sudo kill $JPID 2>/dev/null || true
echo "samples: $(wc -l < $LOG)"

# Stats
awk "{ f=\$2; if (NR==1) {min=f;max=f;sum=f;n=1; next}
  sum+=f; n++; if (f<min) min=f; if (f>max) max=f }
  END { print \"fill min:\", min, \"frames (\" min/48 \"ms)\"
        print \"fill max:\", max, \"frames (\" max/48 \"ms)\"
        print \"fill mean:\", int(sum/n), \"frames\"
        print \"p2p swing:\", max-min, \"frames\" }" $LOG

# Event counts
echo "Large positive: $(grep -c \"Large positive\" $JOURNAL_LOG)"
echo "Large negative: $(grep -c \"Large negative\" $JOURNAL_LOG)"
echo "alsa underrun:  $(grep -c \"recovering from a previous underrun\" $JOURNAL_LOG)"
'
```

**Healthy synced-mode result** (current production with PR #83):

```
fill min:   ~23,360 frames (~487 ms)
fill max:   ~30,720 frames (~640 ms)
fill mean:  ~28,278 frames (~589 ms)
p2p swing:  ~7,168 frames (~149 ms)
Large positive: 0
Large negative: 0
alsa underrun:  0
```

**Pattern-B "broken" signature** (pre-PR #83):

```
fill min:   0 frames (0 ms)
fill max:   25,600 frames (533 ms)
fill mean:  ~23,500 frames (~491 ms)
p2p swing:  25,600 frames (533 ms)   ← buffer crashes to zero
Large positive: 5 per 5 min          ← every ~63 s
Large negative: 10 per 5 min
alsa underrun:  10 per 5 min
```

The smoking-gun signature: **fill peaks at ~533 ms, then crashes to 0 ms
within ~0.5 s of each "Large positive" event**, then refills. That's
shairport's `do_flush` emptying the buffer.

---

## Pattern A — CamillaDSP `rate_adjust` + AsyncSinc oscillation

**Status: fixed in [PR #75](https://github.com/jaspercurry/JTS/pull/75).**

### Symptoms
- Glitches every ~5–15 s, audio sounds broken / warbly
- CamillaDSP log: chronic `Capture read short` warnings (e.g. "Capture
  read 977 frames instead of the requested 1024") on essentially every
  chunk
- CamillaDSP log: `Prepare playback after buffer underrun` every ~5 s
- shairport log: also has alsa underruns and sync errors but typically
  at higher rate than the resync_threshold cadence

### Cause
CamillaDSP was configured with **both** `enable_rate_adjust: true` AND
an `AsyncSinc Balanced` resampler. Per [HEnquist/camilladsp#207](https://github.com/HEnquist/camilladsp/issues/207),
that's two drift controllers fighting each other on a snd-aloop capture.
They oscillate; AsyncSinc smears every rate_adjust correction across
its sinc kernel, producing chronic short reads and periodic underruns.

CamillaDSP's own startup warning flags this:
```
WARN: Needless 1:1 sample rate conversion active.
Not needed since capture device supports rate adjust
```

### Fix
In every music-path CamillaDSP config, including generated correction
profiles: **pick exactly one**.

Our shipped config (when capture rate == playback rate, both 48 kHz):

```yaml
devices:
  enable_rate_adjust: true
  # no resampler block at all
```

The alternative (also valid):
```yaml
devices:
  enable_rate_adjust: false
  resampler:
    type: AsyncSinc
    profile: Balanced
```

Never both. If you see the `Needless 1:1 sample rate conversion` warning
in `journalctl -u jasper-camilla`, the config is wrong.

Important gotcha: room-correction profiles are generated under
`/var/lib/camilladsp/configs/` and can be the active config even when
`/etc/camilladsp/outputd-cutover.yml` is clean. Check the statefile first:

```sh
sudo cat /var/lib/camilladsp/outputd-statefile.yml
sudo grep -nE 'enable_rate_adjust|resampler|AsyncSinc' \
  "$(sudo awk '/config_path:/ {print $2}' /var/lib/camilladsp/outputd-statefile.yml)"
```

If a correction profile contains both `enable_rate_adjust: true` and
`AsyncSinc`, reset to `/etc/camilladsp/outputd-cutover.yml` or
regenerate after the generator fix in `jasper/sound/camilla_yaml.py`.

### Verify the fix
After restarting `jasper-camilla`, the 5-min log scan should show **zero**
`Capture read short` warnings.

---

## Pattern A2 — CamillaDSP playback buffer too shallow

**Status: fixed in the 2026-05-14 target-level + latency-offset update.**

### Symptom
Small periodic tears while AirPlay is otherwise stable. The important
distinction from Pattern B: shairport-sync is quiet. The sender is not
being resynced; CamillaDSP is running out of playback-side buffer.

### Log signature

```text
shairport-sync: no Large positive / Large negative / underrun entries
jasper-camilla: PB: Prepare playback after buffer underrun
jasper-camilla: no Capture read short flood
```

Live CamillaDSP websocket snapshot in the broken state typically shows
the playback buffer below the default 1024-sample target:

```text
buffer 647 rate_adjust 1.0003161 capture_rate 47935
buffer 761 rate_adjust 1.0003043 capture_rate 47936
```

Healthy steady state after the fix sits around the new 4096-sample
target:

```text
buffer 4046 rate_adjust 1.0004227
buffer 4472 rate_adjust 1.0004344
```

### Root cause
CamillaDSP's default `target_level` is `chunksize`. In this topology,
that means a 1024-sample playback target (~21 ms at 48 kHz), which can be
too shallow once capture goes through `plug:jasper_capture` (dsnoop) and
playback goes through the outputd post-DSP loopback. CamillaDSP's own
docs say too small a `target_level` can produce occasional buffer
underruns, and usable values can range up to `(2 + queuelimit) *
chunksize` when latency is less important than underrun margin.

First-principles split:
- `enable_rate_adjust` handles **long-term clock drift** by nudging the
  effective capture rate.
- `target_level` handles **short-term underrun margin** by deciding how
  much playback-buffer audio should be present when the next processed
  chunk arrives.

The 2026-05-14 investigation isolated this from the shairport layer:
- `plug:jasper_capture` + default target: shairport events 0, Camilla
  playback underruns every few dozen seconds.
- Direct `plughw:Loopback,1,0` test: shairport events 0, Camilla events 0
  for the watch window.
- `plug:jasper_capture` + `target_level: 4096`: one initial fill underrun
  immediately after restart, then steady-state shairport events 0 and
  Camilla events 0.

Interpretation: dsnoop itself is still the right architecture because it
lets CamillaDSP and the optional AEC bridge share the music reference.
The missing piece was playback-buffer margin, not removing dsnoop.

Latency implication: any `target_level > chunksize` adds a fixed
downstream delay of `(target_level - chunksize)` samples. At CamillaDSP's
48 kHz runtime rate the original 2026-05-14 setting (`target_level: 4096`)
cost 64 ms; the 2026-05-25 trim to `target_level: 2048` brings that down
to 21 ms. Either is large enough to matter for video sync but exactly the
kind of fixed backend delay shairport-sync can compensate.

### Fix
Set `target_level: 2048` in every music-path CamillaDSP config, including
generated room-correction profiles. With `chunksize: 1024` and
`queuelimit: 4`, this is two chunks (~43 ms) — `2 × chunksize` is the
documented floor for stable operation (jitter absorption convention).
The original 2026-05-14 fix shipped at `target_level: 4096` (~85 ms,
generous margin); the 2026-05-25 trim halved it as an independent
latency optimization with a 24-hour zero-xrun soak gating the change.
Revert to 4096 if `event=camilla.playback_underrun` lines appear at any
rate above zero.

Pair that buffer change with the rendered shairport latency offset:

```libconfig
general = {
    audio_backend_latency_offset_in_seconds = -0.149333;
};
```

This does **not** shrink CamillaDSP's underrun-protection buffer. It tells
shairport to feed the hidden downstream DSP path early so that the sound
leaving the speaker lands at the AirPlay-scheduled time.

Do not hard-code this value by hand in the template. The template contains
`__AUDIO_BACKEND_LATENCY_OFFSET_SECONDS__`; `jasper-apply-airplay-mode`
derives it as
`-((target_level - chunksize + fanin_output_buffer + outputd_content_bridge + outputd_dac_buffer) / samplerate)`,
where `target_level` and `chunksize` come from the active CamillaDSP
config, `fanin_output_buffer` comes from `JASPER_FANIN_OUTPUT_BUFFER_FRAMES`,
`outputd_content_bridge` is `0` in outputd's packaged `direct` mode
or the configured target fill in opt-in `rate_match` lab mode, and
the outputd DAC buffer comes from `JASPER_OUTPUTD_DAC_BUFFER_FRAMES`.
If any of those change in their owning env files, the next shairport
render/restart follows automatically. The old output dmix term was
added 2026-05-25; the 2026-05-28 outputd topology replaces it with
outputd's direct-DAC queue.

Required places:
- `deploy/camilladsp/outputd-cutover.yml`
- `jasper/sound/camilla_yaml.py` generated configs
- Any already-active correction profile under `/var/lib/camilladsp/configs/`
- `deploy/shairport-sync.conf.template` +
  `deploy/bin/jasper-apply-airplay-mode` for the derived fixed latency
  offset

Do **not** ship direct `plughw:Loopback,1,0` as the permanent fix. It is
a useful isolation test, but it prevents the AEC bridge from opening the
`pcm.jasper_capture` dsnoop slave later.

### Verify the fix
After the initial restart/fill, a 2-5 minute scan should show zero
`Prepare playback after buffer underrun` entries and zero shairport
sync/discrete-correction entries.

```sh
START=$(date '+%Y-%m-%d %H:%M:%S')
sleep 120
SH=$(sudo journalctl -u shairport-sync --since "$START" --no-pager | grep -Ec 'Large positive|Large negative|underrun|Broken pipe|Too much|resync')
CM=$(sudo journalctl -u jasper-camilla --since "$START" --no-pager | grep -Ec 'Capture read [0-9]+ frames|PB: Prepare playback after buffer underrun|Broken pipe|Could not write')
echo "shairport_events=$SH"
echo "camilla_events=$CM"
/opt/jasper/.venv/bin/python -c 'from camilladsp import CamillaClient; c=CamillaClient("127.0.0.1",1234); c.connect(); print("buffer", c.query("GetBufferLevel"), "rate_adjust", c.query("GetRateAdjust")); c.disconnect()'
grep -n 'audio_backend_latency_offset_in_seconds' /etc/shairport-sync.conf
```

### Isolation recipe
Use this only to prove whether the dsnoop wrapper is part of the symptom;
restore `plug:jasper_capture` afterward.

```sh
sudo cp /etc/camilladsp/outputd-cutover.yml /var/lib/camilladsp/configs/test_direct_plughw.yml
sudo sed -i 's|device: "plug:jasper_capture"|device: "plughw:Loopback,1,7"|' \
  /var/lib/camilladsp/configs/test_direct_plughw.yml
sudo sed -i 's|^config_path:.*|config_path: /var/lib/camilladsp/configs/test_direct_plughw.yml|' \
  /var/lib/camilladsp/outputd-statefile.yml
sudo systemctl restart jasper-camilla
```

If direct capture is clean but `plug:jasper_capture` underruns, increase
`target_level` on the dsnoop config before considering topology changes.

---

## Pattern A3 — dmix-induced burst-head drops (the WiFi-aggregation interaction)

**Status: fixed by switching the audio topology from dmix to fanin
(2026-05-26). The latency-offset compensation work that landed first
(PRs for renderer-dmix folding then output-dmix folding) is correct
for video / multi-room sync but did NOT eliminate this specific drop
pattern — that required the topology change. See "The fanin verdict"
below.**

### Symptoms
- Occasional clicks / brief discontinuities during steady-state AirPlay
  playback. Roughly 5–7 events per minute. Both Mac Studio and iPhone
  senders produced this — it's NOT sender-specific.
- shairport log:
  ```
  player.c:1130 Dropping out of date packet ###### with timestamp ##########.
                Lead time is 0.115-0.120 seconds.
  ```
- Lead-time distribution **tightly clustered** within ~3 ms across
  100+ events, sitting right at `desired_lead_time=0.120 s` minus a
  few ms.
- **1:1 correlation** with shairport's "Player: packets out of sequence"
  warnings — the OOS warning is a CONSEQUENCE of the drop (when the
  player removes packet N from the buffer, the next ring-read finds
  packet N+1 where it expected N). The thing actually causing both is
  the late-arrival timing math, not network reordering.
- shairport otherwise clean — **no** `Large positive`/`Large negative`
  events, **no** mid-stream `recovering from a previous underrun`.
- CamillaDSP playback chain healthy — buffer near `target_level`, no
  `PB: Prepare playback after buffer underrun` events.

### Cause (corrected 2026-05-26)

For 24 hours we thought this was a "downstream buffers invisible to
`snd_pcm_delay()`" story — that the latency offset
(`audio_backend_latency_offset_in_seconds`) needed to account for the
two dmix buffers (renderer-side `pcm.jasper_renderer_mix` + output-side
`pcm.jasper_out`) the shairport thread couldn't see. We shipped fixes
for both (rendered offset went from `-0.064` → `-0.107` → `-0.192`).
The latency-offset story is REAL and the fixes are CORRECT for what
that offset actually does in shairport's code — it propagates through
the PTP anchor at `rtsp.c:2003` / `rtp.c:1837` and influences the
sync-error math that shairport uses for video AirPlay timing and
multi-room sync. Keep those fixes.

But — and this took on-Pi measurement and a tcpdump to figure out —
**those fixes did not reduce the drop rate.** Across offset values
`-0.064` → `-0.107` → `-0.192`, the drop rate stayed at ~5-7/min
with identical lead-time clustering. The drops are NOT triggered by
"shairport doesn't know how much pipeline latency there is."

The actual mechanism is a layered interaction between WiFi delivery
timing and dmix's internal mutex/context-switch:

1. **802.11 A-MPDU aggregation** delivers AirPlay 2 RTP packets in
   **bursts of ~4 packets every ~30 ms** instead of the nominal
   1 packet every ~8 ms. Measured directly via tcpdump on
   2026-05-26: 2445 packets arriving with 0 ms inter-packet gap,
   then a cluster of ~30-35 ms gaps. Max observed gap ~40 ms.
2. **shairport faithfully writes those bursts** into its dmix-fronted
   ALSA output (`pcm.jasper_renderer_in` → `pcm.jasper_renderer_mix`
   dmix → `hw:Loopback,0,0`).
3. **The dmix layer's per-write mutex / context-switch** introduces a
   small amount of timing perturbation in shairport's player thread.
   Estimated ~5 ms slip in when the player thread computes
   `should_be_frame` for the head packet of each burst.
4. That ~5 ms slip is **exactly enough** to push the head packet of
   each WiFi burst past `desired_lead_time=0.120 s`. The drop check
   at `player.c:1130` fires (`frame_difference < 0` where
   `frame_difference = packet.timestamp - should_be_frame`), and the
   packet gets dropped with `Lead time = 0.120 - 5 ms ≈ 0.115 s`.
5. Sometimes the timing aligns badly enough that 2-4 packets in the
   same burst all get dropped (the "do_flush" cluster).

The deterministic ~0.115 s lead-time signature isn't because of any
specific dmix-buffer size — it's because `desired_lead_time` is the
hardcoded 0.120 s constant in shairport's source, and the drops cluster
just below it.

This is structurally NOT the same class of bug as Pattern B, despite
the surface similarity (both are clock-mismatch drops). Pattern B's
trigger was real crystal drift between the host and the Apple dongle
crossing `resync_threshold`. Pattern A3's trigger is shairport's
player thread running on a 5-ms slip caused by the dmix's write-side
contention.

### What ruled out the obvious alternatives

We worked through each candidate root cause individually before
arriving at the dmix-perturbation explanation:

- **Network packet loss / WiFi link issues** — `ip -s link show wlan0`
  showed clean RX counters, gateway ping was 3.3 ms avg / 7.8 ms max
  with 0% loss, nqptp anchor adjustments were all sub-frame (p99 < 1
  frame). Network is fine.
- **macOS-vs-iOS sender differences** ([shairport-sync#1942](https://github.com/mikebrady/shairport-sync/issues/1942))
   — Mac Studio at 7.2 drops/min, iPhone at 5.3 drops/min over
  comparable windows. Same lead-time signature on both. Sender is
  NOT the differentiator.
- **CPU pressure from triple-stream wake config** — toggling DTLN off
  halved `jasper-voice` CPU (43% → 27%) and load average dropped
  significantly. Drop rate stayed at ~5/min. CPU pressure is NOT the
  amplifier.
- **shairport configuration** — verified `audio backend latency
  offset is -0.192000 seconds` in shairport's startup banner;
  verified `resync_threshold=0.2` is in effect (Pattern B's fix).
  shairport config is correct.

### The fanin verdict (2026-05-26)

The fan-in daemon (`jasper-fanin`, shipped in PR #308 on 2026-05-25
and promoted by PR #329 on 2026-05-26) replaces the userspace dmix with per-renderer
snd-aloop substreams + a small Rust mixer. No shared dmix mutex, no
shared write-timing perturbation across renderers. The A/B from the
cutover:

| Window | Duration | Drops | OOS | Rate |
|---|---|---|---|---|
| dmix baseline | 10 min | 55 | 49 | **5.5/min** |
| fanin | 5 min | **0** | **0** | **0/min** |

Identical Mac Studio sender, identical WiFi link, identical music.
The only changed variable was the renderer-side topology. **Replacing
dmix → eliminating drops was the structural fix.**

One follow-up was required: the fanin daemon's default per-input
ALSA buffer of 1024 frames (~21 ms) was less than the WiFi
inter-burst gap, so the input ring overran every ~30-60 s with
EPIPE → 5.3 ms of injected silence per recovery → a different
audible click. **Bumping `JASPER_FANIN_INPUT_BUFFER_FRAMES=4096`**
(matching the WiFi-burst absorption the old dmix had at
`buffer_size 4096`) eliminated those. The output buffer stays at
`JASPER_FANIN_OUTPUT_BUFFER_FRAMES=3072` so CamillaDSP can consistently
read full 1024-frame chunks without turning the output side into the
large 4096-frame WiFi burst absorber. Both fixes — topology cutover +
input buffer bump — shipped together. See
[docs/HANDOFF-fan-in-daemon.md](HANDOFF-fan-in-daemon.md)
"Configuration → Buffer sizing" for the full mechanism + reasoning.

### Fix

Two changes shipped together on 2026-05-26:

1. **Audio topology flipped from dmix → fanin** in `deploy/install.sh`.
   Follow-up cleanup retired the dmix/fanin switcher entirely:
   install now writes the fan-in asoundrc directly, enables
   `jasper-fanin.service`, removes stale `audio_topology.env`, and
   removes any installed `jasper-audio-topology` command.
2. **Fanin input buffer bumped to 4096 frames** via
   `Environment="JASPER_FANIN_INPUT_BUFFER_FRAMES=4096"` in
   `deploy/systemd/jasper-fanin.service`. The output buffer remains
   bounded at `3072` frames. This matches the input-side absorption
   capacity dmix accidentally provided without carrying the large queue
   downstream.

The latency-offset compensation work now accounts for CamillaDSP's
target buffer, the fan-in output buffer, and outputd's DAC buffer in
`derive_audio_backend_latency_offset()`. The retired renderer-dmix
term is gone, so current main renders `-0.149333`.

### Verify the fix

```sh
# 1. Confirm fan-in service is active
systemctl is-active jasper-fanin.service

# 2. Confirm fanin buffer is 4096 in startup log
sudo journalctl -u jasper-fanin --no-pager | grep "fanin.input.opened" | head -1
#   ... period_frames=256 buffer_frames=4096

# 3. 5-min log scan during AirPlay playback should show zero drops + zero xruns
sleep 300
sudo journalctl -u shairport-sync --since '5 minutes ago' | grep -c "Dropping out of date packet"
#   Expected: 0
sudo journalctl -u jasper-fanin --since '5 minutes ago' | grep "label=airplay" | grep -c "fanin.xrun"
#   Expected: 0

# 4. Rendered shairport latency offset (correct for video sync — different concern from drops)
grep audio_backend_latency_offset /etc/shairport-sync.conf
#   Expected on fanin/outputd direct mode: -0.149333
#   Expected on fanin/outputd rate_match with 4096-frame bridge target: -0.234667
#   Expected on dmix (legacy): -0.192000
```

### What does NOT fix this
- Disabling synchronization (`disable_synchronization=yes`) — masks
  the symptom but loses A/V sync for video AirPlay and inter-speaker
  sync for multi-room.
- Increasing `audio_backend_buffer_desired_length_in_seconds` further
  — absorbs jitter, not the fixed-size scheduling bias.
- More-negative latency offset (e.g. `-0.300`) — provably doesn't
  shift the drop rate. The offset feeds into `should_be_frame` via
  the PTP anchor, which moves the drop threshold's reference frame
  but doesn't change how often shairport's player thread slips by
  ~5 ms.
- Switching senders (iPhone vs Mac Studio) — both produce the same
  drop rate on dmix.
- Reducing CPU load (DTLN off, etc.) — doesn't help. The dmix mutex
  contention isn't from competing-with-other-processes; it's from
  the dmix's own write-path scheduling.
- Shrinking the dmix `buffer_size` alone — helps proportionally (this
  is Tier 1B in the audio architecture plan) but doesn't address the
  model mismatch on its own.

### Notes for Tier 1B / 2A follow-on work
- Superseded on 2026-05-28 by the outputd mainline topology:
  `pcm.jasper_out` is no longer the active final-output convergence
  point. CamillaDSP and TTS now converge inside jasper-outputd, and the
  offset derivation compensates outputd's DAC buffer instead.

---

## Pattern B — shairport `resync_threshold` misfire (the classic)

**Status: fixed in [PR #83](https://github.com/jaspercurry/JTS/pull/83).**

### Symptoms
- Glitches every ~60 s (60–66 s window), very regular
- Audible brief tear (~half-second silence/discontinuity) then quiet for
  the next ~60 s
- shairport log signature, every ~63 s:
  ```
  player.c:2883 Large positive (i.e. late) sync error of 2210 frames (0.050294 seconds)
  audio_alsa.c:1823 alsa: recovering from a previous underrun.
  audio_alsa.c:1823 alsa: recovering from a previous underrun.
  player.c:2908 Large negative (i.e. early) sync error of -21000 frames (-0.480 seconds)
  player.c:2908 Large negative (i.e. early) sync error of -10000 frames (-0.230 seconds)
  ```
- CamillaDSP log is clean (no underruns, no short reads)

### Cause
shairport's drift correction relies on `snd_pcm_delay()` returning the
"frames remaining in the DAC's hardware FIFO." On our chain, shairport
writes to its private fan-in lane, so what comes back is the **snd-aloop
ring buffer fill** (writes minus fan-in reads) — a function of the
fan-in/CamillaDSP drain path, not the dongle's actual latency. As the
buffer slowly fills from
real crystal drift between the host (48 kHz nominal) and the dongle
(~667 ppm slow), shairport reads the rising fill as DAC drift, crosses
the 50 ms `resync_threshold`, and triggers the **discrete correction
path**: drops ~6,600 source frames + injects up to 250 ms of zeros.
That's the audible tear.

Mike Brady identified this exact misreporting in [shairport-sync#1980](https://github.com/mikebrady/shairport-sync/issues/1980)
and left it unresolved.

The "+50 ms / -485 ms" pattern is one event in two stages: the +50 ms
is the trigger value; the -485 ms is shairport reading the now-empty
buffer after its own `do_flush` and concluding "I'm way ahead."

### Fix
In `deploy/shairport-sync.conf.template`:

```libconfig
general = {
    drift_tolerance_in_seconds = 0.1;
    resync_threshold_in_seconds = 0.2;   // THE knob
    audio_backend_buffer_desired_length_in_seconds = 0.5;
    ...
};
```

Raising `resync_threshold_in_seconds` to 0.2 above the peak fill swing
(~50 ms above setpoint, max ~150 ms) keeps shairport in the **continuous
±1-sample stuffing path** instead of the discrete one. The continuous
path can absorb up to ~2500 ppm of drift (the dongle is ~667 ppm — 4×
margin).

### Verify the fix
```sh
sudo journalctl -u shairport-sync --since "10 seconds ago" -o cat \
  | grep "resync time"
# Expected: "resync time is 0.200000 seconds."
```

If it says `0.050000`, the config didn't load — check the template was
rendered (it goes through `/usr/local/sbin/jasper-apply-airplay-mode`
ExecStartPre).

5-min log scan: zero events.

### What does NOT fix this
- `drift_tolerance_in_seconds` (any value). It gates a different code
  path; see [the deep dive](#why-drift_tolerance-is-the-wrong-knob).
- `audio_backend_buffer_desired_length_in_seconds` increases (already
  at 0.5; raising further just delays the trigger by seconds).
- `disable_synchronization=yes` (the old workaround). It eliminates the
  trigger by disabling sync entirely — costs A/V sync for video AirPlay
  and multi-room sync. Not a fix; an avoidance.
- Adding `timer_source="hw:A,0"` to snd-aloop. Ruled out by direct
  control test (PR #83 commit message); was an early hypothesis that
  didn't pan out.

---

## Pattern C — DAC swap: drift exceeds continuous-correction headroom

**Status: hypothetical (no observed instance), prepared in case it happens.**

### Symptoms
- Glitches reappear after swapping to a different USB DAC
- Otherwise looks like [Pattern B](#pattern-b--shairport-resync_threshold-misfire-the-classic):
  ~60 s interval, same log signature
- May have shorter intervals (every 30 s, every 20 s) on very poor DACs

### Cause
The fix from PR #83 raised `resync_threshold` to 0.2, giving shairport's
continuous ±1-sample stuffing path headroom up to **~2500 ppm of crystal
drift** before the discrete (audible) path fires. The Apple dongle is
~667 ppm — within the safety margin. A cheaper or older USB DAC with
crystal drift >~2500 ppm would exceed this margin and reintroduce
audible events.

### Diagnostic — measure the drift rate
Run the [full polling diagnostic](#full-polling-diagnostic-5-min-run-5-min-wall-time)
and look at fill mean over time. If you see fill ramping up linearly
across the 5-min window (e.g., mean drifts from 500 → 600 → 700 ms),
that's the drift rate. Convert to ppm:

```
ppm = (delta_fill_ms / sample_window_seconds) * 1000 / 1ms × ratio
ppm ≈ ms_drift_per_second × 1000
```

A 1 ms/s drift rate is 1000 ppm. Compare to the 2500-ppm continuous-path
ceiling.

### Fix options (ladder)
1. **Raise `resync_threshold_in_seconds` further** — try 0.3, 0.4, 0.5.
   The fix is symmetric: as long as threshold > peak fill swing, the
   discrete path never fires. Test with [full polling diagnostic](#full-polling-diagnostic-5-min-run-5-min-wall-time).
2. **Raise `audio_backend_buffer_desired_length_in_seconds`** — gives
   more buffer room. Costs more startup latency (~2 s instead of 0.5 s).
3. **If still not enough**, escalate to one of the [untried options](#escalation--untried-options-bcd)
   (shairport → stdin → CamillaDSP pipe, PipeWire, or fork shairport).

### Diagnostic warning: dongle/DAC card name
Older drafts of the fix included `timer_source="hw:A,0"` in
`/etc/modprobe.d/snd-aloop.conf`. We removed it — but if it gets
reintroduced, note that `"A"` is the Apple dongle's specific card ID.
On a different DAC, snd-aloop would fall back to jiffies. Currently the
modprobe.d config has no `timer_source`, which is correct (DAC-agnostic).

---

## Pattern D — Non-periodic glitches: network / sender / CPU

### Symptoms
- Glitches don't follow a clean periodic cadence
- Sometimes worse when laptop is busy, WiFi crowded, or sender is mid-task
- Mix of brief tears and occasional `Broken pipe` errors

### Likely causes (in rough order of probability)
1. **WiFi power-save sneaking back on** — Pi 5's brcmfmac driver default
   is power-save ON. install.sh disables it via NetworkManager. Verify:
   ```sh
   nmcli -t -f 802-11-wireless.powersave c show "<your-connection-name>"
   # Expected: 2 (disable)
   ```
2. **AirPlay sender behavior** — macOS and iOS push fundamentally
   different payload shapes per [shairport-sync#1942](https://github.com/mikebrady/shairport-sync/issues/1942).
   Test from the *other* sender (iPhone vs Mac); if cadence changes
   meaningfully, the sender is involved.
3. **WiFi RX errors / retries** — capture before/after counters:
   ```sh
   ip -s link show wlan0
   cat /proc/net/wireless
   # RX errors / dropped / retry counters increasing during glitches?
   ```
4. **Network jitter to gateway** — sustained packet-loss test:
   ```sh
   ping -c 100 -i 0.5 $(ip route | awk '/default/ {print $3; exit}')
   # Expected: 0% loss, sub-10ms RTT
   ```
5. **CPU pressure / scheduler latency** — usually rules out, but
   re-verify shairport's priority:
   ```sh
   ps -eo pid,pri,ni,policy,comm | grep shairport
   # Expected: PR=29, NI=-10
   ```
6. **AEC bridge or voice daemon competing for ALSA** — usually fine,
   but check if the bridge has its mic-stall recovery logs:
   ```sh
   sudo journalctl -u jasper-aec-bridge --since "5 minutes ago" -o cat \
     | grep -iE "stall|empty|drop"
   ```

### Fix
Depends on which root cause. The diagnostic commands narrow it down.
Each has its own remediation:
- WiFi power-save: re-run `tune_wifi_for_airplay()` from install.sh
- WiFi quality: 5 GHz, closer to AP, channel survey
- Sender: try the other one for comparison, or wait for the offending
  sender app to settle (e.g. don't AirPlay from a Mac doing video
  encoding)
- AEC bridge / voice: see [HANDOFF-aec.md](HANDOFF-aec.md)

---

## Pattern E — No audio: AirPlay can't connect or wedged

### Symptoms
- AirPlay device "JTS" appears in the Mac/iPhone picker
- Selecting it doesn't produce audio (or it pretends to connect then drops)
- shairport-sync.service is active but no session is established

### Cause
The canonical shairport AP2 wedge — the per-connection RTSP handshake
hangs after `accept()`. Closest upstream report,
[shairport-sync#2024](https://github.com/mikebrady/shairport-sync/issues/2024),
showed the listener thread stuck in `pselect6`. No upstream fix exists.

### Recovery (automatic)
The Tier 3 supervisor at
[`jasper/control/shairport_supervisor.py`](../jasper/control/shairport_supervisor.py)
catches this without manual intervention. Detection latency is ~90 s
(3 consecutive RTSP-`OPTIONS` failures at 30 s cadence) plus a ~2 s
restart. Gated on `PlaybackStatus != "Playing"` so a live session is
never disrupted; rate-limited to one restart per 10 minutes.

Design rationale: [docs/HANDOFF-resilience.md (Tier 3)](HANDOFF-resilience.md).
Disable knob: `JASPER_SHAIRPORT_SUPERVISOR=disabled` in
`/etc/jasper/jasper.env`.

### Recovery (manual, faster)
When you don't want to wait the 90 s detection window:

```sh
sudo systemctl restart nqptp shairport-sync
# or, equivalently:
bash scripts/airplay-reset.sh
```

After ~2 s the device should be selectable again. The supervisor
notices the recovery on its next probe.

### Variant — SETUP progresses, audio never starts (open, 2026-05-21)

Distinct from the listener-wedge case above. Observed against the
Mac Studio: shairport accepts the AP2 PTP connection, accepts the
SETUP, logs `Connection N. AP2 Realtime Audio Stream.` at
`rtsp.c:3304`, then nothing further in the journal. The sender
retries — captured run from 2026-05-21 shows the same Connection 5
SETUP'd 18 times over 31 minutes (`12:49:35` → `13:20:55`), every
attempt reaching the same line then silence. `bash
scripts/airplay-reset.sh` clears it.

**Why the Tier 3 supervisor doesn't catch this**: the supervisor
probes RTSP `OPTIONS *`, which shairport handles in a fresh
per-connection thread independent of `principal_conn` state. The
probe correctly reports shairport as responsive (because it is) —
the failure is in the post-SETUP handshake, invisible to the
probe's contract. This is the case the resilience design doc names
explicitly as out of scope ([HANDOFF-resilience.md:181-187](HANDOFF-resilience.md)).

**Diagnostic gap before Layer 1.** At `log_verbosity = 1` the
journal stops mid-handshake:

```
rtsp.c:2913 Connection N: AP2 PTP connection from <client>
rtsp.c:3270 Connection N: SETUP AP2 ...
rtsp.c:3289 Connection N: SETUP AP2 doesn't include DACP-ID ...
rtsp.c:3304 Connection N. AP2 Realtime Audio Stream.
                                                   ← log_verbosity=1 stops here
```

We have no record of what shairport tries (or fails to do) next:
RECORD command, cipher negotiation, audio UDP port binding, PTP
clock-sync state.

**Layer 1 — done (2026-05-21)**: bumped `log_verbosity 1 → 2` in
[`deploy/shairport-sync.conf.template`](../deploy/shairport-sync.conf.template).
Level 2 emits enough post-SETUP detail to name the failing
component. Cost is roughly 2× shairport's baseline log volume —
well within the 200 MB persistent-journal cap from PR #160. Next
recurrence should be diagnosable from the journal alone.

**On the table if Layer 1 isn't enough**:

- **Layer 2 — nqptp verbose output.** AP2 cannot start audio
  without a healthy PTP sync. `nqptp.service` currently runs
  `ExecStart=/usr/local/bin/nqptp` with no flags. Confirm
  upstream's verbose flag and add it. Cheap, adds modest journal
  volume.
- **Layer 3 — structured anomaly watcher in
  [`jasper/control/shairport_supervisor.py`](../jasper/control/shairport_supervisor.py).**
  Tail shairport's journal; when a SETUP reaches `AP2 Realtime
  Audio Stream` but no audio-start marker appears within N
  seconds, emit `event=airplay.setup_no_audio` with a snapshot of
  nqptp shm (`/dev/shm/nqptp`) and the last 20 shairport lines.
  Surfaces the next occurrence proactively into `/state` and
  journald structured logs, instead of requiring a human to
  re-read hundreds of log lines after the fact. ~30 lines of code
  + tests; biggest engineering lift of the three.

**Mac-side companion capture** for next recurrence — run on the
Mac while reproducing:

```sh
log stream --style compact --info --debug \
  --predicate 'subsystem == "com.apple.AirPlay" OR subsystem == "com.apple.coreaudio.AirPlay"'
```

JTS-side logs show only the receiver's view; pairing with the
Mac's CoreAudio AirPlay subsystem log gives the sender's view of
where the handshake breaks.

---

## Pattern F — Speaker not discoverable

### Symptoms
- JTS doesn't appear in the AirPlay picker at all
- Possibly works for a moment then disappears

### Quick checks
```sh
# 1. Services up?
sudo systemctl is-active shairport-sync nqptp avahi-daemon

# 2. shairport listening on port 7000?
sudo ss -tln | grep :7000

# 3. mDNS advertising?
avahi-browse -t _airplay._tcp 2>/dev/null

# 4. AEC bridge or voice daemon holding loopback hostage?
sudo fuser -v /dev/snd/pcmC6D* /dev/snd/pcmC7D* 2>&1
```

### Fix
Per failure mode:
- Services down → `sudo systemctl restart <service>`
- mDNS broken → `sudo systemctl restart avahi-daemon`
- Loopback held → identify and restart the offending service (often
  `jasper-aec-bridge` or `jasper-voice`)
- Nothing else → reboot, then file a follow-up. This shouldn't happen.

---

## Unknown pattern — data capture recipe

If the symptom doesn't fit any pattern above, capture enough data that
a future debugger (or this doc's next reader) can characterize what's
happening.

```sh
# 1. Run the full polling diagnostic above. Save outputs.
# 2. Concurrent service log capture for ALL audio path daemons:
sudo journalctl --since "10 minutes ago" \
  -u shairport-sync -u jasper-camilla -u jasper-aec-bridge \
  -u jasper-voice -u nqptp -u librespot \
  -o short-iso > /tmp/audio_journals.log

# 3. dmesg for USB / kernel events:
sudo dmesg --since "10 minutes ago" > /tmp/dmesg.log

# 4. ALSA state snapshot:
aplay -l > /tmp/aplay.txt
arecord -l > /tmp/arecord.txt
cat /proc/asound/cards > /tmp/cards.txt
cat /etc/shairport-sync.conf > /tmp/shairport-conf.txt
cat /etc/camilladsp/outputd-cutover.yml > /tmp/camilla-yml.txt
sudo cat /var/lib/camilladsp/outputd-statefile.yml > /tmp/camilla-statefile.yml
ACTIVE=$(sudo awk '/config_path:/ {print $2}' /var/lib/camilladsp/outputd-statefile.yml)
sudo cat "$ACTIVE" > /tmp/camilla-active-yml.txt
cat /etc/asound.conf > /tmp/asoundrc.txt
cat /etc/modprobe.d/snd-aloop.conf > /tmp/aloop-modprobe.txt

# 5. Process state:
ps -eo pid,pri,ni,rtprio,policy,pcpu,pmem,comm | grep -E "shairport|camilla|jasper|librespot|bluealsa" > /tmp/procs.txt
free -m > /tmp/mem.txt
uptime > /tmp/uptime.txt
```

Read `/tmp/*` (or `scp` back to laptop), look for anomalies, and add a
new Pattern G... to this doc if it's worth preserving.

---

## The system as designed

### Audio chain

```
AirPlay sender (Mac / iPhone)
        │  RTP audio + PTP timestamps over WiFi
        ▼
shairport-sync (AirPlay 2 receiver, source-built v4.3.7)
        │  44.1 kHz S32 → pcm.shairport_substream
        ▼
snd-aloop lane 1 (Card 6 "Loopback", 8 substreams)
        │  hw:Loopback,1,1
        ▼
jasper-fanin (sums renderer/test lanes 0..4)
        │  hw:Loopback,0,7 → hw:Loopback,1,7
        ▼
pcm.jasper_capture (dsnoop on summed substream 7 @ 48 kHz S16_LE)
        │
        ▼
CamillaDSP (Rust, enable_rate_adjust=true, target_level=2048, NO resampler)
        │  48 kHz S16_LE → outputd_content_playback
        ▼
jasper-outputd → outputd_dac
        │
        ▼
selected final-output DAC (Apple USB-C dongle by default; DAC8x on jts3 lab)
        │
        ▼
TPA3255 class-D amp + speakers
```

Other renderers (librespot, bluealsa-aplay, USB-in) write to their own
private fan-in lanes. `jasper-fanin` is the only renderer summing point
and publishes the combined music stream on substream 7. `pcm.jasper_capture`
is a dsnoop reader on `hw:Loopback,1,7`; it lets multiple readers
(CamillaDSP and optional AEC bridge) safely tap the same summed music
reference. The summing point for music + TTS is downstream at
`jasper-outputd`, which owns direct DAC playback.

The final-output card is detected at install time in `install.sh`.
DAC8x lab systems prefer the enumerated `snd_rpi_hifiberry_dac8x`
card; otherwise `outputd_dac` falls back to the detected Apple dongle
card (`detect_card aplay 'usb-c to 3.5mm'`, then `"A"`).

### The four clocks at play

| Clock | What it is | What it drives |
|---|---|---|
| **A** | Mac's audio clock (CoreAudio's internal sample clock) | `rtptime` stamps on RTP audio packets |
| **B** | Mac's PTP master clock | PTP Sync/Follow_Up over WiFi UDP 319/320 |
| **C** | Pi's CPU clock (nqptp-disciplined to B) | `should_be_frame` in shairport's player.c |
| **D** | snd-aloop's PCM clock (default = jiffies) | loopback `hw_ptr` advance — and what `snd_pcm_delay()` *reports as DAC delay* |
| **E** | Apple dongle's USB-audio crystal (independent) | actual playback rate |

**The fundamental problem with sync-mode glitches on this chain:**
shairport reads `snd_pcm_delay()` and assumes it's measuring DAC latency
(clock E). What it actually returns is the snd-aloop ring fill (a function
of writes − reads, which depends on clock D and CamillaDSP's drain rate).
The fill **looks** like drift to shairport but is decoupled from the
real audio clock.

### Currently in production

| Component | Setting | Why |
|---|---|---|
| `deploy/shairport-sync.conf.template` | `resync_threshold_in_seconds = 0.2` | THE fix — keeps shairport in continuous path |
| `deploy/shairport-sync.conf.template` | `drift_tolerance_in_seconds = 0.1` | Gates the continuous path; lets ±1-sample stuffing work |
| `deploy/shairport-sync.conf.template` | `audio_backend_buffer_desired_length_in_seconds = 0.5` | Steady-state buffer level |
| `deploy/shairport-sync.conf.template` + `jasper-apply-airplay-mode` | `audio_backend_latency_offset_in_seconds = -((target_level - chunksize + fanin_output_buffer + outputd_content_bridge + outputd_dac_buffer) / samplerate)` | Compensates the fixed downstream-delay invisible to shairport's `snd_pcm_delay()`. With current outputd defaults the bridge term is `0` (`JASPER_OUTPUTD_CONTENT_BRIDGE=direct`) and this renders as `-0.149333` (CamillaDSP + fan-in output + outputd DAC). If lab mode enables `rate_match`, the bridge target fill is included. Compensation is for video/multi-room sync correctness — Pattern A3 drops require the fan-in topology. |
| `deploy/shairport-sync.conf.template` | `interpolation = "auto"` | soxr when CPU has slack, basic when buffer shallow |
| `deploy/systemd/shairport-sync.service` | `Nice=-10, IOSchedulingClass=realtime` | Matches CamillaDSP priority — shairport doesn't lose scheduler races |
| `deploy/camilladsp/outputd-cutover.yml` | `enable_rate_adjust=true`, no resampler block | Canonical 1:1 config — no double-correction oscillation |
| `deploy/camilladsp/outputd-cutover.yml` | `target_level: 2048` | Two-chunk playback target — the documented floor for stable operation. Avoids downstream underruns; saves ~21 ms vs the original 4096 (2026-05-25 trim). Revert to 4096 if underruns reappear. |
| `deploy/systemd/jasper-fanin.service` | `Environment="JASPER_FANIN_INPUT_BUFFER_FRAMES=4096"`, `Environment="JASPER_FANIN_OUTPUT_BUFFER_FRAMES=3072"` | Production defaults. Input provides the ~85 ms WiFi-burst absorption capacity the old dmix layer accidentally supplied; output gives CamillaDSP two extra 1024-frame read chunks of safety while staying below the input burst absorber. Pattern A3 fix companion. |
| `deploy/install.sh` `retire_audio_topology_switch()` | Removes stale `/var/lib/jasper/audio_topology.env`; fan-in asoundrc and renderer lanes are canonical | Prevents dmix/fanin split-brain after deploy. |
| `deploy/modprobe.d/snd-aloop.conf` | Default (no `timer_source`) | Ruled out as load-bearing; default keeps DAC-agnostic |
| `deploy/install.sh` | Disables NM WiFi power-save | brcmfmac default-ON would micro-stall AP2 RX |
| Default mode env | `JASPER_AIRPLAY_FREE_RUNNING=no` (synced) | Synced is glitch-free, works for video + multi-room |
| `/airplay/` toggle | Available | Safety net for unforeseen DAC issues |

---

## First-principles mechanism (source-cited)

The deep technical material that makes the patterns above explainable
from code. If you're debugging a new pattern, this section is the
reference for what shairport actually does.

### CamillaDSP has two separate stability controls

CamillaDSP's `enable_rate_adjust` and `target_level` solve different
classes of problem:

- `enable_rate_adjust: true` is long-term drift control. It watches the
  playback buffer level and nudges the capture rate so independent clocks
  don't slowly drift apart.
- `target_level` is short-term buffer margin. It is the playback-buffer
  level CamillaDSP tries to have available when the next processed chunk
  arrives. Too small means normal scheduler, dsnoop, or dmix jitter can
  produce playback underruns even if long-term drift correction is working.

CamillaDSP defaults `target_level` to `chunksize`. With our
`chunksize: 1024`, that is ~21 ms at 48 kHz. The shipped target is 2048
samples (~43 ms) — `2 × chunksize`, the documented floor for stable
operation. The original Pattern A2 fix shipped at 4096 (~85 ms,
generous margin); 2026-05-25 trim halved it as an independent latency
optimization. The documented ceiling is `(2 + queuelimit) * chunksize`
= 6144 samples for our `queuelimit: 4`.

Practical implication: if shairport logs are clean but Camilla logs
`PB: Prepare playback after buffer underrun`, do not touch
`resync_threshold` first. Check `target_level` and `GetBufferLevel`.

### AirPlay timing compensation is explicit, but not automatic here

AirPlay's core synchronization model gives each audio packet a playback
time relative to the sender/PTP clock, and shairport-sync schedules output
so the samples emerge at that time. The receiver still needs an accurate
view of backend latency to do that scheduling correctly.

On a simple hardware ALSA device, shairport's `snd_pcm_delay()` view is a
reasonable proxy for the DAC queue. On JTS, shairport writes to its
private fan-in lane; the real audible path is downstream:

```text
shairport -> snd-aloop lane -> jasper-fanin -> CamillaDSP target buffer -> outputd -> USB DAC
```

That means shairport can see the loopback handle but cannot dynamically
see CamillaDSP's target buffer, outputd DAC queue, or DAC processing delay. The
correct way to expose a known fixed downstream delay is therefore
shairport's documented `audio_backend_latency_offset_in_seconds` setting,
not the deprecated per-source AirPlay latency settings and not
`disable_synchronization`. JTS renders this value programmatically from
the active CamillaDSP config instead of keeping a second hard-coded copy.

The 2026-05-14 fix introduced the CamillaDSP target-level delay; the
2026-05-22 PR #214 introduced a renderer-side dmix delay; and the
2026-05-25/26 fan-in work removed that renderer-side dmix again after
Pattern A3 proved it was the drop mechanism. The offset now includes
only fixed downstream delay that still exists after shairport's private
fan-in lane: CamillaDSP's target buffer above the implicit one-chunk
baseline, jasper-fanin's output buffer, outputd's optional content
bridge, and outputd's direct-DAC buffer.

The worked-example math with the current production values:

```text
chunksize                  = 1024 samples (CamillaDSP's implicit baseline)
target_level               = 2048 samples (2026-05-25 trim from 4096)
fanin output buffer        = 3072 samples (bounded queue to CamillaDSP/AEC)
outputd content bridge     = 0 samples (direct mode; rate_match adds target_fill)
outputd DAC buffer         = 3072 samples (direct-DAC outputd queue)
extra delay (camilla)      = (2048 - 1024) / 48000 = 0.021333 s
extra delay (fanin output) =          3072 / 48000 = 0.064000 s
extra delay (bridge)       =             0 / 48000 = 0.000000 s
extra delay (outputd DAC)  =          3072 / 48000 = 0.064000 s
combined extra delay       =                         0.149333 s
offset                     = -0.149333
```

The negative sign is intentional. Upstream's own example says a backend
that takes 100 ms to process audio should use `-0.1`, so shairport feeds
the backend 100 ms early. We use the same principle for CamillaDSP's
hidden fixed buffer and outputd's invisible DAC queue.

### AirPlay 2 latency is sender-authored — the bonded-leader consequence

*Added 2026-06-21 from a source-level review (shairport-sync master) of
how AirPlay sets the latency and where the offset acts. Resolves
multi-room open question #2 (`HANDOFF-multiroom.md` §9).*

**AP1 and AP2 invert the latency contract.** In AP1/RAOP the *receiver*
advertised a floor (`Audio-Latency: 11025`, ~0.25 s) that the sender
added to its own figure. In AP2 the receiver advertises
`Audio-Latency: 0` and the **sender authors the whole timeline**: it
picks the latency (from the stream type it chose plus its own network
assessment), ships it as the PTP anchor (`SETRATEANCHORI` →
`rtp_ap2_control_receiver` → `set_ptp_anchor_info`), and delays its
*own* on-screen video by that same number to hold lip-sync (Apple patent
US 11,196,899, "Synchronization of wireless-audio to video"). The
receiver is *informed, not consulted* — the one theoretical
receiver→sender lever (`outputLatencyMicros` in the GET /info plist) is
not emitted by shairport and is ignored by the one inspectable AP2
sender, so it is not something to rely on. The sender does **not** measure
our real hardware latency; it *assumes* sound emerges at the anchor time.
The entire burden of landing sound on the anchor — and thus any
uncompensated downstream delay — is the receiver's, and surfaces as
audio-late lip-sync.

**Our offset is local, and universal across AP2 stream types.**
`audio_backend_latency_offset_in_seconds` never goes on the wire. In the
AP2 path it is folded into the PTP anchor *unconditionally*
(`set_ptp_anchor_info(conn, clock_id, frame_1 - 11035 - added_latency,
…)`), and AP2 playout time is computed purely from that anchor
(`frame_to_ptp_local_time` reads `anchor_rtptime`/`anchor_local_time`,
**not** `conn->latency`). So a single static offset shifts playout by
exactly `added_latency / rate` for **both** AP2 stream types — realtime
(ALAC) and buffered (AAC) — with no stream-type branch around the anchor
math. The `net_latency <= 0` guard clamps only `conn->latency`, which in
PTP mode governs the packet-resend window, **not** playout — so an
over-budget offset is *not dropped* for AP2: shairport warns and
continues, the anchor still shifts, and realized early-play is bounded
only by the physical pre-roll the sender provides (too small a budget →
bounded residual lag, never a crash or corruption). (AirPlay 1/NTP is a
genuinely different path — `rtp_control_receiver` folds the offset
through `conn->latency` into the NTP anchor — but AP1's ~2 s budget makes
any realistic offset fit trivially.) Verified against shairport-sync
master: `rtp_ap2_control_receiver`, `frame_to_ptp_local_time`,
`set_ptp_anchor_info`, `rtp_control_receiver` in rtp.c; `buffer_get_frame`
in player.c.

**The bonded-leader gap.** A bonded leader plays its *own* channel
through the Snapcast round-trip ("a follower of itself"), inserting the
Snapcast playout buffer (`cfg.buffer_ms`, default 400 ms —
`jasper/multiroom/config.py`) into the leader's own path to its DAC. That
delay is invisible to the solo offset derivation
(`derive_audio_backend_latency_offset` in
`deploy/bin/jasper-apply-airplay-mode` reads CamillaDSP + fan-in +
outputd only — no Snapcast term) and is never recomputed on bond (the
grouping reconciler never calls `jasper-apply-airplay-mode`). So a bonded
leader receiving AirPlay emits ~`buffer_ms` after the anchor → its audio
lags the sender's video by ~the Snapcast buffer.

**Fix shape — conditional, with a hard solo-untouched invariant.** Make
the offset bond-aware: add a Snapcast term to the derived offset *only
while this speaker is an active bonded leader*, and re-render + restart
shairport on bond/unbond (the reconciler's compare-before-write →
restart-on-change idiom). **INVARIANT — a solo or follower speaker gets
zero AirPlay-timing change from this feature**: the Snapcast term is 0
when this speaker is not a bonded leader, so the derived value stays the
current `-0.149333`, shairport is not restarted on a no-op solo
reconcile, and the bonded term is torn down (offset restored to the solo
value) on unbond. Whether the fix fully restores lip-sync then depends on
the sender's negotiated budget vs. the total hidden delay (~150 ms
pipeline + `buffer_ms`): the AP2 offset shifts the anchor regardless, but
realized early-play is capped by the sender's pre-roll, so a too-small
budget degrades to bounded residual lag (≈ the shortfall) with a
shairport warning. Measure the real per-app budget before assuming the
free regime.

**Measuring the negotiated budget (no config change needed).** shairport
runs at `log_verbosity = 2` on JTS (diagnostics block in
`deploy/shairport-sync.conf.template`), so the journal already names both
the stream type and any non-default latency:
- Stream type — `Connection N. AP2 Realtime Audio Stream.` /
  `… AP2 Buffered Audio Stream.` (rtsp.c).
- Negotiated latency — `Notified latency is N frames.`, emitted only when
  `N != 77175`; **absence means the default 77175 frames (≈1.75 s; ~2.0 s
  with the +11035 shairport adds) = the comfortable/free regime.** A
  printed `N` below the frames-equivalent of `150 ms + buffer_ms` is the
  tight regime.

`scripts/airplay-latency-probe.sh` captures this during a live session.

**JTS-side observability — the tight regime is now visible without the
journal (Stage D, 2026-06-21).** Three surfaces, all OBSERVABILITY-ONLY
(they do not touch the offset derivation, the reconciler, or any audio
path):
- **Proactive computed fit** — `jasper/multiroom/airplay_latency.py`'s
  pure `assess_fit(buffer_ms, notified_frames)` computes the budget
  (`(frames + 11035) / 44100`; the default 77175 when no `Notified
  latency` line) vs. the need (`~150 ms + buffer_ms`) and flags
  `tight` + a bounded `residual_lag_sec`. **The tight test mirrors
  shairport's own** (verified against `rtp.c` `rtp_ap2_control_receiver`,
  `net_latency <= 0`): shairport applies the negative offset only while
  `budget ≥ |offset| + audio_backend_buffer_desired_length` (0.5 s in
  `shairport-sync.conf.template`); below that it logs "too short" and
  **drops the offset entirely**. So `tight` fires at
  `budget < need + 0.5 s` and `residual_lag_sec` is the FULL `need` (the
  whole pipeline+buffer delay goes uncompensated), not the shortfall.
  Consequence: with the default `buffer_ms` (400 → need ~0.55 s) the
  threshold is ~1.05 s, far under the ~2.0 s default budget; but a
  `buffer_ms` above ~1350 ms is tight **even at the default budget**.
  Surfaced fail-soft at
  `/state.grouping.airplay_latency_fit` — `{"applicable": false}` unless
  this speaker is an active bonded leader (the journal is read ONLY in
  that rare case, gated behind a one-line config parse, so a solo speaker
  pays nothing; and even a leader's read is TTL-cached via
  `cached_notified_frames` so the 5 s `/state` poll cannot spawn a
  `journalctl` per request). The gate is the shared
  `config.is_active_leader` — the SAME predicate the reconciler uses to
  WRITE the offset (`airplay_grouping_env`), so the surface can never claim
  a fit for an offset that is not armed. The reader (`read_notified_frames`)
  is fail-soft: an unreadable journal resolves to the default budget, never
  a false warn.
- **Doctor check** — `check_grouping_airplay_latency` (grouping domain)
  skips (`ok`, "n/a") on solo/follower and warns only when a bonded
  leader's budget is genuinely too short, naming the residual lag. The
  remediation is honest about the lever that exists: the sender budget is
  AP2-authored (not growable locally), and `buffer_ms` has **no wizard
  control** (it lives in `grouping.env` as `JASPER_GROUPING_BUFFER_MS`,
  default 400), so the warn says to lower that env value if it was raised —
  it does **not** point at a non-existent `/rooms` knob.
- **Reactive ground truth** — shairport's own "stream latency … too short
  to accommodate an offset" warning is classified by the AirPlay-health
  sampler (`classify_journal_line` →
  `type=shairport_offset_too_short`, severity `issue`) so it lands in the
  existing `/system` AirPlay-health event ring with no new journal reads,
  and rolls into the `shairport_events` counter (like
  `shairport_oos`/`shairport_broken_pipe`) so it also moves the
  AirPlay-health status verdict, not just the raw event list.

**Step-0 measurement (2026-06-21).** Mining the persistent shairport
journals on jts.local found **9 real AP2 (Realtime) sessions, zero
`Notified latency` lines, zero "too short" warnings** — with
`log_verbosity = 2` confirmed live, so a non-default budget WOULD have been
logged. Every observed session used the default ~2.0 s budget (the free
regime). This is why the surface above is deliberately scoped down (quiet
when comfortable, gated journal reads) rather than an always-on detector.
(That measurement is about the *sender* budget; the corrected tight test
also makes the regime reachable from JTS's side via a `buffer_ms` raised
above ~1350 ms even at the default budget — a config the household controls
and the doctor/`/state` now flag.)
Caveat / still owed: those sessions are audio-app AirPlay; a per-app
**video** sweep (Apple TV app, YouTube/Safari, QuickTime across a couple
of iOS/macOS versions) with `scripts/airplay-latency-probe.sh` is a
human/hardware measurement not yet run — but AP2 buffered (video) streams
take a *larger*, not smaller, budget, so the free regime is expected to
hold there too.

### What shairport logs actually mean

The two log messages in [Pattern B](#pattern-b--shairport-resync_threshold-misfire-the-classic)
come from one switch in [`player.c:2880-2936`](https://github.com/mikebrady/shairport-sync/blob/4.3.7/player.c#L2880-L2936)
gated by the predicate at lines 2856-2864:

```c
if ((config.no_sync == 0) && (inframe->given_timestamp != 0) &&
    (config.resync_threshold > 0.0) &&
    (abs_sync_error > config.resync_threshold * config.output_rate)) {
  sync_error_out_of_bounds++;
} else {
  sync_error_out_of_bounds = 0;
}
if (sync_error_out_of_bounds > 3) {
  // ...fire the discrete correction path
```

`config.resync_threshold` defaults to **0.050 s** ([`shairport.c:2054`](https://github.com/mikebrady/shairport-sync/blob/4.3.7/shairport.c#L2054)).
At 44.1 kHz, the trigger threshold is **2205 frames**. The "Large
positive ... 2210 frames (0.050294 seconds)" we see in the logs is the
threshold value, **NOT a measurement of drift magnitude**.

The two branches do different things:
- **Large positive (late)** at [`player.c:2894-2905`](https://github.com/mikebrady/shairport-sync/blob/4.3.7/player.c#L2894-L2905):
  drops `sync_error + 4410` source frames via `do_flush(flush_to_frame, conn)`.
  Audible CUT.
- **Large negative (early)** at [`player.c:2915-2935`](https://github.com/mikebrady/shairport-sync/blob/4.3.7/player.c#L2915-L2935):
  generates up to **5 × filler_length = 11,025 frames of silence** (~250
  ms) and writes it via `config.output->play(long_silence, ...)`. Audible
  DROP-TO-SILENCE.

### How sync_error is computed

[`player.c:2722-2756`](https://github.com/mikebrady/shairport-sync/blob/4.3.7/player.c#L2722-L2756):

```c
local_time_to_frame(local_time_now, &should_be_frame_32, conn);
int64_t should_be_frame = should_be_frame_32 * conn->output_sample_ratio;
int64_t will_be_frame = inframe->given_timestamp * conn->output_sample_ratio;
will_be_frame = (will_be_frame - current_delay) & output_rtptime_mask;
sync_error = should_be_frame - will_be_frame;
```

- `should_be_frame` ← `local_time_to_frame(now)` — projects the
  sender's rtptime onto the **PTP-disciplined Pi CPU clock** via
  `local_ptp_time_to_frame` ([`rtp.c:1486`](https://github.com/mikebrady/shairport-sync/blob/4.3.7/rtp.c#L1486)).
  nqptp accuracy folds in here.
- `current_delay` ← `snd_pcm_delay()` on shairport's output handle
  ([`audio_alsa.c:1538-1607`](https://github.com/mikebrady/shairport-sync/blob/4.3.7/audio_alsa.c#L1538-L1607)).
  **In our chain, the output handle is `shairport_substream`, so this
  returns that loopback lane's ring fill, not DAC latency.** This is
  the bug class.

### Why `drift_tolerance` is the wrong knob

[`player.c:2950-2989`](https://github.com/mikebrady/shairport-sync/blob/4.3.7/player.c#L2950-L2989)
is the **in-bounds** path — runs when `abs_sync_error <= resync_threshold * output_rate`.
It does ±1-sample stuffing per packet, randomly weighted toward sync.
This path is gated by `config.tolerance` (a.k.a. `drift_tolerance_in_seconds`),
which is a **separate code branch** from `config.resync_threshold`.

When the discrete (`resync_threshold`) path fires, the continuous
(`drift_tolerance`) path never runs that cycle. Setting
`drift_tolerance_in_seconds=0.1` had **zero effect** on the visible
symptom we were chasing.

### Mike Brady's own diagnosis

On [shairport-sync#1980](https://github.com/mikebrady/shairport-sync/issues/1980),
2025-02-20:

> "Thanks for the report, Tim. At a guess, I'd say that the latency
> being reported back to Shairport Sync is wrong. I'll take a look."

Then 2025-03-02: *"Still looking at this … but it doesn't look great"*.
Issue closed unresolved. moodeaudio reports the same symptom on a chain
that has NO snd-aloop (shairport → `alsa_cdsp` → CamillaDSP → DAC),
confirming the issue is fundamentally "shairport vs any DSP downstream"
— not snd-aloop-specific. snd-aloop is the most common manifestation.

### Why DSP downstream amplifies discrete corrections

CamillaDSP's rubato AsyncSinc resampler ([`asynchro_sinc.rs:170-298`](https://github.com/HEnquist/rubato/blob/master/src/asynchro_sinc.rs#L170-L298))
uses a wide sinc kernel (64-256 samples for Balanced). When shairport
drops a chunk of source frames or injects 250 ms of zeros upstream,
AsyncSinc convolves that discontinuity through its kernel, producing
audible pre- and post-ringing — a smeared transient on each side of the
cut.

A direct `shairport → plughw:DAC` chain runs through ALSA's plug-layer
resampler with a much narrower kernel — same discontinuity is sharper
in time and less audibly conspicuous, especially at headphone volume.
Through a DSP plus an amplifier at music level, it's clearly audible.

### Why crystal drift produces a slow ramp

The fill ramp from ~491 ms (setpoint) to ~533 ms peak over 60 s equates
to ~0.67 ms/s = ~667 ppm. shairport writes at the host's nominal 48 kHz;
snd-aloop's `hw_ptr` advance (and thus CamillaDSP's effective read rate)
follows the dongle's actual sample clock. If the dongle is 667 ppm slow
relative to the host's 48 kHz, the loopback ring fills by ~32 frames/s
= ~0.67 ms/s. Real physical clock drift — exactly what shairport's
continuous-correction path is supposed to absorb.

The continuous path's ceiling is ~2500 ppm (±1 sample per chunk at
~124 Hz chunk rate). 4× headroom over what we observe.

---

## What we've tried — record of dead ends

So future operators don't re-walk the same paths.

| Tried | Outcome |
|---|---|
| `drift_tolerance_in_seconds = 0.1` (was 0.002 default) | **No effect** on the Pattern B symptom. Gates a different code branch. (Other parts of PR #75 still useful.) |
| `audio_backend_buffer_desired_length_in_seconds = 0.5` (was 0.15) | Marginal — gives more buffer slack but the threshold still trips. Still in production. |
| `interpolation = "auto"` (was "soxr") | Marginal CPU reduction. Still in production. |
| shairport-sync.service `Nice=-10, IOSchedulingClass=realtime` | Marginal — eliminates scheduler-stall events that were a separate small contributor. Still in production. |
| WiFi power-save disable via NM | Real fix for a separate (rare) WiFi-driven contributor. Still in production. |
| CamillaDSP `enable_rate_adjust=false` (with no resampler) | **No effect** on Pattern B. CDSP controller is not the ramp source. |
| `snd-aloop timer_source="hw:A,0"` | Marginal (fewer underrun-recoveries per event, no broken pipes) but doesn't eliminate Pattern B. **Removed** for DAC portability. |
| USB port move (mic + dongle on different host hubs) | **No effect**. USB scheduling contention was not the cause. |
| Stop `jasper-aec-bridge` | **No effect**. AEC bridge is not involved. |
| `resync_threshold_in_seconds = 0.2` | **THIS is the fix.** Eliminated all Pattern B events. PR #83. |
| Active correction profile still had `AsyncSinc` | **Real regression path.** `/etc/camilladsp/outputd-cutover.yml` can be clean while `/var/lib/camilladsp/configs/correction_*.yml` remains active and stale. Fix generator and regenerate/reset active profile. |
| Direct `plughw:Loopback,1,0` instead of `plug:jasper_capture` | Clean in the 2026-05-14 isolation test, proving shairport was not the tear source. **Not shippable** because it breaks AEC bridge sharing. |
| `target_level: 4096` with `plug:jasper_capture` | **Fix for Pattern A2.** Preserves dsnoop/AEC-compatible topology and eliminated steady-state Camilla underruns in the watch window. |
| Derived `audio_backend_latency_offset_in_seconds` | **Required companion for video/multi-room sync.** Does not affect underrun margin; exposes the fixed CamillaDSP buffer delay to shairport's AirPlay timing model without duplicating target-level constants. |
| Folding only the renderer-side dmix into the offset (Tier 1A as first shipped 2026-05-25) | **Did not reduce drops.** Investigated, expected drops to fall; rate stayed at ~5/min. Latency offset is still load-bearing for video/multi-room sync — keep — but it does not fix the drop class we were chasing. |
| Folding both dmix buffers into the offset (Tier 1A symmetric fix, 2026-05-25 same-day follow-up) | **Also did not reduce drops.** Going from -0.107 to -0.192 left the drop rate unchanged at ~5-7/min. Same correction-for-the-wrong-thing as the above. |
| Toggling DTLN leg off (2026-05-26) | **Did not reduce drops.** Halved jasper-voice CPU (43% → 27%) but the drop cadence stayed the same. Rules out CPU pressure as the amplifier. |
| iPhone vs Mac Studio sender A/B (2026-05-26) | **Did not reduce drops.** Mac at 7.2/min, iPhone at 5.3/min on identical music + WiFi link + topology. Both produced the same tight lead-time cluster. Rules out sender as the differentiator. |
| tcpdump of on-wire RTP delivery (2026-05-26) | **Surfaced the actual cause.** 2445 packets arriving with 0ms gaps (back-to-back bursts) followed by ~30-35 ms gaps — classic 802.11 A-MPDU aggregation. Inter-burst gap exceeds the inter-packet nominal spacing, which is the timing perturbation shairport's player thread couldn't tolerate when the dmix's per-write mutex added another ~5 ms slip on top. |
| **Topology switch dmix → fanin** (2026-05-26) | **THIS is what eliminated the drops.** 0 drops over 5 min vs 55 drops over the prior 10 min on dmix, same Mac + WiFi + music. fanin replaces the userspace dmix with per-renderer snd-aloop substreams + a Rust summing daemon — no shared write mutex, no shared write-timing perturbation. Promoted to production default. |
| Fanin with default `BUFFER_FRAMES=1024` | **New failure mode discovered.** Eliminated the player.c:1130 drops but introduced fanin-side input EPIPE-overruns at ~2/min — each produced 5.3 ms of injected silence (audible click). The dmix buffer of 4096 frames had been *incidentally* the WiFi-burst absorption layer. |
| **`JASPER_FANIN_INPUT_BUFFER_FRAMES=4096`** (2026-05-26) | **Eliminated the fanin xruns too.** 0 xruns over 4.5 min vs 44 xruns over the prior 21 min on the smaller buffer. Output buffer kept at 1024 frames to avoid adding downstream latency. Both audio quality issues now fully resolved. |

---

## Escalation — untried options (B/C/D)

If a future scenario produces a glitch pattern unfixable by raising
`resync_threshold` further, these are the next-rung options. None are
currently necessary; documented in case.

### Option B — Direct shairport → stdin → CamillaDSP pipe

shairport supports `output_backend = "stdio"` (write raw PCM to stdout).
CamillaDSP can capture from stdin. Eliminates snd-aloop from the AirPlay
path:

```
shairport-sync --output-backend=stdio | camilladsp --capture-stdin → dongle
```

shairport's output handle is a pipe, so `snd_pcm_delay()` is never called
— drift correction falls back to a simpler chunk-counting model.

**Pros:** Eliminates the `snd_pcm_delay()` misreporting at the root.
Keeps CamillaDSP in chain.
**Cons:** Different runtime model per renderer (snd-aloop still serves
librespot + bluealsa). Different chain per source. Install + mux + ducking
integration all need updates.

### Option C — Minimal PipeWire as the audio bus

Install minimal `pipewire` daemon (no wireplumber, no compat layers).
Migrate shairport → pipewire output, CamillaDSP → pipewire capture/playback,
librespot → pipewire, bluealsa → pipewire.

PipeWire's link graph provides sample-accurate scheduling. Each node
reports true end-to-end latency to its writer.

**Pros:** Structural fix; honest delay reporting at every node.
**Cons:** ~10-20 MB RAM (negligible on 2 GB Pi). Major architecture
change — every renderer, the mux, the volume coordinator, the AEC
bridge integration needs revalidation. The "no PipeWire" memory rule
was AEC-engine scoped, not audio-bus-scoped — but still a significant
shift.

### Option D — Patch shairport to take a delay source

Fork shairport, add a config option to source the actual DAC delay
from somewhere outside the ALSA output handle. Submit upstream.

**Pros:** Cleanest theoretical fix.
**Cons:** Open-ended upstream contribution. Maintenance burden.

---

## References

### Internal
- [PR #75 — camilla rate_adjust + shairport tuning (Pattern A fix + groundwork)](https://github.com/jaspercurry/JTS/pull/75)
- [PR #76 — user-toggleable sync mode (initial workaround)](https://github.com/jaspercurry/JTS/pull/76)
- [PR #81 — initial version of this HANDOFF doc (was named HANDOFF-airplay-sync.md until PR #85)](https://github.com/jaspercurry/JTS/pull/81)
- [PR #84 — reshape into troubleshooting guide](https://github.com/jaspercurry/JTS/pull/84)
- [PR #85 — rename HANDOFF-airplay-sync.md → HANDOFF-airplay.md](https://github.com/jaspercurry/JTS/pull/85)
- [PR #83 — resync_threshold=0.2 (Pattern B fix, current production)](https://github.com/jaspercurry/JTS/pull/83)
- [`deploy/shairport-sync.conf.template`](../deploy/shairport-sync.conf.template) — current shairport config template
- [`deploy/camilladsp/outputd-cutover.yml`](../deploy/camilladsp/outputd-cutover.yml) — current outputd CamillaDSP config
- [`jasper/web/airplay_setup.py`](../jasper/web/airplay_setup.py) — the `/airplay/` toggle
- [`deploy/bin/jasper-apply-airplay-mode`](../deploy/bin/jasper-apply-airplay-mode) — template renderer
- [`docs/audio-paths.md`](audio-paths.md) — generic audio path reference

### Upstream / external
- [shairport-sync #1980 — CamillaDSP-in-chain sync errors, maintainer diagnosis](https://github.com/mikebrady/shairport-sync/issues/1980) (THE canonical issue, unresolved by upstream)
- [shairport-sync #1768 — clock model statement](https://github.com/mikebrady/shairport-sync/issues/1768)
- [shairport-sync #1942 — iOS vs macOS sender differences](https://github.com/mikebrady/shairport-sync/issues/1942)
- [shairport-sync source — player.c (4.3.7)](https://github.com/mikebrady/shairport-sync/blob/4.3.7/player.c) — sync error code
- [shairport-sync source — audio_alsa.c (4.3.7)](https://github.com/mikebrady/shairport-sync/blob/4.3.7/audio_alsa.c) — `precision_delay_and_status`
- [shairport-sync AdjustingSync.md](https://github.com/mikebrady/shairport-sync/blob/master/ADVANCED%20TOPICS/AdjustingSync.md) — upstream guidance for fixed backend latency offsets
- [shairport-sync sample config](https://github.com/mikebrady/shairport-sync/blob/master/scripts/shairport-sync.conf) — documents negative offsets for slow output backends
- [shairport-sync TROUBLESHOOTING.md](https://github.com/mikebrady/shairport-sync/blob/master/TROUBLESHOOTING.md)
- [Apple TV User Guide — calibrate video and audio](https://support.apple.com/guide/tv/calibrate-video-and-audio-atvb228b7711/tvos) — Apple-side Wireless Audio Sync calibration context
- [CamillaDSP docs — Devices / `target_level` / rate adjust](https://github.com/HEnquist/camilladsp) — Pattern A2's buffer-margin root
- [HEnquist/camilladsp issue #207 — rate_adjust + AsyncSinc oscillation](https://github.com/HEnquist/camilladsp/issues/207) — Pattern A's root
- [rubato AsyncSinc source](https://github.com/HEnquist/rubato/blob/master/src/asynchro_sinc.rs)
- [Linux aloop.c (kernel snd-aloop driver)](https://github.com/torvalds/linux/blob/master/sound/drivers/aloop.c)
- [ALSA Project Matrix:Module-aloop wiki](https://www.alsa-project.org/wiki/Matrix:Module-aloop)
- [nqptp (PTP daemon for shairport-sync)](https://github.com/mikebrady/nqptp)

---

Last verified: 2026-06-01
