# XVF3800 ch2-5 silence: the pre-resolution ranking (2026-05)

> **Status: historical.** The ranked-hypothesis analysis written during the
> 2026-05-15 jts2 investigation, before the root cause was found. The cause was
> host-side: the ALSA kernel mixer had ch2-5 muted, persisted across reboot by
> `alsactl restore`. `jasper-aec-reconcile` self-heals it via
> `ensure_capture_mixer_open` and doctor flags drift, so the resolved
> root cause and its fix live in
> [HANDOFF-xvf3800.md](../HANDOFF-xvf3800.md) § 7.
>
> Kept because the ranking covers failure modes that root cause does not: if
> ch2-5 go silent again for a different reason, start here.

---

### Original ranking (pre-resolution)

Given the evidence in the issue prompt:

- Same chip + same 6-ch firmware (`v2.0.8`, hash
  `a1f70651e992d6f0bcff655b26925d33999b9c2d`)
- Ch0/1 have signal, ch2-5 are literal zeros
- `SHF_BYPASS=1` saturates ch0/1 but **ch2-5 still zero**
- `MIC_GAIN=90` (default)
- `SELECTED_CHANNELS=[3,3]` (default)
- `OP_PACKED=[0,0]` (default)
- `BOOT_STATUS='Jof'`
- Works on jts.local with the same firmware

Some of these confirm the silent-channel mechanism cannot be host-controllable parameter state (because ch2-5 routing is fixed in firmware on stock 6-ch — see §3). So the fault is either upstream of the output mux in the chip itself, or on the host-side USB data plane.

Ranked hypotheses, most → least likely:

### 7.1 **USB bandwidth / endpoint truncation at the host** (most likely)

**Why:** The symptom — channels delivered in order, with later
channels truncated to silence — is exactly what happens when a
UAC2 isoc IN endpoint underruns or the host can't keep up with
the negotiated packet size. ch0-1 fit in roughly the same packet
budget as the 2-ch firmware, so they're delivered cleanly; ch2-5
are the "marginal" addition and fail first.

This is consistent with the host being a 1 GB Pi 5 (less DMA
headroom than the 2 GB jts.local) and potentially a different
USB cable / hub topology.

**Diagnostic moves (host side):**

```sh
# 1. Confirm chip is on USB 2.0 High-Speed (480M), not Full-Speed (12M)
lsusb -t | grep -B3 2886
#   Expect "480M" near the chip's row.

# 2. Look for kernel-level USB errors
sudo dmesg -T | grep -i -E 'usb|audio|underrun|xhci|dwc' | tail -100

# 3. Are the bytes actually arriving? Capture raw and check per-channel content
arecord -D plughw:Array,0 -c 6 -r 16000 -f S16_LE -d 5 /tmp/raw6.wav
ffmpeg -i /tmp/raw6.wav -map_channel 0.0.0 /tmp/ch0.wav \
                       -map_channel 0.0.1 /tmp/ch1.wav \
                       -map_channel 0.0.2 /tmp/ch2.wav \
                       -map_channel 0.0.3 /tmp/ch3.wav \
                       -map_channel 0.0.4 /tmp/ch4.wav \
                       -map_channel 0.0.5 /tmp/ch5.wav
for c in 0 1 2 3 4 5; do
    sox /tmp/ch$c.wav -n stat 2>&1 | grep -E 'RMS|Maximum amp'
done

# 4. Try a different USB port (avoid hubs; XVF chip into a Pi USB-A port directly)
# 5. Try a different USB cable (rule out a marginal C-to-A or C-to-C cable)
```

If the symptom moves with the cable or the port, it's USB
bandwidth / electrical and the chip is fine.

### 7.2 **DataPartition has cached state from a prior `SAVE_CONFIGURATION`** (medium-likely)

**Why:** Even though ch2-5 routing is "not host-controllable on
stock 6-ch firmware," that's true only of routing — the
DataPartition can persist GAIN settings (MIC_GAIN, REF_GAIN), AGC
state, SHF bypass, and the AUDIO_MGR_OP_L/R routing for ch0/1.
What it CAN'T directly persist is "ch2-5 muted" (no parameter to
do that). But if the partition is corrupted, the chip's data
plane behaviour is undefined — including potentially silencing
later USB capture slots.

This is most likely if jts2 was previously used for some XMOS
demo (e.g. someone followed the official "Output Selection"
wiki and called `SAVE_CONFIGURATION` to persist a routing
change).

**Diagnostic boundary:** keep ordinary diagnosis read-only. A managed XVF
with bad capture/profile state stays parked while `jasper-doctor`, mixer
readback, firmware identity, and USB descriptors identify the fault. Do not
call `REBOOT` or `CLEAR_CONFIGURATION` as probes; the foreground commissioner
owns the sole volatile reset. Confirmed DataPartition corruption uses the
explicit DFU recovery procedure in §5.1. Never call `SAVE_CONFIGURATION`.

### 7.3 **The chip's PDM decimator path for mic 0-3 raw output is faulty** (lower likelihood)

The chip has separate data paths from the PDM decimator to (a)
the SHF cores (which feed ch0/1) and (b) the raw-output mux
(which feeds ch2-5 on 6-ch firmware). The fact that ch0/1 work
proves the PDM decimator output is fine for **the SHF input
path**. But the raw-output mux on the 6-ch firmware is its own
data plane — added in v2.0.8 as a new feature — and could in
principle fail without affecting ch0/1.

If this is the case, a volatile reset would not fix it (live-state parameters
are not involved), nor would clearing parameter defaults. Re-flashing the same
firmware **could** fix it (if the
issue is corrupted firmware bits in the run-time partition), or
not (if the chip silicon itself has a latch-up condition the
firmware can't clear).

The most useful diagnostic is **re-flashing v2.0.8 6chl** and
re-testing. If symptom persists, swap to the 2-ch firmware and
verify ch0/1 still work; that confirms the chip is at least
partially fine. This is a hardware diagnostic only: 2-channel firmware is
unsupported for the managed product route, so voice remains parked rather
than falling back to either channel.

### 7.4 **Different revision of v2.0.8 6chl firmware between the two Pis** (less likely but cheap to rule out)

The prompt says same `BLD_REPO_HASH=a1f70651e992d6f0bcff655b26925d33999b9c2d`
on both Pis. That's a strong signature — same sw_xvf3800 commit.
But XMOS does sometimes ship "same hash, different binary" if
config files change without the source tree changing.

```sh
# Compare the actual .bin files used to flash, byte for byte
ssh pi@jts.local sudo find / -name 'respeaker_xvf3800_*' 2>/dev/null
ssh pi@jts2.local sudo find / -name 'respeaker_xvf3800_*' 2>/dev/null
# Get both files local and `cmp -s` them.
```

If hashes match, this is eliminated.

### 7.5 **PDM mic clock or DC-bias hardware fault on one channel of jts2's mic array** (low likelihood, but possible)

PDM mics on the XVF3800 are arranged so the 4 mics share clock
lines but have independent data lines. A solder defect on the
data line for mics 0-3 would silence all four (since they share
the same PDM clock). BUT — that would also kill the SHF cores'
input, which would kill ch0/1 too. So this is unlikely UNLESS
the chip has a hardware bypass somewhere that runs the SHF
cores from a separate (e.g. simulated / test-tone) input path,
which isn't documented anywhere we've seen.

Lowest priority for investigation, but if everything else is
ruled out, swapping the XVF board between the two Pis is the
definitive A/B test.

### 7.6 Catch-all: kernel/ALSA driver state vs. PortAudio enumeration

It's worth ruling out at the kernel/ALSA layer that the audio
stack actually believes the device has 6 channels. If ALSA only
exposes 2 channels (maybe the kernel UAC2 driver detected the
endpoint as 2-channel for some reason), the "channels 2-5
silent" interpretation may be misleading — they could be
absent entirely.

```sh
# What does the kernel think?
cat /proc/asound/Array/stream0
# Channels: 6   <- required for 6-ch firmware
#   If 2, ALSA hasn't seen the 6-ch endpoint.

# Re-plug the chip and check dmesg for the descriptor parse
sudo dmesg -T | grep -A5 -B2 -i 'XVF3800\|reSpeaker\|2886:001a' | tail -40
```

If `/proc/asound/Array/stream0` shows `Channels: 2` on jts2 while
the chip says it's running 6-ch firmware via `VERSION` and
`BLD_REPO_HASH`, that's a **firmware↔kernel disagreement**, which
points back to either §7.1 (USB bandwidth — the kernel couldn't
allocate ISO endpoints for 6 channels) or §7.4 (different firmware
binary actually running, despite hash match).

---
