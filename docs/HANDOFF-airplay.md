# HANDOFF — AirPlay glitch troubleshooting guide

Canonical entry point for diagnosing audio artifacts on the shairport-sync
path: which pattern you are hearing, how to prove it, and what fixes it.

Neighbouring owners — do not restate their content here:
[HANDOFF-fan-in-daemon.md](HANDOFF-fan-in-daemon.md) (the mixer and its buffer
sizing) · [audio-paths.md](audio-paths.md) (the signal path) ·
[HANDOFF-resilience.md](HANDOFF-resilience.md) (the Tier 3 supervisor's design) ·
[HANDOFF-multiroom.md](HANDOFF-multiroom.md) (grouping and Snapcast) ·
[the investigation archive](historical/airplay-glitch-investigation-2026-05.md)
(root-cause derivations, source-cited shairport internals, the dead-end record,
and the retired-topology recipes behind the patterns below).

## Which lane transport is this box on?

`output_device` in the rendered `/etc/shairport-sync.conf` is substituted by
`jasper-apply-airplay-mode` from the renderer-lane map
(`/var/lib/jasper/renderer_lanes.env`, single writer
`jasper-audio-config renderer-lanes`): `shairport_substream` — the snd-aloop
lane, the fleet default, and the fallback when no map exists — or
`shairport_ring_lane` (the SHM ring) when the `airplay` lane is armed. The conf
re-renders at every unit start, so a conf that disagrees with the armed set means
the unit has not restarted since the map changed; `jasper-doctor`'s
"shairport-sync.conf: output_device" check names that state with the restart
remedy. **Transport labels in this document are load-bearing** — Pattern B and
the loopback-ring-fill `snd_pcm_delay()` mechanism are historical, not ring
guidance.

## Shipped sync values

`drift_tolerance_in_seconds = 0.002`, `resync_threshold_in_seconds = 0.2`, and
`audio_backend_buffer_desired_length_in_seconds = 0.5`, all in
`deploy/shairport-sync.conf.template`. The 2 ms drift tolerance is the fleet
default on direct evidence: a controlled 2×2 A/B on JTS4 (2026-08-18, four
120-second trials over the 16-slot AirPlay ring) eliminated the regular
out-of-date packet / packet-order event class while ring occupancy stayed at
15/16, proving occupancy alone was not the cause. **Long-run, WiFi-disruption,
and end-to-end A/V validation remain open.** The 64 ms backend target trialled
alongside it created headroom only in combination and is deliberately not
shipped.
[Trial table](historical/airplay-glitch-investigation-2026-05.md#jts4-shm-ring-validation-2026-08-18).

The latency offset is never hand-set —
[ADR-0118](adr/0118-the-airplay-latency-offset-is-derived-never-hand-set.md).
`jasper-apply-airplay-mode` derives it at every render from the active
CamillaDSP config plus the fan-in and outputd daemons' live STATUS
`snd_pcm_delay_frames`, falling back to configured buffers only when a daemon is
unavailable. Generic fallbacks render `-0.106667`; a live low-latency DAC floor
renders smaller. AirPlay video lip-sync on a computer-sourced stream is a
separate, still-open calibration question — clean counters are not proof of it.

**Preemption and pause are different contracts.** Losing the audible lane to
another source: mux completes the guarded fan-in handoff **first**, then calls
shairport-sync's native D-Bus `DropSession` so the sender is not left routed to
an inaudible speaker. MPRIS `Stop` is the compatibility fallback for an older or
unavailable native interface, and AP2 senders may ignore it. Both cleanups are
best-effort and emit stable `airplay.preempt_*` events; the completed handoff
stays authoritative if cleanup fails. Voice transport "pause AirPlay" is
MPRIS/DACP pause, deliberately leaving the session resumable.

---

## Quick triage — is it actually AirPlay?

```sh
curl -s http://localhost:8780/state | jq .active_source   # airplay|spotify|bluealsa|usbsink
systemctl is-active jasper-camilla shairport-sync jasper-voice jasper-aec-bridge
```

A `spotify` or `bluealsa` source puts the glitch in that renderer's chain; the
log signatures below are AirPlay-specific. And `jasper-voice`/`jasper-aec-bridge`
are not in the music path, but a watchdog restart loop perturbs scheduling — if
either flaps, get a clean music-only window before touching any knob.

## Pattern match: what does your glitch sound like?

| Symptom (audible) | Symptom (in logs) | Likely cause | Section |
|---|---|---|---|
| Regular click/drop about every 9-10 s on an SHM-ring AirPlay lane | 1:1 `Dropping out of date packet` and `Player: packets out of sequence`; ring may sit at 15/16; downstream faults stay zero | Drift tolerance still at the old 100 ms value; ring fullness and WiFi loss are **not** established by these messages | [Pattern R](#pattern-r--shm-ring-out-of-date-packet-drops) |
| Glitches every ~5–15 s, broken audio | CamillaDSP `Capture read short` floods + `Prepare playback after buffer underrun` every ~5 s | rate_adjust + AsyncSinc oscillating | [Pattern A](#pattern-a--camilladsp-rate_adjust--asyncsinc-oscillation) |
| Periodic small tears, shairport clean | CamillaDSP `Prepare playback after buffer underrun`, no `Capture read short` flood | CamillaDSP playback target too shallow | [Pattern A2](#pattern-a2--camilladsp-playback-buffer-too-shallow) |
| Occasional clicks during steady-state playback | shairport `Dropping out of date packet … Lead time is 0.115-0.120 seconds` in a tight cluster, 1:1 with packets-out-of-sequence | dmix-induced player-thread slip × WiFi A-MPDU bursts — **fixed by the fan-in cutover** | [Pattern A3](#pattern-a3--dmix-induced-burst-head-drops-historical) |
| Glitches every ~60 s, brief tear then quiet | shairport `Large positive sync error +50ms` → `alsa underrun` → `Large negative sync error -485ms` | `resync_threshold` misfire on snd-aloop fill (NOT actual DAC drift) — or, if the interval varies and it started after a DAC swap, crystal drift past the ~2500 ppm headroom | [B](#pattern-b--shairport-resync_threshold-misfire-historical) / [C](#pattern-c--dac-swap-drift-exceeds-correction-headroom) |
| Random / non-periodic, worse on a busy Pi or network | Mostly clean, occasional events, possibly `Broken pipe`; WiFi RX errors increment | Network, sender, WiFi, or CPU contention | [Pattern D](#pattern-d--non-periodic-glitches) |
| No audio at all, but the speaker is discoverable | shairport active but never accepts a session | shairport AP2 wedge / nqptp | [Pattern E](#pattern-e--no-audio-airplay-cant-connect-or-wedged) |
| No audio + speaker missing from the picker | Services down | Service failure, not a sync issue | [Pattern F](#pattern-f--speaker-not-discoverable) |

Listen for ~60 seconds first: interval (every 5 s? 60 s? non-periodic?),
character (brief tear? ~250 ms of silence? static?), and variation (all music?
loud passages? busy WiFi?) are what pick the row.

## Fast log scan (10 s)

```sh
S='5 minutes ago'
echo "shairport sync errors: $(sudo journalctl -u shairport-sync --since "$S" -o cat | grep -c 'Large positive')"
echo "camilla short reads:   $(sudo journalctl -u jasper-camilla --since "$S" -o cat | grep -c 'Capture read')"
echo "camilla underruns:     $(sudo journalctl -u jasper-camilla --since "$S" -o cat | grep -c 'Prepare playback after buffer underrun')"
sudo journalctl -u shairport-sync --since "$S" -o short-iso \
  | grep -E "Large positive|Large negative|underrun|Broken pipe|recovering"

# Active Camilla config + live playback-buffer snapshot
ACTIVE=$(sudo awk '/config_path:/ {print $2}' /var/lib/camilladsp/outputd-statefile.yml)
sudo grep -nE 'target_level|enable_rate_adjust|resampler|device:' "$ACTIVE"
/opt/jasper/.venv/bin/python -c 'from camilladsp import CamillaClient; c=CamillaClient("127.0.0.1",1234); c.connect(); print("buffer", c.query("GetBufferLevel"), "rate_adjust", c.query("GetRateAdjust"), "capture_rate", c.query("GetCaptureRate")); c.disconnect()'

# Lead-time distribution: a tight ~0.118 s cluster is Pattern A3 on the historical
# path; a 50-500 ms spread points at Pattern D.
sudo journalctl -u shairport-sync --since '10 minutes ago' -o cat \
  | grep "Dropping out of date packet" | awk -F'Lead time is ' '{print $2}' \
  | awk '{print $1}' | sort | uniq -c | sort -rn | head -10
```

A healthy 5-minute sample with music playing is **zero on all three counters**.
`jasper-camilla.service` caps its journal at `LogRateLimitBurst=120` per 60 s to
preserve previous-boot forensics during a short-read flood, so any non-zero count
— or journald's "messages suppressed" line — is evidence even when capped below
the true rate.

**Decision rule.** SHM-ring lane, paired `Dropping out of date packet` +
`Player: packets out of sequence`, no downstream faults → verify
`drift_tolerance_in_seconds = 0.002` (Pattern R). Historical snd-aloop/dmix path,
tightly clustered lead times with the same 1:1 warnings → Pattern A3, after
verifying `shairport_substream` in
the conf and `jasper-fanin.service` active. shairport `Large
positive`/`Large negative` → Pattern B/C/D. Camilla short reads → Pattern A.
Camilla underruns while shairport and short reads are zero → Pattern A2.
`event=fanin.xrun source=input label=airplay` → Pattern A3's companion (confirm
`buffer_frames=4096`). And always check the **active** config under
`/var/lib/camilladsp/configs/` — a correction profile can be live and stale
while `/etc/camilladsp/outputd-cutover.yml` is clean.

## System dashboard readout

`/system/audio/` includes AirPlay in the normalized audio-health projection in
[`jasper/control/audio_health.py`](../jasper/control/audio_health.py), which
consumes bounded observations from
[`jasper/control/airplay_health.py`](../jasper/control/airplay_health.py). It is
a recent-health view sampled on a 5 s / 30 s cadence, not a diagnostics runner.
Three properties are not derivable from the code and matter when reading it:
AirPlay counts as the current stream only when the selected mux lane and
`jasper-mux STATUS.sources.airplay.playing` agree, so a free-running silent lane
or a phantom macOS SETUP session cannot fake one; tiny recovered partial reads
(1016-1023 frames for a 1024-frame request) are deliberately ignored, because
CamillaDSP loops to fill the chunk and they appear on the healthy path with no
underrun; and while the deploy wrapper's bounded
`/run/jasper-airplay-health-suppress-until` marker is active the sampler still
reads live state but counts no xruns or recovery lines into the reliability
buckets, so deploy restarts do not pollute the data.

The legacy `airplay_health` snapshot stays in `/system/snapshot`. Its vocabulary:
`ok` (streaming, receiving frames, clean 5 m/30 m windows) · `inactive` (nothing
streaming — **decided by MPRIS `PlaybackStatus`, not the frame rate**, because
the airplay lane free-runs ~48 kHz of *silence* whenever the pipeline is up,
sender or no) · `watch` (streaming with non-fatal evidence — correlate, do not
reconfigure; idle short reads stay `inactive`) · `issue` (recovery event in the
last 5 m, input buffer below 4096, stale fan-in watchdog, shairport
sync/drop/underrun, fan-in xrun, Camilla playback underrun, or shairport playing
while fan-in receives nothing) · `unknown` (cannot read fan-in state or
`PlaybackStatus`, or still waiting for the first baseline — past one interval,
check `jasper-fanin.service` and the control socket first).

**A `watch` whose only signal is Camilla short reads is almost always the
rate-adjust short-read storm, not a defect.** It is inaudible (137,046 short
reads over 72 h of real use produced zero playback underruns), DAC-specific (the
drifting Apple dongle storms; a crystal-locked DAC does not), clears only on a
controller reset, and needs no intervention. The sampler captures it when it
happens — `event=camilla_rate.storm_onset` / `storm_offset` at WARNING, a
bounded per-storm CSV under `/var/lib/jasper/rate-storms/`, and
`/state.airplay_health.storm` — fail-soft and additive, touching neither the
classification nor steady-state cost.
[Mechanism](historical/airplay-glitch-investigation-2026-05.md#the-rate-adjust-short-read-storm--full-mechanism).

---

## Pattern R — SHM-ring out-of-date packet drops

**Status: fixed by the 2 ms drift tolerance (2026-08-18).** Regular paired
`Dropping out of date packet` / `Player: packets out of sequence` on a
`shairport_ring_lane` box with zero downstream faults. Confirm the rendered conf
carries `drift_tolerance_in_seconds = 0.002`; the old 100 ms value means the
unit has not restarted since the template changed. Do **not** infer ring
fullness or WiFi loss from these messages — the JTS4 trial produced them at
unchanged occupancy.

## Pattern A — CamillaDSP `rate_adjust` + AsyncSinc oscillation

**Status: fixed in PR #75.** `enable_rate_adjust: true` **and** an AsyncSinc
resampler are two drift controllers fighting on one capture; they oscillate and
AsyncSinc smears every correction across its kernel. Pick exactly one in every
music-path config **including generated correction profiles**; ours is
`enable_rate_adjust: true` with no resampler block at all. CamillaDSP names the
mistake at startup — `Needless 1:1 sample rate conversion active` means the
config is wrong. Check the **statefile**, not just the cutover config: a
generated profile under `/var/lib/camilladsp/configs/` can be the active one.
Fixed when a 5-minute scan shows zero `Capture read short`.

## Pattern A2 — CamillaDSP playback buffer too shallow

**Status: fixed 2026-05-14, trimmed 2026-05-25.** Small periodic tears with
**shairport quiet** — the sender is not being resynced, CamillaDSP is running
out of playback-side buffer. CamillaDSP defaults `target_level` to `chunksize`,
which at our `chunksize: 1024` is ~21 ms and too shallow for this topology.

Fix: `target_level: 2048` in every music-path config —
`deploy/camilladsp/outputd-cutover.yml`, `jasper/sound/camilla_yaml.py`'s output,
and any already-active profile under `/var/lib/camilladsp/configs/`. With
`queuelimit: 4` that is two chunks (~43 ms); `2 × chunksize` is the documented
floor and `(2 + queuelimit) * chunksize` the ceiling. Revert to 4096 if
`event=camilla.playback_underrun` reappears at any rate above zero. The
`(target_level - chunksize)` delay this adds is compensated by the derived offset
— do not hand-edit the offset to match. Fixed when a post-fill 2-5 minute scan
shows zero `Prepare playback after buffer underrun` and zero shairport entries.

## Pattern A3 — dmix-induced burst-head drops (historical)

**Status: fixed by the dmix → fan-in topology cutover (2026-05-26.)** 802.11
A-MPDU aggregation delivers AP2 RTP in bursts; the shared dmix write mutex added
~5 ms of player-thread slip, just enough to push each burst's head packet past
shairport's hardcoded `desired_lead_time = 0.120 s`. The A/B was unambiguous:
55 drops over 10 minutes on dmix, 0 over 5 minutes on fan-in, same sender, WiFi,
and music. Two changes shipped together and both are still load-bearing —
install writes the fan-in asoundrc directly and enables `jasper-fanin.service`,
and `JASPER_FANIN_INPUT_BUFFER_FRAMES=4096` supplies the WiFi-burst absorption
the old dmix incidentally provided (output stays at the JTS2-verified stable
floor of `1024`; 512 produced immediate output xruns).

```sh
systemctl is-active jasper-fanin.service
sudo journalctl -u jasper-fanin --no-pager | grep "fanin.input.opened" | head -1
#   expect ... period_frames=256 buffer_frames=4096, then zero of each below
sudo journalctl -u shairport-sync --since '5 minutes ago' | grep -c "Dropping out of date packet"
sudo journalctl -u jasper-fanin --since '5 minutes ago' | grep "label=airplay" | grep -c "fanin.xrun"
```

Mechanism, ruled-out alternatives, and the latency-offset work that did *not*
fix it: [archive](historical/airplay-glitch-investigation-2026-05.md#pattern-a3--dmix-induced-burst-head-drops-the-wifi-aggregation-interaction).

## Pattern C — DAC swap: drift exceeds correction headroom

**Status: hypothetical, no observed instance.** Pattern B's signature
reappearing after a DAC swap. The 0.2 s threshold gives the continuous path
~2500 ppm of headroom and the Apple dongle is ~667 ppm, so only a much worse
crystal exceeds it. Ladder: raise `resync_threshold_in_seconds` (0.3, 0.4, 0.5 —
as long as it exceeds the peak fill swing the discrete path never fires); then
raise `audio_backend_buffer_desired_length_in_seconds` at the cost of startup
latency; then the untried structural options in the
[archive](historical/airplay-glitch-investigation-2026-05.md#escalation--untried-options-bcd).
`/etc/modprobe.d/snd-aloop.conf` deliberately has **no** `timer_source` — the
old `hw:A,0` value named the Apple dongle's card ID and was removed for DAC
portability. Do not reintroduce it.

## Pattern B — shairport `resync_threshold` misfire (historical)

**Status: fixed in PR #83.** On the snd-aloop path `snd_pcm_delay()` returns the
loopback ring fill rather than DAC latency, so real crystal drift reads as sync
error, crosses the default 50 ms `resync_threshold`, and fires the discrete
correction path: drop ~6,600 source frames, inject up to 250 ms of zeros — the
audible tear. Upstream diagnosed and did not fix it (shairport-sync#1980).
`resync_threshold_in_seconds = 0.2` keeps shairport in the continuous
±1-sample stuffing path, which absorbs up to ~2500 ppm.

```sh
sudo journalctl -u shairport-sync --since "10 seconds ago" -o cat | grep "resync time"
# Expected: "resync time is 0.200000 seconds."   (0.050000 → the render did not run)
```

**`drift_tolerance` is not this knob.** It gates a different code branch and had
zero effect here. Do not generalize that to Pattern R, where 2 ms was causal.

## Pattern D — non-periodic glitches

Network, sender, WiFi, or CPU contention. In rough order of probability:

```sh
nmcli -t -f 802-11-wireless.powersave c show "<connection-name>"   # expect 2 (disable)
ip -s link show wlan0; cat /proc/net/wireless                      # RX errors during glitches?
ping -c 100 -i 0.5 $(ip route | awk '/default/ {print $3; exit}')  # expect 0% loss, <10 ms
ps -eo pid,pri,ni,policy,comm | grep shairport                     # expect PR=29, NI=-10
sudo journalctl -u jasper-aec-bridge --since "5 minutes ago" -o cat | grep -iE "stall|empty|drop"
```

Also try the *other* sender (iPhone vs Mac): they push different payload shapes,
and a meaningfully different cadence implicates the sender. Remedies are
per-cause: re-run `tune_wifi_for_airplay()` from install.sh, move to 5 GHz or
closer to the AP, wait out a busy sender, or see [HANDOFF-aec.md](HANDOFF-aec.md).

## Pattern E — no audio: AirPlay can't connect or wedged

The canonical shairport AP2 wedge — the per-connection RTSP handshake hangs
after `accept()`. No upstream fix exists (shairport-sync#2024).

**Automatic recovery.** The Tier 3 supervisor
([`jasper/control/shairport_supervisor.py`](../jasper/control/shairport_supervisor.py))
catches it in ~90 s (3 consecutive RTSP-`OPTIONS` failures at 30 s cadence) plus
a ~2 s restart, gated on `PlaybackStatus != "Playing"` so a live session is never
disrupted, rate-limited to one restart per 10 minutes. Its final mutation is an
ordinary `systemctl restart`, so a desired-On receiver gone fully inactive can
recover; the unit's final source-intent `ExecCondition` is the last gate, so a
concurrent AirPlay Off or follower park makes the restart skip rather than revive
the source. Disable knob: `JASPER_SHAIRPORT_SUPERVISOR=disabled` in
`/etc/jasper/jasper.env`. Manual and faster: `bash scripts/airplay-reset.sh`.

**Known variant the supervisor cannot catch (open):** shairport accepts the AP2
PTP connection and the SETUP, logs `AP2 Realtime Audio Stream.`, then goes
silent while the sender retries. The supervisor probes RTSP `OPTIONS *`, which
shairport answers in a fresh per-connection thread, so it correctly reports
shairport responsive — the failure is in the post-SETUP handshake, outside the
probe's contract and named out of scope in
[HANDOFF-resilience.md](HANDOFF-resilience.md). `airplay-reset.sh` clears it, and
`log_verbosity = 2` should make the next recurrence diagnosable from the journal
alone ([layered plan](historical/airplay-glitch-investigation-2026-05.md#pattern-e-variant--setup-progresses-audio-never-starts)).

## Pattern F — speaker not discoverable

```sh
sudo systemctl is-active shairport-sync nqptp avahi-daemon
sudo ss -tln | grep :7000                              # shairport listening?
avahi-browse -t _airplay._tcp 2>/dev/null              # mDNS advertising?
sudo fuser -v /dev/snd/pcmC6D* /dev/snd/pcmC7D* 2>&1   # loopback held hostage?
```

Restart the down service, `avahi-daemon` for broken mDNS, or whoever holds the
loopback (usually `jasper-aec-bridge` or `jasper-voice`). Nothing else — reboot,
then file a follow-up. This should not happen.

## Unknown pattern

Capture enough that the next reader can characterise it — the
[full-capture recipe](historical/airplay-glitch-investigation-2026-05.md#unknown-pattern--data-capture-recipe)
collects the audio-path journals, dmesg, the ALSA and CamillaDSP state, and
process priorities in one pass — then add a pattern here if it is worth
preserving.

---

## The audio chain

```
AirPlay sender ──RTP + PTP over WiFi──▶ shairport-sync (AP2, source-built v4.3.7)
  → shairport_substream (snd-aloop)  or  shairport_ring_lane (SHM ring)
  → jasper-fanin (sums every renderer/test lane) → Ring A
  → CamillaDSP (jts_ring_capture)
  → jasper-outputd → outputd_dac → TPA3255 class-D amp + speakers
```

Other renderers write to their own private fan-in lanes. `jasper-fanin` is the
only renderer summing point, and CamillaDSP is Ring A's only reader. Music + TTS
converge downstream inside `jasper-outputd`.
**The fundamental problem behind the historical sync-mode glitches:** shairport
reads `snd_pcm_delay()` and assumes it measures DAC latency. On this chain it
returns the renderer lane's ring fill — a function of the drain path, decoupled
from the real audio clock. That misreporting is the root of Patterns A2, B, and
C, and is why the latency offset exists at all.

## Currently in production

Beyond the three sync values and the derived offset above:

| Component | Setting | Why |
|---|---|---|
| `deploy/shairport-sync.conf.template` | `interpolation = "auto"`; `log_verbosity = 2` | soxr when CPU has slack, basic when the buffer is shallow; verbosity 2 gives post-SETUP detail for the open Pattern E variant at ~2× baseline log volume, inside the persistent-journal cap. |
| `deploy/systemd/shairport-sync.service` | `Nice=-10`, `IOSchedulingClass=realtime` | Matches CamillaDSP priority so shairport does not lose scheduler races. |
| `deploy/camilladsp/outputd-cutover.yml` | `enable_rate_adjust=true`, no resampler; `target_level: 2048` (Pattern A/A2) | Canonical 1:1 config with a two-chunk playback target. |
| `deploy/systemd/jasper-fanin.service` | `JASPER_FANIN_INPUT_BUFFER_FRAMES=4096`, `..._OUTPUT_...=1024` (Pattern A3) | Input supplies ~85 ms of WiFi-burst absorption; output is the JTS2-verified stable floor. |
| `deploy/install.sh` | disables NetworkManager WiFi power-save | brcmfmac's default-ON would micro-stall AP2 RX. |
| Default mode env | `JASPER_AIRPLAY_FREE_RUNNING=no` | Synced mode preserves video and multi-room timing; free-running (`/airplay/` toggle) is the fallback for unvalidated DAC/path issues. |

## File map

- [`jasper/control/airplay_health.py`](../jasper/control/airplay_health.py)
  (sampler, journal classifier, storm capture) ·
  [`audio_health.py`](../jasper/control/audio_health.py) (the normalized
  `/system/audio/` projection) ·
  [`shairport_supervisor.py`](../jasper/control/shairport_supervisor.py)
  (Tier 3 wedge recovery).
- [`jasper/multiroom/airplay_latency.py`](../jasper/multiroom/airplay_latency.py)
  — the bonded-leader lip-sync fit on `/state.grouping`, `/rooms.json`, the
  `/rooms` card, and `check_grouping_airplay_latency`. Solo and follower
  speakers report `{"applicable": false}` and read no journal.
- [`deploy/shairport-sync.conf.template`](../deploy/shairport-sync.conf.template)
  + [`deploy/bin/jasper-apply-airplay-mode`](../deploy/bin/jasper-apply-airplay-mode);
  `jasper/renderer_lanes.py` owns the lane map;
  [`jasper/web/airplay_setup.py`](../jasper/web/airplay_setup.py) is the
  `/airplay/` toggle; `scripts/airplay-reset.sh` and
  `scripts/airplay-latency-probe.sh` are the operator tools. Upstream issues and
  shairport source references live in
  [the archive](historical/airplay-glitch-investigation-2026-05.md#upstream--external-references).

Last verified: 2026-08-26 (triage pass — shipped shairport values, the offset
derivation and its live-STATUS preference, storm constants, health-status
vocabulary, fan-in buffers, CamillaDSP cutover values, and the file map
rechecked against their owning files. Root-cause derivations, the dead-end
record, and the retired-topology recipes moved to
`docs/historical/airplay-glitch-investigation-2026-05.md`; the derived offset
became ADR-0118.)
