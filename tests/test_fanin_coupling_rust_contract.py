# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-language contract for the fan-in -> Camilla SHM-ring coupling.

The Rust ``RingWriter`` writes the fan-in -> Camilla SHM ring (Ring A) and the
Python emitter describes it as a CamillaDSP ioplug capture. If the ring path, env
names, slot bounds, or coupling token diverge, fan-in writes a ring nobody reads
or the daemon and the emitted/armed config disagree on the transport.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_SHM_RING,
    DEFAULT_FANIN_RING_PATH,
    DEFAULT_FANIN_RING_SLOTS,
    RING_PATH_ENV_VAR,
    RING_SLOTS_ENV_VAR,
    RING_SLOTS_MAX,
    RING_SLOTS_MIN,
    RING_WIRE_FORMAT_ENV_VAR,
    RING_WIRE_FORMATS,
    resolve_ring_slots,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FANIN_CONFIG_RS = _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "config.rs"
_FANIN_LANE_RESAMPLER_RS = (
    _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "lane_resampler.rs"
)
_FANIN_MIXER_RS = _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "mixer.rs"
_FANIN_STATE_RS = _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "state.rs"
_FANIN_HOST_COMPLIANCE_RS = (
    _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "host_compliance.rs"
)


def _config_rs_text() -> str:
    if not _FANIN_CONFIG_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_CONFIG_RS}")
    return _FANIN_CONFIG_RS.read_text(encoding="utf-8")


def _lane_resampler_rs_text() -> str:
    if not _FANIN_LANE_RESAMPLER_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_LANE_RESAMPLER_RS}")
    return _FANIN_LANE_RESAMPLER_RS.read_text(encoding="utf-8")


def _mixer_rs_text() -> str:
    if not _FANIN_MIXER_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_MIXER_RS}")
    return _FANIN_MIXER_RS.read_text(encoding="utf-8")


def _state_rs_text() -> str:
    if not _FANIN_STATE_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_STATE_RS}")
    return _FANIN_STATE_RS.read_text(encoding="utf-8")


def _host_compliance_rs_text() -> str:
    if not _FANIN_HOST_COMPLIANCE_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_HOST_COMPLIANCE_RS}")
    return _FANIN_HOST_COMPLIANCE_RS.read_text(encoding="utf-8")


def test_coupling_selector_env_var_name_agrees():
    text = _config_rs_text()
    assert f'"{COUPLING_ENV_VAR}"' in text, (
        f"Rust must read the coupling selector from {COUPLING_ENV_VAR}"
    )


def test_coupling_shm_ring_token_agrees():
    # Ring A: the Rust normalizer MUST accept the same shm_ring token Python's
    # resolve_coupling accepts, or the daemon and the emitted/armed config
    # disagree on the transport of the SHARED realtime capture.
    text = _config_rs_text()
    assert f'Some("{COUPLING_SHM_RING}") => Coupling::ShmRing' in text, (
        f"Rust coupling parse must map the {COUPLING_SHM_RING!r} token to "
        "Coupling::ShmRing"
    )


def test_shm_ring_env_var_names_and_defaults_agree():
    # The Rust daemon resolves the ring path + slot count from the SAME env var
    # names, with the SAME defaults, that Python fanin_coupling exposes — the
    # n_slots <-> JASPER_FANIN_RING_SLOTS pairing is the drift axis.
    text = _config_rs_text()
    assert f'"{RING_PATH_ENV_VAR}"' in text, (
        f"Rust must read the ring path from {RING_PATH_ENV_VAR}"
    )
    assert f'"{RING_SLOTS_ENV_VAR}"' in text, (
        f"Rust must read the ring slots from {RING_SLOTS_ENV_VAR}"
    )
    assert f'"{DEFAULT_FANIN_RING_PATH}"' in text, (
        f"Rust must default the ring path to {DEFAULT_FANIN_RING_PATH}"
    )
    # The default slot count is a bare integer literal in the env_u32 fallback.
    assert f'"{RING_SLOTS_ENV_VAR}", {DEFAULT_FANIN_RING_SLOTS}' in text, (
        f"Rust must default JASPER_FANIN_RING_SLOTS to {DEFAULT_FANIN_RING_SLOTS}"
    )


def test_shm_ring_wire_format_env_var_name_agrees():
    """The Ring-A wire FORMAT key is the third cross-language env name.

    Its two siblings above (path, slots) have been pinned since the ring
    shipped; the wire format arrived later and did not get the same treatment.
    It is the same drift axis and a worse one to get wrong: the header compares
    ``sample_format`` field-by-field, so a Python side reading one key name
    while the daemon reads another does not mis-declare a wire — it silently
    reads the DEFAULT wire while the reconciler's four-ends gate reports
    agreement, and the ioplug attach is the first thing to notice.
    """
    text = _config_rs_text()
    assert f'"{RING_WIRE_FORMAT_ENV_VAR}"' in text, (
        f"Rust must read the Ring-A wire format from {RING_WIRE_FORMAT_ENV_VAR}"
    )
    # The two-token vocabulary is the other half of the contract: a value Python
    # accepts must be a value Rust accepts, spelled identically.
    for token in RING_WIRE_FORMATS:
        assert f'"{token}"' in text, (
            f"Rust must accept the {token} wire token Python's vocabulary declares"
        )


def test_shm_ring_slots_out_of_range_fails_loud_on_both_sides():
    # SF-1: the JASPER_FANIN_RING_SLOTS normalizer is a declared must-agree axis.
    # BOTH sides fail loud on a present out-of-range value — no silent clamp,
    # per repo doctrine. Otherwise a future arm script that resolved slots via the
    # Python resolver could write an N-slot ioplug conf.d geometry while the
    # daemon refuses to start on the same env (split-brain, fail-closed but
    # maximally confusing on-Pi). This pins the exact agreed behavior:
    #   unset      -> the same default (2) on both sides
    #   2 and 16   -> accepted on both sides
    #   17 (and other out-of-range) -> rejected on both sides

    # Python side (runs live).
    assert resolve_ring_slots(None) == DEFAULT_FANIN_RING_SLOTS
    assert resolve_ring_slots(str(RING_SLOTS_MIN)) == RING_SLOTS_MIN
    assert resolve_ring_slots(str(RING_SLOTS_MAX)) == RING_SLOTS_MAX
    for bad in (RING_SLOTS_MAX + 1, RING_SLOTS_MIN - 1, 0, 100):
        with pytest.raises(ValueError):
            resolve_ring_slots(str(bad))

    # Rust side (source pin — the crate does not build on macOS). The daemon
    # bails on the same range with the same bound constants, and its from_env
    # fail-loud is exercised by the Rust unit test in the CI rust job.
    text = _config_rs_text()
    assert f"pub const RING_SLOTS_MIN: u32 = {RING_SLOTS_MIN};" in text, (
        "Rust RING_SLOTS_MIN must match the Python RING_SLOTS_MIN bound"
    )
    assert f"pub const RING_SLOTS_MAX: u32 = {RING_SLOTS_MAX};" in text, (
        "Rust RING_SLOTS_MAX must match the Python RING_SLOTS_MAX bound"
    )
    # The out-of-range guard returns an Err, it does NOT clamp.
    opener = "if !(RING_SLOTS_MIN..=RING_SLOTS_MAX).contains(&ring_slots) {"
    assert opener in text, (
        "Rust must range-check JASPER_FANIN_RING_SLOTS against the shared bounds"
    )
    # Slice to the block's own closing brace, NOT to the first `}` in the body:
    # the guard's message is a format string, so `{}` placeholders sit inside it
    # and a first-`}` split truncates the block mid-literal (it silently read
    # only 2 lines once the guard grew past its placeholders).
    body = text.split(opener, 1)[1]
    guard, sep, _ = body.partition("\n        }\n")
    assert sep, "could not find the ring-slots guard's closing brace"
    # Containment: the slice must stop at THIS guard and not run on into the
    # next one. Without this, an over-capturing slice would satisfy every
    # assertion below using text that belongs to the adjacent slot-shear guard,
    # and the ring-slots guard could be gutted while the test stayed green.
    assert "must be a whole multiple" not in guard, (
        "the ring-slots guard slice over-captured into the adjacent "
        "slot-shear guard — the assertions below would pass on the wrong block"
    )
    # Fail-loud is the promise; the spelling is not. `anyhow::bail!` and
    # `return Err(anyhow::anyhow!(...))` both satisfy it.
    assert "anyhow::bail!" in guard or "return Err(anyhow::anyhow!(" in guard, (
        "Rust out-of-range ring slots must FAIL LOUD (bail!/return Err), not clamp"
    )
    assert "clamp" not in guard.lower(), "Rust must not silently clamp ring slots"
    # And the failure is CONFIG-class: it exits 78 so jasper-fanin.service PARKS
    # (RestartPreventExitStatus=78) instead of climbing the restart burst into
    # StartLimitAction=reboot. This guard is the ENV-declaration half of that
    # class: an out-of-range JASPER_FANIN_RING_SLOTS is re-read from the env file
    # on every start, so it is identical across restarts AND across reboots —
    # a restart loop here reboots the speaker indefinitely. (The other half, a
    # stale ring file, does clear on a reboot because /dev/shm is tmpfs; it
    # survives a RESTART, which is the loop the park prevents.)
    assert "crate::ConfigClassError" in guard, (
        "Rust out-of-range ring slots must be tagged config-class so the unit "
        "parks at exit 78 rather than reboot-looping"
    )


def test_shm_ring_status_block_emitted_by_rust_state():
    # The Rust STATUS snapshot emits the ring counter block under shm_ring —
    # the /state.transport + ring:{...} contract the doctor/dashboard read.
    text = _state_rs_text()
    assert '"shm_ring"' in text, "Rust STATUS must echo transport shm_ring"
    assert '"ring":{' in text, "Rust STATUS must emit a ring block"
    for field in (
        "path",
        "slots",
        "occupancy",
        "published",
        "full_waits",
        "drops",
    ):
        assert f'"{field}"' in text, f"ring block missing {field!r} key"


def _rust_code_only(text: str) -> str:
    """`text` with whole-line `//` and `///` comments dropped.

    Every negative assertion in this module is about CODE. A retired thing is
    normally retired together with a comment saying it was retired and why —
    so a bare `"mirror" not in text` fails on the very sentence documenting the
    removal, and the natural "fix" is to delete the explanation. Strip the prose
    instead and let the assertion mean what it says.

    WHOLE-LINE ONLY, deliberately. A trailing comment (`let n = 1; // output_pcm`)
    survives the strip and will trip the negative assertions below. That is the
    conservative direction — a naive `//`-to-end-of-line cut would eat the `//`
    inside a string literal or a URL and quietly shrink what the guard inspects —
    but the failure message has to say so, or the next reader sees an assertion
    blaming code for something a comment did.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


def _shm_ring_construction_arm(text: str) -> str:
    """`Mixer::new`'s `Coupling::ShmRing` construction arm, comments stripped.

    SCOPED ON PURPOSE, and comment-stripped on purpose. The assertions below are
    negative — "this arm opens nothing" — and a negative assertion over the whole
    file would be satisfied by the `Coupling::Loopback` arm's legitimate
    `open_output`, while one over the raw arm text would be defeated by the
    arm's own comment explaining what U4/P7-4 removed. Both failure shapes read
    as covered and guard nothing, so the slice is bounded and the prose is cut.
    """
    opener = "Coupling::ShmRing => {"
    assert opener in text, "the mixer must still have a ShmRing output arm"
    body, sep, _ = text[text.index(opener) :].partition("\n        };\n")
    # Containment: the slice must stop at the transport `match`'s own terminator.
    assert sep, "could not find the end of the transport match"
    assert "RingWriter::create_or_attach" in body and "Output::Ring(Box::new(" in body, (
        "the slice does not look like the ShmRing construction arm"
    )
    return _rust_code_only(body)


def test_shm_ring_mixer_publishes_slots_and_opens_no_aloop_pcm():
    """The ring arm publishes 128-frame slots — and opens NO ALSA PCM (U4/P7-4).

    Until P7-4 this arm ALSO opened `config.output_pcm` as a lossy aloop mirror,
    so a ring-coupled box kept feeding `hw:Loopback,0,7` for the dsnoop taps.
    Those readers are gone (P7-1, P7-2, P7-3) and the mirror went last, in that
    order: on a ring box the ring is the whole output. Re-adding any playback open here
    silently restores a second writer of a lane nobody reads — and, worse, one
    that would make the aloop tap look alive to the next consumer that reaches
    for it.
    """
    text = _mixer_rs_text()
    assert "Output::Ring" in text
    assert "RingWriter" in text
    assert ".publish(" in text
    # The 128-frame slot is pinned via the shared RING_SLOT_FRAMES constant.
    assert "RING_SLOT_FRAMES" in text

    ring_arm = _shm_ring_construction_arm(text)
    # `open_music_output(` was a third name here until 2026-08-14, when the
    # unconsumed `JASPER_FANIN_MUSIC_OUTPUT_PCM` tap was deleted by owner ruling.
    # `PCM::new(` catches a re-added INLINE open in this arm and nothing more — a
    # NEW helper (`fn open_whatever` calling `PCM::new` at its own definition site,
    # outside this slice) is invisible here, exactly as `open_music_output` was
    # before it got its own name in this list. The music tap's own resurrection is
    # pinned by name in `test_fanin_music_output_tap_stays_deleted` below; the
    # general helper-shaped case is a known gap in THIS assertion, named so the
    # list does not read as broader than it is.
    for opener in ("open_output(", "PCM::new("):
        assert opener not in ring_arm, (
            f"the ShmRing arm must open no ALSA PCM — found {opener!r}. The aloop "
            "mirror was removed in U4/P7-4; the ring is the whole output here. "
            "(If that hit is inside a TRAILING comment, the strip only drops "
            "whole-line comments — move it onto its own line.)"
        )
    assert "output_pcm" not in ring_arm, (
        "the ShmRing arm must not reach for config.output_pcm — that field "
        "belongs to the Coupling::Loopback arm now. (If that hit is inside a "
        "TRAILING comment, the strip only drops whole-line comments — move it "
        "onto its own line.)"
    )

    # POSITIVE CONTROL: the same slicing + stripping applied to the arm that DOES
    # open a PCM must find it. Without this, a slicer that silently returned ""
    # would make every assertion above pass.
    loopback_arm = _rust_code_only(
        text[
            text.index("Coupling::Loopback => {") : text.index("Coupling::ShmRing => {")
        ]
    )
    assert "open_output(" in loopback_arm and "config.output_pcm" in loopback_arm, (
        "the loopback arm must still open config.output_pcm — if this fails, the "
        "negative assertions above are not proving anything"
    )


def test_fanin_music_output_tap_stays_deleted():
    """The multi-room music-only tap is gone from fan-in and does not come back.

    `JASPER_FANIN_MUSIC_OUTPUT_PCM` was fan-in's OPTIONAL second, pre-TTS output
    PCM (multi-room Increment 1). Its read path was live — env parse, a
    best-effort open in `Mixer::new`, a per-period `write_music_only`, a
    `music_output` STATUS block — while **no writer anywhere ever set the env
    var**, so the producer half was inert for its whole life. Deleted 2026-08-14
    by owner ruling (#2285 deletion arc, PR #2483), with both HANDOFF-multiroom.md
    and HANDOFF-fan-in-daemon.md telling a future session to rebuild deliberately
    rather than revive this one.

    This is the assertion that makes those two sentences enforceable, and it is
    the shape P7-1, P7-2, and P7-3 each shipped for their own retirements: a
    name-absence guard, comment-stripped, so the crate's prose ABOUT the deletion
    neither satisfies nor trips it.

    Why it is not redundant with the ShmRing-arm opener list above: that list is
    SCOPED to one construction arm and catches an INLINE `PCM::new`. A revived
    `open_music_output` helper calls `PCM::new` at its own definition site,
    outside that slice — invisible there, caught here. Reviving the tap also
    means reviving the env parse and the config field, and each is pinned by
    name, so a partial resurrection fails just as loudly as a whole one.
    """
    sources = {
        "config.rs": _config_rs_text(),
        "mixer.rs": _mixer_rs_text(),
        "state.rs": _state_rs_text(),
    }
    # Env key, config field, and the opener helper. Three names because the tap
    # had three separable halves; any one of them coming back is the regression.
    retired = (
        "JASPER_FANIN_MUSIC_OUTPUT_PCM",
        "music_output_pcm",
        "open_music_output",
    )
    for filename, text in sources.items():
        code = _rust_code_only(text)
        for name in retired:
            assert name not in code, (
                f"{filename} names the retired music-only tap ({name!r}). It was "
                "deleted 2026-08-14 by owner ruling — nothing ever wrote its env "
                "var, and the shipped bonded split routes the leader's TTS to "
                "jasper-outputd instead (see docs/HANDOFF-multiroom.md §0). If "
                "multi-room v2 wants a pre-TTS fan-in tap, design it against the "
                "then-current topology and update this guard deliberately. (If "
                "that hit is inside a TRAILING comment, the strip only drops "
                "whole-line comments — move it onto its own line.)"
            )

    # POSITIVE CONTROL: the readers + stripper must yield real code, or every
    # assertion above passes vacuously on an empty string. One surviving,
    # load-bearing name per file — each the direct neighbour of a deleted one.
    for filename, needle in (
        ("config.rs", "JASPER_FANIN_OUTPUT_PCM"),
        ("mixer.rs", "open_output"),
        ("state.rs", "push_output_json"),
    ):
        code = _rust_code_only(sources[filename])
        assert needle in code, (
            f"positive control failed: {filename} should still contain {needle!r}. "
            "If this fails, the absence assertions above are not proving anything."
        )


# NOTE — the retired `mirror_frames` / `mirror_drops` keys are deliberately NOT
# re-pinned here as a source grep. `state.rs`'s
# `snapshot_json_shm_ring_reports_ring_observability` asserts their absence on
# the PARSED ring object, which is strictly stronger than reading the emitter's
# text, and nothing on the Python side ever consumed them (no doctor, dashboard,
# or /state reader). One fact, one owner.


def test_step_fills_output_buf_once_above_the_transport_dispatch():
    """`step()`'s narrow saturate is SHARED by both transports, above the match.

    The mutant this catches: moving (or duplicating)
    `saturate_to_i16(&self.sum_buf, &mut self.output_buf, self.program_width)` into the
    `Output::Alsa` arm. `output_buf` is ALSO the NARROW ring wire's published
    payload — `write_ring_period` publishes it slot by slot whenever the ring's
    attached header is S16LE. A saturate that ran only on the ALSA path would
    leave a narrow ring-coupled box publishing a stale (or, on the first period,
    all-zero) buffer into Ring A, with CamillaDSP reading it and every counter
    healthy. That is the whole fleet's narrow boxes going silent-or-stuttering
    from a mutant with no error path.

    Before U4/P7-4 this test's subject was the aloop mirror, which took
    `output_buf` on BOTH wires; the mirror is gone, so the wide wire no longer
    reads `output_buf` at all and the narrow wire is the reason the ordering
    still matters.

    This is pinned in Python because `step()` has no hardware-free Rust test at
    all: it reads live ALSA inputs. The in-crate ring tests
    (`wide_ring_slots_carry_the_left_justified_narrow_slots`) enter at
    `write_ring_period`, one call BELOW the ordering asserted here, so they
    cannot see which transport filled the buffer they are handed.
    """
    text = _mixer_rs_text()

    opener = "fn step(&mut self) -> Result<()> {"
    assert opener in text, "the mixer must still have a step() render period"
    body, sep, _ = text[text.index(opener) :].partition("\n    }\n")
    # Containment: the slice must stop at step()'s OWN closing brace. A sentinel
    # that misses it lands on the next method's instead, and the assertions below
    # would then read text that is not step()'s — a saturate in a later method or
    # a unit test could satisfy them while step() itself was gutted. (The count is
    # 1, not 0: the slice starts AT step()'s own opener. It counts `fn `, not
    # `pub fn `, because mixer.rs's methods are private.)
    assert sep, "could not find step()'s closing brace"
    assert body.count("fn ") == 1, "the step() slice ran past its own function"

    saturate = "saturate_to_i16(&self.sum_buf, &mut self.output_buf, self.program_width)"
    dispatch = "match &mut self.output {"
    assert body.count(saturate) == 1, (
        "step() must fill output_buf with exactly ONE saturate — a second one "
        "means the call was duplicated into the transport arms"
    )
    assert body.count(dispatch) == 1, "step() must dispatch on the transport once"
    assert body.index(saturate) < body.index(dispatch), (
        "the saturate that fills output_buf must sit ABOVE the transport match, "
        "shared by both arms — inside Output::Alsa it would leave a narrow "
        "ring-coupled box publishing a stale output_buf into Ring A"
    )


def test_input_resampler_status_exports_live_lock_state():
    resampler_text = _lane_resampler_rs_text()
    state_text = _state_rs_text()

    assert "pub locked: Arc<AtomicBool>" in resampler_text
    assert "locked_state.store(true, Ordering::Relaxed)" in resampler_text
    assert "locked_state.store(false, Ordering::Relaxed)" in resampler_text
    assert '"locked"' in state_text
    assert "r.locked.load(Ordering::Relaxed)" in state_text


def test_cushion_decay_held_target_is_single_source_of_truth():
    """The DEFAULT-OFF post-lock cushion decay's held target must be ONE value.

    The resampler owns the live held-target gauge; `hold_fill_frames` reads it (so
    render/trim discipline toward it); the outer host-clock DLL re-pins its
    setpoint from the SAME gauge each tick (never a duplicated config value); and
    STATUS surfaces both the live held target and the decay block. If any of these
    wires drifts, the two controllers can disagree about where the fill sits — the
    documented two-controller oscillation class this design avoids.
    """
    resampler_text = _lane_resampler_rs_text()
    host_clock_text = (
        _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "host_clock.rs"
    ).read_text(encoding="utf-8")
    mixer_text = _mixer_rs_text()
    state_text = _state_rs_text()

    # 1. The resampler OWNS the live held-target gauge, and hold_fill_frames reads
    #    it (the setpoint render_period / trim_ring discipline toward).
    assert "held_target_frames: Arc<AtomicU64>" in resampler_text
    assert "self.held_target_frames.load(Ordering::Relaxed) as usize" in resampler_text, (
        "hold_fill_frames must read the live held-target gauge, not a static field"
    )
    # 2. The decay is a render-PERIOD-clocked pure state machine ticked by the mixer.
    assert "pub fn tick_decay(" in resampler_text
    assert "r.tick_decay(decay_l0, decay_commanded_ppm_abs)" in mixer_text, (
        "the mixer must tick the decay once per render period with the DLL signals"
    )
    # 3. The outer DLL re-pins its setpoint from the SAME live gauge each tick.
    assert "pub held_target_frames: Arc<AtomicU64>" in host_clock_text
    assert "hc.set_target_fill_frames(signals.held_target_frames.load(Ordering::Relaxed)" in (
        host_clock_text
    ), "the servo thread must re-pin its setpoint from the live held-target gauge"
    # 4. STATUS surfaces the live held target AND the decay block (additive).
    assert '"held_target_frames"' in state_text
    assert '"decay":{' in state_text
    assert '"frozen_reason"' in state_text


def test_cushion_decay_notl0_snap_back_is_prime_aware():
    """The `NotL0` snap-back must be PRIME-AWARE — the floor-prime railed-regime fix.

    A floor-primed session locks at the floor, but the host-clock ladder is
    necessarily still Probing (`dll_l0=false`) at session start (l0 arrives only
    after the ~21 s probe). Before this fix the FIRST locked tick took the `NotL0`
    branch and SNAPPED the held target floor→ceiling, railing the resampler's ±500
    ppm authority for ~40 s while it rebuilt the fill — the observed −500 ppm probe
    rail. The fix: while a floor prime is live, the `NotL0` branch HOLDS the floor
    (STATUS `prime_hold`) until the ladder reaches l0. This pins that contract in the
    Rust source so it can't silently regress to the unconditional snap.
    """
    resampler_text = _lane_resampler_rs_text()
    state_text = _state_rs_text()

    # The prime-aware hold branch guards the NotL0 snap on `floor_prime_pending`.
    assert "if self.floor_prime_pending && !s.dll_l0_locked {" in resampler_text, (
        "the NotL0 branch must HOLD the floor while a floor prime is live and the "
        "ladder is still Probing (the floor-prime railed-regime fix)"
    )
    # The unconditional NotL0 snap-back is STILL present for the UNPRIMED case (it is
    # load-bearing for a genuine mid-stream DLL demotion / cold acquisition — the
    # #1145 bit-identical invariant depends on it).
    assert "self.snap_back(DecayFrozenReason::NotL0);" in resampler_text, (
        "the unconditional NotL0 snap must remain for the UNPRIMED case"
    )
    # The honest STATUS token for the primed-holding state exists and rides an
    # append-only wire code (6).
    assert "PrimeHold," in resampler_text, "the PrimeHold frozen-reason variant must exist"
    assert "Some(DecayFrozenReason::PrimeHold) => 6," in resampler_text, (
        "PrimeHold must map to the append-only wire code 6"
    )
    assert '6 => "prime_hold",' in resampler_text, (
        "wire code 6 must render as the prime_hold STATUS token"
    )
    # The token reaches STATUS via the shared code_str mapper (no separate emitter).
    assert "DecayFrozenReason::code_str(" in state_text, (
        "STATUS must render frozen_reason via the shared code_str mapper"
    )


def test_cushion_decay_session_boundary_snap_honours_the_live_proof():
    """Session-boundary paths must snap the held target via the proof-honouring
    primitive, NOT the unconditional ceiling snap.

    Per-session prime-at-floor (PR #1146 → per-session): `reset` (idle/host-pause /
    device-loss) and `unlock_for_underfill` (starvation = the natural session end)
    both call `snap_decay_back_honoring_proof`, which re-primes at the FLOOR when a
    live valid proof is present (skip the ~2.5-min descent every session) and the
    CEILING otherwise. The unconditional `snap_decay_back` (ceiling) is reserved for
    the revocation escape (`snap_decay_to_ceiling`).
    """
    resampler_text = _lane_resampler_rs_text()
    assert "fn snap_decay_back(" in resampler_text
    assert "fn snap_decay_back_honoring_proof(" in resampler_text, (
        "the per-session honouring snap primitive must exist"
    )
    # Both session-boundary paths route through the honouring snap so a live proof
    # re-primes at the floor.
    reset_start = resampler_text.index("pub fn reset(")
    reset_end = resampler_text.index("fn ", reset_start + 10)
    assert "snap_decay_back_honoring_proof" in resampler_text[reset_start:reset_end], (
        "reset() must snap via snap_decay_back_honoring_proof (floor when a proof is live)"
    )
    unlock_start = resampler_text.index("fn unlock_for_underfill(")
    unlock_end = resampler_text.index("fn render_silence(", unlock_start)
    assert (
        "snap_decay_back_honoring_proof" in resampler_text[unlock_start:unlock_end]
    ), (
        "unlock_for_underfill() must snap via snap_decay_back_honoring_proof "
        "(the natural session end honours the live proof)"
    )

    # The revocation escape stays the UNCONDITIONAL ceiling snap — a distrusted
    # proof must always re-acquire deep, never re-prime at the floor.
    ceiling_start = resampler_text.index("pub fn snap_decay_to_ceiling(")
    ceiling_end = resampler_text.index("fn ", ceiling_start + 10)
    ceiling_body = resampler_text[ceiling_start:ceiling_end]
    assert "self.snap_decay_back(" in ceiling_body, (
        "snap_decay_to_ceiling must call the UNCONDITIONAL snap_decay_back (ceiling)"
    )
    assert "snap_decay_back_honoring_proof" not in ceiling_body, (
        "the revoke escape must NOT honour the proof — it always snaps to the ceiling"
    )

    # The proof-validity signal at snap time is the SAME `flag_present` atomic the
    # revoke path clears — single source of truth (no second copy).
    assert "fn live_proof_present(" in resampler_text
    live_start = resampler_text.index("fn live_proof_present(")
    live_end = resampler_text.index("fn ", live_start + 10)
    assert "flag_present" in resampler_text[live_start:live_end], (
        "live_proof_present must read the shared flag_present atomic (the revoke SSOT)"
    )


def test_host_compliance_status_block_and_wiring():
    """The DEFAULT-OFF host-compliance persistence surfaces `resampler.compliance`
    and rides the cushion-decay flag (no new top-level feature gate).

    The prime-at-floor + revalidation feature persists a proof to a
    fan-in-owned JSON file, primes the decay at its floor when a valid proof
    exists, and revokes on the per-session probe fail / DLL demotion / early
    unlock (a probe fail is TWO-strike; DLL demotion / confirmed churn are one).
    STATUS must additively expose the four fields the operator watches
    (`flag_present` / `proved_at` / `revoked_reason_last` /
    `consecutive_failures`); the mixer must service it once per period; and the
    persistence must be gated behind `input_resampler_cushion_decay_enabled`, not
    a separate flag.
    """
    config_text = _config_rs_text()
    mixer_text = _mixer_rs_text()
    state_text = _state_rs_text()
    resampler_text = _lane_resampler_rs_text()

    # 1. STATUS surfaces the additive compliance fields under resampler, including
    #    the two-strike probe-fail counter.
    assert '"compliance":{' in state_text
    for field in (
        '"flag_present"',
        '"proved_at"',
        '"revoked_reason_last"',
        '"consecutive_failures"',
    ):
        assert field in state_text, f"STATUS resampler.compliance must carry {field}"

    # 2. The mixer services the compliance state once per period.
    assert "fn service_host_compliance(" in mixer_text
    assert "self.service_host_compliance(" in mixer_text, (
        "the mixer step must call service_host_compliance once per render period"
    )

    # 3. Persistence rides the cushion-decay flag — NO separate top-level gate.
    assert "config.input_resampler_cushion_decay_enabled" in mixer_text, (
        "prime-at-floor / persistence must be gated behind the cushion-decay flag"
    )

    # 4. The prime-at-floor primitive exists on the resampler and is invoked only
    #    from the gated build helper.
    assert "pub fn prime_decay_at_floor(" in resampler_text
    assert "resampler.prime_decay_at_floor()" in mixer_text, (
        "the gated build helper must prime the decay at the floor for a valid proof"
    )

    # 5. The persistence path is a config knob defaulting under the fan-in state
    #    dir (already-owned write path — no new StateDirectory / privilege grant).
    assert "JASPER_FANIN_HOST_COMPLIANCE_PATH" in config_text
    assert "/var/lib/jasper/fanin/host_compliance.json" in _host_compliance_rs_text(), (
        "the default compliance path must be a sibling of the xrun log under the "
        "fan-in state dir (the already-owned ReadWritePaths=/var/lib/jasper posture)"
    )

    # 6. The write/revoke/strike journal events are present (observability
    #    contract). The two-strike probe-fail RETAIN and the probe-PASS reset are
    #    their own events so an operator can tell a retained strike (proof kept)
    #    from a delete-revoke and a counter reset.
    for event in (
        "event=fanin.host_compliance.written",
        "event=fanin.host_compliance.revoked",
        "event=fanin.host_compliance.prime_at_floor",
        "event=fanin.host_compliance.strike_retained",
        "event=fanin.host_compliance.pass_reset",
    ):
        assert event in mixer_text, f"mixer must emit {event}"

    # 7. The two-strike probe-fail policy is a pure, testable classifier that the
    #    mixer consults: a probe FAIL retains the proof the first time (a
    #    measurement), a DLL demotion / confirmed churn revoke on one strike, and
    #    the proof is deleted only at the strike limit. The mixer keeps
    #    `flag_present` TRUE on a retained strike (so the next session still
    #    primes) — the SSOT interaction with #1154 (a counter=1 session's snap
    #    still lands at the floor).
    host_compliance_text = _host_compliance_rs_text()
    assert "pub fn classify_strike(" in host_compliance_text, (
        "the two-strike policy must be a pure classifier"
    )
    assert "pub const PROBE_FAIL_STRIKE_LIMIT: u32 = 2;" in host_compliance_text, (
        "a probe fail must be two-strike (retain once, delete on the second)"
    )
    assert "RetainWithStrike" in host_compliance_text
    assert "classify_strike(reason, current_failures)" in mixer_text, (
        "the mixer must decide keep-vs-delete via the pure classifier"
    )
    assert "on_strike_retained(" in mixer_text, (
        "a retained probe-fail strike must persist the bumped counter and KEEP "
        "flag_present true (the next session still primes at the floor)"
    )


def test_host_compliance_prime_is_per_session_not_construction_only():
    """The prime-at-floor is PER-SESSION: the session-boundary snap honours the
    live proof and the revalidation `floor_primed` is re-sampled per lock.

    Hardware evidence (jts.local 2026-07-03): a second session in ONE fanin daemon
    lifetime was descending from the full ceiling because the prime happened only at
    lane construction. The fix routes both session-boundary snaps through the
    proof-honouring primitive AND feeds the tracker the LIVE `flag_present` at each
    lock so session B (primed off session A's fresh proof) both seats at the floor
    and runs the one-strike revalidation.
    """
    mixer_text = _mixer_rs_text()
    resampler_text = _lane_resampler_rs_text()
    host_compliance_text = _host_compliance_rs_text()

    # The RevalidationTracker.step takes the live per-lock prime signal, and the
    # mixer feeds it from the SAME flag_present atomic (single source of truth).
    assert "floor_primed_now: bool" in host_compliance_text, (
        "RevalidationTracker::step must take the live per-lock floor_primed signal"
    )
    assert "self.floor_primed = floor_primed_now" in host_compliance_text, (
        "the tracker must re-sample floor_primed at the rising edge (per-lock)"
    )
    assert "hc.obs.flag_present.load(Ordering::Relaxed)" in mixer_text, (
        "the mixer must feed step() the live flag_present (the revoke SSOT), so the "
        "snap destination and the per-lock revalidation gate share one signal"
    )
    # The honouring snap re-primes at the floor via the same prime_at_floor the
    # construction path uses (one floor-prime mechanism, called per session).
    honor_start = resampler_text.index("fn snap_decay_back_honoring_proof(")
    honor_end = resampler_text.index("fn live_proof_present(", honor_start)
    honor_body = resampler_text[honor_start:honor_end]
    assert "self.decay.prime_at_floor()" in honor_body, (
        "the honouring snap must re-prime at the floor via prime_at_floor when a "
        "live proof is present"
    )


def test_host_compliance_prime_gated_on_host_clock_servo_armed():
    """The prime-at-floor must be gated on the host-clock DLL actually being armed.

    The prime skips the ~2.5-min cushion descent on the strength of a PRIOR
    session's host-clock compliance proof, and its whole safety story ("the
    per-session servo probe revalidates the prime") is enacted by the
    `fanin-host-clock` servo thread: `dll_l0_locked`, the probe verdict, the
    two-strike ProbeFail (#1160), and the `PrimeHold` exit all ride it. That
    thread runs ONLY when host-clock AND USB-direct are both armed
    (`Config::host_clock_servo_armed`). If a valid proof primes a session while
    that servo is NOT running, `dll_l0_locked` is pinned false forever: the
    session sits in `PrimeHold` at the floor with no ladder to reach l0, no probe
    to revalidate, and no demotion — the floor held FOREVER on stale evidence
    (only the underfill/churn net could catch an unsustainable floor). #1145
    proved this exact misconfig inert (armed-but-frozen decay == disabled); a prime
    without the DLL would silently convert that into a permanent unvalidated
    divergence. This pins the config gate so the prime and the servo can never
    disagree about whether the revalidating DLL is live.
    """
    config_text = _config_rs_text()
    mixer_text = _mixer_rs_text()
    main_text = (_REPO_ROOT / "rust" / "jasper-fanin" / "src" / "main.rs").read_text(
        encoding="utf-8"
    )

    # 1. The single-source-of-truth predicate is the AND of host-clock and direct.
    assert "pub fn host_clock_servo_armed(&self) -> bool {" in config_text, (
        "Config must expose the servo-armed predicate (the SSOT the prime gate + "
        "the servo-spawn gate both read)"
    )
    armed_start = config_text.index("pub fn host_clock_servo_armed(&self) -> bool {")
    armed_body = config_text[armed_start : config_text.index("}", armed_start)]
    # Containment — the SAFE half of the first-`}` slicing class: this body has
    # no braces of its own, so the slice lands on the fn's own closing brace. If
    # it ever grows one the slice truncates EARLY, which fails the assertion
    # below loudly rather than passing on a neighbour's text. This pins the
    # other direction: the slice must not run past the function into the next
    # one. (The count is 1, not 0 — the slice starts AT this fn's own opener.)
    #
    # Counts `fn `, not `pub fn `: a PRIVATE `fn` is the overrun this assertion
    # is most likely to meet — `config.rs` carries private helpers between its
    # public ones — and a `pub fn `-only count would run straight past one and
    # keep reporting containment.
    assert armed_body.count("fn ") == 1, (
        "the host_clock_servo_armed slice ran past its own function"
    )
    assert "self.host_clock_enabled && self.usb_direct_enabled" in armed_body, (
        "host_clock_servo_armed must be host_clock_enabled AND usb_direct_enabled"
    )

    # 2. The mixer's prime arm is guarded on that predicate — a valid proof alone
    #    does NOT prime; the servo must be armed too. Pin the guarded match arm and
    #    the un-primed fall-through arm for the disarmed case.
    assert "let host_clock_armed = config.host_clock_servo_armed();" in mixer_text, (
        "the prime gate must resolve host_clock_armed via the SSOT predicate"
    )
    assert (
        "Some(rec) if rec.valid_for(live_floor) && host_clock_armed =>" in mixer_text
    ), (
        "the prime-at-floor arm must additionally require host_clock_armed "
        "(the DLL that revalidates the prime must be live)"
    )
    assert "reason=host_clock_disarmed" in mixer_text, (
        "a valid proof with the DLL disarmed must log the suppressed-prime "
        "diagnostic and descend from the ceiling (proof preserved, lane inert)"
    )

    # 3. `main`'s servo-spawn gate derives from the SAME predicate, so the servo
    #    and the prime can never disagree about whether the DLL is live.
    assert "config.host_clock_servo_armed()" in main_text, (
        "main's host_clock_enabled_effective must derive from the shared "
        "servo-armed predicate (single source of truth)"
    )


def test_servo_thread_exit_clears_reverse_signals():
    """A stopped `fanin-host-clock` servo thread must clear its REVERSE signals.

    The exit path (graceful shutdown OR caught panic) neutralizes the pitch ctl so
    the host free-runs. It must ALSO clear the outer-loop signals the mixer's decay
    tick + compliance revalidation read (`ladder_l0`, `commanded_milli_ppm`, and —
    since the host-compliance persistence landed — `fallback_reason_code`, `probe_result_code`,
    `probe_response_ratio_milli`). Otherwise a dead thread leaves `ladder_l0=true`
    frozen (driving the thin-cushion free-run churn loop) OR a stale host fallback
    cause / probe FAIL that would make compliance spuriously REVOKE a
    proof for a session whose ladder was fine when the daemon was told to stop —
    revocation must be driven only by a LIVE explicit cause, not by the servo
    shutting down. All stores sit AFTER the `catch_unwind` block so they run on both
    exit paths.
    """
    host_clock_text = (
        _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "host_clock.rs"
    ).read_text(encoding="utf-8")
    # The exit-neutralize block ends the thread body; every reverse-signal clear
    # follows the actuator neutralize (so they run on graceful + caught-panic exit).
    exit_start = host_clock_text.index('neutralize_for_exit("shutdown")')
    exit_tail = host_clock_text[exit_start:]
    assert "signals.ladder_l0.store(false, Ordering::Relaxed)" in exit_tail, (
        "servo-thread exit must clear ladder_l0 so a dead thread cannot leave the "
        "decay tick reading a stale l0=true"
    )
    assert "signals.commanded_milli_ppm.store(0, Ordering::Relaxed)" in exit_tail, (
        "servo-thread exit must clear commanded_milli_ppm"
    )
    # The three compliance-revalidation reverse signals must clear on exit too, so a
    # stopped servo cannot leave a stale fallback cause / probe FAIL.
    assert "signals.fallback_reason_code" in exit_tail, (
        "servo-thread exit must clear the explicit fallback cause"
    )
    assert "fallback_reason_code(FallbackReason::None)" in exit_tail, (
        "servo-thread exit must store the no-fallback code"
    )
    assert "signals.probe_result_code.store(" in exit_tail, (
        "servo-thread exit must clear probe_result_code (→ ProbeResult::None) so a "
        "dead thread cannot leave a stale probe FAIL that revokes a good proof"
    )
    assert "ProbeResult::None" in exit_tail, (
        "the probe_result_code clear must store the None verdict, not a stale value"
    )
    assert "signals.probe_response_ratio_milli.store(" in exit_tail, (
        "servo-thread exit must clear probe_response_ratio_milli alongside the verdict"
    )


def test_input_resampler_recovery_restarts_capture_pcm():
    text = _mixer_rs_text()
    recovery_start = text.index("fn recover_resampler_input_xrun(")
    recovery_end = text.index("fn read_into_resampler_and_render(", recovery_start)
    recovery_body = text[recovery_start:recovery_end]

    assert ".try_recover(error, true)" in recovery_body
    # `input.pcm` is now `Option<PCM>` (None only on the USB DIRECT lane, which
    # uses recover_direct_xrun instead); the aloop resampler lane binds it and
    # still restarts the capture PCM if a post-recover try_recover left it
    # PREPARED. Assert the state-check + restart on the bound handle.
    assert "pcm.state() != State::Running" in recovery_body
    assert ".start()" in recovery_body
