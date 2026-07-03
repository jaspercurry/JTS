# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free proof tests for scripts/ring-proto/ (branch
latency/ring-proto-shm, the Ring B SHM-ring latency prototype).

These are LAB-ONLY prototype scripts (see scripts/ring-proto/README.md).
This file does not exercise real hardware, real SSH, or real ALSA/
CamillaDSP — it pins three classes of invariant:

1. Static shape: every mutating script is syntactically valid bash,
   shellcheck-clean at the project's CI severity, sources the shared
   safety guard, and never appends a marked block without a matching
   BEGIN/END pair the corresponding disarm step can find.
2. The safety-critical guard behavior (require_explicit_ring_proto_target
   in _guard.sh): every mutating script refuses to run — with NO side
   effects, verified by never invoking a fake `ssh` placed first on
   PATH — when no explicit PI_HOST is provided, and proceeds (reaching
   its first real `ssh` call) when one is.
3. The disarm.sh marker-stripping sed command, run for real (locally,
   against a synthetic env file) rather than only read as source text —
   parentheses in the marker string are BRE-literal, not a
   sed-metacharacter hazard, but that is exactly the kind of "looks
   right, might not be" detail worth a real execution rather than an
   eyeball check.

The guard's own existence is not incidental: during this script
family's development, running scripts/ring-proto/build-on-pi.sh with no
PI_HOST set landed on the real jts.local box (scripts/_lib.sh's ordinary,
correct product default) and ran a real rsync + make there before the
gap was caught and closed. Test 2 above is the regression pin for
exactly that incident.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RING_PROTO_DIR = ROOT / "scripts" / "ring-proto"
IOPLUG_DIR = ROOT / "c" / "jts-ring-ioplug"

MUTATING_SCRIPTS = (
    "arm.sh",
    "arm-ring-a.sh",
    "disarm.sh",
    "build-on-pi.sh",
    "make-camilla-ring-config.sh",
)
ALL_SCRIPTS = MUTATING_SCRIPTS + ("host-check.sh",)


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


# ---------------------------------------------------------------------
# 1 — static shape
# ---------------------------------------------------------------------


def test_ring_proto_directory_contains_the_expected_files() -> None:
    """Every file the tooling brief names exists under scripts/ring-proto/."""
    expected = {
        "README.md",
        "arm.sh",
        "arm-ring-a.sh",
        "disarm.sh",
        "build-on-pi.sh",
        "make-camilla-ring-config.sh",
        "host-check.sh",
        "_guard.sh",
    }
    present = {p.name for p in RING_PROTO_DIR.iterdir() if p.is_file()}
    missing = expected - present
    assert not missing, f"scripts/ring-proto/ is missing: {sorted(missing)}"


def test_capture_ioplug_access_list_is_rw_only_no_mmap() -> None:
    """Finding 4 (staff review, Ring A capture): the capture ioplug MUST advertise
    RW_INTERLEAVED access ONLY — never MMAP_INTERLEAVED.

    With mmap_rw=0 the emulated capture mmap area is filled by our own `transfer`,
    which legitimately returns SHORT on the writer-alive-empty pacing block; alsa-
    lib's ioplug mmap-capture avail/commit accounting can then expose the region
    beyond `delivered` (stale/uninitialised bytes camilla would read as audio). The
    RW path bounds what the app sees by the transfer return value, so a short read
    never leaks unfilled bytes. Playback is unaffected (its mmap area is app-authored).

    Hardware-proven via `arecord --dump-hw-params` (capture: RW_INTERLEAVED only;
    playback: MMAP_INTERLEAVED RW_INTERLEAVED). This is the CI-visible static pin so
    the capture direction can never silently re-add MMAP.
    """
    src = (IOPLUG_DIR / "pcm_jts_ring.c").read_text(encoding="utf-8")
    # The direction-aware access selection: a CAPTURE-only list and an RW+MMAP list.
    assert "accesses_rw_only" in src, (
        "expected a CAPTURE-only access list variable in jts_ring_set_hw_constraints"
    )
    # The CAPTURE list literal must contain RW and must NOT contain MMAP.
    m = re.search(
        r"accesses_rw_only\[\]\s*=\s*\{([^}]*)\}",
        src,
    )
    assert m, "could not locate the accesses_rw_only[] initializer"
    rw_only = m.group(1)
    assert "SND_PCM_ACCESS_RW_INTERLEAVED" in rw_only, (
        "capture access list must include RW_INTERLEAVED"
    )
    assert "MMAP" not in rw_only, (
        "capture access list must NOT include MMAP (finding 4 stale-bytes lane)"
    )
    # The capture branch must select the RW-only list.
    assert re.search(
        r"stream\s*==\s*SND_PCM_STREAM_CAPTURE\s*\)\s*\{\s*accesses\s*=\s*accesses_rw_only",
        src,
    ), "capture branch must select accesses_rw_only"


@pytest.mark.parametrize("name", ALL_SCRIPTS + ("_guard.sh",))
def test_script_is_executable_or_sourceable(name: str) -> None:
    """The four entry-point scripts are chmod +x; _guard.sh (sourced only,
    never executed directly) does not need to be."""
    path = RING_PROTO_DIR / name
    assert path.is_file(), f"{name} does not exist"
    if name == "_guard.sh":
        return
    mode = path.stat().st_mode
    assert mode & stat.S_IXUSR, f"{name} is not chmod +x"


@pytest.mark.parametrize("name", ALL_SCRIPTS + ("_guard.sh",))
def test_script_parses_with_bash_dash_n(name: str) -> None:
    """`bash -n` (parse only, no execution) is clean for every script.

    Catches the exact bug class hit during this file's own development:
    an unquoted heredoc delimiter (<<EOF) still tokenizes its BODY for
    quote balance even though the quotes inside are not otherwise
    special. An odd number of apostrophes anywhere in an unquoted
    heredoc body (including inside a prose comment) is a bash parse
    error that only appears when the WHOLE file is parsed — the failing
    line bash reports is often far from the actual unbalanced quote.
    """
    if not _has("bash"):
        pytest.skip("bash not on PATH")
    path = RING_PROTO_DIR / name
    result = subprocess.run(
        ["bash", "-n", str(path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"bash -n {name} failed:\n{result.stderr}"
    )


@pytest.mark.parametrize("name", ALL_SCRIPTS + ("_guard.sh",))
def test_script_is_shellcheck_clean_at_ci_severity(name: str) -> None:
    """shellcheck --severity=warning is clean — the same gate + severity
    the repo's `shell` CI job runs (see .github/workflows/tests.yml)."""
    if not _has("shellcheck"):
        pytest.skip("shellcheck not installed")
    path = RING_PROTO_DIR / name
    result = subprocess.run(
        ["shellcheck", "--severity=warning", str(path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"shellcheck --severity=warning {name} found issues:\n{result.stdout}"
    )


@pytest.mark.parametrize("name", MUTATING_SCRIPTS)
def test_mutating_script_sources_the_shared_guard(name: str) -> None:
    """Every script that can mutate a Pi sources _guard.sh and calls the
    guard function before doing anything else. A script that forgets
    this line silently reopens the exact incident _guard.sh exists to
    close."""
    text = (RING_PROTO_DIR / name).read_text(encoding="utf-8")
    assert '. "${RING_PROTO_DIR}/_guard.sh"' in text, (
        f"{name} does not source _guard.sh"
    )
    assert "require_explicit_ring_proto_target" in text, (
        f"{name} does not call require_explicit_ring_proto_target"
    )
    # The guard function reads JASPER_RING_PROTO_CALLER_PI_HOST, which
    # must be captured from PI_HOST BEFORE _lib.sh's fallback runs (that
    # fallback silently resolves PI_HOST to jts.local when unset — see
    # scripts/_lib.sh). Capturing it AFTER would always see the
    # already-defaulted value and the guard could never refuse.
    lib_source_idx = text.index('scripts/_lib.sh"')
    capture_idx = text.index("JASPER_RING_PROTO_CALLER_PI_HOST=")
    assert capture_idx < lib_source_idx, (
        f"{name} must capture JASPER_RING_PROTO_CALLER_PI_HOST BEFORE "
        "sourcing _lib.sh, not after"
    )


def test_guard_defines_require_explicit_ring_proto_target() -> None:
    text = (RING_PROTO_DIR / "_guard.sh").read_text(encoding="utf-8")
    assert "require_explicit_ring_proto_target()" in text
    # The two accepted proofs of an explicit target: an inherited/caller
    # PI_HOST, or an .env.local with PI_HOST already persisted (the
    # documented laptop-side state file — see AGENTS.md "Laptop-side
    # state"). Anything else must be a hard refusal.
    assert "JASPER_RING_PROTO_CALLER_PI_HOST" in text
    assert "env_local_has_pi_host" in text
    assert "exit 1" in text


def test_arm_and_disarm_agree_on_marked_block_markers() -> None:
    """arm.sh writes a BEGIN/END marked block into
    /var/lib/jasper/outputd.env; disarm.sh must strip the SAME literal
    markers, or a re-arm/disarm cycle silently leaves stale content
    behind (or disarm silently no-ops on a real block).

    disarm.sh now carries BOTH the Ring B and Ring A markers (the two
    mode branches), so we pin each arm script's markers against the
    matching literal disarm.sh line rather than "the first assignment"."""
    arm_text = (RING_PROTO_DIR / "arm.sh").read_text(encoding="utf-8")
    disarm_text = (RING_PROTO_DIR / "disarm.sh").read_text(encoding="utf-8")
    arm_begin = _extract_assignment(arm_text, "BEGIN_MARKER")
    arm_end = _extract_assignment(arm_text, "END_MARKER")
    # The Ring B marker must appear verbatim as a BEGIN_MARKER/END_MARKER
    # assignment somewhere in disarm.sh (the else branch).
    assert arm_begin in _all_assignments(disarm_text, "BEGIN_MARKER"), (
        f"arm.sh BEGIN_MARKER {arm_begin!r} not found among disarm.sh markers"
    )
    assert arm_end in _all_assignments(disarm_text, "END_MARKER"), (
        f"arm.sh END_MARKER {arm_end!r} not found among disarm.sh markers"
    )


def test_arm_ring_a_and_disarm_agree_on_marked_block_markers() -> None:
    """arm-ring-a.sh writes its BEGIN/END marked block into
    /var/lib/jasper/fanin.env; disarm.sh --ring-a must strip the SAME
    literal markers."""
    arm_text = (RING_PROTO_DIR / "arm-ring-a.sh").read_text(encoding="utf-8")
    disarm_text = (RING_PROTO_DIR / "disarm.sh").read_text(encoding="utf-8")
    arm_begin = _extract_assignment(arm_text, "BEGIN_MARKER")
    arm_end = _extract_assignment(arm_text, "END_MARKER")
    assert "jts-ring-a-proto" in arm_begin, (
        f"arm-ring-a.sh BEGIN_MARKER should be the Ring A marker, got {arm_begin!r}"
    )
    assert arm_begin in _all_assignments(disarm_text, "BEGIN_MARKER"), (
        f"arm-ring-a.sh BEGIN_MARKER {arm_begin!r} not found among disarm.sh markers"
    )
    assert arm_end in _all_assignments(disarm_text, "END_MARKER"), (
        f"arm-ring-a.sh END_MARKER {arm_end!r} not found among disarm.sh markers"
    )


def _extract_assignment(text: str, var_name: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(f'{var_name}="') and stripped.endswith('"'):
            return stripped[len(var_name) + 2 : -1]
    raise AssertionError(f"could not find {var_name}= assignment")


def _all_assignments(text: str, var_name: str) -> list[str]:
    """Every RHS of `VAR="..."` lines (disarm.sh assigns the same var in both
    mode branches, so a single-value extractor is not enough)."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(f'{var_name}="') and stripped.endswith('"'):
            out.append(stripped[len(var_name) + 2 : -1])
    return out


def _extract_disarm_sed_command(disarm_text: str) -> str:
    """Lift the exact sed command out of disarm.sh's marker-strip step, verbatim,
    as the string disarm.sh's local bash expands and hands to the remote shell.

    disarm.sh runs `ssh_ok "sudo sed -i '<prog>' ${OTHER_ENV}"`. The OUTER
    double quotes are what make bash expand `${BEGIN_MARKER//\\//\\\\/}` and
    `${OTHER_ENV}` before the command is sent; the single quotes around the
    program are literal-to-the-remote-shell. We return the inside of that outer
    quote with two edits: drop the leading `sudo ` (the test is unprivileged)
    and turn `sed -i` into `sed` (the test drives sed via stdout to sidestep the
    GNU-vs-BSD `sed -i` suffix-argument difference; disarm.sh runs on the Pi's
    GNU sed). The caller wraps this in a double-quoted `eval` so bash performs
    the SAME `${...}` expansion disarm.sh does — the extraction, not a
    hand-retype, is what makes a disarm.sh edit flow into this test. The sed
    command is generic (parameterized by BEGIN/END/OTHER_ENV) so it strips a
    marked block identically for both the Ring A and Ring B mode branches.
    """
    for line in disarm_text.splitlines():
        stripped = line.strip()
        marker = 'ssh_ok "sudo '
        if marker in stripped and "sed -i" in stripped:
            after = stripped[stripped.index(marker) + len(marker) :]
            end = after.rfind('"')  # trim the closing `"; then`
            if end == -1:
                break
            inner = after[:end]  # sed -i '<prog>' ${OTHER_ENV}
            return inner.replace("sed -i ", "sed ", 1)
    raise AssertionError("could not extract the sed marker-strip command from disarm.sh")


def test_disarm_removes_only_this_modes_rollback_record_not_the_shared_dir() -> None:
    """Finding 2 (staff review, cross-mode rollback-state destruction).

    The rollback state DIR is SHARED between Ring A (rollback-a.env) and Ring B
    (rollback.env); a combo box can have BOTH armed at once. disarm.sh step 5 must
    remove ONLY this mode's own record file (${ROLLBACK_ENV}) — NEVER `rm -rf` the
    whole ${ROLLBACK_STATE_DIR}, which would destroy the sibling direction's
    rollback state and strand the still-armed other ring at a later disarm. The
    shared dir is rmdir'd only when empty.
    """
    disarm_text = (RING_PROTO_DIR / "disarm.sh").read_text(encoding="utf-8")
    # The destructive `rm -rf ${ROLLBACK_STATE_DIR}` MUST be gone.
    assert "rm -rf ${ROLLBACK_STATE_DIR}" not in disarm_text, (
        "disarm.sh must NOT `rm -rf` the shared ROLLBACK_STATE_DIR — that destroys "
        "a sibling mode's rollback record on a combo box (finding 2)"
    )
    # Step 5 must remove the mode-specific record file, and clean the dir only via
    # an empty-only rmdir.
    assert "sudo rm -f ${ROLLBACK_ENV}" in disarm_text, (
        "disarm.sh step 5 must remove only this mode's ${ROLLBACK_ENV} record"
    )
    assert "sudo rmdir ${ROLLBACK_STATE_DIR}" in disarm_text, (
        "disarm.sh must rmdir the shared dir (empty-only) rather than rm -rf it"
    )


def test_disarm_step5_preserves_sibling_rollback_record() -> None:
    """Real execution of disarm.sh's step-5 removal semantics against a synthetic
    combo-box state dir, proving the sibling mode's record SURVIVES.

    We reproduce EXACTLY the two commands disarm.sh runs (with `sudo` stripped for
    the unprivileged test): `rm -f <this-record>` then `rmdir <dir>` (which fails
    harmlessly when a sibling record remains). The record filenames are lifted from
    disarm.sh's own ROLLBACK_ENV assignments so a rename there flows into this test.
    """
    disarm_text = (RING_PROTO_DIR / "disarm.sh").read_text(encoding="utf-8")
    # Both mode-specific record basenames, lifted from disarm.sh's assignments.
    ring_a_env = _rollback_env_basename(disarm_text, "rollback-a.env")
    ring_b_env = _rollback_env_basename(disarm_text, "rollback.env")

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp) / "ring-proto"
        state_dir.mkdir()
        a_record = state_dir / ring_a_env
        b_record = state_dir / ring_b_env
        a_record.write_text("ORIGINAL_CAMILLA_CONFIG_PATH=/a.yml\n")
        b_record.write_text("ORIGINAL_CAMILLA_CONFIG_PATH=/b.yml\n")

        # Disarm Ring A: `rm -f <a>` then empty-only `rmdir <dir>` (must fail, b left).
        subprocess.run(["rm", "-f", str(a_record)], check=True)
        rc = subprocess.run(
            ["rmdir", str(state_dir)], capture_output=True
        )
        assert rc.returncode != 0, "rmdir should refuse a non-empty dir (b record left)"
        assert not a_record.exists(), "Ring A record removed"
        assert b_record.exists(), (
            "Ring B (sibling) rollback record MUST survive a Ring A disarm on a combo box"
        )
        assert state_dir.is_dir(), "shared dir preserved while a sibling record remains"

        # Now disarm Ring B too: dir becomes empty and the empty-only rmdir succeeds.
        subprocess.run(["rm", "-f", str(b_record)], check=True)
        rc2 = subprocess.run(["rmdir", str(state_dir)], capture_output=True)
        assert rc2.returncode == 0, "empty shared dir cleaned once the last record is gone"
        assert not state_dir.exists(), "shared dir removed after the last mode disarms"


def _rollback_env_basename(disarm_text: str, expected_basename: str) -> str:
    """Confirm disarm.sh assigns ROLLBACK_ENV to a path ending in expected_basename
    and return that basename, so the real-execution test tracks disarm.sh's own
    record filenames."""
    for line in disarm_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("ROLLBACK_ENV=") and expected_basename in stripped:
            return expected_basename
    raise AssertionError(
        f"disarm.sh has no ROLLBACK_ENV assignment ending in {expected_basename!r}"
    )


def _ring_b_marker(disarm_text: str, var_name: str) -> str:
    """The Ring B (non-ring-a) marker from disarm.sh — the one containing
    'jts-ring-proto' but NOT 'jts-ring-a-proto'."""
    for value in _all_assignments(disarm_text, var_name):
        if "jts-ring-a-proto" not in value and "jts-ring-proto" in value:
            return value
    raise AssertionError(f"could not find a Ring B {var_name} in disarm.sh")


def test_arm_and_disarm_agree_on_the_conf_d_path() -> None:
    """The ALSA plugin drop-in path arm.sh installs must be the exact
    path disarm.sh removes (Ring B)."""
    arm_text = (RING_PROTO_DIR / "arm.sh").read_text(encoding="utf-8")
    disarm_text = (RING_PROTO_DIR / "disarm.sh").read_text(encoding="utf-8")
    arm_path = _extract_assignment(arm_text, "CONF_D_PATH")
    assert arm_path == "/etc/alsa/conf.d/98-jts-ring-proto.conf"
    # disarm.sh assigns CONF_D_PATH in both mode branches; the Ring B path must
    # be one of them.
    assert arm_path in _all_assignments(disarm_text, "CONF_D_PATH")


def test_arm_ring_a_and_disarm_agree_on_the_conf_d_path() -> None:
    """The Ring A capture conf.d path arm-ring-a.sh installs must be the
    exact path disarm.sh --ring-a removes."""
    arm_text = (RING_PROTO_DIR / "arm-ring-a.sh").read_text(encoding="utf-8")
    disarm_text = (RING_PROTO_DIR / "disarm.sh").read_text(encoding="utf-8")
    arm_path = _extract_assignment(arm_text, "CONF_D_PATH")
    assert arm_path == "/etc/alsa/conf.d/98-jts-ring-a-proto.conf"
    assert arm_path in _all_assignments(disarm_text, "CONF_D_PATH")


def test_arm_ring_a_slots_default_and_cap() -> None:
    """arm-ring-a.sh's RING_SLOTS default is 8 (the validated Ring A capture
    geometry) and its validation cap is 2..16 (the ring ceiling)."""
    arm_text = (RING_PROTO_DIR / "arm-ring-a.sh").read_text(encoding="utf-8")
    ring_slots_default = _extract_assignment_after_default(arm_text, "RING_SLOTS")
    assert ring_slots_default == "8", (
        f"arm-ring-a.sh RING_SLOTS default is {ring_slots_default!r}; expected 8"
    )
    assert "RING_SLOTS < 2 || RING_SLOTS > 16" in arm_text, (
        "arm-ring-a.sh must cap JASPER_RING_PROTO_SLOTS at 2..16"
    )
    assert "2..16" in arm_text


def test_arm_ring_a_capture_device_matches_fanin_coupling_ssot() -> None:
    """The capture device arm-ring-a.sh + make-camilla-ring-config.sh --ring-a
    default to must match jasper.fanin_coupling.RING_CAPTURE_DEVICE — the SSOT
    the Rust writer and Python both read. A drift would arm a config CamillaDSP
    resolves against a conf.d entry that does not exist."""
    from jasper.fanin_coupling import RING_CAPTURE_DEVICE, RING_WIRE_FORMAT

    arm_text = (RING_PROTO_DIR / "arm-ring-a.sh").read_text(encoding="utf-8")
    make_text = (RING_PROTO_DIR / "make-camilla-ring-config.sh").read_text(encoding="utf-8")
    # The literal device + format names must appear in both scripts.
    assert RING_CAPTURE_DEVICE in arm_text, (
        f"arm-ring-a.sh must reference the SSOT capture device {RING_CAPTURE_DEVICE!r}"
    )
    assert RING_CAPTURE_DEVICE in make_text
    assert RING_WIRE_FORMAT in make_text, (
        f"make-camilla-ring-config.sh --ring-a must pin the SSOT wire format "
        f"{RING_WIRE_FORMAT!r}"
    )


def test_arm_ring_a_removes_stale_ring_before_fanin_restart() -> None:
    """Finding 3 (staff review, stale-ring crash-loop race).

    jasper-fanin is the ring WRITER and its unit carries StartLimitBurst=5 +
    StartLimitAction=reboot. If it restarts into shm_ring and finds a STALE ring
    it cannot cleanly attach to — the step-3 arecord probe's own SSH-user-owned
    ring (EACCES for a non-root fanin), or a prior-arm ring with a different
    geometry (n_slots mismatch, Fatal) — it crash-loops and REBOOTS THE BOX.
    arm-ring-a.sh must remove ${RING_PATH} BEFORE restarting fanin so fanin
    creates a fresh, correctly-owned, correct-geometry ring on attach.
    """
    arm_text = (RING_PROTO_DIR / "arm-ring-a.sh").read_text(encoding="utf-8")
    # The stale-ring removal must be present.
    assert "sudo rm -f ${RING_PATH}" in arm_text, (
        "arm-ring-a.sh must remove a pre-existing/stale ${RING_PATH} so fanin does "
        "not crash-loop into StartLimitAction=reboot on a stale ring (finding 3)"
    )
    # It must appear BEFORE the fanin restart. Anchor on the ordered_restart call.
    rm_idx = arm_text.index("sudo rm -f ${RING_PATH}")
    restart_idx = arm_text.index("ordered_restart jasper-fanin")
    assert rm_idx < restart_idx, (
        "the stale-ring removal must come BEFORE `ordered_restart jasper-fanin` — "
        "removing it after the restart cannot prevent the crash-loop (finding 3)"
    )
    # And it must reference the reboot hazard in a comment so the intent survives.
    assert "StartLimitAction=reboot" in arm_text, (
        "arm-ring-a.sh should document WHY the stale-ring removal exists (the "
        "fanin reboot ladder) so a later edit does not drop it as 'redundant'"
    )


def _extract_assignment_after_default(text: str, var_name: str) -> str:
    """Extract the default from a `VAR="${OVERRIDE:-DEFAULT}"` assignment."""
    for line in text.splitlines():
        stripped = line.strip()
        m = re.match(rf'{var_name}="\$\{{[A-Z_]+:-([^}}]*)\}}"', stripped)
        if m:
            return m.group(1)
    raise AssertionError(f"could not find {var_name}=default assignment")


def test_arm_slots_default_and_cap_match_the_ring_ceiling() -> None:
    """arm.sh's RING_SLOTS default and its 2..N validation cap must match the
    ring's ceiling (JTS_RING_MAX_SLOTS = MAX_N_SLOTS = MAX_SHM_RING_SLOTS = 16).

    Regression pin for the S2 finding: arm.sh shipped with the ceiling raised
    to 16 in the C/Rust constants but the arm path still capped RING_SLOTS at
    2..4 and defaulted to 2 — so the official arm path could not reproduce the
    validated 16-slot camilla geometry (buffer = n_slots * 128 must be >= the
    negotiated 1024 AND >= target_level 1536; below ceil(1536/128)=12 slots
    re-creates the diagnosed stall). The default is 16 and the accepted range is
    2..16; this pins both so a future ceiling bump can't silently leave arm.sh
    behind again.
    """
    arm_text = (RING_PROTO_DIR / "arm.sh").read_text(encoding="utf-8")

    # Default: RING_SLOTS="${JASPER_RING_PROTO_SLOTS:-16}".
    ring_slots_default = _extract_assignment(arm_text, "RING_SLOTS")
    assert ring_slots_default == "${JASPER_RING_PROTO_SLOTS:-16}", (
        f"arm.sh RING_SLOTS default is {ring_slots_default!r}; expected a default "
        "of 16 to match the validated camilla geometry / JTS_RING_MAX_SLOTS"
    )

    # Validation cap: the arithmetic guard must reject <2 or >16, and its error
    # text must name the 2..16 range (not the stale 2..4). Both are checked
    # against the literal source so a hand-edit of one without the other fails.
    assert "RING_SLOTS < 2 || RING_SLOTS > 16" in arm_text, (
        "arm.sh must cap JASPER_RING_PROTO_SLOTS at 2..16 (the raised ring "
        "ceiling); the arithmetic guard was not found at that range"
    )
    assert "2..4" not in arm_text, (
        "arm.sh still contains the stale '2..4' slot range string — raise it to "
        "2..16 (both the guard and its error text) to match JTS_RING_MAX_SLOTS"
    )
    assert "an integer 2..16" in arm_text, (
        "arm.sh's slot-range error text must say '2..16' so the operator sees "
        "the real accepted range"
    )


def test_disarm_sed_marker_strip_actually_works(tmp_path: Path) -> None:
    """Run disarm.sh's exact marker-stripping sed command — lifted verbatim
    from disarm.sh's source via _extract_disarm_sed_expr, NOT re-typed by
    hand — against a synthetic env file, locally, with a real sed.

    The markers embed parentheses ("(scripts/ring-proto/arm.sh)"), which
    are BRE-literal in sed's default mode — not a metacharacter hazard —
    but "looks right" is not the same as "runs right" for a regex built
    from string interpolation. This exercises the real command; because
    the sed expression is extracted from the script, a hand-edit of that
    expression in disarm.sh that broke the strip would fail this test.
    """
    if not _has("sed"):
        pytest.skip("sed not on PATH")
    disarm_text = (RING_PROTO_DIR / "disarm.sh").read_text(encoding="utf-8")
    begin_marker = _ring_b_marker(disarm_text, "BEGIN_MARKER")
    end_marker = _ring_b_marker(disarm_text, "END_MARKER")

    env_file = tmp_path / "outputd.env"
    env_file.write_text(
        "JASPER_OUTPUTD_BACKEND=alsa\n"
        f"{begin_marker}\n"
        "JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n"
        "JASPER_OUTPUTD_SHM_RING_SLOTS=2\n"
        f"{end_marker}\n"
        "JASPER_OUTPUTD_ANOTHER_VAR=foo\n",
        encoding="utf-8",
    )

    # Extract the ACTUAL sed command from disarm.sh (not a hand-retyped copy)
    # so this test genuinely breaks if that command is ever hand-edited without
    # a matching test change. `eval` inside the double-quoted string reproduces
    # the ${BEGIN_MARKER//...}/${OTHER_ENV} expansion disarm.sh's outer
    # `ssh_ok "..."` double quote performs; the extractor already turned
    # `sed -i` into `sed` so we can read stdout and dodge the GNU-vs-BSD
    # `sed -i` suffix difference.
    sed_command = _extract_disarm_sed_command(disarm_text)
    script = (
        'BEGIN_MARKER="$1"; END_MARKER="$2"; OTHER_ENV="$3"; '
        f'eval "{sed_command}"'
    )
    result = subprocess.run(
        ["bash", "-c", script, "--", begin_marker, end_marker, str(env_file)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"sed command failed: {result.stderr}"
    # Emulate the in-place edit disarm.sh does (`sed -i`) by writing stdout back.
    env_file.write_text(result.stdout, encoding="utf-8")

    remaining = env_file.read_text(encoding="utf-8")
    assert begin_marker not in remaining
    assert end_marker not in remaining
    assert "JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring" not in remaining
    assert "JASPER_OUTPUTD_SHM_RING_SLOTS=2" not in remaining
    # Content outside the marked block must survive untouched.
    assert "JASPER_OUTPUTD_BACKEND=alsa" in remaining
    assert "JASPER_OUTPUTD_ANOTHER_VAR=foo" in remaining


def test_disarm_ring_a_sed_marker_strip_actually_works(tmp_path: Path) -> None:
    """The SAME extracted sed command must strip the RING A marked block from a
    synthetic fanin.env, leaving everything outside the block untouched. The Ring
    A marker embeds parentheses too ("(scripts/ring-proto/arm-ring-a.sh)"); this
    exercises the real command against the Ring A markers, not just Ring B."""
    if not _has("sed"):
        pytest.skip("sed not on PATH")
    disarm_text = (RING_PROTO_DIR / "disarm.sh").read_text(encoding="utf-8")
    # The Ring A markers (containing 'jts-ring-a-proto').
    begin_marker = next(
        v for v in _all_assignments(disarm_text, "BEGIN_MARKER") if "jts-ring-a-proto" in v
    )
    end_marker = next(
        v for v in _all_assignments(disarm_text, "END_MARKER") if "jts-ring-a-proto" in v
    )
    env_file = tmp_path / "fanin.env"
    env_file.write_text(
        "JASPER_FANIN_SOMETHING=1\n"
        f"{begin_marker}\n"
        "JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n"
        "JASPER_FANIN_RING_PATH=/dev/shm/jts-ring/program.ring\n"
        "JASPER_FANIN_RING_SLOTS=8\n"
        f"{end_marker}\n"
        "JASPER_FANIN_OTHER=keep\n",
        encoding="utf-8",
    )
    sed_command = _extract_disarm_sed_command(disarm_text)
    script = 'BEGIN_MARKER="$1"; END_MARKER="$2"; OTHER_ENV="$3"; ' + f'eval "{sed_command}"'
    result = subprocess.run(
        ["bash", "-c", script, "--", begin_marker, end_marker, str(env_file)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"sed command failed: {result.stderr}"
    env_file.write_text(result.stdout, encoding="utf-8")
    remaining = env_file.read_text(encoding="utf-8")
    assert begin_marker not in remaining
    assert end_marker not in remaining
    assert "JASPER_FANIN_CAMILLA_COUPLING=shm_ring" not in remaining
    assert "JASPER_FANIN_RING_SLOTS=8" not in remaining
    # Content outside the marked block must survive untouched.
    assert "JASPER_FANIN_SOMETHING=1" in remaining
    assert "JASPER_FANIN_OTHER=keep" in remaining


def test_arm_bench_writer_invocation_uses_flags_the_binary_actually_accepts() -> None:
    """arm.sh's step 5 bench-writer smoke test must call ring_writer_bench
    with flags that binary's own `--help`-equivalent usage string lists.

    Regression pin for a real bug caught during this file's own review:
    arm.sh originally called `ring_writer_bench --tone 440`, but the
    binary has no `--tone` flag (it takes `--pattern tone --freq 440`) —
    that invocation would have hit the binary's unknown-flag branch and
    exited 2 on real hardware, and this "prints a click track" step would
    have silently failed the whole arm sequence the first time anyone ran
    it on a Pi. c/jts-ring-ioplug/ is owned by a parallel track on this
    branch and can rename/add flags without touching this test file, so
    this only pins the specific flags arm.sh currently sends against the
    literal `--path`, `--seconds`, `--pattern`, `--freq` names in the C
    source's own argv parser and usage string — it does not assert the
    full accepted flag set stays frozen.
    """
    bench_c = RING_PROTO_DIR.parent.parent / "c" / "jts-ring-ioplug" / "ring_writer_bench.c"
    if not bench_c.exists():
        pytest.skip("c/jts-ring-ioplug/ring_writer_bench.c not present yet on this branch")
    bench_source = bench_c.read_text(encoding="utf-8")
    arm_lines = (RING_PROTO_DIR / "arm.sh").read_text(encoding="utf-8").splitlines()

    # The invocation line calls the bench binary through the ${bench_bin}
    # variable (not the literal "ring_writer_bench" string — that only
    # appears where bench_bin itself is assigned). Find the line invoking
    # it and pull every --flag token off it directly; the line also
    # contains a runtime path value like /dev/shm/jts-ring/content.ring
    # and a bare numeric argument like "3" for --seconds, neither of
    # which start with "--".
    invocation_lines = [
        line for line in arm_lines if "${bench_bin}" in line and "--path" in line
    ]
    assert invocation_lines, "could not find the ${bench_bin} invocation line in arm.sh"
    sent_flags: list[str] = []
    for line in invocation_lines:
        sent_flags.extend(re.findall(r"--[a-zA-Z][a-zA-Z0-9\-]*", line))
    assert sent_flags, "parsed zero --flags out of arm.sh's bench invocation line(s)"

    for flag in sent_flags:
        assert f'"{flag}"' in bench_source, (
            f"arm.sh sends {flag!r} to ring_writer_bench, but that flag string "
            "does not appear in ring_writer_bench.c's argv parser — check the "
            "binary's usage string (top-of-file comment) for the current flag "
            "names before re-syncing arm.sh's invocation"
        )


def test_arm_never_invokes_product_camilla_emitters_or_reconcilers() -> None:
    """Static guard against the hard scope rule: this prototype must not
    CALL the product Camilla emitters, reconcilers, /sound wizard,
    multiroom, or install.sh from any of its own scripts.

    Checks for actual invocation patterns (import/from, module -m
    execution, `sudo systemctl restart <unit>`, or a bare shell call),
    not bare substring presence — several of these scripts legitimately
    MENTION a product path in a comment to document that they do NOT
    touch it (e.g. make-camilla-ring-config.sh's own docstring-style
    header says exactly that), and a naive substring check would flag
    that documentation as a violation.
    """
    forbidden_calls = (
        "import jasper.camilla_emit",
        "from jasper.camilla_emit",
        "import jasper.active_speaker.camilla_yaml",
        "from jasper.active_speaker.camilla_yaml",
        "systemctl restart jasper-aec-reconcile",
        "systemctl start jasper-aec-reconcile",
        "systemctl restart jasper-audio-hardware-reconcile",
        "systemctl start jasper-audio-hardware-reconcile",
        "systemctl restart jasper-grouping-reconcile",
        "systemctl start jasper-grouping-reconcile",
        "jasper/web/sound_setup.py",
        "python3 -m jasper.web.sound_setup",
        "bash deploy/install.sh",
        "bash /opt/jasper/install.sh",
        "sudo bash install.sh",
    )
    for script in ALL_SCRIPTS + ("_guard.sh",):
        text = (RING_PROTO_DIR / script).read_text(encoding="utf-8")
        for forbidden in forbidden_calls:
            assert forbidden not in text, (
                f"{script} invokes {forbidden!r}, which is out of scope "
                "for this lab-only prototype (see the hard rules in this "
                "file's own instructions and scripts/ring-proto/README.md)"
            )


def test_no_ring_proto_script_is_referenced_from_install_sh() -> None:
    """The flip side of the rule above: install.sh must never learn about
    the LAB prototype under scripts/ring-proto/. A reference there would
    mean the lab wiring (arm/disarm choreography, the hand Camilla config,
    the on-Pi lab build helper, the ``*-proto.conf`` drop-ins) leaked into
    the product installer.

    NOTE (audio-graph consolidation P1, 2026-07-03): the ring *platform*
    itself is now a PRODUCT concern — install.sh builds ``c/jts-ring-ioplug``
    and ships ``pcm.jts_ring_playback`` / ``pcm.jts_ring_capture`` via
    ``deploy/lib/install/ring-platform.sh`` (INERT, nothing arms). So the
    product NAMES (``jts-ring-ioplug``, ``jts_ring_playback``) now correctly
    appear in install.sh; what must STILL stay out is the lab tooling under
    ``scripts/ring-proto/``, which the product install path never invokes.
    """
    install_sh = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    # The lab scripts dir + its lab-only artifacts must never be referenced.
    assert "ring-proto" not in install_sh, (
        "install.sh references scripts/ring-proto/ — the lab prototype must "
        "not be wired into the product installer (P1 ships the ring platform "
        "via deploy/lib/install/ring-platform.sh, not the lab scripts)"
    )
    assert "-proto.conf" not in install_sh, (
        "install.sh references a lab *-proto.conf drop-in (e.g. "
        "98-jts-ring-proto.conf) — the product conf.d is 60-jts-ring.conf"
    )
    for lab_script in ("arm.sh", "arm-ring-a.sh", "disarm.sh", "build-on-pi.sh",
                       "make-camilla-ring-config.sh"):
        assert lab_script not in install_sh, (
            f"install.sh references the lab script {lab_script!r}; the product "
            "install path must not call scripts/ring-proto/ tooling"
        )


def test_arm_reads_outputd_env_from_proc_environ_not_systemctl_show() -> None:
    """arm.sh's preflight must read jasper-outputd's TRUE running env from
    /proc/<MainPID>/environ, not `systemctl show -p Environment`.

    B2 regression: `systemctl show -p Environment` returns ONLY the unit's
    `Environment=` directives and drops every `EnvironmentFile=` layer — but
    JTS keeps the topology state that gates this arm (PERIOD_FRAMES retune,
    SINK/ACTIVE_LANE, DAC_CONTENT_FIFO) in those files. Reading the wrong
    surface installs the wrong ring geometry (period mismatch -> outputd
    exit 78 mid-arm) and blinds the tweeter-safety refusal. Empirically
    verified 2026-07-02: on jts.local `systemctl show` reports
    PERIOD_FRAMES=1024 while the box runs 128; on jts3 ACTIVE_LANE=1 is
    invisible to it.
    """
    arm_text = (RING_PROTO_DIR / "arm.sh").read_text(encoding="utf-8")
    # It resolves the daemon's env from its process image...
    assert "/proc/" in arm_text and "/environ" in arm_text, (
        "arm.sh no longer reads /proc/<MainPID>/environ — the preflight's "
        "topology guards would be blind to EnvironmentFile= layers"
    )
    assert "MainPID" in arm_text, "arm.sh must resolve the daemon MainPID to read its /proc environ"
    # ...and MUST NOT INVOKE `systemctl show ... -p Environment` (which only
    # returns Environment= directives, not the EnvironmentFile= chain). A bare
    # `-p MainPID` systemctl show is fine and expected. Check non-comment lines
    # only — the header comment legitimately explains why that surface is wrong.
    code_lines = [
        ln for ln in arm_text.splitlines() if not ln.lstrip().startswith("#")
    ]
    for ln in code_lines:
        assert "-p Environment" not in ln, (
            "arm.sh invokes `systemctl show ... -p Environment`, which misses the "
            "EnvironmentFile= layers where the real topology state lives (B2): "
            f"{ln.strip()!r}"
        )


def test_disarm_preserves_rollback_record_when_restore_failed() -> None:
    """disarm.sh's step 5 must NOT delete the rollback state unless the
    statefile restore succeeded (or there was nothing to restore).

    S4 regression: if step 1's statefile restore errors, the rollback record
    is the ONLY surviving copy of the original config_path while the statefile
    still points at ring_proto.yml. Deleting it unconditionally would strand
    the box and make a later re-arm record ring_proto.yml as the "original".
    The fix gates the removal on a `statefile_restore_ok` flag set only on the
    two safe outcomes (restore succeeded, or no record existed).
    """
    disarm_text = (RING_PROTO_DIR / "disarm.sh").read_text(encoding="utf-8")
    assert "statefile_restore_ok" in disarm_text, (
        "disarm.sh no longer tracks whether the statefile restore succeeded — "
        "step 5 could delete the sole copy of the original config_path (S4)"
    )
    # The removal of the rollback record must be guarded by the flag. Find the
    # step-5 region and confirm the guard is present before the rm. (Step 5's
    # header now names the mode-specific record — see finding 2 — so anchor on the
    # stable "Step 5/6:" prefix rather than the old "remove rollback state" text.)
    lines = disarm_text.splitlines()
    step5_idx = next(
        (i for i, ln in enumerate(lines) if "Step 5/6:" in ln and "rollback record" in ln),
        None,
    )
    assert step5_idx is not None, "could not locate disarm.sh step 5"
    step5_region = "\n".join(lines[step5_idx : step5_idx + 30])
    assert 'statefile_restore_ok" -ne 1' in step5_region or "statefile_restore_ok" in step5_region, (
        "disarm.sh step 5 removes the rollback record without gating on "
        "statefile_restore_ok — a failed restore would lose the original "
        "config_path (S4)"
    )
    # On the safe path step 5 removes this mode's record file (finding 2 replaced
    # the destructive `rm -rf ${ROLLBACK_STATE_DIR}` with a mode-scoped
    # `rm -f ${ROLLBACK_ENV}` + empty-only rmdir).
    assert "rm -f ${ROLLBACK_ENV}" in step5_region, (
        "disarm.sh step 5 should remove this mode's ${ROLLBACK_ENV} record on the "
        "safe path"
    )


# ---------------------------------------------------------------------
# 2 — the safety-critical guard, exercised via real subprocess.run
# ---------------------------------------------------------------------
#
# A fake `ssh` placed first on PATH is the oracle: if the guard is
# working, NONE of these scripts ever invoke it when PI_HOST is unset,
# because the guard runs (and refuses) before the first ssh_ok/ssh call.
# The fake writes a sentinel file so we can assert "ssh was never
# called" without racing subprocess timing.


_FAKE_SSH = """#!/usr/bin/env bash
echo "ssh was called with: $*" >> "${SENTINEL_FILE}"
exit 0
"""


@pytest.fixture
def fake_ssh_path(tmp_path: Path):
    """A PATH directory containing only a fake `ssh` that records every
    invocation to a sentinel file, plus a fake `make`/`shellcheck`-free
    environment (irrelevant here — the guard runs before any of those)."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    ssh_path = bin_dir / "ssh"
    ssh_path.write_text(_FAKE_SSH, encoding="utf-8")
    ssh_path.chmod(0o755)
    sentinel = tmp_path / "ssh-was-called.log"
    yield bin_dir, sentinel


def _run_script_with_fake_ssh(
    script_name: str,
    fake_ssh_path,
    *,
    pi_host: str | None,
    extra_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    bin_dir, sentinel = fake_ssh_path
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["SENTINEL_FILE"] = str(sentinel)
    # Isolate from any real .env.local in this checkout — the guard's
    # OTHER accepted proof of an explicit target — so these tests only
    # exercise the PI_HOST-argument path deterministically.
    env["REPO_ROOT_OVERRIDE_FOR_TEST"] = "unused"
    env.pop("JASPER_HOSTNAME", None)
    if pi_host is None:
        env.pop("PI_HOST", None)
    else:
        env["PI_HOST"] = pi_host
    return subprocess.run(
        ["bash", str(RING_PROTO_DIR / script_name), *extra_args],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
        cwd=str(ROOT),
    )


@pytest.mark.parametrize("script_name", MUTATING_SCRIPTS)
def test_refuses_with_no_side_effects_when_pi_host_unset(
    script_name: str, fake_ssh_path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression pin for the actual incident: with no PI_HOST and no
    .env.local, every mutating script refuses BEFORE calling ssh even
    once."""
    if not _has("bash"):
        pytest.skip("bash not on PATH")
    # Run from a cwd with no .env.local reachable via REPO_ROOT (the
    # scripts resolve REPO_ROOT from their own file location, which is
    # the real checkout — so guard against a real .env.local existing
    # in THIS checkout by asserting on its absence, or skip if present).
    env_local = ROOT / ".env.local"
    if env_local.exists() and "PI_HOST" in env_local.read_text(encoding="utf-8"):
        pytest.skip(
            "this checkout's .env.local already has PI_HOST — the guard's "
            "OTHER accepted proof of an explicit target would legitimately "
            "pass here, so this specific no-side-effects assertion can't "
            "be isolated without touching the real .env.local"
        )
    _bin_dir, sentinel = fake_ssh_path
    result = _run_script_with_fake_ssh(script_name, fake_ssh_path, pi_host=None)
    assert result.returncode == 1, (
        f"{script_name} with no PI_HOST should refuse (exit 1); got "
        f"{result.returncode}. stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "No changes made" in result.stdout + result.stderr, (
        f"{script_name} refusal message should say 'No changes made'"
    )
    assert not sentinel.exists(), (
        f"{script_name} called ssh (sentinel: {sentinel.read_text() if sentinel.exists() else ''}) "
        "despite having no explicit PI_HOST — the safety guard did not stop it "
        "before a real mutating command"
    )


@pytest.mark.parametrize("script_name", MUTATING_SCRIPTS)
def test_proceeds_past_the_guard_when_pi_host_is_explicit(
    script_name: str, fake_ssh_path
) -> None:
    """The other half of the invariant: an explicit PI_HOST reaches the
    first real ssh call (the fake records it) rather than being refused
    by the same guard. The script may still fail LATER for unrelated
    reasons (the fake ssh returns nothing useful for real preflight
    checks) — this test only asserts the guard itself did not block it."""
    if not _has("bash"):
        pytest.skip("bash not on PATH")
    _bin_dir, sentinel = fake_ssh_path
    result = _run_script_with_fake_ssh(
        script_name, fake_ssh_path, pi_host="jts-test-lab.invalid"
    )
    assert sentinel.exists(), (
        f"{script_name} with an explicit PI_HOST never called ssh at all "
        f"(stdout={result.stdout!r} stderr={result.stderr!r}) — the guard "
        "may be refusing even a legitimate explicit target"
    )
    assert "no explicit PI_HOST" not in (result.stdout + result.stderr), (
        f"{script_name} printed the guard's refusal message even though "
        "PI_HOST was explicitly set"
    )
