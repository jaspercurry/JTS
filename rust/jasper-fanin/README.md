# jasper-fanin

Per-renderer snd-aloop substream fan-in for JTS. Reads N capture-side
substreams (one per music renderer), sums them sample-wise, writes one
summed-music stream that CamillaDSP and the AEC bridge dsnoop on.

See [`docs/HANDOFF-fan-in-daemon.md`](../../docs/HANDOFF-fan-in-daemon.md)
for the architecture, the resilience + observability contract, and the
4-phase migration plan.

## Build

```sh
cd rust/jasper-fanin
cargo build --release
```

The release binary lands at `target/release/jasper-fanin`. JTS's
`install.sh` builds and copies it to `/opt/jasper/bin/jasper-fanin`
during deploy (Phase 2 chunk 4 ships that wiring).

Build dependencies on Trixie: `libasound2-dev`, `rustc`, `cargo`.
Build takes ~3-5 minutes on a Pi 5.

## Test

```sh
cargo test
```

Hardware-free unit tests only. Integration tests (the systemd unit
shape, the asoundrc references, the doctor checks) live in the Python
pytest suite under `tests/test_renderer_mix_wiring.py` and
`tests/test_fanin_*.py`.

## Status

Skeleton — Phase 2 chunk 1 of [the Tier 2A
plan](../../docs/HANDOFF-fan-in-daemon.md). Today the daemon starts up,
sends READY=1 to systemd, idles bumping its watchdog sentinel once a
second, and exits cleanly on SIGTERM. Chunks 2-4 add the actual mixer,
the UDS state endpoint, and the install/systemd wiring.

## Run manually (for local dev)

```sh
# As-is (idles):
cargo run --release

# With debug logging:
JASPER_FANIN_LOG_LEVEL=debug cargo run --release

# With non-default config:
JASPER_FANIN_OUTPUT_PCM=hw:Loopback,0,7 \
JASPER_FANIN_INPUT_PCMS=hw:Loopback,1,0,hw:Loopback,1,1 \
JASPER_FANIN_INPUT_RENDERERS=spotify,airplay \
cargo run --release
```
