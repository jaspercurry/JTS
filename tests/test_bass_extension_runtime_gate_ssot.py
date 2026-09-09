# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract: an adapter is armed only where its emission actually exists.

``BASS_EXTENSION_RUNTIME_ADAPTER_IDS`` (``jasper/bass_extension/__init__.py``)
is the single source of truth for which enclosure adapters may arm the bass
stage, and ``jasper.bass_extension.candidate_field`` is the one gate that
reads it. That gate is not adapter-agnostic: ``_sealed_filters`` recomputes a
boosted target's transform through ``alignment.linkwitz_transform_params``,
the sealed plant's second-order alignment and no other. Arming a second
adapter without teaching that recompute its plant would hand a ported or
passive-radiator cabinet the sealed transform silently.

So this pins the two halves of the same rule: the armed set is exactly the
adapters whose alignment the field reader implements, and every other
registered adapter is refused at the persistence boundary. **When this fails**
after a new adapter id is armed: teach ``_sealed_filters`` that plant's
alignment and the emitter its stage first, then widen the frozenset and this
pin together — never this pin alone.
"""
from __future__ import annotations

import pytest

from jasper.bass_extension import BASS_EXTENSION_RUNTIME_ADAPTER_IDS
from jasper.bass_extension.adapters import ADAPTERS
from jasper.bass_extension.adapters.sealed import SEALED_ADAPTER
from jasper.bass_extension.candidate_field import (
    BassCandidateFieldError,
    validate_bass_extension_field,
)
from jasper.bass_extension.refusals import BassExtensionRefusal

from tests.test_bass_extension_candidate_field import bass_extension_field


def test_the_armed_adapters_are_the_ones_the_field_reader_can_recompute() -> None:
    assert set(BASS_EXTENSION_RUNTIME_ADAPTER_IDS) == {SEALED_ADAPTER.adapter_id}
    assert set(BASS_EXTENSION_RUNTIME_ADAPTER_IDS) <= set(ADAPTERS)


@pytest.mark.parametrize(
    "adapter_id",
    sorted(set(ADAPTERS) - set(BASS_EXTENSION_RUNTIME_ADAPTER_IDS)),
)
def test_an_unarmed_adapter_refuses_at_the_persistence_boundary(adapter_id) -> None:
    field = bass_extension_field()
    field["adapter_id"] = adapter_id
    with pytest.raises(BassCandidateFieldError) as refusal:
        validate_bass_extension_field(field)
    assert refusal.value.reason is BassExtensionRefusal.ADAPTER_UNKNOWN
