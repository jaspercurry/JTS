# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Where the grouped program reaches snapserver. A leaf, so a graph door can
name the pipe without importing the grouping planner (ADR-0226)."""

# The FIFO the fan-in chain writes the mixed stereo program into and snapserver
# reads as its pipe source. Lives in snapserver's OWN per-unit runtime dir
# (RuntimeDirectory=jasper-snapserver): a unit's RuntimeDirectory is reaped when
# it stops, so a shared one would let snapserver stopping destroy another
# daemon's sockets. tmpfs-backed, recreated each boot.
SNAPFIFO = "/run/jasper-snapserver/snapfifo"
