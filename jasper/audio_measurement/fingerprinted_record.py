# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The shared ``to_dict``/``fingerprint`` mixin for measurement records."""

from __future__ import annotations

import abc
from typing import Any


class FingerprintedRecord(abc.ABC):
    """A frozen dataclass's `to_dict`: `_core()` plus its `fingerprint`.

    Persisted receipts and evidence are read back through each type's
    `from_mapping`, which expects exactly this shape -- changing it breaks
    every already-written record.
    """

    __slots__ = ()
    fingerprint: str

    @abc.abstractmethod
    def _core(self) -> dict[str, Any]:
        raise NotImplementedError

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}
