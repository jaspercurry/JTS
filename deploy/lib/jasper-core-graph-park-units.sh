#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# This fragment is sourced, never executed. The shebang exists only so the
# static linter assumes bash (matches deploy/lib/jasper-env-file.sh).

# Canonical rosters for a core-graph (CamillaDSP / outputd / fan-in) restart:
# the audio clients to STOP so DAC and Camilla ALSA ownership can be
# reclaimed, and — below — how each of them comes back.
#
# Why this exists: these are the units that can hold fan-in, Camilla,
# outputd, or renderer ALSA endpoints during deploy/runtime churn. If the
# core graph restarts while one of them still owns /dev/snd, CamillaDSP or
# outputd start fails with "Device or resource busy" (EBUSY) — the exact
# failure class the camilla EBUSY recovery handler exists to fix
# (the 2026-06-25 JTS5 incident).
#
# Both consumers source this single definition rather than re-inlining it;
# tests/test_core_graph_park_units_contract.py pins that.
#
# Scope: this is the DEPLOY/RECOVERY park set (full speaker hardware
# ownership reclaim). It is intentionally NOT the same set as
# jasper.local_sources.registry.local_source_park_units() (the multiroom-
# follower park set, which parks bluealsa/bt-agent/usbsink and omits the
# core daemons) — those are different concerns. Keep them separate.
#
# Missing units are harmless on streambox or partial installs (both
# consumers stop best-effort and ignore not-found).

# SC2034 (appears unused) — consumed by the sourcing scripts
# (jasper-camilla-recover, deploy/lib/install/systemd-units.sh).
# shellcheck disable=SC2034
JASPER_CORE_GRAPH_PARK_UNITS=(
    jasper-voice.service
    jasper-aec-bridge.service
    jasper-outputd.service
    jasper-camilla-crossover.service
    jasper-snapclient.service
    jasper-snapserver.service
    shairport-sync.service
    nqptp.service
    librespot.service
    bluealsa-aplay.service
    jasper-mux.service
)

# The RESTORE side: the source clients the ladder starts again itself, in the
# order it starts them (shairport-sync carries Requires= and After= on nqptp,
# so the clock leads).
#
# bt-agent.service is start-only, and deliberately not parked above: the
# Bluetooth ALSA endpoint is bluealsa-aplay's, not the pairing agent's, so
# stopping it reclaims no device and drops an in-flight pairing.
#
# jasper-voice.service is started here despite jasper-aec-reconcile owning its
# gates: that reconciler's custom-JASPER_MIC_DEVICE branch exits without ever
# starting voice. Harmless elsewhere — the absence marker no-ops it on a no-mic
# box, is-enabled skips a provider-less park, and it precedes (so merges with)
# the reconciler's own restart on a mic-bearing box.
#
# Consumed by jasper-camilla-recover.
# shellcheck disable=SC2034
JASPER_CORE_GRAPH_RESTORE_UNITS=(
    nqptp.service
    shairport-sync.service
    librespot.service
    bt-agent.service
    jasper-mux.service
    bluealsa-aplay.service
    jasper-voice.service
)

# Park-list units the ladder does NOT start directly, paired with the
# reconciler that re-arms each (`unit=owner`); the ladder kicks those owners.
# Each start gate lives in its owner's script, not in a unit file, so a blind
# start would condition-fail or overrule a park the owner decided.
# jasper-outputd.service is absent: the ladder restarts it itself, earlier.
#
# Consumed by jasper-camilla-recover and the contract test.
# shellcheck disable=SC2034
JASPER_CORE_GRAPH_RECONCILER_OWNED_UNITS=(
    # Gate: /run/jasper-aec-reconcile/aec-bridge-ready.
    jasper-aec-bridge.service=jasper-aec-reconcile.service
    # Gate: the arm-time statefile re-seed, without which a cold start is
    # full-range to a tweeter.
    jasper-camilla-crossover.service=jasper-grouping-reconcile.service
    # Gate: the bond role — leader both, follower client only, solo neither.
    jasper-snapclient.service=jasper-grouping-reconcile.service
    jasper-snapserver.service=jasper-grouping-reconcile.service
)
