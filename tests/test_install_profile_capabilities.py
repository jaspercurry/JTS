# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The install-tier capability axis: registry shape, purity, no drift.

``jasper.install_profile`` names what a tier GRANTS on its own axis —
``Capability.WAKE_DETECTION`` — with a pure-data grant table
(``PROFILE_CAPABILITIES``) and one predicate
(``install_profile_has_capability``).

Three things are pinned here:

1. **Registry shape.** Every profile has a row; every row grants only
   real capabilities. A tier cannot be added without stating its grants,
   and a typo cannot invent a capability nothing checks.
2. **Purity.** ``system_capabilities_for_profile`` is a pure function of
   its argument — no environment, no files, no hardware. install.sh bakes
   it into the static landing page and hubs once; a capability that read
   something dynamic would freeze its install-time answer into the page,
   and ``initSettingsStatus`` fails closed — a section hidden forever, no
   error.
3. **The baked map, pinned.** A golden map per profile fails loudly the
   day what the pages gate on drifts.
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
    install_profile_has_capability,
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


def test_invalid_profile_raises_rather_than_granting_nothing():
    with pytest.raises(ValueError, match="invalid install profile"):
        install_profile_has_capability("bogus", Capability.WAKE_DETECTION)


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

    Not a stylistic preference — the map is baked into the pages at
    install time. Anything it reads besides its argument can differ from
    the running box, and the disagreement is SILENT.

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
    it could bake an answer the profile does not give.
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


# ---------- (3) the baked map, pinned -------------------------------------

_FULL = {"wake_detection": True}
_STREAMBOX = {"wake_detection": False}


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("full", _FULL), ("streambox", _STREAMBOX), ("endpoint", _STREAMBOX),
        ("satellite", _STREAMBOX), (None, _FULL), ("", _FULL),
    ],
)
def test_capability_map_is_pinned(profile, expected):
    """The baked pages must see exactly these answers, as JSON booleans:
    they gate on ``=== true``."""
    live = system_capabilities_for_profile(profile)

    assert live == expected
    assert {type(value) for value in live.values()} == {bool}


def test_only_the_full_tier_grants_wake_detection():
    """See docs/adr/0363-the-assistant-is-on-every-tier-and-wake-detection-is-the-only-tier-capability.md."""
    assert PROFILE_CAPABILITIES == {
        "full": frozenset({Capability.WAKE_DETECTION}),
        "streambox": frozenset(),
    }
