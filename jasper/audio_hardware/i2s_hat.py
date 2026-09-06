# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The I2S audio-HAT decision: intent file, detection, and the managed block
(ADR-0235).

A fitted HAT that names itself in its ID EEPROM is applied with no operator
step; the intent file is the toggle for the HATs that carry no EEPROM to read
(ADR-0234).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from jasper.atomic_io import atomic_write_text, read_regular_bytes_nofollow
from jasper.env_file import parse_env_lines

from .config_txt import (
    _OVERLAY_LINE_RE,
    _collapse_empty_all_sections,
    _global_or_all_lines,
    _overlay_values,
)
from .dac import (
    DacProfile,
    all_profiles,
    by_id,
    is_boot_managed_i2s_profile,
    profile_for_hat,
)
from .hat_eeprom import DEFAULT_HAT_DIR, read_hat_eeprom


DEFAULT_I2S_HAT_INTENT_PATH = "/var/lib/jasper/i2s_hat.env"
I2S_HAT_INTENT_KEY = "JASPER_I2S_HAT_PROFILE"

I2S_HAT_BLOCK_BEGIN = "# BEGIN JTS I2S AUDIO HAT"
I2S_HAT_BLOCK_END = "# END JTS I2S AUDIO HAT"


def i2s_hat_intent_path() -> Path:
    """The I2S HAT intent file's resolved path, honoring
    ``JASPER_I2S_HAT_INTENT_FILE`` (the env var the reconciler resolves and
    passes down to its own CLI invocation)."""

    return Path(
        os.environ.get("JASPER_I2S_HAT_INTENT_FILE", DEFAULT_I2S_HAT_INTENT_PATH)
    )


def managed_i2s_hat_block_present(content: str) -> bool:
    """Whether a JTS-managed I2S HAT block is present in ``content``.

    Delegates to the canonical block parser (:func:`_without_managed_i2s_hat`)
    rather than a separate marker scan, so a block whose ``BEGIN`` line is
    mangled while its ``dtoverlay=`` line is live raises ``ValueError`` here
    too instead of being reported absent.
    """
    return _without_managed_i2s_hat(content)[1] is not None


def configured_i2s_overlays(
    content: str,
    *,
    profiles: tuple[DacProfile, ...] | None = None,
) -> tuple[str, ...]:
    overlays = _overlay_values(_global_or_all_lines(content))
    candidates = profiles if profiles is not None else all_profiles()
    registered = {
        profile.dtoverlay.lower()
        for profile in candidates
        if is_boot_managed_i2s_profile(profile) and profile.dtoverlay
    }
    return tuple(sorted(overlays & registered))


def _registered_i2s_profile(profile_id: str) -> DacProfile | None:
    profile = by_id(profile_id)
    if profile is None or not is_boot_managed_i2s_profile(profile):
        return None
    return profile


def detected_i2s_hat_profile(
    hat_dir: str | Path = DEFAULT_HAT_DIR,
) -> DacProfile | None:
    """The fitted HAT's own declared profile, or None when nothing declares one.

    The single reading of "JTS can detect this HAT": the reconciler applies it
    without asking, and the wizard reports rather than offers it (ADR-0234).
    """

    profile = profile_for_hat(read_hat_eeprom(hat_dir))
    if profile is None or not is_boot_managed_i2s_profile(profile):
        return None
    return profile


def hat_managed(detected_id: str | None, intent_path: str | Path | None) -> bool:
    """Whether anything currently justifies JTS owning the I2S HAT boot
    block: a detected HAT's profile id, or an intent file at ``intent_path``
    the operator has written (the file's mere existence is what counts --
    even one recording an explicit "None" choice).

    The pure core: takes facts a caller already has rather than probing for
    them, so ``reconcile_boot_config`` (which has already read the fitted
    HAT) can reuse this without a second EEPROM read.
    """
    return detected_id is not None or (
        intent_path is not None and Path(intent_path).is_file()
    )


def i2s_hat_managed(
    *,
    intent_path: str | Path | None = None,
    hat_dir: str | Path = DEFAULT_HAT_DIR,
) -> bool:
    """Whether anything currently justifies JTS owning the I2S HAT boot
    block. Probes the fitted HAT and, when ``intent_path`` is not given,
    resolves it through :func:`i2s_hat_intent_path` (``JASPER_I2S_HAT_INTENT_FILE``)."""
    detected = detected_i2s_hat_profile(hat_dir)
    resolved_intent = (
        intent_path if intent_path is not None else i2s_hat_intent_path()
    )
    return hat_managed(
        detected.id if detected is not None else None, resolved_intent
    )


def selectable_i2s_hat_profiles() -> tuple[DacProfile, ...]:
    """The HATs an operator names by hand: boot-managed, no EEPROM of their own.

    A profile declaring ``hat_products`` is resolved from the fitted HAT
    instead, so it is neither offered by the wizard nor honoured in the intent
    file (ADR-0234).
    """

    return tuple(
        profile
        for profile in all_profiles()
        if is_boot_managed_i2s_profile(profile) and not profile.hat_products
    )


def _selectable_i2s_profile(profile_id: str) -> DacProfile | None:
    profile = _registered_i2s_profile(profile_id)
    return None if profile is None or profile.hat_products else profile


def read_i2s_hat_intent(
    path: str | Path = DEFAULT_I2S_HAT_INTENT_PATH,
) -> str | None:
    try:
        text = read_regular_bytes_nofollow(path, max_bytes=1024).decode("utf-8")
    except FileNotFoundError:
        return None
    values = {
        key: (value or "").strip().strip("'\"")
        for key, value in parse_env_lines(text)
    }
    if set(values) - {I2S_HAT_INTENT_KEY}:
        raise ValueError("I2S HAT intent contains an unsupported key")
    choice = values.get(I2S_HAT_INTENT_KEY)
    if not choice:
        return None
    if _registered_i2s_profile(choice) is None:
        raise ValueError("I2S HAT intent names an unsupported profile")
    # A HAT that declares its own EEPROM product is resolved from the fitted
    # hardware only, so a saved intent naming one is void rather than an error
    # -- the operator can no longer write one, but an old file may say it.
    return choice if _selectable_i2s_profile(choice) is not None else None


def write_i2s_hat_intent(
    profile_id: str | None,
    path: str | Path = DEFAULT_I2S_HAT_INTENT_PATH,
) -> None:
    """Persist the desired I2S HAT profile, or an explicit "none" marker.

    Only a HAT that cannot identify itself may be named here; a profile
    declaring ``hat_products`` is refused because detection is its only
    source (ADR-0234). ``profile_id=None`` writes the key with an empty value
    rather than removing the file: an explicitly-saved "none" (the operator
    chose unmanaged) is a distinct, persisted state from the file never
    having existed at all.
    """
    if profile_id is not None and _selectable_i2s_profile(profile_id) is None:
        raise ValueError(f"unsupported I2S audio-HAT profile: {profile_id!r}")
    atomic_write_text(
        Path(path),
        f"{I2S_HAT_INTENT_KEY}={profile_id or ''}\n",
        mode=0o660,
    )


@dataclass(frozen=True)
class I2sHatCollision:
    """A registered I2S overlay found outside JTS's managed block.

    Surfaced instead of written: two competing I2S machine drivers on one
    boot config is never a state JTS writes on its own initiative, so the
    caller gets this back instead of a rendered change to apply.
    """

    managed_overlay: str
    colliding_overlays: tuple[str, ...]


def _without_managed_i2s_hat(content: str) -> tuple[str, str | None]:
    """Strip the JTS-owned I2S HAT block, whatever overlay it names.

    Returns ``(content_without_block, block_overlay)`` — the second element
    is the overlay the removed block declared, or ``None`` if no managed
    block was present. It is the one parser for the managed block: no
    separate substring scan exists to drift out of sync with it.

    Everything outside the ``BEGIN``/``END`` markers survives untouched —
    a hand-written ``dtoverlay=`` line is never JTS's to delete, even one
    naming the same overlay this block manages (#i2s-hat-intent).
    """
    output: list[str] = []
    in_managed_block = False
    block_overlay: str | None = None
    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == I2S_HAT_BLOCK_BEGIN:
            if in_managed_block:
                raise ValueError("nested JTS I2S audio-HAT block")
            in_managed_block = True
            continue
        if stripped == I2S_HAT_BLOCK_END:
            if not in_managed_block:
                raise ValueError("JTS I2S audio-HAT block ends without a beginning")
            in_managed_block = False
            continue
        if in_managed_block:
            if stripped and not stripped.startswith("#"):
                match = _OVERLAY_LINE_RE.match(line)
                if (
                    match is None
                    or "," in line.split("#", 1)[0]
                    or block_overlay is not None
                ):
                    raise ValueError("unexpected directive in JTS I2S HAT block")
                block_overlay = match.group(1)
            continue
        output.append(line)
    if in_managed_block:
        raise ValueError("JTS I2S audio-HAT block is missing its end marker")
    return _collapse_empty_all_sections("".join(output)), block_overlay


def render_i2s_hat_boot_config(
    content: str, profile_id: str | None
) -> tuple[str, bool, I2sHatCollision | None]:
    """Render the managed I2S HAT block for ``profile_id`` (or remove it).

    Returns ``(rendered_content, changed, collision)``. A hand-written
    ``dtoverlay=`` line is never deleted. Enabling a profile (``profile_id``
    not ``None``) while ANY registered I2S overlay -- the same one or a
    different one -- already sits outside the managed block REFUSES rather
    than writes: ``rendered_content`` comes back byte-identical to
    ``content``, ``changed`` is ``False``, and ``collision`` names what
    collided, for the caller to disclose without silently compounding a
    hand-written line with a managed one. Clearing (``profile_id=None``)
    never refuses -- removing JTS's own block cannot create a collision.
    """
    profile: DacProfile | None = None
    if profile_id is not None:
        profile = _registered_i2s_profile(profile_id)
        if profile is None:
            raise ValueError(f"unsupported I2S audio-HAT profile: {profile_id!r}")
    cleaned, prior_overlay = _without_managed_i2s_hat(content)
    cleaned = cleaned.rstrip()
    if profile is not None:
        assert profile.dtoverlay is not None
        colliding = configured_i2s_overlays(cleaned)
        if colliding:
            return (
                content,
                False,
                I2sHatCollision(
                    managed_overlay=profile.dtoverlay,
                    colliding_overlays=colliding,
                ),
            )
    new_overlay = profile.dtoverlay if profile is not None else None
    changed = prior_overlay != new_overlay
    if profile is None:
        return cleaned + ("\n" if cleaned else ""), changed, None
    last_line = cleaned.splitlines()[-1].strip().lower() if cleaned else ""
    section_prefix = "" if last_line == "[all]" else "[all]\n"
    separator = "\n" if last_line == "[all]" else "\n\n"
    block = section_prefix + (
        f"{I2S_HAT_BLOCK_BEGIN}\n"
        f"# JTS hardware reconciliation: enable {profile.label}.\n"
        f"dtoverlay={profile.dtoverlay}\n"
        f"{I2S_HAT_BLOCK_END}\n"
    )
    rendered = f"{cleaned}{separator}{block}" if cleaned else block
    return rendered, changed, None
