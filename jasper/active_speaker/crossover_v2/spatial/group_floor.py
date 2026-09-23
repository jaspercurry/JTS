# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# --------------------------------------------------------------------------- #
# geometry-retry ceiling (#2291 Phase 5c-ii)
# --------------------------------------------------------------------------- #

# How many wider-spread RETAKES of the group's last position the
# geometry-locked check may ask for, once per group.
#
# Retakes rather than appended positions because of the PROTOCOL, not the
# physics: the capture runner completes a set at exactly ``capture_target``
# accepted captures with ``index == accepted_count + 1``, so rejecting a capture
# is the only lever that keeps a plan alive at the same index. Appending is the
# better estimator if the runner ever grows variable-length sets.
#
# Bounded on purpose: `geometry.locked` is a "spread the mic further" hint, not
# a failure, and no amount of mic movement decorrelates a source-fixed null, so
# an unbounded loop would never terminate. Two retakes, then proceed and RECORD
# the verdict.
GEOMETRY_RETRY_POSITIONS = 2
