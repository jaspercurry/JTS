# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Install-profile helpers shared by deploy/runtime code.

Small Pi installs have two product axes:

* install profile — full speaker or streambox local-renderer
* output topology — full-range or future active-crossover

There are exactly TWO install profiles: ``full`` and ``streambox``. The
former third tier (``endpoint`` / ``satellite``) is GONE as an install
tier — "endpoint behavior" is now purely the multiroom *follower*
grouping role at runtime (a full/streambox box bonded as a follower
parks its brain, then hands source parking to the canonical source coordinator;
see jasper.multiroom.reconcile and jasper.source_intent). The legacy
tokens are still ACCEPTED here and mapped to ``streambox`` so a field box
with a persisted ``endpoint``/``satellite`` marker auto-migrates on its
next deploy instead of stranding.

What a tier *grants* is named on its own axis: ``Capability``, with the
per-profile grant table in ``PROFILE_CAPABILITIES`` and one predicate,
``install_profile_has_capability``. A pure-data registry keyed by
install profile — the data half of AGENTS.md's "pure-data registry +
reconciler" pattern, the same shape as ``DacProfile``'s and
``WakeModelEntry``'s registries. It lives HERE rather than in a new
module because this file is already the single import surface every
tier-aware caller uses (install.sh's landing-page bake, jasper-control,
jasper-doctor, jasper.enhanced_aec, jasper.accessories.reconcile, the
multiroom reconciler); a separate module would make all of them learn a
second import for one enum and one mapping.

Keep this module deliberately tiny and stdlib-only so it can be imported
by lightweight surfaces such as jasper-control, jasper-doctor, and the
multi-room reconciler without pulling in the full speaker stack.

**Capabilities are a pure function of the tier — no I/O, ever.** They
must never read hardware, the environment, or a file. Two consumers
compute the same map at different times (install.sh bakes it into the
static landing page; jasper-control serves it live at ``/system``), and
they agree only because both derive from the marker and nothing else.
The moment a capability reads something dynamic, a baked page can be
wrong at runtime and the landing page's ``applyCapabilities`` fails
closed — hiding a section forever with no error. Pinned by
tests/test_install_profile_capabilities.py.
"""
from __future__ import annotations

import logging
import os
from enum import Enum
from pathlib import Path
from typing import Mapping

from .log_event import log_event

logger = logging.getLogger(__name__)

DEFAULT_INSTALL_PROFILE = "full"
FULL_INSTALL_PROFILE = "full"
STREAMBOX_INSTALL_PROFILE = "streambox"
INSTALL_PROFILE_FILE = Path("/var/lib/jasper/install_profile")
VALID_INSTALL_PROFILES = frozenset({
    FULL_INSTALL_PROFILE,
    STREAMBOX_INSTALL_PROFILE,
})

# Legacy install-tier tokens kept ONLY for backwards compatibility: a
# persisted marker or env value from before the third tier was removed
# maps to streambox so the box auto-migrates rather than failing closed.
_LEGACY_STREAMBOX_ALIASES = frozenset({"endpoint", "satellite"})


class Capability(str, Enum):
    """What an install tier grants, named one axis at a time.

    Two capabilities, deliberately — not one per feature:

    ``ASSISTANT``
        The voice daemon, the provider session, tools, assistant
        integrations, TTS playout, the assistant wizards, the spend cap.
        "Can this box hold a conversation."

    ``WAKE_DETECTION``
        This *hardware class* has the headroom to run always-on wake
        inference. What draws the line is structural, and checkable:
        ``WakeLoop._handle_wake_frame`` calls
        ``detector.score_frame(frame)`` synchronously on the asyncio
        loop (jasper/voice_daemon.py — no ``to_thread``), once per
        frame per leg, forever. On a board where that inference eats
        most of a core, the Tier-1 heartbeat starves and
        ``WatchdogSec=30s`` in jasper-voice.service kills the daemon.
        The Zero 2 W measured over that line; the Pi 5 does not. It is
        a property of the BOARD, not of what is plugged into it.

    Note what is deliberately NOT here: "does this box have a
    microphone". Mic presence is *dynamic* and already owned by
    jasper-aec-reconcile; modelling it as a tier fact would break a real
    case — a full-tier Pi 5 with no mic installed but a remote paired,
    which must work. Do not add a ``LOCAL_MIC`` capability.

    The mic/AEC stack rides with ``WAKE_DETECTION`` — not because mic
    implies wake, but because always-on wake is the only always-on
    consumer of that stack. Push-to-talk with a *local* mic and no wake
    would be the second instance; split the axis then, not now.
    """

    ASSISTANT = "assistant"
    WAKE_DETECTION = "wake_detection"


# Pure-data grant table: install profile -> capabilities. The single
# place a tier's grants are stated. Adding a tier means adding a row
# here; tests/test_install_profile_capabilities.py fails if the rows and
# VALID_INSTALL_PROFILES ever disagree, so a new tier cannot silently
# ship with no capabilities.
PROFILE_CAPABILITIES: Mapping[str, frozenset[Capability]] = {
    FULL_INSTALL_PROFILE: frozenset({
        Capability.ASSISTANT,
        Capability.WAKE_DETECTION,
    }),
    # Streambox grants ASSISTANT — a mic-bearing Bluetooth remote turns it
    # into a conversational endpoint — but not WAKE_DETECTION: the Zero 2 W
    # lacks the headroom for always-on wake inference (see the Capability
    # docstring above). See
    # docs/adr/0216-a-streambox-runs-the-assistant-only-while-a-mic-bearing-remote-is-paired.md.
    STREAMBOX_INSTALL_PROFILE: frozenset({Capability.ASSISTANT}),
}


def normalize_install_profile(value: str | None) -> str:
    """Normalize an install-profile token.

    Empty/unset means the historical full-speaker profile. The legacy
    ``endpoint``/``satellite`` tokens map to ``streambox`` (never raise on
    them — that auto-migrates field boxes). Any other invalid value raises
    ``ValueError`` so callers can fail closed.
    """
    raw = (value or "").strip()
    if raw == "":
        return DEFAULT_INSTALL_PROFILE
    if raw in _LEGACY_STREAMBOX_ALIASES:
        return STREAMBOX_INSTALL_PROFILE
    if raw in VALID_INSTALL_PROFILES:
        return raw
    raise ValueError(
        f"invalid install profile {raw!r}; expected full or streambox"
    )


def read_install_profile(
    *,
    path: str | os.PathLike[str] = INSTALL_PROFILE_FILE,
    env: Mapping[str, str] | None = None,
) -> str:
    """Read the active install profile.

    The persisted marker is authoritative once present. ``JASPER_INSTALL_PROFILE``
    is a fallback for tests and early install-time processes before the marker
    exists; absent marker + absent env returns ``"full"`` for backwards
    compatibility with every pre-streambox install.

    A persisted/env value carrying a legacy ``endpoint``/``satellite``
    token resolves to ``streambox`` and emits a single greppable
    ``event=install_profile.migrate`` log line so the auto-migration is
    observable.
    """
    marker = Path(path)
    try:
        value = marker.read_text(encoding="utf-8").splitlines()[0]
    except (FileNotFoundError, IndexError):
        value = None
    except OSError:
        value = None

    if value:
        return _normalize_with_migration_log(value, source="marker")

    source = os.environ if env is None else env
    return _normalize_with_migration_log(
        source.get("JASPER_INSTALL_PROFILE"), source="env",
    )


def _normalize_with_migration_log(value: str | None, *, source: str) -> str:
    raw = (value or "").strip()
    normalized = normalize_install_profile(raw)
    if raw in _LEGACY_STREAMBOX_ALIASES:
        log_event(
            logger,
            "install_profile.migrate",
            previous=raw,
            profile=normalized,
            source=source,
        )
    return normalized


def install_role_for_profile(profile: str | None) -> str:
    """Return the product role for an install-profile marker.

    Role == profile now: ``full`` or ``streambox`` (legacy tokens
    normalized to ``streambox``).
    """
    return normalize_install_profile(profile)


def is_streambox_install_profile(profile: str | None) -> bool:
    return install_role_for_profile(profile) == STREAMBOX_INSTALL_PROFILE


def install_profile_allows_local_sources(profile: str | None) -> bool:
    """Whether this install role may advertise/run local music sources."""
    return install_role_for_profile(profile) in VALID_INSTALL_PROFILES


def install_profile_has_capability(
    profile: str | None, capability: Capability,
) -> bool:
    """Whether an install profile grants ``capability``.

    Legacy ``endpoint``/``satellite`` tokens normalize to ``streambox``
    first, so a field box reads its real grants. Invalid tokens raise
    ``ValueError`` (from ``normalize_install_profile``) and a valid
    profile missing from ``PROFILE_CAPABILITIES`` raises ``KeyError`` —
    both loud on purpose. A ``.get(role, frozenset())`` would turn "we
    forgot to grant the new tier anything" into a silent, permanent
    feature blackout, which is exactly the failure this axis exists to
    make impossible.

    Pure: derived from the argument alone — no env, no files, no
    hardware. See the module docstring for why that is load-bearing.
    """
    return capability in PROFILE_CAPABILITIES[install_role_for_profile(profile)]


def install_profile_allows_voice_brain(profile: str | None) -> bool:
    """Whether the assistant runs locally on this install profile.

    Exactly ``Capability.ASSISTANT``, under the name every existing
    caller already imports. It does NOT also mean "wake detection runs
    here" — that is ``install_profile_supports_wake_detection``. They
    are separate questions; a caller that means one should not ask the
    other.
    """
    return install_profile_has_capability(profile, Capability.ASSISTANT)


def install_profile_supports_wake_detection(profile: str | None) -> bool:
    """Whether this hardware class can run always-on wake inference.

    "supports", not "allows": this is a headroom fact about the board,
    not a policy knob. A tier without it cannot be granted it by
    changing its mind — the Zero 2 W starves its watchdog trying. The
    mic/AEC stack rides along, because always-on wake is its only
    always-on consumer.

    Consumers: the voice daemon (``jasper.voice_daemon``) plans wake legs
    only where this is granted; the accessory reconciler
    (``jasper.accessories.reconcile``) owns jasper-voice's lifecycle where
    it is not. Enhanced AEC
    (``jasper.enhanced_aec.install_profile_supports_enhanced_aec``) rides
    with it too.
    """
    return install_profile_has_capability(profile, Capability.WAKE_DETECTION)


def system_capabilities_for_profile(profile: str | None) -> dict[str, object]:
    """The management-UI capability map for an install profile.

    Single source of truth, shared by two consumers: jasper-control's
    /system snapshot (runtime) AND install.sh, which bakes the result into
    the static landing page so its capability-gated sections are correct at
    first paint with no network round-trip. Kept here (stdlib-only) so the
    installer can compute it without importing the full control stack.

    Values are derived purely from the profile — no env, no files, no
    hardware probes — so the baked page and the live snapshot always
    agree for the same marker. That purity is the whole contract here;
    see the module docstring for what breaks without it.
    """
    role = install_role_for_profile(profile)
    full = role == FULL_INSTALL_PROFILE
    local_sources = install_profile_allows_local_sources(profile)
    voice_brain = install_profile_allows_voice_brain(profile)
    wake_detection = install_profile_supports_wake_detection(profile)
    return {
        # `install_profile` echoes the token this is CALLED with; the boolean
        # caps below — what the page gates on — derive from the normalized
        # role. In production both callers (the /system snapshot and the
        # install.sh bake) pass read_install_profile(), which already
        # normalizes endpoint/satellite -> streambox, so this field reads
        # full|streambox and baked vs live always agree. A raw legacy token
        # only appears if the function is called directly with one.
        "install_profile": profile,
        "role": role,
        "local_sources": local_sources,
        "content_dsp": local_sources,
        "voice_brain": voice_brain,
        # Separate key on purpose: a tier can hold a conversation without
        # having the headroom to listen for a wake word all day. The
        # landing page's mic card and /wake/ row gate on THIS key; the
        # Assistant and Integrations blocks stay on voice_brain, whose
        # wizards are served on both tiers (deploy/index.html). See
        # install_profile_supports_wake_detection's docstring for its
        # consumers.
        "wake_detection": wake_detection,
        "network_settings": True,
        "speaker_settings": True,
        "pair_management": True,
        "developer_tools": full,
        "audio_quality": local_sources,
        "restart_voice": voice_brain,
        "restart_audio": local_sources,
        "reboot": True,
        "poweroff": True,
        "diagnostics": True,
    }
