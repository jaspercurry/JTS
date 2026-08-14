"""Public API for the ComXim USB turntable controller."""

# SPDX-License-Identifier: Apache-2.0

from .controller import OperationResult, ProbeResult, TurntableController
from .discovery import discover_devices
from .errors import (
    CommandRejected,
    CommunicationTimeout,
    CompletionTimeout,
    PortDiscoveryError,
    ProtocolError,
    StartupSynchronizationError,
    TurntableError,
)

__version__ = "0.1.0"

__all__ = [
    "CommandRejected",
    "CommunicationTimeout",
    "CompletionTimeout",
    "OperationResult",
    "PortDiscoveryError",
    "ProbeResult",
    "ProtocolError",
    "StartupSynchronizationError",
    "TurntableController",
    "TurntableError",
    "__version__",
    "discover_devices",
]
