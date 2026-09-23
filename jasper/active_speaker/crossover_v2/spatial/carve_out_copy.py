# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any, Mapping


def _geometry_guidance_copy(geometry: Mapping[str, Any]) -> str:
    """Plain-language "spread the mic further" guidance from a persisted
    geometry verdict dict.

    Softened, never suppressed, when ``thin_evidence``, and the softened copy
    names the qualitative floor rather than a count: ``thin_evidence`` is a
    cliff at an exact confident-estimate count, so naming it would read as a
    gradient the instrument does not claim. ``""`` when not locked.
    """
    if not geometry.get("locked"):
        return ""
    if geometry.get("thin_evidence"):
        return (
            "The measured echo pattern looks the same at every microphone "
            "position, but only the bare minimum of positions gave a "
            "confident enough reading to tell. Spreading the microphone "
            "further apart next time would make this more certain."
        )
    return (
        "The measured echo pattern did not change between microphone "
        "positions. Spreading the microphone further apart next time may "
        "help JTS tell the speaker's own sound apart from the room's."
    )
