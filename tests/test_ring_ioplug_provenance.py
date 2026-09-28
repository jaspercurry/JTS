# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The ioplug provenance record: what the installer built, and what it can parse.

Presence is not capability. The jts_ring ioplug build is DEGRADE-TO-WARN, so a
failed rebuild leaves the previous ``.so`` installed beside freshly-installed
Rust daemons, and both the doctor's presence check and its open-probe pass on a
stale-but-valid plugin. The installer therefore records the sha256 of the plugin
it installed plus the conf.d fields that plugin can parse, and REVOKES that
record on every path where the deploy did not produce the installed file.

Two contracts live here:

* the Python reader / capability gate (``jasper.ring_assets``). The fixed
  program wire requires the format capability; generic protocol tests also
  cover the C ioplug's narrow baseline; and
* the cross-language pins — the record path, its key names, the capability
  tokens, and the marker strings the installer greps for — against
  ``deploy/lib/install/ring-platform.sh`` and the C source those markers come
  from. A reworded ``SNDERR`` would otherwise silently turn a capable plugin
  into an uncapable-looking one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jasper import ring_assets, ring_conf
from jasper.fanin_coupling import RingWire

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RING_PLATFORM_SH = _REPO_ROOT / "deploy" / "lib" / "install" / "ring-platform.sh"
_IOPLUG_C = _REPO_ROOT / "c" / "jts-ring-ioplug" / "pcm_jts_ring.c"

# The two capability markers the installer greps the built .so for. Each is a
# diagnostic literal emitted at the parse site of the conf.d field it proves, so
# it is present in the binary exactly when that field is understood.
_CAP_MARKERS = {
    ring_assets.RING_CAP_WIRE_FORMAT: "format %s unsupported (S16_LE|S32_LE)",
    ring_assets.RING_CAP_WIRE_CHANNELS: "channels out of range 2..=8",
    ring_assets.RING_CAP_PACE_NOMINAL: "pace_nominal must be 0 or 1",
}


def _wire(sample_format="S16_LE", ring_a=2, ring_b=2, ring_active=None) -> RingWire:
    """A generic protocol wire; defaults mirror the C ioplug, not the program writer."""
    return RingWire(
        sample_format=sample_format,
        ring_a_channels=ring_a,
        ring_b_channels=ring_b,
        period_frames=128,
        ring_active_channels=ring_active,
    )


def _sh_text() -> str:
    if not _RING_PLATFORM_SH.exists():  # pragma: no cover - always present in repo
        pytest.skip(f"installer not present: {_RING_PLATFORM_SH}")
    return _RING_PLATFORM_SH.read_text(encoding="utf-8")


# --- the ioplug-default wire, which needs nothing ---------------------------


def test_the_ioplug_default_wire_needs_no_capability():
    """The generic C baseline forces no conf.d field beyond the plugin defaults."""
    assert ring_assets.ring_wire_capabilities(_wire()) == frozenset()


def test_the_ioplug_default_wire_is_supported_without_any_record(tmp_path):
    """No record + no plugin on disk still passes on the plugin's own wire.

    The short-circuit must happen BEFORE the record is read and before the
    plugin is hashed. Pointing both at paths that do not exist is how this test
    proves neither was consulted rather than asserting it in prose.
    """
    support = ring_assets.ring_ioplug_wire_supported(
        _wire(),
        plugin_dir=str(tmp_path / "nonexistent"),
        provenance_path=str(tmp_path / "nonexistent.provenance"),
    )
    assert support.ok is True
    assert support.needed == frozenset()


@pytest.mark.parametrize(
    "call",
    [
        lambda: ring_assets.ring_ioplug_so_sha256(),
        lambda: ring_assets.ring_ioplug_so_path(),
        lambda: ring_assets.ring_asset_presence().so_present,
    ],
    ids=["sha256", "so_path", "presence"],
)
def test_the_plugin_dir_is_resolved_at_call_time_not_bound_at_import(
    call, monkeypatch, tmp_path
):
    """A repointed :data:`RING_ALSA_PLUGIN_DIR` must actually be read.

    THE RULE THIS MODULE STATES ABOUT ITSELF, made falsifiable. Every ``None``
    default here is documented as resolving its module constant at CALL time,
    because a default bound at import captures the constant forever: a caller
    that repoints the module attribute is then silently ignored while every
    message still names the constant — one fact, two answers. That is not
    hypothetical; the doctor's provenance check shipped with exactly that bug,
    naming ``RING_IOPLUG_PROVENANCE`` in its own text while reading a path
    nothing could redirect.

    ``ring_ioplug_so_sha256`` and ``ring_ioplug_wire_supported`` had ``plugin_dir``
    bound at def time and no test noticed, so this is the guard, not a
    restatement: it fails if either signature goes back to a bound default.
    """
    plugin_dir = tmp_path / "elsewhere"
    plugin_dir.mkdir()
    (plugin_dir / ring_assets.RING_IOPLUG_SO).write_bytes(b"\x7fELF repointed")
    monkeypatch.setattr(ring_assets, "RING_ALSA_PLUGIN_DIR", str(plugin_dir))

    result = call()

    assert result not in (None, False), (
        "the repointed plugin dir was not read — the constant is bound at import"
    )
    if isinstance(result, str) and result.startswith("/"):
        assert str(plugin_dir) in result


def test_the_wire_support_predicate_also_resolves_the_plugin_dir_at_call_time(
    monkeypatch, tmp_path
):
    """The same rule at the predicate that HASHES the plugin.

    ``ring_ioplug_wire_supported`` reports the stale/absent verdicts by path, so
    a def-time binding would hash one file and name another. Driven through a
    wire that needs a capability, because the no-capability arm short-circuits
    before any path is touched.
    """
    plugin_dir = tmp_path / "elsewhere"
    plugin_dir.mkdir()
    so_bytes = b"\x7fELF repointed"
    (plugin_dir / ring_assets.RING_IOPLUG_SO).write_bytes(so_bytes)
    provenance = tmp_path / "record"
    provenance.write_text(
        _record_text(_sha_of(so_bytes), ring_assets.RING_CAP_WIRE_FORMAT),
        encoding="utf-8",
    )
    monkeypatch.setattr(ring_assets, "RING_ALSA_PLUGIN_DIR", str(plugin_dir))

    support = ring_assets.ring_ioplug_wire_supported(
        _wire(sample_format="S32_LE"), provenance_path=str(provenance)
    )

    # It hashed the plugin in the REPOINTED dir; a def-time binding would have
    # hashed the real system path (absent here) and reported "could not be read".
    assert support.ok is True, support.detail


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        (_wire(sample_format="S32_LE"), {ring_assets.RING_CAP_WIRE_FORMAT}),
        (_wire(ring_b=6), {ring_assets.RING_CAP_WIRE_CHANNELS}),
        (_wire(ring_a=4), {ring_assets.RING_CAP_WIRE_CHANNELS}),
        # THE ACTIVE AXIS. Each disjunct alone must be sufficient, or the
        # predicate reads as covered while one block's `channels` key is
        # unweighed — which is exactly the state this axis was added to fix.
        (_wire(ring_active=4), {ring_assets.RING_CAP_WIRE_CHANNELS}),
        (_wire(ring_active=8), {ring_assets.RING_CAP_WIRE_CHANNELS}),
        (
            _wire(sample_format="S32_LE", ring_b=8),
            {
                ring_assets.RING_CAP_WIRE_FORMAT,
                ring_assets.RING_CAP_WIRE_CHANNELS,
            },
        ),
        (
            _wire(sample_format="S32_LE", ring_active=4),
            {
                ring_assets.RING_CAP_WIRE_FORMAT,
                ring_assets.RING_CAP_WIRE_CHANNELS,
            },
        ),
    ],
)
def test_off_default_wires_need_the_matching_capability(wire, expected):
    assert ring_assets.ring_wire_capabilities(wire) == frozenset(expected)


@pytest.mark.parametrize("ring_active", [None, 2])
def test_a_stereo_or_absent_active_ring_forces_no_channels_key(ring_active):
    """The ACTIVE axis must not fire on the two shapes that declare nothing.

    ``None`` is every non-roleful box and ``2`` is jts3's 2-way shape; both
    leave the ACTIVE block exactly as shipped (``render_ring_conf_wire``
    coerces ``None`` to the default and writes nothing at the default), so
    neither forces a key. Without this the new axis would demand
    ``wire_channels`` from the whole fleet and refuse every box whose plugin
    predates that field — a fleet-wide disarm dressed as a fix.
    """
    assert ring_assets.ring_wire_capabilities(_wire(ring_active=ring_active)) == (
        frozenset()
    )


def test_the_active_axis_is_read_from_the_block_the_renderer_writes():
    """The axis and the renderer must agree on WHICH boxes force the key.

    Derived rather than asserted: render a wire whose ACTIVE width is off the
    default into a real conf.d, then check the predicate demanded the capability
    for the same wire. A predicate keyed on a different rule than the renderer's
    is the defect this closes — the ACTIVE block gets `channels` from
    ``ring_active_channels`` while a roleful box's Ring A/B stay structurally 2,
    so no Ring A/B comparison can stand in for it.
    """
    import shutil
    import tempfile

    wire = _wire(sample_format="S32_LE", ring_active=4)
    tmp = Path(tempfile.mkdtemp()) / "60-jts-ring.conf"
    shutil.copy(
        _REPO_ROOT / "deploy" / "alsa" / "conf.d" / "60-jts-ring.conf", tmp
    )

    ring_conf.render_ring_conf_wire(wire, conf_d=str(tmp))

    # The renderer put `channels` in the ACTIVE block and NOWHERE else...
    assert ring_conf.ring_conf_channels(ring_conf.RING_ACTIVE_CONF_PCM, str(tmp)) == 4
    assert ring_conf.ring_conf_channels(ring_conf.RING_A_CONF_PCM, str(tmp)) == 2
    assert ring_conf.ring_conf_channels(ring_conf.RING_B_CONF_PCM, str(tmp)) == 2
    # ...so the predicate must demand the capability that block now needs.
    assert ring_assets.RING_CAP_WIRE_CHANNELS in ring_assets.ring_wire_capabilities(
        wire
    )


# --- the three fail-closed shapes -------------------------------------------


def _install_plugin(tmp_path, content=b"\x7fELF-pretend-ioplug"):
    plugin_dir = tmp_path / "alsa-lib"
    plugin_dir.mkdir()
    (plugin_dir / ring_assets.RING_IOPLUG_SO).write_bytes(content)
    return plugin_dir


def _write_record(tmp_path, *, sha, caps):
    path = tmp_path / "ring-ioplug.provenance"
    path.write_text(
        f"# comment line\n"
        f"{ring_assets.RING_PROVENANCE_SHA_KEY}={sha}\n"
        f"{ring_assets.RING_PROVENANCE_CAPS_KEY}={caps}\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("record_caps", "same_so", "wire_kwargs", "expected_ok", "expected_detail"),
    [
        pytest.param(None, True, {}, False, ("no provenance record", "-EINVAL"), id="wide_wire_without_a_record_is_refused"),
        # The sha binds the claim to a binary: a record for a DIFFERENT .so
        # must not vouch for the one on disk (the degraded-deploy shape),
        # even though it claims the needed capability.
        pytest.param("wire_format,wire_channels", False, {}, False, ("STALE ioplug",), id="wide_wire_with_a_record_for_a_DIFFERENT_so_is_refused_as_stale"),
        pytest.param("wire_channels", True, {}, False, ("cannot parse [wire_format]",), id="wide_wire_with_a_matching_record_lacking_the_cap_is_refused"),
        pytest.param("wire_format,wire_channels", True, {"ring_b": 6}, True, (), id="wide_wire_with_a_matching_capable_record_is_allowed"),
    ],
)
def test_wide_wire_provenance_verdicts(
    record_caps, same_so, wire_kwargs, expected_ok, expected_detail, tmp_path
):
    plugin_dir = _install_plugin(tmp_path)
    if record_caps is None:
        provenance_path = str(tmp_path / "absent.provenance")
    else:
        sha = (
            ring_assets.ring_ioplug_so_sha256(plugin_dir=str(plugin_dir))
            if same_so
            else "0" * 64
        )
        provenance_path = str(_write_record(tmp_path, sha=sha, caps=record_caps))
    support = ring_assets.ring_ioplug_wire_supported(
        _wire(sample_format="S32_LE", **wire_kwargs),
        plugin_dir=str(plugin_dir),
        provenance_path=provenance_path,
    )
    assert support.ok is expected_ok, support.detail
    for substring in expected_detail:
        assert substring in support.detail


def test_a_record_without_a_sha_vouches_for_nothing(tmp_path):
    path = tmp_path / "ring-ioplug.provenance"
    path.write_text(f"{ring_assets.RING_PROVENANCE_CAPS_KEY}=wire_format\n")
    record = ring_assets.read_ring_ioplug_provenance(str(path))
    assert record.recorded is False
    assert record.caps == frozenset()


def test_reader_never_raises_on_garbage(tmp_path):
    path = tmp_path / "ring-ioplug.provenance"
    path.write_bytes(b"\x00\xff not= even ==text\n")
    assert ring_assets.read_ring_ioplug_provenance(str(path)).recorded is False
    assert ring_assets.read_ring_ioplug_provenance(str(tmp_path / "nope")).recorded is (
        False
    )


def test_sha_tracks_content(tmp_path):
    """Not a hashlib test — a proof the helper reads THIS file, not a cached one."""
    plugin_dir = _install_plugin(tmp_path, content=b"first")
    first = ring_assets.ring_ioplug_so_sha256(plugin_dir=str(plugin_dir))
    (plugin_dir / ring_assets.RING_IOPLUG_SO).write_bytes(b"second")
    second = ring_assets.ring_ioplug_so_sha256(plugin_dir=str(plugin_dir))
    assert first and second and first != second
    assert ring_assets.ring_ioplug_so_sha256(plugin_dir=str(tmp_path / "gone")) is None


# --- cross-language pins ----------------------------------------------------


def test_installer_and_python_agree_on_the_record_path_and_keys():
    """One spelling per fact, across the shell writer and the Python reader."""
    text = _sh_text()
    assert f'JTS_RING_IOPLUG_PROVENANCE:-{ring_assets.RING_IOPLUG_PROVENANCE}' in text
    assert f"{ring_assets.RING_PROVENANCE_SHA_KEY}=" in text
    assert f"{ring_assets.RING_PROVENANCE_CAPS_KEY}=" in text


def test_installer_emits_exactly_the_capability_tokens_python_knows():
    """A token the installer writes that Python cannot name is a silent refusal.

    The installer would record a capability, the gate would not find it in its
    needed-set vocabulary, and a wire needing it would be refused with a message
    listing capabilities that look present. Pin both directions.
    """
    text = _sh_text()
    for token in ring_assets.RING_IOPLUG_CAPS:
        assert f'caps+=("{token}")' in text, token
    # ...and no OTHER token is emitted.
    emitted = {
        line.split('caps+=("', 1)[1].split('"', 1)[0]
        for line in text.splitlines()
        if 'caps+=("' in line
    }
    assert emitted == set(ring_assets.RING_IOPLUG_CAPS)


@pytest.mark.parametrize("cap", sorted(_CAP_MARKERS))
def test_capability_markers_exist_in_the_c_source_they_prove(cap):
    """The installer greps the BUILT .so for these literals.

    They are diagnostic strings at the parse site of the conf.d field each one
    proves, so they land in the compiled binary exactly when that field is
    understood. If a ``SNDERR`` is reworded without updating the installer, a
    fully capable plugin records no capability and every wide wire is refused —
    a silent, confusing regression. This pins the literal to the C source; the
    installer-side spelling is pinned below.
    """
    if not _IOPLUG_C.exists():  # pragma: no cover - always present in repo
        pytest.skip(f"ioplug source not present: {_IOPLUG_C}")
    assert _CAP_MARKERS[cap] in _IOPLUG_C.read_text(encoding="utf-8")


@pytest.mark.parametrize("cap", sorted(_CAP_MARKERS))
def test_installer_greps_for_the_same_marker(cap):
    assert _CAP_MARKERS[cap] in _sh_text()


# With unreadable topology evidence, the doctor reports the record alone.
# The fixed program wire instead requires its format capability record.


def _doctor_env(monkeypatch, tmp_path, *, so_bytes=None, record=None):
    """Point the doctor's provenance check entirely inside ``tmp_path``.

    Returns the ``.so`` path. ``so_bytes=None`` leaves it absent; ``record=None``
    leaves the provenance file absent.
    """
    from jasper.cli.doctor import audio_runtime_ring as audio

    plugin_dir = tmp_path / "plugindir"
    plugin_dir.mkdir()
    monkeypatch.setattr(audio, "_JTS_RING_ALSA_PLUGIN_DIR", str(plugin_dir))
    provenance = tmp_path / "ring-ioplug.provenance"
    monkeypatch.setattr(ring_assets, "RING_IOPLUG_PROVENANCE", str(provenance))
    so_path = plugin_dir / "libasound_module_pcm_jts_ring.so"
    if so_bytes is not None:
        so_path.write_bytes(so_bytes)
    if record is not None:
        provenance.write_text(record, encoding="utf-8")
    return so_path


def _record_text(sha, caps=""):
    return (
        "# installer-written\n"
        f"{ring_assets.RING_PROVENANCE_SHA_KEY}={sha}\n"
        f"{ring_assets.RING_PROVENANCE_CAPS_KEY}={caps}\n"
    )


def _sha_of(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def test_provenance_check_skips_when_the_so_is_absent(monkeypatch, tmp_path):
    """ONE absent file, ONE reason. ``check_ring_platform_assets`` owns the
    missing-asset verdict; a second refusal here would bury the one that names
    the fix."""
    from jasper.cli.doctor import audio_runtime_ring as audio

    _doctor_env(monkeypatch, tmp_path)
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "skipped"
    assert res.reason == audio.REASON_RING_IOPLUG_ABSENT


def test_provenance_check_names_an_unvouched_plugin(
    monkeypatch, tmp_path, _wire_unavailable
):
    """Unavailable topology evidence leaves an absent record informational."""
    from jasper.cli.doctor import audio_runtime_ring as audio

    _doctor_env(monkeypatch, tmp_path, so_bytes=b"\x7fELF plugin")
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "ok"
    assert res.reason == audio.REASON_RING_IOPLUG_UNVOUCHED


def test_provenance_check_names_a_stale_installed_so(
    monkeypatch, tmp_path, _wire_unavailable
):
    """Unavailable topology evidence still distinguishes a stale installed plugin."""
    from jasper.cli.doctor import audio_runtime_ring as audio

    _doctor_env(
        monkeypatch,
        tmp_path,
        so_bytes=b"\x7fELF the plugin actually on disk",
        record=_record_text(_sha_of(b"\x7fELF a different plugin")),
    )
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "ok"
    assert res.reason == audio.REASON_RING_IOPLUG_STALE


def test_provenance_check_reports_the_caps_when_the_record_matches(
    monkeypatch, tmp_path
):
    """The vouched path. The capability list is the operationally useful part —
    it is what the reconciler's gate compares a wide wire against — so it is
    printed rather than reduced to 'ok'."""
    from jasper.cli.doctor import audio_runtime_ring as audio

    so_bytes = b"\x7fELF the real plugin"
    _doctor_env(
        monkeypatch,
        tmp_path,
        so_bytes=so_bytes,
        record=_record_text(
            _sha_of(so_bytes),
            f"{ring_assets.RING_CAP_WIRE_FORMAT},{ring_assets.RING_CAP_WIRE_CHANNELS}",
        ),
    )
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "ok"


def test_provenance_check_reports_a_vouched_plugin_with_no_capabilities(
    monkeypatch, tmp_path, _wire_unavailable
):
    """Unavailable topology evidence reports a matching record without claiming wire support."""
    from jasper.cli.doctor import audio_runtime_ring as audio

    so_bytes = b"\x7fELF an old but freshly-installed plugin"
    _doctor_env(
        monkeypatch,
        tmp_path,
        so_bytes=so_bytes,
        record=_record_text(_sha_of(so_bytes), ""),
    )
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "ok"


def _resolved_capabilities():
    from jasper.cli.doctor import audio_runtime_ring as audio

    return ring_assets.ring_wire_capabilities(audio._resolved_ring_wire())


def test_the_wire_is_resolved_through_the_arm_gates_own_two_calls(monkeypatch):
    """Doctor must pass the saved topology to the same resolver the arm gate uses."""
    from jasper.cli.doctor import audio_runtime_ring as audio

    topology = object()
    passed = []

    def _spy(arg=None):
        passed.append(arg)
        return "RESOLVED-WIRE"

    monkeypatch.setattr(
        "jasper.fanin.ring_readiness.load_topology_for_wire", lambda: topology
    )
    monkeypatch.setattr("jasper.fanin_coupling.resolve_ring_wire", _spy)
    assert audio._resolved_ring_wire() == "RESOLVED-WIRE"
    assert passed == [topology]


def test_an_undeclared_box_now_needs_the_capability_so_the_verdict_is_a_failure(
    monkeypatch, tmp_path, _wire_topology
):
    """The fixed wide wire requires a format capability record."""
    from jasper.cli.doctor import audio_runtime_ring as audio

    _doctor_env(monkeypatch, tmp_path, so_bytes=b"\x7fELF plugin")
    assert _resolved_capabilities() == {ring_assets.RING_CAP_WIRE_FORMAT}
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "fail"
    assert res.reason == audio.REASON_RING_IOPLUG_WIRE_UNSUPPORTED


def test_the_arm_gate_itself_refuses_an_undeclared_box_with_no_record(
    monkeypatch, tmp_path, _wire_topology
):
    """§10.4(13): the capability gate is LIVE, asserted at the gate, not the doctor.

    The doctor only reports what `ring_wire_caps_ready` will decide. This drives
    the decision itself, so "the flip promotes a dormant gate to load-bearing" is
    a tested property of the arm path rather than an inference from a check that
    quotes it.
    """
    import jasper.fanin.ring_readiness as rr
    from jasper.cli.doctor import audio_runtime_ring as audio

    so_path = _doctor_env(monkeypatch, tmp_path, so_bytes=b"\x7fELF plugin")
    monkeypatch.setattr(
        ring_assets, "RING_ALSA_PLUGIN_DIR", str(so_path.parent)
    )

    ok, detail = rr.ring_wire_caps_ready()

    assert ok is False
    assert "no provenance record" in detail
    # The doctor plumbs the gate's own verdict through, not a second one it
    # derives itself.
    doctor_res = audio.check_ring_ioplug_provenance()
    assert doctor_res.status == "fail"
    assert doctor_res.reason == audio.REASON_RING_IOPLUG_WIRE_UNSUPPORTED

    # A stale record is the other refusing shape, and it names a different fix.
    (tmp_path / "ring-ioplug.provenance").write_text(
        _record_text(_sha_of(b"\x7fELF a different plugin"), "wire_format"),
        encoding="utf-8",
    )
    ok, detail = rr.ring_wire_caps_ready()
    assert ok is False
    assert "STALE ioplug" in detail

    # And a record that vouches for THIS plugin with the capability admits it —
    # the positive control, so the two refusals above are not just "always False".
    (tmp_path / "ring-ioplug.provenance").write_text(
        _record_text(_sha_of(b"\x7fELF plugin"), ring_assets.RING_CAP_WIRE_FORMAT),
        encoding="utf-8",
    )
    ok, detail = rr.ring_wire_caps_ready()
    assert ok is True, detail


def test_a_declared_wide_wire_with_no_record_is_a_failure(
    monkeypatch, tmp_path, _wire_topology
):
    """A missing capability record refuses the fixed program wire."""
    from jasper.cli.doctor import audio_runtime_ring as audio

    _doctor_env(monkeypatch, tmp_path, so_bytes=b"\x7fELF plugin")
    assert _resolved_capabilities() == {ring_assets.RING_CAP_WIRE_FORMAT}
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "fail"
    assert res.reason == audio.REASON_RING_IOPLUG_WIRE_UNSUPPORTED


def test_a_declared_wide_wire_with_a_stale_record_is_a_failure(
    monkeypatch, tmp_path, _wire_topology
):
    from jasper.cli.doctor import audio_runtime_ring as audio

    _doctor_env(
        monkeypatch,
        tmp_path,
        so_bytes=b"\x7fELF the plugin actually on disk",
        record=_record_text(
            _sha_of(b"\x7fELF a different plugin"),
            ring_assets.RING_CAP_WIRE_FORMAT,
        ),
    )
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "fail"
    assert res.reason == audio.REASON_RING_IOPLUG_WIRE_UNSUPPORTED


def test_a_vouched_plugin_that_cannot_parse_the_wire_is_a_failure(
    monkeypatch, tmp_path, _wire_topology
):
    """The shape a severity keyed on the RECORD alone cannot see.

    This plugin is genuinely the one the last deploy installed — nothing is
    stale and nothing is unvouched — it simply predates the conf.d field the
    declared wire renders. The record-compare branches all pass it, so before
    the wire was consulted this box read `ok` while its arm was refused.
    """
    from jasper.cli.doctor import audio_runtime_ring as audio

    so_bytes = b"\x7fELF an old but freshly-installed plugin"
    _doctor_env(
        monkeypatch,
        tmp_path,
        so_bytes=so_bytes,
        record=_record_text(_sha_of(so_bytes), ring_assets.RING_CAP_WIRE_CHANNELS),
    )
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "fail"
    assert res.reason == audio.REASON_RING_IOPLUG_WIRE_UNSUPPORTED


def test_a_declared_wide_wire_the_record_covers_is_ok(
    monkeypatch, tmp_path, _wire_topology
):
    """The armed wide box (jts.local's shape): the escalation must not fire on
    a plugin whose record vouches for exactly this wire."""
    from jasper.cli.doctor import audio_runtime_ring as audio

    so_bytes = b"\x7fELF the real plugin"
    _doctor_env(
        monkeypatch,
        tmp_path,
        so_bytes=so_bytes,
        record=_record_text(_sha_of(so_bytes), ring_assets.RING_CAP_WIRE_FORMAT),
    )
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "ok"


def test_an_absent_so_still_defers_even_when_the_wire_is_wide(
    monkeypatch, tmp_path, _wire_topology
):
    """The missing-asset deferral stays ahead of the wire escalation: one absent
    file must not also produce a capability verdict about the file that is not
    there."""
    from jasper.cli.doctor import audio_runtime_ring as audio

    _doctor_env(monkeypatch, tmp_path)
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "skipped"
    assert res.reason == audio.REASON_RING_IOPLUG_ABSENT


def test_the_build_failure_warn_hands_off_to_the_check_by_its_real_name(
    monkeypatch, tmp_path
):
    """One fact, one spelling, across the installer and the doctor.

    A failed build's transcript scrolls away, so the WARN's job is to name the
    surface that outlives it. Pinning the installer's string against the label
    the check actually reports keeps a rename from sending an operator to a
    heading `jasper-doctor` no longer prints.

    AND THE AXIS IT CLAIMS. The WARN tells the operator WHICH boxes the doctor
    will call a `fail`, and that claim is only as narrow as the predicate: the
    escalation fires on the SAMPLE FORMAT axis (plus the channel axes), not on
    "the wire" or "the geometry" generally. An earlier form over-claimed, and
    nothing pinned the correction — so the wording is asserted here rather than
    left to survive on care.
    """
    from jasper.cli.doctor import audio_runtime_ring as audio

    _doctor_env(monkeypatch, tmp_path, so_bytes=b"\x7fELF plugin")
    sh = _sh_text()
    assert f"'{audio.check_ring_ioplug_provenance().name}'" in sh
    assert "non-default ring sample format" in sh, (
        "the ioplug-build WARN stopped naming the FORMAT axis its verdict is "
        "keyed on; a broader claim over-promises what the capability gate weighs"
    )


@pytest.fixture
def _wire_topology(monkeypatch):
    monkeypatch.setattr("jasper.fanin.ring_readiness.load_topology_for_wire", lambda: None)


@pytest.fixture
def _wire_unavailable(monkeypatch):
    def unreadable():
        raise OSError("topology unavailable")

    monkeypatch.setattr(
        "jasper.cli.doctor.audio_runtime_ring.evidence.saved_topology_for_wire", unreadable
    )
