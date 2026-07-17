# Implementation Plan — USB Microphone Export Latency Fix

**Target branch:** `codex/usb-mic-export` (HEAD `96a1a6458`, "Add independent low-latency USB microphone export") **plus uncommitted working-tree edits that MUST be preserved** (see "Worktree preservation" below).
**Audience:** Codex, executing without this session's context. Everything you need is in this document. Read it fully before starting.
**Source of truth for the diagnosis:** a completed 3-agent audit + live read-only probe of jts.local (2026-07-16). This plan implements its recommended sequence and deepens it. Every file/function/line anchor below was verified by reading the tree at `96a1a6458` + working edits, and key anchors were independently spot-checked a second time. Line numbers are approximate ("`path:~NNN`") — locate by the named symbol, not the number. One dependency (`pyalsaaudio`) is Linux-only and could not be exercised on the review machine; every claim about its exact API surface is explicitly flagged **[verify on Pi]**.

---

## 0. Context for Codex — what this feature is and why the plan has this shape

**The feature.** JTS exports its AEC-cleaned microphone to a USB host (Mac/PC) as a UAC2 capture endpoint, so the connected computer can use "JTS Mic" as an input device. The signal path today:

```
XVF3800 chip beam
  → jasper-aec-bridge (jasper/cli/aec_bridge.py), _aec_loop emits the cleaned frame
  → UDP 127.0.0.1:9894  (one 320-sample / 640-byte raw-PCM packet per 20 ms frame; NOT batched)
  → jasper-usbmic relay (jasper/cli/usb_mic.py, service deploy/systemd/jasper-usbmic.service)
      → LatestAudioQueue (2×20 ms, drop-oldest)
      → aplay subprocess stdin OS pipe
      → aplay -D plughw:CARD=UAC2Gadget,DEV=0  (16 kHz → 48 kHz plug conversion)
  → u_audio UAC2 gadget playback ring (Pi→host capture endpoint)
  → host records
```

The `:9894` leg is a **dedicated, isolated duplicate** of the cleaned mic. It exists precisely so the export never touches jasper-voice's frozen `:9876` wake/session carrier. That isolation is verified sound and must be preserved.

**The verified latency story (do not re-litigate these):**

1. **The "~490 ms pipe backlog" is NOT steady-state latency — it was REFUTED by live measurement.** During genuine host recording the aplay stdin pipe holds a stable **50 ms** and the gadget ring **~35 ms** (sawtooth 30–40 ms; hw_ptr advancing 48012 fr/s). The 490 ms reading was a *frozen residual* from the idle regime (aplay blocked on a non-draining gadget; the last-written bytes just sit there). Three different frozen values were seen in one session: 10 ms, 180 ms, 490 ms — all history artifacts. **Do not build anything that assumes 490 ms is the standing latency.**

2. **The actual product-killing defect is that latency is history-dependent and stale audio leads every capture.** There is **no flush anywhere at start-of-capture**: the only flush is the per-frame `stdin.flush()`; the queue clears only at shutdown; there is no `snd_pcm_drop`/prepare/reset. When the host starts recording, whatever is frozen in the pipe + gadget ring (up to ~550 ms of minutes-old room audio) (a) **plays out first as a stale leading burst**, and (b) — by the conservation law this repo proved during the playback-latency campaign (*"blocking-writer chains with rate-matched ends hold latency = the sum of queue capacities; warm-up head-starts become permanent fill"*) — **persists as permanent added latency for the whole session.**

3. **The fix is two moves the current design lacks:** an **occupancy-targeted writer** (standing latency set by a chosen target, not by history) and a **reset-on-host-resume** (capture start is history-independent and burst-free). Removing the aplay pipe is necessary but not sufficient — an in-process ALSA writer without these two reproduces the same disease inside the 40 ms gadget ring.

4. **Verified-good, do not touch:** voice isolation (independent non-blocking UDP sockets, drop-newest, no shared locks — a dead relay cannot stall `:9876`); `:9894` emits true 20 ms packets (the `frame=1280` startup log line describes the *voice legs'* 80 ms aggregation — see the comment at `jasper/cli/aec_bridge.py:~2079`); UDP transit ≈ 0 ms (live Recv-Q=0); the bridge's 32-deep `mic_q`/`chip_aec_qs` are a bounded, fast-draining transient (unclocked consumer), **not** a standing reservoir.

**Plan shape.** Instrumentation first (so every later change is falsifiable), then the structural fix (in-process occupancy-targeted writer + reset-on-resume), then measurement/acceptance, then evidence-gated tuning. Each PR is independently shippable and rollback-able.

---

## 1. Worktree preservation (READ FIRST — you can destroy work here)

The working tree at `96a1a6458` has **uncommitted edits you MUST build on top of, never discard**:

```
AGENTS.md  PRIVACY.md  README.md
docs/HANDOFF-usb-gadget.md  docs/HANDOFF-usbsink.md
jasper/usb_mic.py  jasper/web/wake_setup.py
tests/test_usb_mic.py  tests/test_wake_setup.py
```

These are cosmetic "Mac" → "computer" host-agnostic wording changes (e.g. `jasper/usb_mic.py` `build_usb_mic_status` details) plus HANDOFF doc edits. **Do not `git checkout`/`git reset`/`git stash` them away.** All line anchors in this plan are against the working tree (= what you see), so they already include these edits. Commit them into your first PR branch (or a preceding "preserve working edits" commit) so they are not lost. Three of the files this plan edits (`jasper/usb_mic.py`, `docs/HANDOFF-usb-gadget.md`, `docs/HANDOFF-usbsink.md`) already carry working edits — layer your changes on top.

---

## 2. Deployed-Pi state (measurement baseline caveat)

jts.local currently runs build `4c8c4ea3b-dirty` from a **sibling branch** `codex/close-usb-role-review` (installed 2026-07-15). The deployed `jasper/cli/usb_mic.py` and `jasper/cli/aec_bridge.py` are sha256-identical to this branch's working tree; deployed `jasper/usb_mic.py` differs (sibling copy). **The 2026-07-16 live numbers in §0 are bound to that build.** Before you trust any before/after measurement, do a clean deploy of `codex/usb-mic-export` and **re-baseline**:

```sh
bash scripts/deploy-to-pi.sh
ssh pi@jts.local 'sudo cat /var/lib/jasper/build.txt'   # confirm the short-SHA matches your commit
```

`jts.local` is a lab box (USB-in configuration) — test freely; it is not production.

---

## 3. Global conventions that bind EVERY PR (non-negotiable)

- **Logging:** every operational log line goes through `log_event(logger, "domain.action", key=value, level=logging.WARNING)` from `jasper.log_event`. A conventions test — `tests/test_log_event_conventions.py::test_no_unmigrated_event_logger_calls` — fails CI on any hand-written `logger.info("event=...")`. The relay already imports and uses `log_event` (`jasper/cli/usb_mic.py:24`). Fields whose name collides with a reserved param (`level`) ride the `fields={...}` mapping. `log_event` signature (`jasper/log_event.py:143`): `log_event(logger, name, /, *, level=logging.INFO, exc_info=False, fields=None, **kwfields)`.
- **Tests pin behavior:** every behavior change ships hardware-free pytest in the same PR. Network/device I/O is mocked.
- **PR flow is mandatory:** branch → PR → CI green → merge. Never direct-push `main`. `main` moves fast; `git fetch origin` before starting and before pushing; rebase onto `origin/main` before merge. Run `scripts/test-fast` before every push, `scripts/test-merge` before publishing/merging.
- **Deploy only via** `bash scripts/deploy-to-pi.sh`. Never hand-roll `rsync + install.sh`.
- **Doc-map routing** (`docs/doc-map.toml`): edits to `jasper/cli/usb_mic.py`, `jasper/usb_mic.py`, `deploy/systemd/jasper-usbmic*.service`, `jasper/cli/doctor/usbsink.py` route to **`docs/HANDOFF-usb-gadget.md`** and **`docs/HANDOFF-usbsink.md`**. Edits to `jasper/cli/aec_bridge.py` route to the aec-bridge test group. Update the mapped HANDOFF doc when behavior/commands/paths/invariants change; otherwise leave a "no doc impact" note in the PR.
- **Env vars must be codified** (`tests/test_env_vars_codified.py`): any new `JASPER_*` read via `os.environ.get` in `jasper/` must either appear in `.env.example` (with the required prose comment block) / `deploy/**` / `scripts/**`, or be added to that test's `_UNCODIFIED` allowlist with a reason. The test is two-sided: a var that gains a surface must NOT stay in `_UNCODIFIED`.
- **Never touch AEC reference/tail timing:** `AUDIO_MGR_SYS_DELAY`, the 192 ms filter tail, the outputd→chip reference queue, `FRAME_SAMPLES=320`. No process-wide `SCHED_FIFO` (historical `LimitRTTIME` SIGKILL crash-loop).
- **Do not reshape the bridge's voice-shared queues** (`mic_q`, `chip_aec_qs` — 32-deep drop-newest). They are bounded transients feeding wake/voice, not reservoirs.
- **COAH quality bar** (AGENTS.md): Clean / Observable / Available+resilient / Hardware-safe. Preserve, in every slice: fail-loud on source loss, restart-on-writer-death, assistant-pause independence, the `MemoryMax=48M` fit.

---

## 4. Acceptance targets (freeze after the PR 1 baseline lands)

- **Post-PR 2:** Pi-side p95 (source-emit → ALSA write) **≤ 120 ms and history-independent** — record after 10 min idle and see the same p95 as continuous recording. **Stale leading burst = 0** (first 250 ms of a capture contains no audio older than 250 ms). **Drift splices ≤ 1 per 10 min.**
- **Post-PR 4/5 (stretch):** Pi-side p95 **≤ 80 ms**.
- **Reliability gates (any slice):** 2 h continuous capture, **zero xruns**, zero seq-gap bursts; idle→resume ×100 clean (no burst, no wedge); simultaneous bidirectional audio (host playing to JTS + JTS mic to host) + NCM `iperf` soak clean; CPU/memory-pressure soak clean; **wake-rate unchanged** (bridge stats parity) for any slice that restarts the bridge.
- **Cold start:** first audio ≤ 500 ms after the host starts pulling; no leading silence > 200 ms.

---

## PR 1 — Slice 0: Wire metadata + baseline instrumentation

**Goal.** Make every later slice falsifiable. Add a `:9894` v2 packet header (seq + monotonic capture-ns), teach the relay to measure source-age percentiles and seq-gap loss, split drop counters by host regime, capture a one-shot pipe-occupancy baseline while the pipe still exists, and log the bridge's actual PortAudio input-ring latency. **No structural change — aplay stays.** Both ends restart together (`PartOf=`), and v1 tolerance means mixed versions never brick audio.

### 1a. Bridge: v2 header on the `usb_host_mic` leg ONLY

Files: `jasper/cli/aec_bridge.py`.

**Wire format.** Define a shared header spec (put the constants in `jasper/usb_mic.py` so both the bridge and the relay import the *same* definition — that module is already imported by both, and is the config/state layer, so a `struct` format string + magic belongs there):

```
USB_MIC_PACKET_MAGIC   = b"JM"          # 2 bytes
USB_MIC_PACKET_VERSION = 2              # u8
USB_MIC_HEADER_STRUCT  = "<2sBBIQ"      # magic(2s) version(B) flags(B) seq(u32 LE) t_capture_mono_ns(u64 LE)
USB_MIC_HEADER_BYTES   = struct.calcsize(USB_MIC_HEADER_STRUCT)   # = 16
```

A v2 packet = 16-byte header + 640-byte PCM = **656 bytes**. `flags` is reserved 0. `t_capture_mono_ns` is `time.clock_gettime_ns(time.CLOCK_MONOTONIC)` at emit time in `_aec_loop`. **Honesty note to carry in code comments and the doc:** this is *bridge emit time*, a proxy for capture; the true mic→emit latency (PortAudio ring + `mic_q` + processing) is small and separately bounded by the `stream.latency` log in 1c. So the relay's "source age" measures **emit → ALSA-write**, not mic → ALSA-write. `CLOCK_MONOTONIC` is system-wide, so cross-process subtraction (bridge emit-ns vs relay write-ns) is valid.

**Emitter seam — do NOT change `LegEmitter`/`emit_packet` (frozen for wake legs).** The `usb_host_mic` emitter is created at `jasper/cli/aec_bridge.py:~1723`:

```python
usb_host_mic_emitter = (
    add_emitter("usb_host_mic", config.out_port_usb_host_mic, frame_samples=FRAME_SAMPLES)
    if config.emit_usb_host_mic else None
)
```

Introduce a **subclass** `class TimestampedLegEmitter(LegEmitter)` (right after `LegEmitter` at `:~678`) that overrides `emit()`:

- Keep the batching contract of `emit_packet` (accumulate into `self.batch`; act only when a full `frame_bytes` frame is available). For `usb_host_mic`, `frame_samples=FRAME_SAMPLES=320` → `frame_bytes=640`, and `_aec_loop` calls `emit(clean)` with exactly one 640-byte frame per iteration (`clean` is never empty at the emit site — in chip mode the loop `continue`s at `:~2306` before reaching the emit if the primary beam frame is missing; in software mode `clean = engine.process(...)` is always 640 B). So the batch fills exactly and sends one packet per emit.
- On each full frame: increment a per-emitter `self._seq` (u32, wraps via `& 0xFFFFFFFF`), build the header with `struct.pack(USB_MIC_HEADER_STRUCT, USB_MIC_PACKET_MAGIC, USB_MIC_PACKET_VERSION, 0, self._seq, time.clock_gettime_ns(time.CLOCK_MONOTONIC))`, and `sendto(header + frame)`. Reuse the existing non-blocking socket and the `packets_sent_by_leg` / `udp_send_drops_by_leg` stats + drop-newest-on-`BlockingIOError` semantics from `emit_packet` (factor the send+stats into a small helper both `emit_packet` and this subclass call, OR duplicate the ~6 lines — prefer the shared helper, but keep `emit_packet`'s public signature identical since the wake-leg tests import it).

Wire it by making `add_emitter` accept an optional `emitter_cls=LegEmitter` kwarg (default unchanged) and pass `emitter_cls=TimestampedLegEmitter` at the `usb_host_mic` call site only. Every other `add_emitter` call is untouched → wake legs keep the exact frozen wire format.

### 1b. Relay: parse v2, tolerate v1; measure age percentiles, seq loss, regime-split drops

Files: `jasper/cli/usb_mic.py`.

- **Length-dispatch** in the recv loop (`run_relay`, `:~395`). Extend the existing `ACCEPTED_PACKET_BYTES` pattern:
  - `len == 656` → v2: parse header via `struct.unpack`; validate magic + version; extract `seq`, `t_capture_mono_ns`; payload = last 640 bytes. Malformed header (bad magic/version) → `malformed_packets += 1`, skip.
  - `len == 640` (`PACKET_BYTES`) → v1 raw: `t_capture_mono_ns = time.clock_gettime_ns(CLOCK_MONOTONIC)` (recv time as fallback), `seq = None`.
  - `len == 2560` (`LEGACY_PACKET_BYTES`, the existing 80 ms rolling-upgrade shape, never emitted by current code) → split into 4×640 as today; `seq = None`, recv-time capture.
  - Keep `ACCEPTED_PACKET_BYTES` as the fast membership test but add the 656 case.
- **Queue carries the timestamp.** Change `LatestAudioQueue` items from `bytes` to a small tuple `(t_capture_ns: int, seq: int | None, pcm: bytes)` (or a `NamedTuple`/frozen dataclass `QueuedFrame`). `put`/`get`/drop logic unchanged except item type. The write loop (`AplaySink._write_loop`) pops the tuple, writes `pcm` to the pipe as today, and computes **source-age-at-dequeue** = `(time.clock_gettime_ns(CLOCK_MONOTONIC) - t_capture_ns) / 1e6` ms. (In PR 1, age-at-dequeue is the honest metric — the opaque pipe + ring after it are not traversable; PR 2 upgrades this to true age-at-ALSA-write.)
- **Percentiles, cheaply.** Keep a bounded ring of recent ages (e.g. `collections.deque(maxlen=512)`; at ~50 frames/s that is ~10 s of history) appended in the write loop under `_progress_lock`. At the existing 0.5 s status cadence in `run_relay` (`:~406`), snapshot the ring and compute p50/p95/p99 via `nearest_rank_percentile` imported from `jasper.cli.route_latency_artifact` (defined at `:71`, exported in its `__all__`, already reused by `route_latency_harness.py`; note it takes the percentile as a fraction, e.g. `0.95`). Write them into status.json. **Read `_write_status` first (`:~349`)** — it only stamps `schema_version`/`updated_epoch_sec` and writes; percentile computation happens in the `run_relay` status block, not in `_write_status`.
- **Seq-gap loss counter.** Track `last_seq`; when a v2 packet's `seq != (last_seq + 1) & 0xFFFFFFFF` and `last_seq is not None`, add the positive gap to `packets_lost`. Reset tracking on a v1/legacy packet (no seq). Export `packets_lost` in status.json.
- **Regime-split drop counters.** The relay already computes `host_streaming` in `_audio_health_snapshot` (`:~254`) and `drops_since_status` in the status block. Split the per-tick `drops_since_status` into `periods_dropped_streaming` vs `periods_dropped_idle` by bucketing the delta on the tick's `host_streaming` value (cumulative, in status.json). This directly answers the COAH complaint: the "4.1 M idle drops" become clearly labeled non-faults.
- **One-shot pipe FIONREAD baseline** (transitional — the pipe is deleted in PR 2). Once, shortly after `AplaySink` starts, read the pipe's `F_GETPIPE_SZ` capacity and `FIONREAD` pending bytes on `self.process.stdin.fileno()` and emit a single `log_event(logger, "usb_mic.pipe_baseline", capacity_bytes=..., pending_bytes=..., pending_ms=...)`. Use `fcntl.ioctl(fd, termios.FIONREAD, ...)` (add `import termios`) and `fcntl.fcntl(fd, fcntl.F_GETPIPE_SZ)`. Also **fix the swallowed error** at `:~119`: the code requests `F_SETPIPE_SZ` 4096, discards the return, and swallows `OSError` — log the *actual* resulting capacity once (`log_event(... "usb_mic.pipe_configured", requested=PIPE_BYTES, actual=fcntl.fcntl(fd, F_GETPIPE_SZ))`). This closes the "capacity silently clamped to one 16 KiB page" blind spot the audit found.
- **status.json schema bump** to `schema_version: 3` in `_write_status` (`:~349`). New keys: `source_age_ms_p50/p95/p99`, `packets_lost`, `periods_dropped_streaming`, `periods_dropped_idle`. **Preserve every existing key** consumed by `jasper/usb_mic.py::build_usb_mic_status` (`host_streaming`, `audio_healthy`, `audio_stalled`, `source_stalled`, `sustained_drops`, `periods_dropped`, `drop_rate_periods_per_sec`, `updated_epoch_sec`) and by the doctor (`jasper/cli/doctor/usbsink.py::check_usb_mic_export`, `:~481`). Do NOT remove `periods_dropped`.

### 1c. Bridge: log the actual PortAudio input-ring latency

Files: `jasper/cli/aec_bridge.py`, `_mic_thread` (`:~1402`), at the `sd.InputStream(...)` open (`:~1465`, which sets no `latency=` kwarg). After the `with sd.InputStream(...) as stream:` opens, emit one line: `log_event(logger, "aec.mic_stream_latency", latency_s=stream.latency, samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES)`. This closes the "PortAudio ring depth unknown" gap. **Note:** the current code uses `with sd.InputStream(...):` without binding a name; bind it (`as stream`) so `stream.latency` is readable. `sd.InputStream.latency` is a documented PortAudio attribute (input latency, seconds). Restart blast radius: bridge restart only (shared with voice — this is a pure log, no behavior change).

### Tests (PR 1)

- `tests/test_aec_bridge_stall.py` (home of `test_aec_loop_emits_both_streams`, `:~251`):
  - `test_usb_host_mic_emitter_prepends_v2_header` — feed the chip/AEC loop (or drive `TimestampedLegEmitter.emit` directly) with two 640-byte frames; assert the two `sendto` payloads are each 656 bytes, magic/version parse, `seq` increments 0→1, and the trailing 640 bytes equal the input PCM.
  - `test_wake_legs_wire_format_unchanged` — assert `on`/`off`/`raw0` emitters still send exactly `frame_bytes` with no header (guards the "voice legs frozen" invariant).
- `tests/test_usb_mic.py`:
  - `test_relay_parses_v2_header_and_measures_age` — feed a v2 packet with a known `t_capture_mono_ns`; assert the payload reaches the queue unchanged and a plausible `source_age_ms_*` lands in status.json.
  - `test_relay_accepts_v1_raw_packets` — 640-byte raw packet still enqueues (rolling-upgrade tolerance).
  - `test_relay_counts_seq_gaps` — seq 0,1,3 → `packets_lost == 1`.
  - `test_relay_splits_drops_by_regime` — construct a streaming vs idle tick and assert drops land in the right bucket.
  - **Adapt** `test_relay_forwards_nonzero_pcm_unchanged` (`:~188`) — the queue item is now a tuple; update the `FakeQueue`/assertion to check the `pcm` field equals the input chunk (PRESERVE the assistant-pause-independence semantic — a nonzero PCM frame flows through unchanged).
  - Keep `test_host_pcm_status_parser_requires_running_and_reads_hw_ptr` (`:~374`) green.

### Observability / rollback / latency effect (PR 1)

- Observability: `event=usb_mic.pipe_baseline`, `event=usb_mic.pipe_configured`, `event=aec.mic_stream_latency`; status.json p50/p95/p99, `packets_lost`, regime-split drops.
- Rollback: revert the commit. v1 tolerance means a bridge running old code (raw 640) and a relay running new code (or vice-versa) both keep audio flowing.
- Latency effect: **none** (measurement only). The 16-byte header adds ~0.5 µs/frame of framing; negligible.

### Definition of done (PR 1)

- [ ] `scripts/test-fast` and `scripts/test-merge` green.
- [ ] New tests above present and passing; `test_log_event_conventions.py` green (all new logs via `log_event`).
- [ ] Deploy to jts.local; `curl -s http://jts.local:8780/aec | jq .usb_mic` and `ssh pi@jts.local 'cat /run/jasper-usbmic/status.json'` show schema_version 3 + percentiles while a host records; `journalctl -u jasper-aec-bridge | grep event=aec.mic_stream_latency` shows the PortAudio ring depth; `journalctl -u jasper-usbmic | grep event=usb_mic.pipe_baseline` shows the real pipe capacity.
- [ ] Doc-map: add a one-line note to `docs/HANDOFF-usb-gadget.md` "Toggling the computer microphone" section (`:~202`) that the relay now carries seq/timestamp telemetry (do NOT yet remove the pipe follow-up prose — that is PR 2).

---

## PR 2 — Slice 1: Replace aplay+pipe with an in-process occupancy-targeted ALSA writer (the core fix)

**Goal.** Delete the subprocess + opaque OS pipe. Replace `AplaySink` with an in-process `pyalsaaudio` playback writer that (a) holds gadget-ring occupancy at a chosen target by dropping-oldest above it, (b) resets on host resume to kill the stale burst and the permanent history fill, (c) fills silence to keep the stream continuous, and (d) owns the 16 k→48 k clock crossing with a counted drift band. This is the playback campaign's "target-bounded writer" lever, transferred.

### 2a. Layering decision (already made — do not re-decide)

The new ALSA writer lives in **`jasper/cli/usb_mic.py`** (the relay CLI). Do NOT put ALSA code in `jasper/usb_mic.py` — that module is config/state helpers (`read_intent`, `build_usb_mic_status`, constants) imported by `jasper-control` and `jasper-web`, and must stay import-light and device-free. Shared constants (packet header from PR 1, port, bcdDevice, paths) already live in `jasper/usb_mic.py`; keep it that way.

### 2b. The writer class

Replace `class AplaySink` (`jasper/cli/usb_mic.py:~96`) with `class AlsaGadgetSink` exposing the **same interface `run_relay` depends on** so the daemon loop is otherwise unchanged: `.queue` (a `LatestAudioQueue`), `.check()`, `.progress() -> (frames_written, last_progress_monotonic, last_progress_epoch_sec)`, `.close()`. Internally it runs one writer thread (like today's `_write_loop`).

**PCM open** (mirror the verified prior art in `jasper/chip_aec_experiment.py:~102` playback PCM and `jasper/cli/aec_bridge.py:~1253` capture PCM):

```python
import alsaaudio  # keep lazy/injectable — see the CI note in the tests section
pcm = alsaaudio.PCM(
    type=alsaaudio.PCM_PLAYBACK,
    mode=alsaaudio.PCM_NONBLOCK,          # see "blocking semantics" below
    device=UAC2_DEVICE,                    # "plughw:CARD=UAC2Gadget,DEV=0" — keep the plug (proven 16→48 k resampler)
    rate=SOURCE_RATE,                      # 16_000
    channels=CHANNELS,                     # 1
    format=alsaaudio.PCM_FORMAT_S16_LE,
    periodsize=PERIOD_FRAMES_ALSA,         # 10 ms @ 16 k = 160 frames  [verify on Pi]
)
```

**[verify on Pi]** `pyalsaaudio>=0.11` is already a production dependency (`pyproject.toml:89`, `"pyalsaaudio>=0.11; sys_platform == 'linux'"`). It is Linux-only and NOT installed on the review Mac, so these are unverified locally and MUST be confirmed on-Pi before relying on them:
  1. Whether `alsaaudio.PCM(...)` accepts `periodsize` in *input-rate* frames on a `plughw:` device and what buffer size it derives (older pyalsaaudio picks buffersize ≈ `periodsize × N` internally and does NOT expose ALSA `-B`/period-count directly). **Design is robust to this:** the occupancy target below is the primary latency governor (it reads `/proc` fill and drops above target regardless of the ALSA buffer size); the ALSA buffer only sets the worst-case ceiling.
  2. Non-blocking `write()` return semantics: pyalsaaudio `PCM.write(data)` returns frames accepted; on a full buffer (EAGAIN) it returns 0 or short. Confirm the exact contract, then handle a short/zero write as backpressure (see loop).
  3. That the sandbox (`deploy/systemd/jasper-usbmic.service`: `SupplementaryGroups=audio`, no `PrivateDevices`) lets the in-process open reach `/dev/snd` — it should, because today's `aplay` child opens the same device under the same unit, but confirm the first open succeeds (it is the same permission surface, not a new one).

**Occupancy target.** Extend `HostPcmSnapshot` / `_read_host_pcm_status` (`:~185`) to ALSO parse `appl_ptr` (standard ALSA `/proc/asound/UAC2Gadget/pcm0p/sub0/status` contains both `hw_ptr:` and `appl_ptr:` — the live probe already read `hw_ptr` from this file; `appl_ptr` is the adjacent line). Add `appl_ptr: int | None` to the frozen `HostPcmSnapshot`. **Ring fill (48 k-domain frames)** = `appl_ptr - hw_ptr`; `fill_ms = (appl_ptr - hw_ptr) / GADGET_RATE * 1000` where `GADGET_RATE = 48_000`. **[verify on Pi]** confirm that writing through the `plughw` plug advances the underlying hw substream's `appl_ptr` faithfully (measure `appl_ptr - hw_ptr` while writing steadily and confirm it tracks the intended target; a plug-internal buffer could offset it). `WRITER_TARGET_MS = 20` (≈ 2 × 10 ms periods; ≈ 960 frames @ 48 k) — a module constant.

**Writer loop** (the writer thread; must never block > 1 period):

```
open PCM (NONBLOCK). last_hw_ptr = None; frozen_since = None.
loop:
    frame = queue.get(timeout=0.2)            # (t_capture_ns, seq, pcm) or None
    snap  = _read_host_pcm_status()           # writer reads /proc itself, its own cadence
    # --- resume detection (frozen -> advancing) ---
    if snap.running and snap.hw_ptr advanced since last tick after being frozen >= RESUME_FREEZE_MS:
        close+reopen PCM (or snd_pcm_drop+prepare if pyalsaaudio exposes it — close/reopen is bulletproof and cheap at 16 k mono);
        drain queue to empty; log_event usb_mic.writer_reset;
        prefill = write WRITER_TARGET_MS of the freshest queued audio (or silence if queue empty) to seed the target.
    # --- host not pulling: never block on a non-draining device ---
    if not snap.running or snap.hw_ptr not advancing:
        if frame is not None: discard it (drop-oldest keeps queue bounded);   # do NOT write
        continue
    # --- host pulling: occupancy-targeted write ---
    fill_ms = (snap.appl_ptr - snap.hw_ptr)/48000*1000
    if frame is None:
        if host pulling and queue empty > SILENCE_AFTER_MS: write one period of silence;  # keep stream continuous
        continue
    if fill_ms > WRITER_TARGET_MS + HIGH_BAND_MS:
        drop-oldest (skip write); increment writer_splices (drop); rate-limited log
    elif fill_ms < WRITER_TARGET_MS - LOW_BAND_MS:
        write frame.pcm; then write one extra period (insert) to climb toward target; writer_splices (insert); rate-limited log
    else:
        write frame.pcm    # NONBLOCK; if short/zero (EAGAIN) treat as "buffer full" -> drop-oldest, do not spin
    on successful write: frames_written += 160; last_progress_monotonic/epoch update (under _progress_lock)
    on alsaaudio.ALSAAudioError (xrun): count xrun; snd_pcm_prepare/reopen; rate-limited log_event usb_mic.writer_xrun
```

Constants (module-level, tunable, prose-commented if promoted to env later — PR 2 keeps them code constants): `WRITER_TARGET_MS=20`, `HIGH_BAND_MS=20` (drift-band upper), `LOW_BAND_MS=10` (drift-band lower), `RESUME_FREEZE_MS=200`, `SILENCE_AFTER_MS=40`, `GADGET_RATE=48_000`, `PERIOD_FRAMES_ALSA=160`.

**Why NONBLOCK + `/proc`-gated drop (not blocking write):** the current aplay design blocks aplay on ALSA when the host isn't pulling, which fills the pipe and triggers `LatestAudioQueue` drop-oldest at the recv side. The in-process writer must preserve "never wedge on a non-draining device." NONBLOCK write + explicit `/proc` occupancy gating is the clean, device-buffer-agnostic way; the `/proc` fill read is the single authoritative latency governor. Silence is `b"\x00" * PERIOD_BYTES` — no numpy needed (keeps the `MemoryMax=48M` fit; do not add numpy to the relay).

**Drift ownership (the counted clock-crossing owner).** The 16 k→48 k crossing currently has no owner. The drift band above IS that owner: at ±100 ppm relative drift, fill migrates ~1.6 samples/s; over a long recording the band edge is hit occasionally and one 20 ms period is dropped/inserted, counted as `writer_splices`, logged rate-limited. A full DLL is overkill at this magnitude — an occupancy-band controller is sufficient and observable. This must be explicit, not incidental. (Implementation note: for the low-band "insert," a duplicated frame or a silence period are both acceptable 20 ms splices — pick one, comment why, count it either way.)

**Source-age-at-ALSA-write.** Because pop→write is now fully in-process (no opaque pipe), compute the honest end-to-end age = `(clock_gettime_ns(CLOCK_MONOTONIC) - frame.t_capture_ns)/1e6` **at the moment of the ALSA write** and append to the percentile ring (upgrade PR 1's age-at-dequeue). This is the true product metric.

### 2c. Delete / keep

- **Delete:** the `subprocess`/`aplay` machinery, `PIPE_BYTES`, the pipe `F_SETPIPE_SZ`/`FIONREAD` code (and the `import subprocess`, `import fcntl`, `import termios` if now unused — remove orphaned imports YOUR change creates). Delete `ALSA_PERIOD_US`/`ALSA_BUFFER_US` ONLY if nothing references them; but note `test_usb_mic_transport_uses_one_aec_frame_and_conservative_buffers` (`:~164`) asserts `ALSA_BUFFER_US == 40_000` — either keep `ALSA_BUFFER_US` as a documented target-geometry constant and keep that assertion, or update that test to assert the new writer constants. Prefer: keep a `GADGET_BUFFER_MS = 40` constant (documenting the four-period 48 k gadget ring the Pi kernel realizes) and repoint the test.
- **Keep unchanged:** `LatestAudioQueue` (now tuple-carrying from PR 1), UDP ingest + `SO_RCVBUF`, `HostProgressTracker` (still used by `run_relay`'s health/`status.json` — the writer's own resume detection is a *separate* internal poll; the small duplicate `/proc` read is acceptable and keeps the writer self-contained), `_audio_health_snapshot`, `_ready`/`--check-ready`, `run_relay`'s structure, signal handling, `_write_status`.
- **systemd:** **no unit change expected.** `deploy/systemd/jasper-usbmic.service` `ExecStart=/opt/jasper/.venv/bin/jasper-usbmic` is the relay entrypoint (aplay was only ever a child of it). `SupplementaryGroups=audio` already grants `/dev/snd`. Confirm on-Pi that `MemoryMax=48M`/`TasksMax=32` still fit (removing the aplay child *frees* memory; in-process `alsaaudio` is lighter than a subprocess). If the first ALSA open needs a device the sandbox blocks, that surfaces as a `check()` RelayError → `Restart=on-failure` — verify it does NOT crash-loop.

### 2d. `check()` and resilience

`AlsaGadgetSink.check()` (called each `run_relay` iteration at `:~390`) raises `RelayError` when the writer thread has died with an unrecoverable exception (mirror today's aplay-death path). Transient `ALSAAudioError` xruns are recovered in-thread (prepare/reopen), NOT surfaced as fatal — only a repeated unrecoverable failure exits so systemd restarts a fresh process. Preserve: fail-loud on source loss (source-stall detection unchanged), assistant-pause independence (the writer never gates on assistant/mute state), restart-on-writer-death, `ExecCondition` gate.

### Tests (PR 2)

Files: `tests/test_usb_mic.py`.

- **Delete/replace** `test_aplay_sink_applies_the_latency_buffer_contract` (`:~170`) — it asserts the aplay `-F 10000 -B 40000` argv, which no longer exists. Replace with `test_alsa_gadget_sink_opens_playback_pcm` that monkeypatches `usb_mic_cli.alsaaudio.PCM` with a fake and asserts the constructor got `type=PCM_PLAYBACK`, `device=UAC2_DEVICE`, `rate=SOURCE_RATE`, `channels=1`, `format=PCM_FORMAT_S16_LE`, `periodsize=PERIOD_FRAMES_ALSA`. (Provide a fake `alsaaudio` module in the test — `sys.modules` injection or a `conftest` stub — since it is not importable on CI's non-Pi runners; **[verify: check whether CI's Linux runners have libasound]** — if not, keep the writer's `import alsaaudio` lazy/injectable and stub it in tests exactly as the bridge does with its lazy `import alsaaudio`.)
- `test_writer_targets_occupancy` — with a fake PCM and a scripted `_read_host_pcm_status` sequence (fill below/at/above target), assert: below→writes (+insert), within→writes, above→drops-oldest and increments `writer_splices`.
- `test_writer_resets_on_host_resume` — script hw_ptr frozen for > `RESUME_FREEZE_MS` then advancing; assert the PCM is reopened (fake records close/open), the queue is drained, and a `usb_mic.writer_reset` event fires — proving the stale-burst + permanent-fill kill.
- `test_writer_fills_silence_when_queue_empty_while_pulling` — host RUNNING, queue empty > `SILENCE_AFTER_MS`; assert a zero-filled period is written.
- `test_writer_does_not_block_when_host_idle` — host not RUNNING; assert no write is attempted and the queue stays bounded (drop-oldest), so the thread can never wedge.
- `test_writer_counts_splices_on_drift_band` — fill above high band → drop+`writer_splices`; below low band → insert+`writer_splices`.
- `test_no_subprocess_in_relay` — a contract test asserting `usb_mic_cli` no longer references `subprocess.Popen`/`aplay` (guard against regression; grep the module source or assert the attribute is gone).
- **Preserve** `test_relay_forwards_nonzero_pcm_unchanged` semantics against the new sink (adapt the `FakeSink` to the `AlsaGadgetSink` interface: `.queue`, `.check()`, `.progress()`, `.close()`).
- `test_read_host_pcm_status_parses_appl_ptr` — extend the parser test (`:~374`) fixture with an `appl_ptr:` line and assert `HostPcmSnapshot.appl_ptr`.

### Observability / rollback / latency effect (PR 2)

- Observability: `event=usb_mic.writer_reset` (on host resume), `event=usb_mic.writer_xrun` (rate-limited), `event=usb_mic.writer_splice` (rate-limited, with direction=drop|insert and fill_ms); status.json gains true `source_age_ms_p50/p95/p99` (at ALSA write), `writer_splices`, `xruns`, `writer_target_ms`, current `fill_ms`.
- Rollback: revert the commit — the aplay path is self-contained and self-restores.
- **Expected latency effect:** Pi-side standing latency becomes **history-independent, ~70–95 ms** (target 20 ms writer fill + XVF capture 21–30 + bridge queue/processing ~10–20 + framing ~10; pipe eliminated); **stale leading burst eliminated**; permanent history fill eliminated.

### Docs (PR 2)

Rewrite **`docs/HANDOFF-usb-gadget.md`** "Toggling the computer microphone from `/wake/`" section (`:~202–215`) — it currently describes the aplay subprocess pipe as an *open follow-up* and repeats the (now-refuted-as-steady-state) 490 ms number. Replace with: the in-process `alsaaudio` writer, occupancy target, reset-on-resume, drift-band splice counter, and the new percentile telemetry. Remove the "removing or explicitly bounding this opaque reservoir … is a follow-up" language (it is now done). Update **`docs/HANDOFF-usbsink.md`** where it references the relay's aplay path.

### Definition of done (PR 2)

- [ ] `scripts/test-fast` + `scripts/test-merge` green; new + preserved tests pass; `test_log_event_conventions.py` green.
- [ ] Deploy to jts.local. Record from the host and verify via `curl -s http://jts.local:8780/aec | jq .usb_mic` + `cat /run/jasper-usbmic/status.json`: `source_age_ms_p95 ≤ 120`, `writer_splices` small, `xruns == 0`.
- [ ] **History-independence:** idle 10 min, then record — p95 matches continuous-recording p95 (no permanent fill).
- [ ] **Stale-burst zero:** the first 250 ms of a fresh capture contains no audio older than 250 ms (host-side listen or the age telemetry at capture start).
- [ ] **2 h capture:** `journalctl -u jasper-usbmic | grep -E 'event=usb_mic.writer_(xrun|reset|splice)'` shows zero xruns and ≤ 1 splice / 10 min.
- [ ] **idle→resume ×100:** scripted host toggling shows no leading burst and no relay wedge (`systemctl status jasper-usbmic` stays active, no restart storm).
- [ ] `sudo /opt/jasper/.venv/bin/jasper-doctor | grep 'USB microphone export'` OK.
- [ ] Doc-map docs updated; PR body lists them.

---

## PR 3 — Slice 2: Measurement + acceptance artifact

**Goal.** Turn the PR 1/2 telemetry into a continuous health surface and a one-shot certification artifact, so latency regressions are caught automatically and each change produces a signed record.

### 3a. Continuous doctor + `/aec` surface

- **Doctor.** Extend `check_usb_mic_export` in `jasper/cli/doctor/usbsink.py` (`:~411`, `@doctor_check(order=59.7, group="usbsink")`). It already reads the relay status.json (`:~481`). Add: when `host_streaming` is true AND the status is fresh (existing `RELAY_STATUS_FRESH_SECONDS` check), assert `source_age_ms_p95` is below a threshold constant (`USB_MIC_LATENCY_WARN_MS`, default **120**, matching the PR 2 acceptance target) → `warn` with the measured p95 if exceeded; stay `ok` (with the p95 in detail) otherwise. **Only assert when the host is actively pulling** — an idle host has no meaningful standing latency (frozen residual). Keep the existing intent/descriptor/relay/privacy checks intact.
- **`/aec` passthrough.** The percentiles must reach the browser/API. `jasper/control/aec_endpoints.py::_aec_full_status` (`:~531`) sets `payload["usb_mic"] = build_usb_mic_status(payload)` (`:~699`). Extend `jasper/usb_mic.py::build_usb_mic_status` (`:~168`) to pass the new relay fields (`source_age_ms_p50/p95/p99`, `writer_splices`, `xruns`, `packets_lost`) through into its returned dict when `relay_fresh` (mirror how it already surfaces `periods_dropped`/`drop_rate_periods_per_sec`). Add a `test_status_surfaces_latency_percentiles` to `tests/test_usb_mic.py`.

### 3b. One-shot certification artifact (mic-direction sibling of the route-latency artifact)

Adapt the pattern in `jasper/cli/route_latency_artifact.py` (`RouteLatencyMetrics` at `:~53`, `nearest_rank_percentile` at `:~71`, `metrics_from_samples` at `:~86`, config-hash binding via provenance). Add a new artifact builder (new module `jasper/cli/usb_mic_latency_artifact.py` OR a function in the relay's tooling) that binds a certification run to:

- `build_sha` (from `/var/lib/jasper/build.txt`),
- `bcd_device` (0x0210 when mic-on; from the gadget, cross-check `USB_MIC_BCD_DEVICE`),
- selected beam/leg (from bridge config; see PR 6 — until then it is the production primary leg, e.g. `chip_aec_150`),
- negotiated XVF capture geometry (period/buffer frames) and gadget ALSA geometry,
- `writer_target_ms`, `writer_splices`, `xruns`, `packets_lost`,
- host app identity (best-effort, operator-supplied for the run — Windows vs macOS + app name),
- the p50/p95/p99 over the run window.

**A run is invalid unless the host was actively pulling (hw_ptr advancing) for the whole window** — assert this in the builder (reject if any sample tick had `host_streaming == false`). Reuse `nearest_rank_percentile` for the percentiles; store provenance as a mapping like the route artifact does. Add `tests/test_usb_mic_latency_artifact.py` covering: config-hash binding present, invalid-run rejection when host idle, percentile correctness on a known sample set.

### Definition of done (PR 3)

- [ ] `scripts/test-fast` + `scripts/test-merge` green.
- [ ] `sudo /opt/jasper/.venv/bin/jasper-doctor` warns when p95 > 120 ms during active recording, OK otherwise; `curl -s http://jts.local:8780/aec | jq .usb_mic` shows the percentiles.
- [ ] One certification artifact generated on jts.local during a real recording window; fields bound; invalid-run guard verified by pointing it at an idle window.
- [ ] Doc-map: `docs/HANDOFF-usbsink.md` measurement section + `docs/testing-tooling.md` "if you want to X, use Y" index entry for the new artifact tool (add it in the same PR per the repo rule).

---

## Follow-ups (evidence-gated — do NOT start until PR 2's baseline is frozen)

Each is a separate PR, gated on the PR 2/3 measurement showing the change is worth it. Keep them small and independently reversible.

### PR 4 — XVF capture `latency=` experiment (bridge-shared; env-gated; soak-gated)

- In `jasper/cli/aec_bridge.py::_mic_thread` `sd.InputStream(...)` (`:~1465`), add `latency=` sourced from a new env knob read in `BridgeConfig.from_env` (`:~317`): `JASPER_AEC_CAPTURE_LATENCY` (string: unset/empty = today's default, i.e. omit the kwarg; `"low"` or an explicit seconds value otherwise). Add a `capture_latency: str` field to `BridgeConfig` (`:~284`). **Default off.** Log the negotiated result via the PR 1c `aec.mic_stream_latency` line.
- **Shared-fate with voice** — the same `InputStream` feeds wake/session. Soak criteria before flipping default: no bridge stall (`BridgeStalled`) events, no rise in `queue_drops` (bridge stats JSON), **wake-rate parity** (bridge stats). Expected win ~10–20 ms.
- Codify `JASPER_AEC_CAPTURE_LATENCY` in `.env.example` with the required prose comment block (what/why-this-default/range). Do NOT add it to `test_env_vars_codified.py::_UNCODIFIED`.
- Tests: `BridgeConfig.from_env` parses the knob; the InputStream receives `latency=` when set.

### PR 5 — Descend writer target + optional gadget buffer (churn-gated)

- Lower `WRITER_TARGET_MS` 20 → 10. Only afterward, optionally shrink the gadget ring capacity (the `GADGET_BUFFER_MS` 40 → 20 target) — capacity shrink only trims worst-case at xrun risk, so keep it separate and last.
- Gate each step on the full reliability matrix: idle/resume ×100, 2 h capture, simultaneous bidirectional audio + NCM `iperf`, CPU/memory-pressure soak, zero splice-storm. Target: Pi-side p95 ≤ 80 ms.

### PR 6 — Beam selection `JASPER_USB_MIC_LEG` (bridge-restart-only path)

- New env `JASPER_USB_MIC_LEG` in `BridgeConfig.from_env`: `primary|chip_aec_150|chip_aec_210` (default `primary` = today's behavior). At the emit site (`jasper/cli/aec_bridge.py:~2419`), when the leg is `chip_aec_150`/`chip_aec_210` and that key exists in `chip_frames` (built at `:~2281`), emit `chip_frames[leg]` to the `usb_host_mic` emitter instead of `clean`; fall back to `clean` when the selected beam is absent (software-AEC mode, or beam not produced this frame). **In-process pre-emitter selection — zero added latency.**
- **Wiring the control path — MUST NOT route through `jasper-usbmic-apply`.** `/aec/usb-mic` and the toggle deliberately trigger `_schedule_usb_gadget_recompose` (`jasper/control/server.py:~554`) → `jasper-usbmic-apply.service`, which restarts the **gadget** (descriptor recompose, `bcdDevice` flip, NCM drop). Beam selection changes NO descriptor — it must be a **bridge-restart-only** operation. Use the restart broker: `jasper.control.restart_broker.manage_units("jasper-aec-bridge.service", verb="restart", reason="usb_mic_leg")`. `jasper-aec-bridge.service` is already in `MANAGED_UNITS` (`jasper/control/restart_broker.py:~104`); the relay follows the bridge via `PartOf=` (`deploy/systemd/jasper-usbmic.service`). This gives a beam switch with **no gadget recompose, no NCM drop**. A new `/aec/usb-mic-leg` POST handler (or a field on `/wake/`) writes the env to the bridge's env file and calls the broker; do NOT reuse `_post_aec_usb_mic`'s recompose scheduling.
- Codify `JASPER_USB_MIC_LEG` in `.env.example` with the prose comment block (it is a real operator knob, unlike the internal `JASPER_AEC_CHIP_AEC_PRIMARY_LEG` which sits in `_UNCODIFIED`). Do NOT add it to `_UNCODIFIED` (the two-sided test would fail).
- Tests: `from_env` parses the leg; emitter selects `chip_frames[leg]` vs `clean` with the absent-beam fallback; the control handler calls the broker with `jasper-aec-bridge.service` and NOT `jasper-usbmic-apply.service`.

---

## Explicit DO-NOT list (rejected paths — do not implement, do not propose)

- Do NOT reshape or resize the bridge's `mic_q` / `chip_aec_qs` (32-deep drop-newest; voice/wake-shared; bounded transient, not a reservoir).
- Do NOT change `FRAME_SAMPLES = 320` (paces the whole bridge — AEC3 cadence, every leg; ≤10 ms average reward for repo-wide blast radius).
- Do NOT add a native 16 kHz UAC2 descriptor (re-enumeration churn on both OSes; macOS CoreAudio device-graph-loss hazard under descriptor cycling; re-opens Windows validation; the plug conversion is a rate conversion, not a buffer, so it is not the bottleneck).
- Do NOT build a SHM frame ring for `:9894` (UDP measured at zero backlog — Recv-Q=0; the ring *concept* lives inside the writer's occupancy target instead).
- Do NOT absorb the relay into `jasper-fanin` (couples an optional export into the RT core-audio fault domain — violates the feature's isolation requirement).
- Do NOT make the bridge write the gadget PCM directly (puts host-coupled ALSA blocking inside the voice-critical bridge — the exact coupling `:9894` exists to prevent).
- Do NOT use process-wide `SCHED_FIFO`/RT (historical `LimitRTTIME` SIGKILL crash-loop). If p99 tails ever demand it, only a single writer *thread* with paired `LimitRTTIME` — and only with measured evidence.
- Do NOT touch AEC reference/tail timing (`AUDIO_MGR_SYS_DELAY`, 192 ms tail, outputd→chip reference queue).
- Do NOT change the wake legs' wire format (`on`/`off`/`raw0`/`chip_aec_*` emitters via `emit_packet`/`LegEmitter` stay frozen — the v2 header is `usb_host_mic`-only).
- Do NOT propose PipeWire / topology re-architecture (standing repo rule).

---

## Appendix A — Verified anchor index (locate by symbol, not line)

| Symbol / fact | Location |
|---|---|
| Relay constants (`SOURCE_RATE`, `PACKET_BYTES=640`, `LEGACY_PACKET_BYTES=2560`, `ACCEPTED_PACKET_BYTES`, `QUEUE_PERIODS=2`, `PIPE_BYTES=4096`, `UAC2_DEVICE`, `ALSA_PERIOD_US=10000`, `ALSA_BUFFER_US=40000`, `HOST_PCM_STATUS_PATH`) | `jasper/cli/usb_mic.py:35–56` |
| `LatestAudioQueue` (drop-oldest) | `jasper/cli/usb_mic.py:63–93` |
| `AplaySink` (Popen aplay, `F_SETPIPE_SZ` swallowed at 119–122, per-frame `stdin.flush()` at 136, no drop/prepare) | `jasper/cli/usb_mic.py:96–183` |
| `_read_host_pcm_status` / `HostPcmSnapshot` (parses `hw_ptr` + `state: RUNNING`; NO `appl_ptr` yet) | `jasper/cli/usb_mic.py:185–202` |
| `HostProgressTracker` (frozen→advancing detection) | `jasper/cli/usb_mic.py:205–251` |
| `_audio_health_snapshot` (computes `host_streaming`) | `jasper/cli/usb_mic.py:254–319` |
| `_write_status` (stamps `schema_version:2`) | `jasper/cli/usb_mic.py:349–355` |
| `run_relay` recv/dispatch + 0.5 s status tick | `jasper/cli/usb_mic.py:358–493` |
| Shared module: `USB_HOST_MIC_UDP_PORT=9894`, `USB_MIC_BCD_DEVICE=0x0210`, `USB_NO_MIC_BCD_DEVICE=0x0200`, `RELAY_STATUS_PATH` | `jasper/usb_mic.py:33–39` |
| `build_usb_mic_status` (consumes status.json keys → `/aec`) | `jasper/usb_mic.py:168–311` |
| Bridge `FRAME_SAMPLES=320`, `OUT_FRAME_SAMPLES=1280`, `OUT_FRAME_BYTES` | `jasper/cli/aec_bridge.py:144, 269–270` |
| `emit_packet` (drop-newest on `BlockingIOError`) | `jasper/cli/aec_bridge.py:657–675` |
| `LegEmitter` | `jasper/cli/aec_bridge.py:678–698` |
| `add_emitter` closure | `jasper/cli/aec_bridge.py:1699–1716` |
| `usb_host_mic_emitter` creation (`frame_samples=FRAME_SAMPLES`) | `jasper/cli/aec_bridge.py:1723–1731` |
| `_mic_thread` + `sd.InputStream(...)` with NO `latency=` | `jasper/cli/aec_bridge.py:1402, 1465–1468` |
| `chip_frames` build + production `clean` selection | `jasper/cli/aec_bridge.py:2281–2310` |
| `usb_host_mic_emitter.emit(clean)` site | `jasper/cli/aec_bridge.py:2418–2420` |
| `frame=1280` log describes voice legs (comment) | `jasper/cli/aec_bridge.py:~2079` |
| `BridgeConfig` fields + `from_env` (`out_port_usb_host_mic=USB_HOST_MIC_UDP_PORT`, `emit_usb_host_mic`) | `jasper/cli/aec_bridge.py:282–316, 411–414` |
| pyalsaaudio PLAYBACK prior art (constructor + `write`) | `jasper/chip_aec_experiment.py:102–110, 145–152` |
| pyalsaaudio CAPTURE prior art (lazy import + constructor) | `jasper/cli/aec_bridge.py:1230, 1253–1259`; `jasper/route_latency/mic_readers.py:185–194` |
| `pyalsaaudio>=0.11; sys_platform=='linux'` | `pyproject.toml:89` |
| `check_usb_mic_export` doctor (reads status.json) | `jasper/cli/doctor/usbsink.py:411–512` |
| `_post_aec_usb_mic` + `_schedule_usb_gadget_recompose` (gadget recompose path — avoid for beam) | `jasper/control/server.py:2870–2909, 554–584` |
| restart broker `manage_units`; `jasper-aec-bridge.service` in `MANAGED_UNITS` | `jasper/control/restart_broker.py:104, 518–642` |
| `_aec_full_status` → `build_usb_mic_status` | `jasper/control/aec_endpoints.py:531, 699` |
| Percentile helper `nearest_rank_percentile` (+ `RouteLatencyMetrics`, `metrics_from_samples`; percentile passed as a fraction, e.g. 0.95) | `jasper/cli/route_latency_artifact.py:53, 71, 86` (exported in `__all__`) |
| systemd relay unit (ExecStart, `SupplementaryGroups=audio`, `MemoryMax=48M`, `PartOf=`) | `deploy/systemd/jasper-usbmic.service` |
| apply oneshot (0.35 s debounce → restarts bridge + gadget) | `deploy/systemd/jasper-usbmic-apply.service` |
| `log_event(logger, name, /, *, level=, exc_info=, fields=, **kw)` | `jasper/log_event.py:143` |
| Logging conventions guard | `tests/test_log_event_conventions.py::test_no_unmigrated_event_logger_calls` |
| Env codification guard (`JASPER_USB_MIC_INTENT_PATH`, `JASPER_AEC_CHIP_AEC_PRIMARY_LEG` in `_UNCODIFIED`) | `tests/test_env_vars_codified.py` |
| Relay tests to touch | `tests/test_usb_mic.py` (`test_aplay_sink_applies_the_latency_buffer_contract:170` — DELETE; `test_relay_forwards_nonzero_pcm_unchanged:188` — PRESERVE semantic; `test_usb_mic_transport_uses_one_aec_frame_and_conservative_buffers:164`; `test_host_pcm_status_parser_requires_running_and_reads_hw_ptr:374`) |
| Bridge emitter tests | `tests/test_aec_bridge_stall.py` (`test_aec_loop_emits_both_streams:251`) |
| Doc to rewrite (pipe = open follow-up) | `docs/HANDOFF-usb-gadget.md` "Toggling the computer microphone from `/wake/`" §, ~lines 190–219 |

## Appendix B — Items flagged "[verify on Pi]" (Linux-only; not exercisable on the review Mac)

1. `alsaaudio.PCM(...)` accepting `periodsize`/`plughw:` and its derived buffer size in `pyalsaaudio>=0.11` (design is robust: `/proc` occupancy is the primary governor, not the ALSA buffer).
2. Non-blocking `PCM.write()` return contract on a full buffer (0/short vs raise) — drives the backpressure branch.
3. That writing through the `plughw` plug advances the underlying hw-substream `appl_ptr` faithfully (so `appl_ptr − hw_ptr` from `/proc` is the true gadget-ring fill; measure while writing steadily).
4. `/proc/asound/UAC2Gadget/pcm0p/sub0/status` contains an `appl_ptr:` line adjacent to `hw_ptr:` (standard ALSA; the live probe already read `hw_ptr` from this file).
5. The `jasper-usbmic.service` sandbox (`SupplementaryGroups=audio`, no `PrivateDevices`) permits the in-process `/dev/snd` open (same surface aplay uses today as a child of the unit).
6. `MemoryMax=48M`/`TasksMax=32` still fit after removing the aplay subprocess (expected to *free* memory).
7. Whether CI's Linux runners have `libasound`/pyalsaaudio importable — if not, keep the writer's `import alsaaudio` lazy/injectable and stub it in tests (as the bridge already does with its lazy `import alsaaudio`).