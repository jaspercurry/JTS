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

import re
from pathlib import Path

import pytest

from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_SHM_RING,
    DEFAULT_FANIN_RING_PATH,
    DEFAULT_FANIN_RING_SLOTS,
    RING_A_CHANNELS,
    RING_PATH_ENV_VAR,
    RING_SLOTS_ENV_VAR,
    RING_SLOTS_MAX,
    RING_SLOTS_MIN,
    RING_WIRE_FORMAT_ENV_VAR,
    RING_WIRE_FORMATS,
    resolve_ring_slots,
)
from jasper.ring_assets import RING_CONF_DEFAULT_CHANNELS

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FANIN_CONFIG_RS = _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "config.rs"
_FANIN_LANE_RESAMPLER_RS = (
    _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "lane_resampler.rs"
)
_FANIN_MIXER_RS = _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "mixer.rs"
# The mixer module's own body, across the files it is split into; the guards
# below are about that body, not about one path.
_FANIN_MIXER_MODULE_RS = (
    _FANIN_MIXER_RS,
    _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "mixer" / "dsp.rs",
    _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "mixer" / "pcm_open.rs",
)
_FANIN_STATE_RS = _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "state.rs"
_FANIN_DIRECT_CAPTURE_RS = (
    _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "mixer" / "direct_capture.rs"
)
_FANIN_RING_CAPTURE_RS = (
    _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "mixer" / "ring_capture.rs"
)
_OUTPUTD_TYPES_RS = _REPO_ROOT / "rust" / "jasper-outputd" / "src" / "types.rs"
_RING_IOPLUG_C = _REPO_ROOT / "c" / "jts-ring-ioplug" / "pcm_jts_ring.c"


def _config_rs_text() -> str:
    if not _FANIN_CONFIG_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_CONFIG_RS}")
    return _FANIN_CONFIG_RS.read_text(encoding="utf-8")


def _lane_resampler_rs_text() -> str:
    if not _FANIN_LANE_RESAMPLER_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_LANE_RESAMPLER_RS}")
    return _FANIN_LANE_RESAMPLER_RS.read_text(encoding="utf-8")


def _mixer_rs_text() -> str:
    for path in _FANIN_MIXER_MODULE_RS:
        if not path.exists():
            pytest.skip(f"rust source not present: {path}")
    return "\n".join(p.read_text(encoding="utf-8") for p in _FANIN_MIXER_MODULE_RS)


def _state_rs_text() -> str:
    if not _FANIN_STATE_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_STATE_RS}")
    return _FANIN_STATE_RS.read_text(encoding="utf-8")


def _direct_capture_rs_text() -> str:
    if not _FANIN_DIRECT_CAPTURE_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_DIRECT_CAPTURE_RS}")
    return _FANIN_DIRECT_CAPTURE_RS.read_text(encoding="utf-8")


def _ring_capture_rs_text() -> str:
    if not _FANIN_RING_CAPTURE_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_RING_CAPTURE_RS}")
    return _FANIN_RING_CAPTURE_RS.read_text(encoding="utf-8")


def _source_text(path: Path) -> str:
    if not path.exists():
        pytest.skip(f"source not present: {path}")
    return path.read_text(encoding="utf-8")


def _call_sites(fn: str, code: str) -> int:
    """How many times `fn(` is spelled in `code` as a WHOLE identifier.

    A plain `code.count("attach_ring(")` is wrong here and wrong in a way that
    reads as covered: `maybe_reattach_ring(` ends in `attach_ring(`, so the
    substring matches the very function whose body must NOT contain a call. A
    negative assertion written that way can never pass, and a positive one
    over-counts by however many differently-named neighbours happen to end in
    the same letters. `\\w` already covers `_`, so one lookbehind is the whole
    fix.
    """
    return len(re.findall(rf"(?<!\w){re.escape(fn)}\(", code))


def test_coupling_selector_env_var_name_agrees():
    text = _config_rs_text()
    assert f'"{COUPLING_ENV_VAR}"' in text, (
        f"Rust must read the coupling selector from {COUPLING_ENV_VAR}"
    )


def test_rust_serves_the_undeclared_key_as_well_as_the_ring_token():
    """The Rust ACCEPT-SET is ``None`` | ``""`` | ``shm_ring`` — all three.

    Ring A is the daemon's only transport (ADR-0100), so this key no longer
    SELECTS anything on the Rust side; it only has to serve what the fleet can
    legitimately present and refuse the rest (that refusal half is pinned
    behaviorally in-crate by `only_a_ring_declaration_or_none_is_served`).

    UNSET is a first-class served state and this is the row that says so.
    `coupling-auto` runs ``After=jasper-fanin.service``, so on a fresh or reset
    box fan-in starts BEFORE the key is written. If Rust refused the undeclared
    key, that box would park on every first boot; because it serves it, no
    Python reader may map undeclared → loopback and derive a runtime
    expectation from it (see ``resolve_coupling``'s docstring, and the doctor's
    `_fanin_health_from_status`, which expects ``shm_ring`` unconditionally).

    Shape-level on purpose: it complements the in-crate behavioral pin rather
    than restating it, and what can drift across the language boundary is the
    accept-set's MEMBERSHIP, which is what this reads.
    """
    text = _config_rs_text()
    assert f'None | Some("") | Some("{COUPLING_SHM_RING}") => {{}}' in text, (
        "the Rust accept arm must serve the undeclared key (None), a cleared "
        f"key (empty), and the {COUPLING_SHM_RING!r} token Python's "
        "resolve_coupling emits — all three in one arm"
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


_CHANNEL_DECLARATIONS = (
    (
        "rust/jasper-fanin/src/mixer.rs (CHANNELS)",
        _FANIN_MIXER_RS,
        r"^pub const CHANNELS:\s*u32\s*=\s*(\d+);",
    ),
    (
        "rust/jasper-outputd/src/types.rs (CHANNELS)",
        _OUTPUTD_TYPES_RS,
        r"^pub const CHANNELS:\s*u16\s*=\s*(\d+);",
    ),
    (
        "c/jts-ring-ioplug/pcm_jts_ring.c (JTS_RING_DEFAULT_CHANNELS)",
        _RING_IOPLUG_C,
        r"^#define\s+JTS_RING_DEFAULT_CHANNELS\s+(\d+)",
    ),
)


def test_stereo_program_channel_count_agrees_across_python_rust_and_c():
    """The stereo program's WIDTH is declared five times and was pinned nowhere.

    The env names, the slot bounds and the wire format above all have a
    cross-language pin. The channel count — the other field the ring header
    compares on attach — had none, in any module: two Rust crates, the C ioplug
    and two Python constants each spell it independently, and
    ``tests/test_ring_assets.py`` pinned the Python conf.d default to a bare
    literal rather than to the C ``#define`` its own comment says it mirrors.

    What drift costs, per site:

    * ``mixer.rs`` sizes the renderer ingress lanes and creates Ring A's header
      with it. A widened fan-in against an unchanged reader is a geometry
      mismatch — ``RING_ATTACH_FATAL`` on the Pi at arm, never in CI.
    * ``types.rs`` is ``AudioFormat::default()``, outputd's program width.
    * ``JTS_RING_DEFAULT_CHANNELS`` is what a conf.d block declares by OMISSION,
      and none of the three shipped blocks in
      ``deploy/alsa/conf.d/60-jts-ring.conf`` spells ``channels``. Every one of
      them therefore rides this default.
    * ``RING_CONF_DEFAULT_CHANNELS`` is Python's model of that same C default —
      the renderer writes a ``channels`` line only where the resolved wire
      differs from it, so a Python side out of step with the ``.so`` omits the
      key it needed to write (the ``format`` axis keeps this pair deliberately
      APART, and its docstring explains why; the channels axis does not).

    ``RING_STEREO_PROGRAM_CHANNELS`` is the same number reached from the
    topology side and is already pinned equal to ``RING_A_CHANNELS`` by
    ``tests/test_runtime_contract_ring.py``, so it is not re-pinned here.
    """
    assert RING_A_CHANNELS == RING_CONF_DEFAULT_CHANNELS, (
        "the two Python spellings of the stereo width disagree: "
        f"jasper.fanin_coupling.RING_A_CHANNELS={RING_A_CHANNELS}, "
        f"jasper.ring_assets.RING_CONF_DEFAULT_CHANNELS={RING_CONF_DEFAULT_CHANNELS}"
    )
    for label, path, pattern in _CHANNEL_DECLARATIONS:
        found = re.findall(pattern, _source_text(path), re.MULTILINE)
        assert len(found) == 1, (
            f"expected exactly one channel-count declaration in {label}, found "
            f"{len(found)} — this pin would silently guard only the first. "
            "Narrow the pattern or pin each site."
        )
        assert int(found[0]) == RING_A_CHANNELS, (
            f"stereo program width drifted: {label} declares {found[0]}, Python's "
            f"RING_A_CHANNELS declares {RING_A_CHANNELS}. Change every site in the "
            "same commit — the ring header's channel count is compared field by "
            "field on attach, so a mismatch surfaces as a failed ioplug attach "
            "on-Pi, not here."
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


def test_fanin_mixer_publishes_slots_and_opens_no_playback_pcm():
    """The mixer publishes 128-frame slots — and opens NO playback PCM at all.

    The ring is the whole output (ADR-0100). The guard is whole-FILE now rather
    than scoped to one arm of a transport match, because there is no second arm
    to be satisfied by: every `PCM::new` in the fan-in mixer must be a CAPTURE
    open (a renderer lane or the USB gadget). A playback open re-added anywhere
    here is a second writer of a lane nobody reads, and one that would make the
    aloop tap look alive to the next consumer that reaches for it.
    """
    text = _mixer_rs_text()
    assert "RingOutput" in text
    assert "RingWriter" in text
    assert ".publish(" in text
    # The 128-frame slot is pinned via the shared RING_SLOT_FRAMES constant.
    assert "RING_SLOT_FRAMES" in text

    code = _rust_code_only(text)
    assert "Direction::Playback" not in code, (
        "the fan-in mixer must open no ALSA playback device — the SHM ring is "
        "the whole output (ADR-0100). (If that hit is inside a TRAILING comment, "
        "the strip only drops whole-line comments — move it onto its own line.)"
    )
    # POSITIVE CONTROL: every surviving open is a capture one, so the absence
    # above is a real fact about the opens rather than about an empty string.
    assert _call_sites("PCM::new", code) == code.count("Direction::Capture") > 0, (
        "every PCM::new in the fan-in mixer must be a Direction::Capture open — "
        "if this fails, the assertion above is not proving anything"
    )


def test_fanin_music_output_tap_stays_deleted():
    """The multi-room music-only tap is gone from fan-in and does not come back.

    `JASPER_FANIN_MUSIC_OUTPUT_PCM` was fan-in's OPTIONAL second, pre-TTS output
    PCM (multi-room Increment 1). Its read path was live — env parse, a
    best-effort open in `Mixer::new`, a per-period `write_music_only`, a
    `music_output` STATUS block — while **no writer anywhere ever set the env
    var**, so the producer half was inert for its whole life. Deleted 2026-08-14
    by owner ruling (#2285 deletion arc, PR #2483), telling a future session to
    rebuild deliberately rather than revive this one.

    This is the assertion that makes those two sentences enforceable, and it is
    the shape P7-1, P7-2, and P7-3 each shipped for their own retirements: a
    name-absence guard, comment-stripped, so the crate's prose ABOUT the deletion
    neither satisfies nor trips it.

    Why it is not redundant with the playback-open guard above: that guard is
    about the OUTPUT DIRECTION, and a revived music tap would trip it only while
    it stayed an ALSA playback open. Reviving the tap also means reviving the env
    parse and the config field, and each is pinned by name here, so a partial
    resurrection fails just as loudly as a whole one.
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
                "jasper-outputd instead. If "
                "multi-room v2 wants a pre-TTS fan-in tap, design it against the "
                "then-current topology and update this guard deliberately. (If "
                "that hit is inside a TRAILING comment, the strip only drops "
                "whole-line comments — move it onto its own line.)"
            )

    # POSITIVE CONTROL: the readers + stripper must yield real code, or every
    # assertion above passes vacuously on an empty string. One surviving,
    # load-bearing name per file — each the direct neighbour of a deleted one.
    for filename, needle in (
        ("config.rs", "JASPER_FANIN_INPUT_PCMS"),
        ("mixer.rs", "configure_pcm"),
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


def test_step_fills_output_buf_once_above_the_ring_publish():
    """`step()`'s narrow saturate runs ONCE, above the ring publish.

    The mutant this catches: moving (or duplicating)
    `saturate_to_i16(&self.sum_buf, &mut self.output_buf, self.program_width)`
    below or past the publish. `output_buf` is the NARROW ring wire's published
    payload — `write_ring_period` publishes it slot by slot whenever the ring's
    attached header is S16LE. A publish that ran before the saturate would leave
    a narrow box publishing a stale (or, on the first period, all-zero) buffer
    into Ring A, with CamillaDSP reading it and every counter healthy. That is
    the whole fleet's narrow boxes going silent-or-stuttering from a mutant with
    no error path.

    This is pinned in Python because `step()` has no hardware-free Rust test at
    all: it reads live ALSA inputs. The in-crate ring tests
    (`wide_ring_slots_carry_the_left_justified_narrow_slots`) enter at
    `write_ring_period`, one call BELOW the ordering asserted here, so they
    cannot see what filled the buffer they are handed.
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
    publish = "write_ring_period("
    assert body.count(saturate) == 1, (
        "step() must fill output_buf with exactly ONE saturate — a second one "
        "means the call was duplicated"
    )
    assert body.count(publish) == 1, "step() must publish to the ring exactly once"
    assert body.index(saturate) < body.index(publish), (
        "the saturate that fills output_buf must sit ABOVE the ring publish — "
        "below it, a narrow box publishes a stale output_buf into Ring A"
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


def test_no_blocking_io_on_the_fanin_render_thread():
    """#2533: no filesystem write and no device open/close may run inside `step()`.

    Measured consequence when they did: fan-in's period budget is 5.33 ms at the
    shipped 256-frame period and both Ring A (fan-in→CamillaDSP) and Ring B
    (CamillaDSP→outputd) are two 128-frame slots deep, so a render-thread block
    over ~2.7 ms costs exactly one slot — a 128-frame silence INSERTION when
    CamillaDSP reads an empty Ring A, or a 128-frame DELETION when fan-in
    free-run-drops a slot it could not publish. Both signs were measured in the
    field. Fan-in's own ring-stall detector has a 1 s floor and is structurally
    blind to it, so nothing counts these; the guard has to be structural.

    Two owners, both off-thread: `fanin-direct-opener` (gadget `snd_pcm_open` /
    `snd_pcm_close`) and `fanin-ring-attacher` (`RingReader::create_or_attach`,
    whose inter-process `flock` is bounded at 500 ms — ~187 slots — and which
    needs no USB host at all to fire: a ring lane detached by a geometry shear or
    a permission refusal stays detached until an operator clears it, paying that
    every ~2 s: #2538).
    """
    direct_text = _direct_capture_rs_text()
    ring_text = _ring_capture_rs_text()

    # 1. The direct-lane opener thread owns every gadget open and close.
    assert "pub(super) fn spawn(" in direct_text
    assert '.name("fanin-direct-opener".to_string())' in direct_text, (
        "the gadget opener must be its own named thread"
    )
    direct_code = _rust_code_only(direct_text)
    # `open_direct_capture` may appear ONLY inside the opener thread body.
    opener_start = direct_code.index("fn spawn(")
    opener_end = direct_code.index("fn publish_pending(", opener_start)
    assert "open_direct_capture(" in direct_code[opener_start:opener_end], (
        "the opener thread performs the device open"
    )
    assert (
        direct_code.count("open_direct_capture(") == 1
    ), "no other call site in the direct lane may open the device"
    # The retiring handle travels to the opener so its Drop (snd_pcm_close) does
    # not run in the render loop.
    assert "fn hand_retired_handle_to_opener(" in direct_code
    assert "retire: Option<PCM>" in direct_code, (
        "a retired PCM must be handed over, not dropped on the render thread"
    )
    # The Absent retry is a poll + queue, never an inline open.
    reopen_start = direct_code.index("fn maybe_reopen_direct(")
    reopen_end = direct_code.index("fn adopt_open_outcome(", reopen_start)
    reopen_body = direct_code[reopen_start:reopen_end]
    assert "open_direct_capture(" not in reopen_body, (
        "the ~2 s Absent retry must not open the device on the render thread"
    )
    assert "opener.request(" in reopen_body and ".poll()" in reopen_body, (
        "the Absent retry must queue an open and poll for the result"
    )

    # 2. The ring-lane attacher thread owns every reattach (#2538).
    assert "pub(super) fn spawn(" in ring_text
    assert '.name(format!("fanin-ring-attacher-{label}"))' in ring_text, (
        "the ring attacher must be its own named thread, one per lane"
    )
    ring_code = _rust_code_only(ring_text)
    # `attach_ring` — and through it `RingReader::create_or_attach` — has exactly
    # TWO production callers: the attacher thread, and `open_ring_input`, which
    # runs in `Mixer::new` on the constructing thread and is not the render loop.
    # The test module's own call sites are excluded by SLICING it off rather than
    # by budgeting for them, so a new test can never loosen the guard.
    ring_production_code = ring_code[: ring_code.index("mod tests {")]
    assert _call_sites("RingReader::create_or_attach", ring_production_code) == 1, (
        "`create_or_attach` may be spelled once, inside `attach_ring`"
    )
    attacher_start = ring_production_code.index("fn spawn(")
    attacher_end = ring_production_code.index("fn publish_pending(", attacher_start)
    assert (
        _call_sites("attach_ring", ring_production_code[attacher_start:attacher_end])
        == 1
    ), "the attacher thread performs the attach"
    construction_start = ring_production_code.index("fn open_ring_input(")
    construction_end = ring_production_code.index(
        "fn read_ring_and_render(", construction_start
    )
    assert (
        _call_sites(
            "attach_ring", ring_production_code[construction_start:construction_end]
        )
        == 1
    ), "the CONSTRUCTION attach stays inline — `Mixer::new` is not the render loop"
    # The ~2 s Detached retry is a poll + queue, never an inline attach. SCOPED
    # assertion FIRST, before the whole-file count below: a re-inline trips both,
    # and the one that fires is the one whose message the next reader gets.
    reattach_start = ring_production_code.index("fn maybe_reattach_ring(")
    reattach_end = ring_production_code.index("fn adopt_attach_outcome(", reattach_start)
    reattach_body = ring_production_code[reattach_start:reattach_end]
    assert not _call_sites("attach_ring", reattach_body), (
        "the ~2 s Detached retry must not attach the ring on the render thread — "
        "`create_or_attach` takes a 500 ms-bounded flock inside a 5.33 ms period"
    )
    assert "attacher.request(" in reattach_body and ".poll()" in reattach_body, (
        "the Detached retry must queue an attach and poll for the result"
    )
    # Backstop for a re-inline the scoped slice above would not see: a NEW caller
    # somewhere else in the file that the render loop can reach.
    assert _call_sites("attach_ring", ring_production_code) == 3, (
        "`attach_ring` has exactly three spellings in production code: its own "
        "definition, the attacher thread, and `open_ring_input`. A fourth means "
        "a new caller — check it is not on the render thread."
    )
    # And the retry latches are phase-seeded so lanes cannot retry in lockstep.
    assert "pub(super) const fn reattach_phase(" in ring_production_code
    assert "reattach_phase(lane_index, lane_count)" in ring_production_code, (
        "each ring lane must seed its retry latch from its own phase"
    )

    # 3. Both queues are observable in STATUS (depth / in-flight), so a hanging
    #    device open or a hanging attach is visible rather than silent.
    state_text = _state_rs_text()
    assert '"reopen_pending"' in state_text
    assert '"attach_pending"' in state_text


def test_servo_thread_exit_clears_reverse_signals():
    """A stopped `fanin-host-clock` servo thread must clear its REVERSE signals.

    The exit path (graceful shutdown OR caught panic) neutralizes the pitch ctl so
    the host free-runs. It must ALSO clear the outer-loop signals the mixer's decay
    tick reads (`ladder_l0`, `commanded_milli_ppm`); otherwise a dead thread leaves
    `ladder_l0=true` frozen, driving the thin-cushion free-run churn loop. Both
    stores sit AFTER the `catch_unwind` block so they run on both exit paths.
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
