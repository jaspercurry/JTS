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
# remedies), and a match reports the capability set. `warn` rather than `fail`
# for the two unvouched shapes is itself a decision — the shipped wire opens on
# a stale plugin, so the record matters at the moment a box's wire needs a
# capability, which is where the coupling reconciler's gate fails closed.


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
