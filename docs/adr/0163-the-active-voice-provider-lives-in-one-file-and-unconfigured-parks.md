# ADR-0163: The active voice provider lives in exactly one file, has no default, and an unconfigured speaker parks

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

`JASPER_VOICE_PROVIDER` selects which realtime backend `jasper-voice` opens.
It was once settable in two places — the wizard-owned
`/var/lib/jasper/voice_provider.env` and the template
`/etc/jasper/jasper.env` — and that produced exactly the confusion two homes
for one value always produce: the file an operator edited was not the file
the daemon read, and diagnosing "I switched to OpenAI and it is still on
Gemini" cost real time.

A default value carries a different hazard. A fresh install with a baked-in
default starts a voice daemon pointed at a provider whose key the household
has not entered, so the speaker fails at first wake rather than at setup,
and it fails as a crash: repeated restarts consume the unit's crash budget
and eventually trip `StartLimitAction=reboot`, turning a missing config into
a reboot loop.

## Decision

**One file, no default, park when unconfigured.**

- The active provider lives only in `/var/lib/jasper/voice_provider.env`.
  The `/voice/` wizard is its single writer; `jasper-voice.service` sources
  it via `EnvironmentFile=`; `install.sh` actively migrates any stale value
  out of `/etc/jasper/jasper.env` on every run.
- There is no fallback default. `Config.from_env` raises
  `VoiceProviderNotConfigured` and `jasper-voice` exits `EX_CONFIG` (78),
  with `SuccessExitStatus=78` + `RestartPreventExitStatus=78` on the unit so
  the park stays out of the crash budget. Real crashes keep
  `Restart=on-failure` and the existing reboot-escalation path.
- The pre-daemon reconciler gates on a **generated projection**, not a
  hand-maintained list: `install.sh` renders
  `/var/lib/jasper/voice_provider_ids` from the catalog, and
  `jasper-aec-reconcile` accepts a provider only when it is an exact line in
  that file. Missing or stale manifest → park, never start voice on an
  unrecognized provider.
- Every consumer that is not `jasper-voice` reads the file **fresh** through
  `provider_state.py`, never `os.environ` (frozen at daemon start), and gets
  `""` for unset or invalid rather than a guess.

## Consequences

- "Which provider is this speaker on?" has exactly one answer, and switching
  it is one write plus one restart.
- A never-configured speaker sits quietly in a parked state that the wizard
  and `jasper-doctor` can explain, instead of reboot-looping.
- Diagnostics can tell *why* no provider was read —
  `read_active_provider_state()` distinguishes configured, unset, missing,
  unreadable, and invalid — so a permission-denied probe is not reported as
  first-time setup.
- The generated manifest means adding a provider needs no shell allow-list
  edit; forgetting to regenerate it fails closed rather than open.
- Deliberately given up: the convenience of a working default on a fresh
  install. Setup is a wizard step, and silence with an explanation beats a
  speaker that wakes and cannot answer.
