# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The words Sound declares a crossover in, and what they compile to.

Both directions of the declared ``filter_type`` / ``slope_db_per_octave``
vocabulary: what an entry surface may offer, what it must accept, and the
compiled ``target_type`` / LR order each spelling means.
"""

from __future__ import annotations

from typing import Any

from .profile import (
    SUPPORTED_CROSSOVER_TYPES,
    SUPPORTED_LR_ORDERS,
    ActiveSpeakerConfigError,
)


def _slope_to_lr_order(raw: Any) -> int | None:
    try:
        slope = float(raw)
    except (TypeError, ValueError):
        return None
    if not (slope > 0):
        return None
    order = int(round(slope / 6.0))
    return (
        order
        if order in SUPPORTED_LR_ORDERS and abs(slope - order * 6.0) < 0.01
        else None
    )


def _filter_type_token(raw: Any) -> str:
    """Separator- and case-insensitive key for one declared filter-type spelling."""

    return str(raw or "").replace("-", "").replace(" ", "").lower()


#: Declared spellings that are NOT merely a separator/case variant of a
#: supported ``target_type``. Every other spelling is matched against
#: :data:`~jasper.active_speaker.profile.SUPPORTED_CROSSOVER_TYPES` itself, so
#: that set stays the one owner of which filters compile -- an alias whose
#: target is not in it resolves to nothing rather than smuggling a filter in.
_FILTER_TYPE_ALIASES = {"lr": "LinkwitzRiley"}


def _normalise_filter_type(raw: Any) -> str | None:
    token = _filter_type_token(raw)
    for target_type in SUPPORTED_CROSSOVER_TYPES:
        if token == _filter_type_token(target_type):
            return target_type
    aliased = _FILTER_TYPE_ALIASES.get(token)
    return aliased if aliased in SUPPORTED_CROSSOVER_TYPES else None


#: The declaration spelling this module compiles from, one per preset
#: ``target_type``. The INVERSE of :func:`_normalise_filter_type`, kept beside it
#: so one module owns the mapping in both directions -- a second table anywhere
#: else is a second answer to "what does Sound call this filter".
_DECLARATION_FILTER_TYPES = ("Linkwitz-Riley",)


def declaration_filter_type(target_type: Any) -> str | None:
    """What Sound must declare for this module to compile ``target_type``.

    ``None`` when no declared spelling compiles to it -- never a guess. The
    answer is *verified* through :func:`_normalise_filter_type` rather than
    asserted, so the pair cannot drift: a spelling that stops compiling stops
    being returned.
    """

    for spelling in _DECLARATION_FILTER_TYPES:
        if _normalise_filter_type(spelling) == target_type:
            return spelling
    return None


def same_declared_filter_type(left: Any, right: Any) -> bool:
    """Whether two declared filter-type spellings mean the same filter here.

    Compiled, not compared as text: ``"LR"``, ``"linkwitz riley"`` and
    ``"Linkwitz-Riley"`` are one filter to this module, and a consumer that
    compared the raw strings would read a household's own spelling as a
    crossover change. A spelling this module cannot compile is not the same as
    anything, itself included — it is not a filter yet.
    """

    compiled = _normalise_filter_type(left)
    return compiled is not None and compiled == _normalise_filter_type(right)


def declaration_slope_db_per_octave(order: Any) -> float | None:
    """What Sound must declare for this module to compile ``order``.

    The INVERSE of :func:`_slope_to_lr_order`, verified through it for the same
    reason :func:`declaration_filter_type` is: ``order * 6`` is only the right
    answer while that function still reads it back as ``order``. ``None`` for an
    order this module refuses to compile (anything outside
    :data:`~jasper.active_speaker.profile.SUPPORTED_LR_ORDERS`).
    """

    if isinstance(order, bool) or not isinstance(order, int):
        return None
    slope = float(order) * 6.0
    return slope if _slope_to_lr_order(slope) == order else None


def supported_declaration_filter_types() -> tuple[str, ...]:
    """Every ``filter_type`` spelling Sound may OFFER, in this module's words.

    The whole of :func:`declaration_filter_type`: one spelling per member of
    :data:`~jasper.active_speaker.profile.SUPPORTED_CROSSOVER_TYPES`. What an
    entry surface renders as a picker, so the wizard cannot present a filter
    that arrives here and is refused. The compile-time blocker that does the
    refusing (``staging``'s ``crossover_preview_filter_unsupported``) used to
    be the first place anyone heard about it, several screens after the field
    was filled in.

    Raises :class:`~jasper.active_speaker.profile.ActiveSpeakerConfigError` when
    a supported ``target_type`` has no declared spelling. Silently omitting it
    would narrow the offer back to what it was, which is the same silence this
    accessor exists to end.
    """

    spellings: list[str] = []
    for target_type in sorted(SUPPORTED_CROSSOVER_TYPES):
        spelling = declaration_filter_type(target_type)
        if spelling is None:
            raise ActiveSpeakerConfigError(
                f"no declared filter-type spelling compiles to {target_type!r}"
            )
        spellings.append(spelling)
    return tuple(spellings)


def supported_declaration_slopes_db_per_octave() -> tuple[float, ...]:
    """Every ``slope_db_per_octave`` Sound may OFFER, ascending.

    The whole of :func:`declaration_slope_db_per_octave`, in the order a picker
    should render it. Nothing raises here, unlike its filter-type sibling: that
    one pairs a set against a hand-written spelling table, while this is
    arithmetic on the set itself and cannot come up short.
    """

    slopes = [declaration_slope_db_per_octave(order) for order in SUPPORTED_LR_ORDERS]
    return tuple(sorted(slope for slope in slopes if slope is not None))


def declared_filter_type_compiles(value: Any) -> bool:
    """Whether this module can compile the declared spelling ``value``.

    Wider than :func:`supported_declaration_filter_types` on purpose: the offer
    is the canonical spellings, while a declaration written by hand or by the
    research assistant may spell the same filter differently (``"LR"``,
    ``"linkwitz riley"``). An entry surface refuses what will not compile
    without narrowing what already does.
    """

    return _normalise_filter_type(value) is not None


def declared_slope_db_per_octave_compiles(value: Any) -> bool:
    """Whether this module can compile the declared slope ``value``."""

    return _slope_to_lr_order(value) is not None
