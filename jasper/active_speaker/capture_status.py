# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Terminal capture states shared by daemon writers and mover clients."""

CAPTURE_COMPLETE = "complete"
CAPTURE_STOPPED = "stopped"
CAPTURE_FAILED = "failed"
SESSION_ENDED_STATUSES = frozenset({CAPTURE_COMPLETE, CAPTURE_STOPPED, CAPTURE_FAILED})
