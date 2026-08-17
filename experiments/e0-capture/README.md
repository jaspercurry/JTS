# E0: a headless capture client for crossover measurement v2

This is an experimental headless "phone" for the crossover-measurement v2
commissioning flow. It stands in for the browser capture page so a measurement
microphone plugged into a Mac can drive the real Pi-side conductor (CHECK →
MEASURE → VERIFY) with no browser and no phone in the loop. It exists because
an agent-driven lab round should not have to steer a web interface: the
2026-08-16 walk lost four sessions to the browser start race.

**The browser flow stays first-class.** A human driving a commissioning
session uses the capture page, and that path keeps its own investment. This
tool is the lab/agent path only. It is EXPERIMENTAL: tracked so it stops
rotting in an untracked working directory, usable as it stands, and retired
only when the owner says so — not silently deprecated by the next change that
makes it inconvenient.

Provenance and standing: issue
[#2636](https://github.com/jaspercurry/JTS/issues/2636) revived it, and
decision 13's companion ruling in
[`docs/active-speaker-tuning-layers-design.md`](../../docs/active-speaker-tuning-layers-design.md)
is what promoted it here.

[`PROTOCOL.md`](PROTOCOL.md) is the distilled wire contract this client
implements, with `file:line` citations into the Pi-side source and a dated
revival addendum listing the four claims that moved since July. Read it first
if anything in [`e0_capture.py`](e0_capture.py) looks surprising.

## What it talks to

Wire protocol v3, the same one the capture page speaks: mint or accept a
session, fetch the capture spec from the relay, verify its MAC, record each
plan entry with `sox`, encrypt the WAV with the session content key, upload,
and post the authenticated phone events the conductor's position gate rides.
The `remote` tier — the externally-positioned walk this tool drives — is
reachable only by posting `{"tier": "remote"}` at mint, which the wizard's own
chooser never offers.

It reads `crypto.py` and `integrity.py` out of
[`jasper/capture_relay/`](../../jasper/capture_relay/) verbatim, by file path,
so the crypto is never a second implementation. It loads them by path rather
than importing `jasper` because those two modules are stdlib +
`cryptography` only, and importing the package would pull in numpy for
nothing. `E0_CAPTURE_RELAY_DIR` points that read at another checkout.

## Run it

The repo's own virtualenv already has everything the tool imports
(`requests`, `cryptography`, `urllib3`). Recording needs `sox` on PATH and is
macOS-only: the recorder asks for the `coreaudio` device named by `--mic`
(default `UMIK-2`).

The offline self-test is the proof that the client still matches the Pi. It
makes no network call at all — every check round-trips fabricated data
in-process or against the live repo's own validator:

```sh
.venv/bin/python experiments/e0-capture/e0_capture.py --selftest
```

Expect `8 passed, 0 failed`. The checks live in
[`test_e0_capture.py`](test_e0_capture.py), which `--selftest` imports. Two of
them are best-effort and say so in their own output: the real-`sox` smoke
check skips when `sox` is absent, and the live-validator check skips when the
repo's `jasper` package cannot be imported. The same checks run in CI through
[`tests/test_e0_capture_experiment.py`](../../tests/test_e0_capture_experiment.py),
which parametrizes over that module's `TESTS` list — one pytest node per
check — and suppresses the `sox` probe, because a hardware-free lane must not
open the machine's default input.

A no-audio pre-flight validates session mint, spec fetch, and MAC verification
against a live Pi **without** arming a capture, so the speaker plays nothing,
then recovers the session volume:

```sh
.venv/bin/python experiments/e0-capture/preflight_noaudio.py --host jts3.local
```

A live run needs a minted session. Either mint one (`--start-session`, tier
`remote` by default) or accept a tap link the conductor already minted:

```sh
.venv/bin/python experiments/e0-capture/e0_capture.py \
  --start-session --host jts3.local --placement tweeter --run 1

.venv/bin/python experiments/e0-capture/e0_capture.py \
  --tap-link 'https://capture.jasper.tech/#s=...&u=...&k=...&a=...' \
  --placement tweeter --run 1
```

WAVs and the run summary land in `<repo>/captures/e0-corpus/<placement>/`,
which is gitignored; `--outdir` overrides it.
[`reset_run.py`](reset_run.py) does the scoped journey reset between runs, and
[`overnight_runs.sh`](overnight_runs.sh) is the worked example of an
unattended series (its mic names, placements, and spacing are one session's —
edit them, do not inherit them).

## Safety boundary

`--start-session` and `--tap-link` reach a live Pi and the live relay, and the
session they open makes the speaker play measurement sweeps at commissioning
level. Only a human hardware operator coordinating a live run invokes them —
never a background or automated context. `--selftest` is the only mode that
touches nothing.

`overnight_runs.sh` gates every run on `FLOOR_EPOCH`, a quiet-hours floor. The
shipped value is a past date, so the gate passes today; export a future epoch
to re-arm it.

## Known residual risk

**The setup wire payload may have drifted, and this client has not been
proven on hardware since.** `e0_capture.py` mirrors the capture page's
`setupWirePayload()` as of `856903ca1`; #1959, #1977, and #2035 touched
capture-page screens after that. The 2026-08-16 revival re-read the contract
offline and moved four claims (see PROTOCOL.md's addendum), but offline
reading is not a hardware run. If the payload shape moved in a way that
reading missed, the first `begin_capture` fails LOUDLY — the Pi refuses it —
so the failure is one debug cycle at the start of a round, not a corrupted
capture. Budget for it on the first hardware series that uses this tool; that
series is what turns the risk into a fact either way.

One diagnostic is knowingly absent: this client sends no `capture_integrity`
sidecar (#2151). That field is optional and never validated, so the loss is a
diagnostic, not a capture.
