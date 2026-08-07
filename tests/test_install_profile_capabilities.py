# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The install-tier capability axis: registry shape, purity, no drift.

``jasper.install_profile`` names what a tier GRANTS on its own axis —
``Capability.ASSISTANT`` and ``Capability.WAKE_DETECTION`` — with a
pure-data grant table (``PROFILE_CAPABILITIES``) and one predicate
(``install_profile_has_capability``).

Three things are pinned here:

1. **Registry shape.** Every profile has a row; every row grants only
   real capabilities. A tier cannot be added without stating its grants,
   and a typo cannot invent a capability nothing checks.
2. **Purity.** ``system_capabilities_for_profile`` is a pure function of
   its argument — no environment, no files, no hardware. Two consumers
   compute it at DIFFERENT times (install.sh bakes it into the static
   landing page; jasper-control serves it live at /system) and agree only
   because of this. A capability that read something dynamic would let a
   baked page be wrong at runtime, and the landing page's
   ``applyCapabilities`` fails closed — a section hidden forever, no error.
3. **Behaviour neutrality of the PR that introduced the axis.** The
   capability map is byte-identical to its pre-axis values except for the
   one added ``wake_detection`` key. Golden values were captured by
   running the pre-PR module, not transcribed by hand.
"""
from __future__ import annotations

import builtins
import contextlib
import io
import os
import pathlib
import socket
import subprocess
from unittest import mock

import pytest

from jasper.install_profile import (
    PROFILE_CAPABILITIES,
    VALID_INSTALL_PROFILES,
    Capability,
    install_profile_allows_voice_brain,
    install_profile_has_capability,
    install_profile_supports_wake_detection,
    system_capabilities_for_profile,
)


# ---------- (1) registry shape --------------------------------------------


def test_every_install_profile_has_a_capability_row():
    """A new tier cannot ship without stating its grants.

    Direct indexing in ``install_profile_has_capability`` means a missing
    row raises KeyError at runtime rather than silently granting nothing;
    this test is what stops that from ever reaching a box.
    """
    assert set(PROFILE_CAPABILITIES) == set(VALID_INSTALL_PROFILES)


def test_granted_capabilities_are_all_real_capabilities():
    """A typo cannot silently create a capability nothing grants."""
    known = set(Capability)
    for profile, granted in PROFILE_CAPABILITIES.items():
        assert isinstance(granted, frozenset), profile
        assert granted <= known, f"{profile}: unknown capabilities {granted - known}"


def test_capability_values_are_stable_snake_case_tokens():
    """The enum values reach logs and the /system JSON; keep them stable."""
    assert Capability.ASSISTANT.value == "assistant"
    assert Capability.WAKE_DETECTION.value == "wake_detection"


def test_legacy_tokens_read_streambox_grants():
    """A field box with an endpoint/satellite marker reads its REAL grants."""
    for token in ("endpoint", "satellite"):
        for capability in Capability:
            assert install_profile_has_capability(
                token, capability,
            ) == install_profile_has_capability("streambox", capability), token


def test_unset_profile_reads_full_grants():
    """Absent marker + absent env means full, as everywhere else here."""
    for capability in Capability:
        assert install_profile_has_capability(None, capability) is (
            capability in PROFILE_CAPABILITIES["full"]
        )
        assert install_profile_has_capability("", capability) is (
            capability in PROFILE_CAPABILITIES["full"]
        )


def test_invalid_profile_raises_rather_than_granting_nothing():
    with pytest.raises(ValueError, match="invalid install profile"):
        install_profile_has_capability("bogus", Capability.ASSISTANT)


def test_named_predicates_agree_with_the_registry():
    """The convenience predicates are thin views on the same table."""
    for profile in ("full", "streambox", "endpoint", None):
        assert install_profile_allows_voice_brain(profile) is (
            install_profile_has_capability(profile, Capability.ASSISTANT)
        )
        assert install_profile_supports_wake_detection(profile) is (
            install_profile_has_capability(profile, Capability.WAKE_DETECTION)
        )


# ---------- (2) purity ----------------------------------------------------


class _ForbiddenEnviron(dict):
    """An os.environ stand-in that screams instead of answering."""

    def __getitem__(self, key):  # pragma: no cover - the raise IS the point
        raise AssertionError(f"capability map read os.environ[{key!r}]")

    def get(self, key, default=None):  # pragma: no cover - same
        raise AssertionError(f"capability map read os.environ.get({key!r})")


@contextlib.contextmanager
def _no_io():
    """Make every route out of the process raise, around ONE call.

    Not a stylistic preference — the two consumers of the capability map
    run at different times on different machines (install-time bake vs
    runtime snapshot). Anything the map reads besides its argument can
    differ between those two moments, and the disagreement is SILENT.

    Deliberately a context manager rather than a fixture. Replacing the
    global ``os.environ`` object for a whole test item also poisons
    pytest's own between-test bookkeeping: tests/conftest.py's autouse
    ``_isolate_environ`` snapshots the environment and, at teardown,
    calls ``os.environ.get(k)`` for every saved key. That teardown ran
    while a fixture-scoped patch was still installed and turned all four
    parametrizations into teardown ERRORs. The key named in the failure
    is just whichever the environment happens to yield first — unsetting
    it only moves the failure to the next one — so it is not a
    macOS-only artifact and would fail on CI too. Scoping the patch to
    the call under test keeps the guard exactly as strong while keeping
    it off code that is not under test.
    """

    def forbid(what):
        def _raise(*_a, **_kw):  # pragma: no cover - the raise IS the point
            raise AssertionError(f"capability map performed I/O: {what}")

        return _raise

    targets = [
        # Path.read_text/read_bytes go through Path.open -> io.open, NOT
        # builtins.open, so both need covering.
        (io, "open", forbid("io.open")),
        (builtins, "open", forbid("builtins.open")),
        (socket, "socket", forbid("socket.socket")),
    ]
    targets += [
        (pathlib.Path, name, forbid(f"Path.{name}"))
        for name in ("read_text", "read_bytes", "open", "exists", "is_file", "iterdir")
    ]
    targets += [
        (subprocess, name, forbid(f"subprocess.{name}"))
        for name in ("run", "Popen", "check_output", "check_call")
    ]
    # Entered LAST so ExitStack's LIFO unwind restores it FIRST — the
    # poisoned environ is live for the smallest possible window.
    targets.append((os, "environ", _ForbiddenEnviron()))

    with contextlib.ExitStack() as stack:
        for target, name, replacement in targets:
            stack.enter_context(mock.patch.object(target, name, replacement))
        yield


@pytest.mark.parametrize("profile", ["full", "streambox", "endpoint", None])
def test_capability_map_touches_nothing_outside_its_argument(profile):
    """No env, no files, no subprocesses, no sockets. At all."""
    with _no_io():
        capabilities = system_capabilities_for_profile(profile)

    assert capabilities


@pytest.mark.parametrize("profile", ["full", "streambox"])
def test_capability_map_ignores_a_contradicting_environment(profile, monkeypatch):
    """The map answers about its ARGUMENT, never about the ambient box.

    install.sh resolves the profile once and passes it in; if the map
    also consulted JASPER_INSTALL_PROFILE (or any other ambient signal)
    it could bake one answer and serve another.
    """
    baseline = system_capabilities_for_profile(profile)

    other = "streambox" if profile == "full" else "full"
    monkeypatch.setenv("JASPER_INSTALL_PROFILE", other)
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("JASPER_MIC_DEVICE", "udp:9876")
    monkeypatch.setenv("JASPER_AUDIO_INPUT_PROFILE", "xvf_chip_aec")

    assert system_capabilities_for_profile(profile) == baseline


def test_capability_map_ignores_the_persisted_marker(monkeypatch, tmp_path):
    """Pointing the marker at the other tier must not move the answer."""
    baseline = system_capabilities_for_profile("full")

    marker = tmp_path / "install_profile"
    marker.write_text("streambox\n", encoding="utf-8")
    monkeypatch.setattr(
        "jasper.install_profile.INSTALL_PROFILE_FILE", marker,
    )

    assert system_capabilities_for_profile("full") == baseline


def test_capability_map_is_deterministic_across_calls():
    for profile in ("full", "streambox", "endpoint", None):
        assert system_capabilities_for_profile(profile) == (
            system_capabilities_for_profile(profile)
        )


# ---------- (3) behaviour neutrality: the golden map ----------------------

# Captured by running jasper.install_profile.system_capabilities_for_profile
# from the commit BEFORE the capability axis landed (git archive of
# origin/main into a scratch tree, then json.dumps(..., sort_keys=True)).
# Not transcribed from a design doc — the point is to compare against what
# the code actually produced.
_PRE_AXIS_CAPABILITIES = {
    "full": {
        "audio_quality": True,
        "content_dsp": True,
        "developer_tools": True,
        "diagnostics": True,
        "install_profile": "full",
        "local_sources": True,
        "network_settings": True,
        "pair_management": True,
        "poweroff": True,
        "reboot": True,
        "restart_audio": True,
        "restart_voice": True,
        "role": "full",
        "speaker_settings": True,
        "voice_brain": True,
    },
    "streambox": {
        "audio_quality": True,
        "content_dsp": True,
        "developer_tools": False,
        "diagnostics": True,
        "install_profile": "streambox",
        "local_sources": True,
        "network_settings": True,
        "pair_management": True,
        "poweroff": True,
        "reboot": True,
        "restart_audio": True,
        "restart_voice": False,
        "role": "streambox",
        "speaker_settings": True,
        "voice_brain": False,
    },
}

# The ONLY key the capability axis was allowed to add.
_ADDED_KEYS = {"wake_detection"}


@pytest.mark.parametrize("profile", sorted(_PRE_AXIS_CAPABILITIES))
def test_capability_map_is_unchanged_except_for_the_added_key(profile):
    """Naming the axis changed no profile's effective capabilities.

    Re-expressing voice_brain on top of Capability.ASSISTANT is a
    refactor; the landing page and /system must see the same answers they
    saw before, plus exactly one new key.
    """
    golden = _PRE_AXIS_CAPABILITIES[profile]
    live = system_capabilities_for_profile(profile)

    assert set(live) - set(golden) == _ADDED_KEYS
    assert not set(golden) - set(live), "a capability key disappeared"
    for key, value in golden.items():
        assert live[key] == value, key
        assert type(live[key]) is type(value), key


def test_wake_detection_key_tracks_the_capability():
    """The new key is the capability, not a second place to state it."""
    for profile in ("full", "streambox", "endpoint", None):
        assert system_capabilities_for_profile(profile)["wake_detection"] is (
            install_profile_supports_wake_detection(profile)
        )


def test_both_tiers_keep_wake_and_assistant_welded_for_now():
    """Behaviour neutrality, stated as data.

    Naming the axis does NOT split any tier's grants yet: full has both,
    streambox has neither. Granting streambox ASSISTANT without
    WAKE_DETECTION — the Bluetooth-remote case this axis exists for — is
    a separate, deliberate change. When it lands, this test SHOULD fail;
    update it then, and only then.
    """
    assert PROFILE_CAPABILITIES["full"] == frozenset(
        {Capability.ASSISTANT, Capability.WAKE_DETECTION},
    )
    assert PROFILE_CAPABILITIES["streambox"] == frozenset()
