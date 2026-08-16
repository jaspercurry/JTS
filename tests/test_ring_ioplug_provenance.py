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

* the Python reader / capability gate (``jasper.ring_assets``), including the
  dormancy that keeps every box on the shipped wire from consulting the record
  at all; and
* the cross-language pins — the record path, its key names, the capability
  tokens, and the marker strings the installer greps for — against
  ``deploy/lib/install/ring-platform.sh`` and the C source those markers come
  from. A reworded ``SNDERR`` would otherwise silently turn a capable plugin
  into an uncapable-looking one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jasper import ring_assets
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
}


def _wire(sample_format="S16_LE", ring_a=2, ring_b=2) -> RingWire:
    return RingWire(
        sample_format=sample_format,
        ring_a_channels=ring_a,
        ring_b_channels=ring_b,
        period_frames=128,
    )


def _sh_text() -> str:
    if not _RING_PLATFORM_SH.exists():  # pragma: no cover - always present in repo
        pytest.skip(f"installer not present: {_RING_PLATFORM_SH}")
    return _RING_PLATFORM_SH.read_text(encoding="utf-8")


# --- the shipped-wire dormancy that makes this whole layer inert today -------


def test_shipped_wire_needs_no_capability():
    """The shipped wire renders no conf.d field beyond the ioplug's defaults.

    This is the entire dormancy argument: the capability a wire needs IS the set
    of keys it forces onto the conf.d, and the shipped wire forces none. Every
    box in the fleet is on that wire, so none of them consults a record.
    """
    assert ring_assets.ring_wire_capabilities(_wire()) == frozenset()


def test_shipped_wire_is_supported_without_any_record(tmp_path):
    """No record + no plugin on disk still passes on the shipped wire.

    The short-circuit must happen BEFORE the record is read and before the
    plugin is hashed, or every box would refuse to arm until its next deploy.
    Pointing both at paths that do not exist is how this test proves neither was
    consulted rather than asserting it in prose.
    """
    support = ring_assets.ring_ioplug_wire_supported(
        _wire(),
        plugin_dir=str(tmp_path / "nonexistent"),
        provenance_path=str(tmp_path / "nonexistent.provenance"),
    )
    assert support.ok is True
    assert support.needed == frozenset()


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        (_wire(sample_format="S32_LE"), {ring_assets.RING_CAP_WIRE_FORMAT}),
        (_wire(ring_b=6), {ring_assets.RING_CAP_WIRE_CHANNELS}),
        (_wire(ring_a=4), {ring_assets.RING_CAP_WIRE_CHANNELS}),
        (
            _wire(sample_format="S32_LE", ring_b=8),
            {
                ring_assets.RING_CAP_WIRE_FORMAT,
                ring_assets.RING_CAP_WIRE_CHANNELS,
            },
        ),
    ],
)
def test_off_default_wires_need_the_matching_capability(wire, expected):
    assert ring_assets.ring_wire_capabilities(wire) == frozenset(expected)


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


def test_wide_wire_without_a_record_is_refused(tmp_path):
    plugin_dir = _install_plugin(tmp_path)
    support = ring_assets.ring_ioplug_wire_supported(
        _wire(sample_format="S32_LE"),
        plugin_dir=str(plugin_dir),
        provenance_path=str(tmp_path / "absent.provenance"),
    )
    assert support.ok is False
    assert "no provenance record" in support.detail
    assert "-EINVAL" in support.detail


def test_wide_wire_with_a_record_for_a_DIFFERENT_so_is_refused_as_stale(tmp_path):
    """The sha is what binds the claim to a binary.

    A record describing some other ``.so`` must not vouch for the one on disk —
    that is the whole degraded-deploy shape, where the previous plugin survives a
    failed rebuild. The record here even CLAIMS the needed capability, so a
    caps-only check would pass it.
    """
    plugin_dir = _install_plugin(tmp_path)
    record = _write_record(tmp_path, sha="0" * 64, caps="wire_format,wire_channels")
    support = ring_assets.ring_ioplug_wire_supported(
        _wire(sample_format="S32_LE"),
        plugin_dir=str(plugin_dir),
        provenance_path=str(record),
    )
    assert support.ok is False
    assert "STALE ioplug" in support.detail


def test_wide_wire_with_a_matching_record_lacking_the_cap_is_refused(tmp_path):
    plugin_dir = _install_plugin(tmp_path)
    sha = ring_assets.ring_ioplug_so_sha256(plugin_dir=str(plugin_dir))
    record = _write_record(tmp_path, sha=sha, caps="wire_channels")
    support = ring_assets.ring_ioplug_wire_supported(
        _wire(sample_format="S32_LE"),
        plugin_dir=str(plugin_dir),
        provenance_path=str(record),
    )
    assert support.ok is False
    assert "cannot parse [wire_format]" in support.detail


def test_wide_wire_with_a_matching_capable_record_is_allowed(tmp_path):
    plugin_dir = _install_plugin(tmp_path)
    sha = ring_assets.ring_ioplug_so_sha256(plugin_dir=str(plugin_dir))
    record = _write_record(tmp_path, sha=sha, caps="wire_format,wire_channels")
    support = ring_assets.ring_ioplug_wire_supported(
        _wire(sample_format="S32_LE", ring_b=6),
        plugin_dir=str(plugin_dir),
        provenance_path=str(record),
    )
    assert support.ok is True, support.detail


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


def test_every_non_producing_install_path_revokes_the_record():
    """A deploy that did not build the plugin must not keep vouching for it.

    Four paths reach the end of the ioplug install without this deploy having
    produced the file at ``so_dest``: the build failing, ``make plugin``
    finishing without an artifact, the source directory being absent, and the
    first-party bundle path finding no installed plugin. Each must revoke, or a
    prior deploy's record silently continues to describe a binary this one did
    not make — which is exactly the stale-ioplug hole the record exists to
    close.
    """
    text = _sh_text()
    assert text.count("revoke_ring_ioplug_provenance") >= 5  # 4 call sites + the def
    assert "rm -f \"${JTS_RING_IOPLUG_PROVENANCE}\"" in text


# --- the doctor check, branch by branch -------------------------------------
#
# `check_ring_ioplug_provenance` is the standing surface for "is the plugin on
# disk the one the installer built". It has four verdicts and each is a distinct
# operator instruction, so each is pinned: an absent .so defers to the
# missing-asset check, no record and a sha mismatch both WARN (with different
# remedies), and a match reports the capability set.
#
# WHICH WEIGHT a verdict carries is decided by the box's own wire, and the tests
# below pin both halves. On a wire that renders no conf.d field beyond the
# ioplug's own defaults an unvouched plugin costs that box nothing, so `warn`
# is the honest weight. On a wire declaring a non-default sample FORMAT the SAME
# record state makes `ring_wire_caps_ready` refuse the arm — loopback on a flat
# box, a parked content lane on a roleful one — so it is a `fail`.
#
# The format axis is what these exercise. `ring_wire_capabilities` also reads
# the Ring A/B channel counts, while the ACTIVE block's own `channels` key is
# outside the predicate — an axis tracked with the ring-wire default flip, not
# something these pins claim coverage of.


def _doctor_env(monkeypatch, tmp_path, *, so_bytes=None, record=None):
    """Point the doctor's provenance check entirely inside ``tmp_path``.

    Returns the ``.so`` path. ``so_bytes=None`` leaves it absent; ``record=None``
    leaves the provenance file absent.
    """
    from jasper.cli.doctor import audio_runtime as audio

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
    from jasper.cli.doctor import audio_runtime as audio

    _doctor_env(monkeypatch, tmp_path)
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "ok"
    assert "skipped" in res.detail
    assert "ring platform" in res.detail


def test_provenance_check_warns_when_the_plugin_is_unvouched(monkeypatch, tmp_path):
    """Installed but no record — the shape a REVOKING deploy leaves behind, and
    also the shape of every box that predates the recording. The detail must
    cover both readings and name the redeploy."""
    from jasper.cli.doctor import audio_runtime as audio

    _doctor_env(monkeypatch, tmp_path, so_bytes=b"\x7fELF plugin")
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "warn"
    assert "UNVOUCHED" in res.detail
    assert "a deploy revoked the record" in res.detail
    assert "scripts/deploy-to-pi.sh" in res.detail


def test_provenance_check_warns_when_the_installed_so_is_stale(monkeypatch, tmp_path):
    """THE HOLE THIS CHECK CLOSES. The build degrades to a WARN, so a failed
    rebuild leaves the PREVIOUS .so beside new daemons — structurally valid, so
    the presence check and the open-probe both pass. The sha is what separates
    it from a fresh build."""
    from jasper.cli.doctor import audio_runtime as audio

    _doctor_env(
        monkeypatch,
        tmp_path,
        so_bytes=b"\x7fELF the plugin actually on disk",
        record=_record_text(_sha_of(b"\x7fELF a different plugin")),
    )
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "warn"
    assert "STALE ioplug" in res.detail
    assert "NOT the one" in res.detail


def test_provenance_check_reports_the_caps_when_the_record_matches(
    monkeypatch, tmp_path
):
    """The vouched path. The capability list is the operationally useful part —
    it is what the reconciler's gate compares a wide wire against — so it is
    printed rather than reduced to 'ok'."""
    from jasper.cli.doctor import audio_runtime as audio

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
    assert "matches the installer's record" in res.detail
    assert ring_assets.RING_CAP_WIRE_FORMAT in res.detail
    assert ring_assets.RING_CAP_WIRE_CHANNELS in res.detail


def test_provenance_check_reports_a_vouched_plugin_with_no_capabilities(
    monkeypatch, tmp_path
):
    """A pre-ring-v2 plugin THIS deploy built is vouched and capability-less.

    Those are two independent facts and the check must not collapse them: the
    plugin is genuinely the one installed (so not stale), and it parses no
    conf.d field (so a wide wire is still refused, by the reconciler's gate).
    """
    from jasper.cli.doctor import audio_runtime as audio

    so_bytes = b"\x7fELF an old but freshly-installed plugin"
    _doctor_env(
        monkeypatch,
        tmp_path,
        so_bytes=so_bytes,
        record=_record_text(_sha_of(so_bytes), ""),
    )
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "ok"
    assert "[none]" in res.detail


# --- the wire-weighted verdict ----------------------------------------------
#
# The record only costs a box something when the wire it resolves renders a
# conf.d field the plugin must parse. These pin that the check reports the
# decision `ring_wire_caps_ready` will actually make, rather than a fixed
# severity that is either alarmist on a narrow box or silent on a wide one.


@pytest.fixture
def _declared_wire(tmp_path, monkeypatch):
    """Declare the box's ring wire the way a real box does, and only that.

    Two things are held still so the assertions are about the wire and nothing
    else. The env chain is isolated because ``resolve_ring_wire`` reads the
    developer host's real ``jasper.env`` -> ``fanin.env`` pair otherwise, and
    the saved output topology is stubbed to absent because a host that ever ran
    the installer carries one whose channel axes would reach these tests. The
    FORMAT axis — the one a declaration moves — stays fully real.
    """
    from jasper.fanin_coupling import RING_WIRE_FORMAT_ENV_VAR

    from tests.test_fanin_coupling_reconcile import isolate_base_jasper_env

    isolate_base_jasper_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.load_topology_for_wire", lambda: None
    )

    def declare(value: str | None) -> None:
        import jasper.fanin.coupling_reconcile as cr

        text = "" if value is None else f"{RING_WIRE_FORMAT_ENV_VAR}={value}\n"
        Path(cr.FANIN_ENV_PATH).write_text(text, encoding="utf-8")

    return declare


def _resolved_capabilities():
    from jasper.cli.doctor import audio_runtime as audio

    return ring_assets.ring_wire_capabilities(audio._resolved_ring_wire())


def test_the_wire_is_resolved_through_the_arm_gates_own_two_calls(monkeypatch):
    """The doctor must ask the question the way the gate asks it.

    `ring_wire_caps_ready` resolves its wire as
    `resolve_wire_for_gate(load_topology_for_wire())`. Dropping the topology
    argument does not raise and does not return nothing — `resolve_ring_wire`
    answers the shipped stereo geometry for `None` — so the two spellings agree
    on every box whose topology happens to be stereo and diverge silently on
    exactly the roleful ones this verdict matters most for. Identity of the
    object handed across is therefore the assertion, not the shape of the call.
    """
    from jasper.cli.doctor import audio_runtime as audio

    topology = object()
    passed = []

    def _spy(arg=None):
        passed.append(arg)
        return "RESOLVED-WIRE", ""

    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.load_topology_for_wire", lambda: topology
    )
    monkeypatch.setattr("jasper.fanin.coupling_reconcile.resolve_wire_for_gate", _spy)
    assert audio._resolved_ring_wire() == "RESOLVED-WIRE"
    assert passed == [topology]


def test_an_undeclared_box_needs_no_capability_so_the_verdict_stays_warn(
    monkeypatch, tmp_path, _declared_wire
):
    """The inertness half, and the tripwire for the ring-wire default flip.

    A box that declares nothing resolves the conf.d default wire, which forces
    no field onto the conf.d, so an unvouched plugin still opens its ring and
    `warn` is the whole truth. Both halves are asserted together on purpose:
    when the resolver's default goes wide, the capability set stops being empty
    and this verdict becomes `fail` on every box carrying no record — which is
    the fleet cost of that flip, and it should surface as a failing pin rather
    than as a silent disarm.
    """
    from jasper.cli.doctor import audio_runtime as audio

    _declared_wire(None)
    _doctor_env(monkeypatch, tmp_path, so_bytes=b"\x7fELF plugin")
    assert _resolved_capabilities() == frozenset()
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "warn"
    assert "UNVOUCHED" in res.detail


def test_a_declared_wide_wire_with_no_record_is_a_failure(
    monkeypatch, tmp_path, _declared_wire
):
    """The refusing class of the wire flip, seen from the doctor.

    Same box, same absent record as the `warn` case above — only the
    declaration differs. The arm is refused from here on, so the check must say
    so with the gate's own sentence plus the command that fixes it.
    """
    from jasper.cli.doctor import audio_runtime as audio

    _declared_wire("S32_LE")
    _doctor_env(monkeypatch, tmp_path, so_bytes=b"\x7fELF plugin")
    assert _resolved_capabilities() == {ring_assets.RING_CAP_WIRE_FORMAT}
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "fail"
    assert "REFUSED" in res.detail
    assert "no provenance record" in res.detail
    assert "scripts/deploy-to-pi.sh" in res.detail


def test_a_declared_wide_wire_with_a_stale_record_is_a_failure(
    monkeypatch, tmp_path, _declared_wire
):
    from jasper.cli.doctor import audio_runtime as audio

    _declared_wire("S32_LE")
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
    assert "STALE ioplug" in res.detail


def test_a_vouched_plugin_that_cannot_parse_the_wire_is_a_failure(
    monkeypatch, tmp_path, _declared_wire
):
    """The shape a severity keyed on the RECORD alone cannot see.

    This plugin is genuinely the one the last deploy installed — nothing is
    stale and nothing is unvouched — it simply predates the conf.d field the
    declared wire renders. The record-compare branches all pass it, so before
    the wire was consulted this box read `ok` while its arm was refused.
    """
    from jasper.cli.doctor import audio_runtime as audio

    so_bytes = b"\x7fELF an old but freshly-installed plugin"
    _declared_wire("S32_LE")
    _doctor_env(
        monkeypatch,
        tmp_path,
        so_bytes=so_bytes,
        record=_record_text(_sha_of(so_bytes), ring_assets.RING_CAP_WIRE_CHANNELS),
    )
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "fail"
    assert "cannot parse [wire_format]" in res.detail


def test_a_declared_wide_wire_the_record_covers_is_ok(
    monkeypatch, tmp_path, _declared_wire
):
    """The armed wide box (jts.local's shape): the escalation must not fire on
    a plugin whose record vouches for exactly this wire."""
    from jasper.cli.doctor import audio_runtime as audio

    so_bytes = b"\x7fELF the real plugin"
    _declared_wire("S32_LE")
    _doctor_env(
        monkeypatch,
        tmp_path,
        so_bytes=so_bytes,
        record=_record_text(_sha_of(so_bytes), ring_assets.RING_CAP_WIRE_FORMAT),
    )
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "ok"
    assert "matches the installer's record" in res.detail


def test_an_illegal_wire_declaration_is_not_reported_as_a_provenance_fault(
    monkeypatch, tmp_path, _declared_wire
):
    """ONE failure, ONE reason. A wire neither language recognizes already
    refuses the arm through ``resolve_wire_for_gate``, with the parser's own
    sentence. Restating that here would give the operator two remedies for one
    fault, so the check falls back to weighing the record alone."""
    from jasper.cli.doctor import audio_runtime as audio

    _declared_wire("S24_3LE")
    _doctor_env(monkeypatch, tmp_path, so_bytes=b"\x7fELF plugin")
    assert audio._resolved_ring_wire() is None
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "warn"
    assert "UNVOUCHED" in res.detail


def test_an_absent_so_still_defers_even_when_the_wire_is_wide(
    monkeypatch, tmp_path, _declared_wire
):
    """The missing-asset deferral stays ahead of the wire escalation: one absent
    file must not also produce a capability verdict about the file that is not
    there."""
    from jasper.cli.doctor import audio_runtime as audio

    _declared_wire("S32_LE")
    _doctor_env(monkeypatch, tmp_path)
    res = audio.check_ring_ioplug_provenance()
    assert res.status == "ok"
    assert "skipped" in res.detail


def test_the_build_failure_warn_hands_off_to_the_check_by_its_real_name(
    monkeypatch, tmp_path
):
    """One fact, one spelling, across the installer and the doctor.

    A failed build's transcript scrolls away, so the WARN's job is to name the
    surface that outlives it. Pinning the installer's string against the label
    the check actually reports keeps a rename from sending an operator to a
    heading `jasper-doctor` no longer prints.
    """
    from jasper.cli.doctor import audio_runtime as audio

    _doctor_env(monkeypatch, tmp_path, so_bytes=b"\x7fELF plugin")
    assert f"'{audio.check_ring_ioplug_provenance().name}'" in _sh_text()
