# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Static lint-policy guards.

These tests do not replace Ruff. They pin the project-level lint contract
that lets Ruff's `BLE001` suppressions be load-bearing while the existing
suppression debt is paid down over time.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOTS = ("jasper", "tests", "scripts", "deploy")

# Ratchet counts after enabling Ruff's BLE rules on 2026-06-18. Lowering
# either number is welcome; raising one means new suppression debt landed.
# 2026-06-20 (+4 suppression markers, all blind-except): distributed-active
# Slice 3 added fail-soft boundaries to the grouping reconcile path (the
# active-follower readiness gate, the active-solo restore, the CamillaDSP swap,
# and the defensive is_active_speaker_box topology probe) — each is a "never
# crash the reconcile / fail safe to solo" handler matching the existing
# reconciler idiom.
# 2026-06-21 (+1 suppression marker, blind-except): the bonded-leader AirPlay
# latency-fit /state snapshot (jasper/multiroom/airplay_latency.py) carries the
# same fail-soft "observability must never break /state" guard every sibling
# /state section does.
# 2026-06-21 (+1 suppression marker, blind-except): the OpenAI barge-in pack's
# truncate_assistant_audio wraps the conversation.item.truncate wire send so
# the LiveTurn seam can honour its "must never raise" contract while still
# surfacing the failure as event=barge.truncate_failed (WARN) — the same
# guarded-wire-send idiom as the adjacent _cancel_response.
# 2026-06-22 (+2 suppression markers, blind-except): distributed-active Stage B
# (active leader, Slice 5) added two fail-soft boundaries to the grouping
# reconcile path — the active-leader camilla#1 program-bake apply + camilla#2
# re-seed, and the unbond active-leader restore — each a "never crash the
# reconcile / fail safe to solo" handler matching the existing reconciler idiom.
# 2026-06-27 (+1 suppression marker, blind-except): PR #1051's /sound topology
# revision-compare-and-write TOCTOU fix wraps the critical section in a
# fail-soft `except BaseException` guard ("surface unexpected failures"). The
# baseline was not bumped when it merged, so main went red on this contract
# (count 622 vs ceiling 621) — reconcile the count here. The suppression is the
# established "never crash the critical write path" idiom; lowering the count by
# narrowing it later is welcome.
# 2026-06-27 (+2 suppression markers, blind-except): PR #1073's 4b-iv lean-lane
# mux wiring adds two fail-loud broad-except handlers in the enter/leave-lean
# ladders — catch-broad -> fall back to the buffered lane + log, the established
# "never crash the _tick / fail safe to buffered" idiom. Justified suppression
# debt for the new resilience path; the same two markers push BOTH ceilings by 2
# (each is a blind-except suppression). Narrowing later is welcome.
# 2026-06-27 (+5 suppression markers, of which +3 blind-except): the phone-mic
# capture relay (jasper/capture_relay/*) adds 2 urllib outbound-HTTPS-only
# suppressions (S310, guarded by an https-scheme check in client.py/health.py)
# and 3 blind-except suppressions in session.py — the no-silent-failure design:
# cue on ANY failure then re-raise, a best-effort cue that must not mask the real
# exception, and a best-effort purge (TTL is the backstop). All reviewed; one
# further best-effort handler was narrowed to a typed except rather than
# suppressed. (Marker strings are spelled out here, not written literally, so
# this very comment does not inflate the count it documents.)
# 2026-06-27 (+1 blind-except): the /correction/ relay-capture daemon adapter
# (jasper/web/correction_setup.py POST /relay/capture) adds one fail-loud
# never-crash-the-background-loop handler around the async capture runner —
# logs + surfaces the failure in /status, mirrors the existing
# _schedule_measurement_sweep idiom in the same file. Pushes BOTH ceilings by 1.
MAX_NOQA_MARKERS = 809
MAX_BLE001_MARKERS = 628
# (Total reflects two independent +1 entries dated 2026-06-21: the AirPlay
# latency-fit /state snapshot and the barge-in truncate wire-send guard.)

_BROAD_EXCEPT = re.compile(
    r"^\s*except (?:BaseException|Exception)(?: as [A-Za-z_][A-Za-z0-9_]*)?:"
)


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        base = REPO / root
        if not base.exists():
            continue
        files.extend(sorted(base.rglob("*.py")))
    return files


def test_ruff_ble_rule_is_enabled() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    selected = set(pyproject["tool"]["ruff"]["lint"]["select"])

    assert "BLE" in selected


def test_broad_exception_suppressions_are_explicit() -> None:
    missing: list[str] = []
    for path in _python_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _BROAD_EXCEPT.match(line) and "# noqa: BLE001" not in line:
                missing.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()}")

    assert not missing, (
        "Broad Exception/BaseException handlers must either catch a narrower "
        "exception or carry an explicit `# noqa: BLE001` suppression marker:\n"
        + "\n".join(missing)
    )


def test_noqa_debt_does_not_grow() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in _python_files())

    assert text.count("# noqa") <= MAX_NOQA_MARKERS
    assert text.count("BLE001") <= MAX_BLE001_MARKERS
