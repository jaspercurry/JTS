# Prompt for next session — AEC wake-rate re-test after bridge fixes

Paste the section below into a fresh Claude Code session in this repo:

---

I want to run the Phase 2 wake-rate test on the JTS speaker again. Yesterday three bugs in the AEC bridge's reference-signal path were fixed (PRs #150, #154, #157) and verified working. The bridge now feeds AEC3 a continuous, full-bandwidth reference for the first time. Before today, every "AEC ON" wake-rate measurement was corrupted by those bugs — only the chip-direct ("AEC OFF") legs were valid.

**Read these first to ground yourself**:
- `docs/HANDOFF-aec.md` — especially the TL;DR callout and "Bridge ref starvation bug — fixed (2026-05-19)" section
- Memory note `project_aec_software_upgrade_planned.md` — current bridge tuning + the re-test protocol

**What I want to do**: same Phase 2 protocol we used yesterday (v6/v7/v8). Three captures at SHF_BYPASS=1 (production chip state) with consistent music and consistent phone volume, then compare AEC ON vs AEC OFF within each capture.

**Setup I'll do**:
- AC off, blinds closed
- Pink Floyd "Money" via AirPlay at my normal home listening volume (whatever feels right)
- Phone playing `logs/wake-test-track/wake-test-track.m4a` at a fixed volume I don't touch between captures
- Phone placed in a consistent spot near the JTS mic

**Walk me through the protocol step by step.** I'd like minimal thinking on my end — just commands to run. The wake-rate test script (`scripts/wake-rate-test.sh`) handles bridge debug-record, capture, offline detection, and pass/fail analysis. Each capture is ~140 s wall time.

**Before we start, please verify**:
1. The bridge is healthy: `ssh pi@jts.local "systemctl is-active jasper-aec-bridge jasper-voice jasper-camilla"` — all should be "active"
2. The latest code is deployed: check `JASPER_GIT_SHA` in `http://jts.local/system/` matches `origin/main` head (or run `bash scripts/deploy-to-pi.sh` if not)
3. Optional but reassuring: run `bash scripts/verify-ref-no-silence-bug.sh` while music is playing — should report PASS with 0% silent frames

**What to compare against**:

Pre-fix Phase 2 baseline (do not cite the AEC ON column — it was corrupted):

| Capture | AEC ON (corrupted) | AEC OFF (valid baseline) |
|---|---|---|
| v2 (clean conditions) | 35% | 60% |
| v3 | 30% | 20% |
| v4 | 0% | 5% |
| v5 | 10% | 20% |
| v6_postfix (resampler only) | 20% | 30% |
| v7_postfix | 30% | 30% |
| v8_postfix | 25% | 20% |
| v6-v8 avg | 25% | 27% |

The AEC OFF column (chip-direct) is the floor we're comparing against. The post-fix "AEC ON" numbers should now reflect AEC's actual contribution for the first time.

**Tooling available** (`scripts/`):
- `wake-rate-test.sh` — main capture + offline analysis
- `make-wake-test-track.sh` / `_make_wake_test_track.py` — generate the 20-Jarvis TTS track (already exists in `logs/wake-test-track/`)
- `_offline_wake_count.py` — per-utterance metadata with template-based cross-correlation; reports detected / near-miss / weak-signal / silent-miss categories + RMS + xcorr per utterance
- `aec-erle-record.sh` / `aec_erle_analyze.py` — ERLE measurement if useful
- `verify-ref-no-silence-bug.sh` — sanity check that bridge fixes are active

**What I don't want to do tonight unless results indicate it**:
- Re-investigate chip AEC (SHF_BYPASS=0). Documented as rejected. Re-litigate only if post-fix wake-rate is still net-negative.
- Engine swap (DTLN-aec). Multi-day project. Only if the current engine is clearly not enough after this fix.
- Band-limiting experiments (tighter HPF / new LPF). Deferred until we know what AEC alone can do.

Lay out the protocol step-by-step, verify the system is healthy, then I'll run the captures. When I send result paths back to you, analyze them and lay out the picture. Be honest if the result is "AEC still doesn't help" — that's a real possible outcome and tells us where to look next.

---

End of prompt.

## Notes for me (Jasper) before starting the new session

- You'll need to be in this worktree: `/Users/jaspercurry/Code/JTS/.claude/worktrees/upbeat-archimedes-dce80e` (or wherever you're testing from). Otherwise the script paths won't resolve.
- If the `wake-test-track.m4a` isn't on your phone anymore, the prompt above directs the AI to regenerate it via `scripts/make-wake-test-track.sh` — that requires the Pi's OpenAI API key to be live in `/etc/jasper/jasper.env` and takes ~1 minute.
- Once you've done the captures, the AI will analyze and likely propose follow-ups based on what the numbers say.
- All pre-fix capture data is preserved under `logs/wake-rate/` and `logs/listening-comparison/`. Don't delete those — they're our reference baseline for the chip-direct floor.
