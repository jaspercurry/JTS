# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from importlib import import_module

import pytest

import jasper.active_speaker as active_speaker


@pytest.mark.parametrize("name", active_speaker.__all__)
def test_package_attribute_is_the_object_its_submodule_defines(name: str) -> None:
    """Every advertised name resolves, and to the definition itself.

    The package re-exports through a module __getattr__, so a name whose
    entry points at the wrong submodule fails here rather than at the first
    caller that reaches for it.
    """
    submodule = import_module(
        f"jasper.active_speaker.{active_speaker._LAZY_ATTRS[name]}"
    )
    assert getattr(active_speaker, name) is getattr(submodule, name)


def test_package_attribute_that_is_not_advertised_raises() -> None:
    with pytest.raises(AttributeError):
        active_speaker.no_such_name
