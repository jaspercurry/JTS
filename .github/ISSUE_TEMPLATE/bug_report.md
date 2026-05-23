---
name: Bug report
about: Something isn't working as expected
labels: bug
---

## What happened

(One paragraph: what did you do, what did you expect, what happened
instead. Include the exact wake phrase / voice command / URL / config
change if relevant.)

## How to reproduce

(Numbered steps. The simpler the repro, the faster the fix.)

## Environment

- **Hardware**: (Pi 5 / something else? mic make+model? amp / speaker?)
- **OS**: (Raspberry Pi OS version — `cat /etc/os-release | head -2`)
- **Voice provider**: (gemini / openai / grok / none / unknown)
- **Build SHA**: (from `http://jts.local/system/` or
  `sudo cat /var/lib/jasper/build.txt`)

## Logs

Run `bash scripts/fetch-pi-logs.sh` from a laptop. Attach the relevant
`logs/*-latest.log` (combined-latest.log is usually the most useful).
Redact any tokens or secrets.

If the issue is hardware-free (e.g. test failure, install script
problem), skip the Pi logs and paste the laptop output here directly.

## What you've tried

(Optional but helpful. "I checked `jasper-doctor` and it says X", "I
restarted jasper-voice and the issue persists", etc.)
