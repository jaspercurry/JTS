# jasper-fanin

Per-renderer snd-aloop substream fan-in for JTS. Reads N capture-side
substreams (one per music renderer), sums them sample-wise, writes one
summed-music stream that CamillaDSP and the AEC bridge dsnoop on.

See [`docs/HANDOFF-fan-in-daemon.md`](../../docs/HANDOFF-fan-in-daemon.md)
for the architecture, the resilience + observability contract, and the
historical migration plan.

## Build

```sh
cd rust/jasper-fanin
cargo build --release
```

The release binary lands at `target/release/jasper-fanin`. JTS's
`install.sh` builds and copies it to `/opt/jasper/bin/jasper-fanin`
during deploy.

Build dependencies on Trixie: `libasound2-dev`, `rustc`, `cargo`.
Build takes ~3-5 minutes on a Pi 5.

## Test

```sh
cargo test
```

Hardware-free unit tests only. Integration tests (the systemd unit
shape, the asoundrc references, the doctor checks) live in the Python
pytest suite under `tests/test_fanin_*.py` and `tests/test_doctor.py`.

## Status

Production default as of 2026-05-26. The daemon opens renderer capture
lanes, sums active inputs into the dedicated summed-output substream,
exposes STATUS over `/run/jasper-fanin/control.sock`, logs xruns to
`/var/lib/jasper/fanin/xrun_history.jsonl`, and participates in systemd
watchdog supervision.

## Run manually (for local dev)

```sh
cargo run --release

# With debug logging:
JASPER_FANIN_LOG_LEVEL=debug cargo run --release

# With non-default config:
JASPER_FANIN_OUTPUT_PCM=hw:Loopback,0,7 \
JASPER_FANIN_INPUT_PCMS='hw:Loopback,1,0|hw:Loopback,1,1' \
JASPER_FANIN_INPUT_RENDERERS='spotify|airplay' \
cargo run --release
```
