# Handoff: runtime memory on 1 GB speakers

> **Status: active investigation and implementation plan.** Written
> 2026-06-29 from live `jts2.local` evidence after an operator report that
> `jasper-voice` and `jasper-control` consume too much RAM. Use this to pick
> up the memory-reduction PR from another machine. The immediate merge target
> is a small, evidence-backed change that reduces avoidable runtime RAM on
> 1 GB full-profile speakers while preserving wake reliability and low voice
> latency.

## TL;DR

`jts2` is a 1 GB Pi 5 running the full voice speaker profile. The RAM issue is
real, but the two daemons have different causes:

- `jasper-voice` is high because the current `xvf_chip_aec` auto profile keeps
  three openWakeWord detector instances resident: `on`, `chip_aec_150`, and
  `chip_aec_210`. Controlled profiling on `jts2` measured each additional
  detector at about 30-32 MB PSS. Silero VAD adds about 25 MB. This is the main
  avoidable runtime lever.
- `jasper-control` looks high in `systemctl`/cgroup memory because
  `memory.current` includes file cache charged to the cgroup. Live process PSS
  was much lower. The real private growth comes from lazy dashboard routes:
  first `/state` adds about 12 MB PSS, and `/system/snapshot`'s Home Assistant
  probe path adds about 10 MB PSS.

Most important next PR:

1. Add a tier-aware or profile-aware lean wake-leg mode for 1 GB speakers.
2. Surface detector count and wake-memory risk in doctor and/or `/state`.
3. Fix dashboard/control memory reporting so cgroup file cache is not mistaken
   for private daemon heap.

Do not remove Silero VAD or the persistent realtime provider connection as a
first move. Those protect endpointing, barge-in correctness, and latency.

## Live evidence from jts2

Captured on 2026-06-29 from `pi@jts2.local`.

System state:

```text
Mem: 991 MiB total, about 150-185 MiB available during the investigation
Swap: 495 MiB zram, observed 162-417 MiB used
Kernel: Linux 6.12.75+rpt-rpi-2712 aarch64
Installed build: 04921c2b on branch feat/dac-latency-floor
```

Initial service memory:

```text
jasper-voice.service
  MemoryCurrent: 269,172,736 bytes
  MemoryPeak:    326,516,736 bytes
  Main process:  about 294 MB RSS at the first sample

jasper-control.service
  MemoryCurrent: 109,477,888 bytes
  MemoryPeak:    115,933,184 bytes
  Main process:  about 69 MB RSS at the first sample
```

Later, after zram pressure reclaimed pages, voice's resident RSS/cgroup current
dropped, but swap rose:

```text
jasper-voice.service
  MemoryCurrent: about 114 MB
  RSS:           about 136 MB

jasper-control.service
  MemoryCurrent: about 102 MB
  RSS:           about 56 MB

System swap used: about 417 MB
```

Do not read the lower later `MemoryCurrent` as "voice became cheap." It means
the kernel pushed a lot of anonymous voice memory into zram.

## Voice RAM ledger

Live `smaps_rollup` for `jasper-voice` during the high-water pass:

```text
Rss:           263,424 kB
Pss:           244,974 kB
PrivateDirty:  223,280 kB
Anonymous:     223,280 kB
Swap:           51,088 kB
```

Grouped `smaps` categories:

```text
anon-private/native-heaps   127,808 kB PSS
[heap]                       89,888 kB PSS
site-packages-other          13,717 kB PSS
scipy                         4,768 kB PSS
numpy                         3,824 kB PSS
native-shared-libs            3,385 kB PSS
python-exe                    1,120 kB PSS
```

This is not mostly shared libraries or file-backed model mmap. It is mostly
private dirty anonymous memory, including Python heap and native allocator
arenas retained by onnxruntime/openWakeWord.

Controlled self-profiling on `jts2` with the real configured
`/var/lib/jasper/wake/jarvis_v2.onnx` model:

```text
python start                         5.9 MB PSS
import jasper.voice.daemon_main     +33.5 MB
import openai_session                +4.2 MB
construct OpenAIRealtimeConnection   +0.0 MB
construct SpeechVAD #1              +25.0 MB
construct WakeWordDetector #1       +32.1 MB
construct WakeWordDetector #2       +30.3 MB
construct WakeWordDetector #3       +30.1 MB
construct SpeechVAD #2               +5.4 MB

Total after this synthetic stack: about 166.6 MB PSS
```

That synthetic process does not include the full long-lived daemon state:
persistent provider websocket, tool registry objects, SQLite handles, UDP mic
streams, TTS playout, flight recorder, timers/research state, and runtime
allocator history. The synthetic profile still explains most of the live
voice footprint and identifies the dominant lever.

Current live voice status:

```json
{
  "provider": "openai",
  "model": "gpt-realtime-2",
  "wake_legs": ["on", "chip_aec_150", "chip_aec_210"],
  "barge_in": {"enabled": false},
  "research": {"configured": true, "provider": "openai", "model": "gpt-5.4"}
}
```

Startup journal confirms this is intentional profile resolution:

```text
jasper-aec-reconcile:
  profile=auto mode=auto current_mic=udp:9876 aec_mic=Array
  legs=raw:0,dtln:0,chip_aec:1

jasper-voice:
  event=voice.input_policy provider=openai profile=xvf_chip_aec
  endpointing=manual_silero openai_noise_reduction=off
```

Important correction: `/var/lib/jasper/aec_mode.env` showed
`JASPER_WAKE_LEG_CHIP_AEC=0`, but the reconciler's `auto` profile promoted the
effective runtime to chip-AEC because the XVF chip and Apple DAC are approved.
The low-level `JASPER_MIC_DEVICE_CHIP_AEC_150=udp:9887` and
`JASPER_MIC_DEVICE_CHIP_AEC_210=udp:9888` lines in `/etc/jasper/jasper.env`
were reconciler output, not a stale manual override.

## Control RAM ledger

Live `smaps_rollup` for `jasper-control`:

```text
Rss:            72,320 kB
Pss:            61,138 kB
PrivateDirty:   59,760 kB
Anonymous:      59,776 kB
Swap:            3,632 kB
```

Control cgroup `memory.current` was around 118 MB, but cgroup
`memory.stat` showed a large file component:

```text
anon:  about 61 MB
file:  about 54 MB
kernel/about cgroup overhead: small
```

So the cgroup number is useful for pressure accounting but misleading as a
"private daemon RAM" number.

Controlled self-profiling on `jts2`:

```text
import jasper.control.server         about 25 MB PSS total
construct/start SystemSampler        +0.2 MB
construct/start AirPlayHealthSampler +1.1 MB
first _get_state() call             +12.2 MB
second _get_state() call             +0.2 MB
/system/snapshot HA probe path      +10.0 MB
```

Thread pools were not the explanation. Forcing the HTTP executor to spawn all
8 workers added only about 0.4 MB PSS.

## Why the detector memory is not trivially shareable

The installed openWakeWord `Model` owns:

- an onnxruntime `InferenceSession`,
- per-model prediction buffers,
- an `AudioFeatures` preprocessor with state,
- optional VAD/noise-suppression state.

Code: `openwakeword.model.Model`; local wrapper:
`jasper/wake.py` `WakeWordDetector`.

Because each wake leg is a different time stream, sharing one `Model` instance
across legs would mix feature/prediction state unless we first introduce a
true per-stream state split. That may be possible as a deeper optimization,
but it is not the first safe PR. Fewer resident legs is the conservative win.

## Mergeable work plan

### PR 1: make wake-leg residency tier-aware

Goal: save 30-60 MB PSS on 1 GB full speakers without making wake sluggish.

Candidate behavior:

- Add a low-memory wake policy selected when the detected hardware tier is
  constrained/1 GB.
- In chip-AEC production mode, run either:
  - primary software `on` plus one selected chip beam, or
  - one selected chip beam only, if corpus evidence supports it.
- Keep the existing three-leg mode available as an explicit "max recall /
  experiment" option.

Likely code touch points:

- `jasper/audio_profile_state.py` `profile_env_updates`
- `deploy/bin/jasper-aec-reconcile` `apply_audio_input_profile` and
  `write_leg_env`
- `jasper/voice_daemon.py` `_configured_wake_legs`
- `jasper/wake_legs.py` if a policy field is needed
- `jasper/cli/doctor/wake.py` for operator visibility
- `tests/test_aec_reconcile.py`
- `tests/test_audio_profile_state.py`
- `tests/test_voice_daemon_wake_triple_stream.py`

Decision point before coding:

- Use corpus data to decide whether the lean default should keep `on` plus one
  chip beam, or chip-only one beam. If evidence is weak, ship the safer
  `on + primary chip beam` default first.

Validation commands:

```sh
pytest tests/test_aec_reconcile.py \
  tests/test_audio_profile_state.py \
  tests/test_voice_daemon_wake_triple_stream.py \
  tests/test_doctor.py

PI_HOST=jts2.local bash scripts/deploy-to-pi.sh
ssh pi@jts2.local 'curl -s http://127.0.0.1:8780/state | jq .voice.wake_legs'
ssh pi@jts2.local 'systemctl show jasper-voice.service -p MemoryCurrent -p MemoryPeak'
ssh pi@jts2.local 'sudo awk "/^(Rss|Pss|Private_Dirty|Swap):/" /proc/$(systemctl show -p MainPID --value jasper-voice.service)/smaps_rollup'
```

Success criteria:

- 1 GB chip-AEC speaker defaults to fewer than three detectors unless the
  operator opts into max-recall/experimental mode.
- `jasper-voice` PSS drops by at least one detector cost, about 30 MB, on
  `jts2`.
- Wake response remains acceptable in a short smoke test.
- The selected policy is visible in `/state` or `jasper-doctor`.

### PR 2: report memory honestly in the dashboard

Goal: stop confusing cgroup file cache with private daemon heap.

Current issue:

- `jasper-control` may show around 100-120 MB cgroup memory even when process
  PSS is around 60 MB and much of the cgroup total is reclaimable file cache.

Candidate behavior:

- Keep showing cgroup `memory.current` as pressure/accounting memory.
- Add an anon/file split from cgroup `memory.stat`, or add process PSS for
  services where `MainPID` is available.
- Label clearly:
  - cgroup total,
  - anonymous/private-ish memory,
  - file cache/reclaimable memory.

Likely code touch points:

- `jasper/control/system_metrics.py` `SystemSampler`
- `deploy/assets/system-status/js/sections.js`
- `deploy/assets/system-status/js/format.js`
- `tests/test_system_metrics.py`
- `tests/test_system_status_thresholds.py`

Validation:

```sh
pytest tests/test_system_metrics.py tests/test_system_status_thresholds.py
PI_HOST=jts2.local bash scripts/deploy-to-pi.sh
open http://jts2.local/system/
```

Success criteria:

- Dashboard no longer implies `jasper-control` has 100+ MB private heap when
  most of that is cgroup file cache.
- Doctor/dashboard thresholds still reflect real low-memory pressure.

### PR 3: reduce control route retention if still worth it

Lower priority. After PR 2, control may be acceptable.

Possible work:

- Cache Home Assistant status in a bounded background sampler rather than
  importing/probing on `/system/snapshot`.
- Audit lazy imports from `/state` and `/system/snapshot`.
- Keep diagnostics as a root oneshot; do not move doctor back in-process.

Expected savings:

- About 10 MB from HA probe path if moved out or made non-resident.
- About 12 MB from first `/state` path only if lazy imports can be avoided or
  isolated, which may not be worth complexity.

## Useful commands for tomorrow

Live high-level memory:

```sh
ssh pi@jts2.local '
  free -h
  swapon --show
  systemctl show jasper-voice.service jasper-control.service \
    -p MainPID -p MemoryCurrent -p MemoryPeak -p TasksCurrent
  ps -o pid,stat,etime,%cpu,%mem,rss,vsz,cmd \
    -p $(systemctl show -p MainPID --value jasper-voice.service),$(systemctl show -p MainPID --value jasper-control.service)
'
```

Process PSS:

```sh
ssh pi@jts2.local '
  for svc in jasper-voice.service jasper-control.service; do
    pid=$(systemctl show -p MainPID --value "$svc")
    echo "--- $svc pid=$pid"
    sudo awk "/^(Rss|Pss|Private_Dirty|Anonymous|Swap):/" /proc/$pid/smaps_rollup
  done
'
```

Cgroup anon/file split:

```sh
ssh pi@jts2.local '
  for svc in jasper-voice.service jasper-control.service; do
    cg=$(systemctl show -p ControlGroup --value "$svc")
    echo "--- $svc $cg"
    sudo grep -E "^(anon|file|kernel|sock|slab_reclaimable|slab_unreclaimable) " \
      /sys/fs/cgroup${cg}/memory.stat
  done
'
```

Voice runtime shape:

```sh
ssh pi@jts2.local '
  python3 - <<PY
import socket
s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(2)
s.connect("/run/jasper/voice.sock")
s.sendall(b"STATUS\n")
print(s.recv(65536).decode())
PY
'
```

Effective AEC/wake config:

```sh
ssh pi@jts2.local '
  sudo grep -R "JASPER_MIC_DEVICE\\|JASPER_WAKE_LEG\\|JASPER_AUDIO_INPUT_PROFILE" \
    -n /etc/jasper /var/lib/jasper 2>/dev/null
  sudo journalctl -u jasper-aec-reconcile --since "30 minutes ago" --no-pager |
    grep "profile="
'
```

## Guardrails

- Do not disable Silero VAD first. It is tied to manual endpointing and
  full-duplex/barge-in safety.
- Do not close the persistent realtime connection between wakes as the first
  memory fix. It protects latency and reconnect reliability.
- Do not treat `MemoryCurrent` alone as private daemon memory. Check PSS and
  cgroup `memory.stat`.
- Do not make chip-AEC experimental legs silently disappear on high-RAM boxes.
  The right behavior is tier/profile/policy selected, observable, and
  overrideable.
- Pin any documented memory claim with a test or an explicit doctor/dashboard
  surface. A hidden RAM policy will drift.

## Current recommended first PR title

`Reduce default wake-detector residency on constrained chip-AEC speakers`

Suggested PR description:

```markdown
On 1 GB full-profile speakers, chip-AEC auto mode currently starts three
openWakeWord detector instances (`on`, `chip_aec_150`, `chip_aec_210`).
Live jts2 profiling measured each extra detector at about 30 MB PSS, with
the wake/VAD stack dominating jasper-voice RAM. This PR makes the default
wake-leg policy tier-aware/lean while preserving an explicit max-recall mode,
and surfaces the active detector policy in diagnostics.
```

Last verified: 2026-06-29
