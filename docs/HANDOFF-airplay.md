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

The shipped status is **green** as of 2026-05-11 — synced mode is glitch-free
on the Apple USB-C dongle after [PR #83](https://github.com/jaspercurry/JTS/pull/83).
If you're hearing artifacts, something has changed (DAC swap, software
update, network change, hardware fault). This doc helps you find what.

---

## Quick triage — is it actually AirPlay?

JTS handles three music sources (AirPlay, Spotify Connect, Bluetooth A2DP).
A "music glitches" report could be any of them. Verify the source first:

```sh
# On the Pi (or via SSH from laptop)
curl -s http://localhost:8780/state | jq .active_source
# Expected: "airplay" (or "spotify" or "bluealsa")
```

If `active_source` is `spotify` or `bluealsa`, the glitch is in that
renderer's chain — this doc may still help (the snd-aloop + CamillaDSP
mechanism applies to all three), but the diagnostic recipes below
emphasize AirPlay-specific log signatures. For Spotify Connect issues
see also [HANDOFF-voice-music-control.md](HANDOFF-voice-music-control.md).

---

## Pattern match: what does your glitch sound like?

Find the row that best matches what you're hearing and jump to that
section. If none fit, follow the "[unknown pattern](#unknown-pattern--data-capture-recipe)"
guide.

| Symptom (audible) | Symptom (in logs) | Likely cause | Section |
|---|---|---|---|
| Glitches every ~5–15 s, broken audio | CamillaDSP `Capture read short` floods + `Prepare playback after buffer underrun` every ~5 s | rate_adjust + AsyncSinc oscillating | [Pattern A](#pattern-a--camilladsp-rate_adjust--asyncsinc-oscillation) |
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
```

**Expected counts in a healthy state** (5 min sample, music playing):

```
shairport sync errors:  0
camilla short reads:    0
camilla underruns:      0
```

Anything non-zero needs diagnosis.

### Full polling diagnostic (5-min run, ~5 min wall time)

Use this when log signatures are inconclusive or you want to characterize
buffer behavior empirically. Polls the loopback ring fill at 2 Hz alongside
shairport's journal.

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
In `deploy/camilladsp/v1.yml`: **pick exactly one**.

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

### Verify the fix
After restarting `jasper-camilla`, the 5-min log scan should show **zero**
`Capture read short` warnings.

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
writes to `plughw:Loopback,0,0`, so what comes back is the **snd-aloop
ring buffer fill** (writes − reads) — a function of CamillaDSP's drain
rate, not the dongle's actual latency. As the buffer slowly fills from
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
This is the canonical shairport AP2 wedge — typically happens after an
abrupt client disconnect (force-quit AirPlay sender, sleep mid-session).
The PTP state in nqptp gets stuck.

### Fix
```sh
sudo systemctl restart nqptp shairport-sync
# or, if you have it:
bash scripts/airplay-reset.sh
```

After ~2 s, the device should be selectable again and audio should flow.

See the memory entry "shairport-sync AP2 wedge" + [`scripts/airplay-reset.sh`](../scripts/airplay-reset.sh)
if it exists in the repo.

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
cat /etc/camilladsp/v1.yml > /tmp/camilla-yml.txt
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
        │  44.1 kHz S32 → plughw:Loopback,0,0
        ▼
snd-aloop kernel module (Card 6 "Loopback", 8 substreams)
        │  pcm.jasper_capture (dsnoop on Loopback,1,0 @ 48 kHz S16_LE)
        ▼
CamillaDSP (Rust, capture+playback, enable_rate_adjust=true, NO resampler)
        │  48 kHz S16_LE → pcm.jasper_out (dmix on Apple USB-C dongle)
        ▼
Apple USB-C → 3.5mm dongle (USB 1.1, 12 Mbit/s, async UAC2)
        │
        ▼
TPA3255 class-D amp + speakers
```

Other renderers (librespot, bluealsa-aplay) write to the same Loopback,
just different substreams. CamillaDSP reads from sub0 only — librespot
and bluealsa use sub1+, snd-aloop sums them via dsnoop.

The Apple dongle's actual card name is detected at install time by
`detect_card aplay 'usb-c to 3.5mm'` in install.sh, falling back to
`"A"` if not found. CamillaDSP's playback target `pcm.jasper_out` is
substituted into `/root/.asoundrc` accordingly.

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
| `deploy/shairport-sync.conf.template` | `interpolation = "auto"` | soxr when CPU has slack, basic when buffer shallow |
| `deploy/systemd/shairport-sync.service` | `Nice=-10, IOSchedulingClass=realtime` | Matches CamillaDSP priority — shairport doesn't lose scheduler races |
| `deploy/camilladsp/v1.yml` | `enable_rate_adjust=true`, no resampler block | Canonical 1:1 config — no double-correction oscillation |
| `deploy/modprobe.d/snd-aloop.conf` | Default (no `timer_source`) | Ruled out as load-bearing; default keeps DAC-agnostic |
| `deploy/install.sh` | Disables NM WiFi power-save | brcmfmac default-ON would micro-stall AP2 RX |
| Default mode env | `JASPER_AIRPLAY_FREE_RUNNING=no` (synced) | Synced is glitch-free, works for video + multi-room |
| `/airplay/` toggle | Available | Safety net for unforeseen DAC issues |

---

## First-principles mechanism (source-cited)

The deep technical material that makes the patterns above explainable
from code. If you're debugging a new pattern, this section is the
reference for what shairport actually does.

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
  **In our chain, the output handle is `plughw:Loopback,0,0`, so this
  returns loopback ring fill, not DAC latency.** This is the bug.

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
- [`deploy/camilladsp/v1.yml`](../deploy/camilladsp/v1.yml) — current CamillaDSP config
- [`jasper/web/airplay_setup.py`](../jasper/web/airplay_setup.py) — the `/airplay/` toggle
- [`deploy/bin/jasper-apply-airplay-mode`](../deploy/bin/jasper-apply-airplay-mode) — template renderer
- [`docs/audio-paths.md`](audio-paths.md) — generic audio path reference

### Upstream / external
- [shairport-sync #1980 — CamillaDSP-in-chain sync errors, maintainer diagnosis](https://github.com/mikebrady/shairport-sync/issues/1980) (THE canonical issue, unresolved by upstream)
- [shairport-sync #1768 — clock model statement](https://github.com/mikebrady/shairport-sync/issues/1768)
- [shairport-sync #1942 — iOS vs macOS sender differences](https://github.com/mikebrady/shairport-sync/issues/1942)
- [shairport-sync source — player.c (4.3.7)](https://github.com/mikebrady/shairport-sync/blob/4.3.7/player.c) — sync error code
- [shairport-sync source — audio_alsa.c (4.3.7)](https://github.com/mikebrady/shairport-sync/blob/4.3.7/audio_alsa.c) — `precision_delay_and_status`
- [shairport-sync TROUBLESHOOTING.md](https://github.com/mikebrady/shairport-sync/blob/master/TROUBLESHOOTING.md)
- [HEnquist/camilladsp issue #207 — rate_adjust + AsyncSinc oscillation](https://github.com/HEnquist/camilladsp/issues/207) — Pattern A's root
- [rubato AsyncSinc source](https://github.com/HEnquist/rubato/blob/master/src/asynchro_sinc.rs)
- [Linux aloop.c (kernel snd-aloop driver)](https://github.com/torvalds/linux/blob/master/sound/drivers/aloop.c)
- [ALSA Project Matrix:Module-aloop wiki](https://www.alsa-project.org/wiki/Matrix:Module-aloop)
- [nqptp (PTP daemon for shairport-sync)](https://github.com/mikebrady/nqptp)
