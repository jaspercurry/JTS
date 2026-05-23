## Summary

(1-3 sentences on what changes and why. Focus on the why — the diff
shows the what.)

## Subsystems touched

(List the subsystem(s) and corresponding `docs/HANDOFF-*.md` file(s)
your PR is relevant to.)

- [ ] I scanned the related HANDOFF doc(s) and updated them inline if
  I found anything stale.

See `scripts/doc-freshness.sh` for HANDOFFs overdue for a verification
pass.

## Test plan

- [ ] `pytest` passes locally
- [ ] Hardware-tested on a Pi (`jasper-doctor` clean), **or** explain
  why this change can't be hardware-tested
- [ ] If voice-eval was run: cost was approximately $___ — see
  AGENTS.md "Voice-eval cost discipline" for the discipline. Don't
  run voice-eval if you can't justify the dollar figure.

## Notes for reviewer

(Anything non-obvious: design trade-offs, things you intentionally
didn't do, follow-up work, surprising files in the diff.)
