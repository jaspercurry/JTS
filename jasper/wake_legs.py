# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Single source of truth for wake-detection / corpus audio "legs".

A *leg* is one named audio stream the AEC bridge emits over UDP and a
consumer subscribes to by port. There are two kinds of consumer:

  * the voice daemon's wake loop (``jasper.voice_daemon.WakeLoop``),
    which OR-gates the *production* legs (``wake_input=True``) for
    wake-word detection; and
  * the corpus recorder (``jasper.web.wake_corpus_setup``) and offline
    tooling, which also subscribe to the corpus-only legs
    (``wake_input=False``) to build training / portability datasets.

Before this module the leg vocabulary lived in two places that could
drift: the daemon's hardcoded ``on`` / ``off`` / ``dtln`` slots and
``jasper.wake_ports.build_ports``'s larger ``on/off/dtln/raw0/ref/usb_*``
port map. This registry unifies them. ``jasper.wake_ports`` now derives
its port constants from here, so each wire port has exactly one
definition (matching ``jasper.cli.aec_bridge``'s ``OUT_PORT*`` emit
constants).

Design intent (mirrors ``jasper.transit.base``): keep this file as small
as the contract and **import-cheap** — the AEC bridge and capture
tooling import it, so it stays stdlib-only with no heavy deps.

Frozen-in-place contract: ``token`` is the on-the-wire / on-disk
identifier — the key in ``build_ports()``, the value in the
``wake_events.fired_legs`` CSV, and the stem of the ``wake_events``
per-leg columns. It is **load-bearing for the historical telemetry
corpus and the analysis tooling** (``scripts/analyze-three-leg.sh``),
so a token is never renamed. ``name`` is the human / code-facing slug
and is free to be more descriptive.

The parametric AEC3 *sweep* variants are intentionally NOT in this
registry — they are tuning experiments enumerated dynamically in
``jasper.aec_sweep``, not stable named legs. ``jasper.wake_ports``
merges them on top of the registry ports for tooling.

Fields grow as later phases consume them: a per-leg threshold offset
lands with the condition-aware fuser, and the ``wake_events`` per-leg
column mapping lands with the ``LegRuntime`` WakeLoop refactor. See
docs/HANDOFF-mic-fusion-architecture.md for the staged plan.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LegKind(str, Enum):
    """What kind of signal a leg carries. Descriptive metadata the
    fuser / topology key off (e.g. "always keep a RAW leg in the OR")."""

    SOFTWARE_AEC = "software_aec"  # WebRTC AEC3 (mic near-end + playback ref)
    NEURAL_AEC = "neural_aec"      # DTLN-aec
    CHIP_DSP = "chip_dsp"          # mic after the array chip's own BF/NS/AGC, pre software AEC
    RAW = "raw"                    # unprocessed mic capture (no chip or software DSP)
    REFERENCE = "reference"        # the playback reference signal itself
    HARDWARE_AEC = "hardware_aec"  # a mic that cancels echo in hardware (future 4th arm)


@dataclass(frozen=True)
class LegSpec:
    """One named audio leg.

    name        human / code slug (e.g. "aec3"); free to be descriptive.
    token       FROZEN wire / on-disk key (e.g. "on"). Never rename: it is
                the build_ports() key, the fired_legs CSV value, and the
                wake_events per-leg column stem for the historical corpus.
    udp_port    localhost UDP port the bridge emits this leg on.
    kind        LegKind (descriptive).
    wake_input  True if WakeLoop OR-gates this leg for wake detection;
                False for corpus-only legs (raw0 / ref / usb_*).
    """

    name: str
    token: str
    udp_port: int
    kind: LegKind
    wake_input: bool


# Ordered registry. Production wake legs first (matching the daemon's
# "on" -> "off" -> "dtln" -> "chip_aec_150" -> "chip_aec_210" priority),
# then corpus-only legs. Ports match jasper.cli.aec_bridge's OUT_PORT*
# emit constants (so the file is grouped by wake_input, not by port — the
# chip legs' ports 9887/9888 sit above the corpus ports by design).
REGISTRY: tuple[LegSpec, ...] = (
    # --- production wake-detection legs (OR-gated by WakeLoop) ---
    # Always-built software legs: the AEC reference is mic-independent, so
    # these run against any mic (aec3, chip-direct raw, DTLN).
    LegSpec("aec3", "on", 9876, LegKind.SOFTWARE_AEC, wake_input=True),
    LegSpec("chip_direct", "off", 9877, LegKind.CHIP_DSP, wake_input=True),
    LegSpec("dtln", "dtln", 9878, LegKind.NEURAL_AEC, wake_input=True),
    # Hardware-conditional extra chip-AEC beam legs — the XVF3800's fixed
    # 150°/210° ASR beams. WakeLoop only builds a chip leg when its device var
    # (cfg.mic_device_chip_aec_150/_210) is non-empty, which the AEC
    # reconciler sets from the matching per-beam custom toggle. The chip-AEC
    # profile itself uses only the primary/session "on" leg by default.
    # Ports 9887/9888 + tokens are frozen because corpus tooling keys off them.
    LegSpec("chip_aec_150", "chip_aec_150", 9887, LegKind.HARDWARE_AEC, wake_input=True),
    LegSpec("chip_aec_210", "chip_aec_210", 9888, LegKind.HARDWARE_AEC, wake_input=True),
    # --- corpus-only legs (recorder + offline tooling; not wake inputs) ---
    LegSpec("raw0", "raw0", 9879, LegKind.RAW, wake_input=False),
    LegSpec("reference", "ref", 9880, LegKind.REFERENCE, wake_input=False),
    LegSpec("usb_raw", "usb_raw", 9881, LegKind.RAW, wake_input=False),
    LegSpec("usb_aec3", "usb_webrtc", 9882, LegKind.SOFTWARE_AEC, wake_input=False),
    LegSpec("usb_dtln", "usb_dtln", 9883, LegKind.NEURAL_AEC, wake_input=False),
    LegSpec(
        "xvf_raw0_aec3",
        "xvf_raw0_webrtc_aec3",
        9889,
        LegKind.SOFTWARE_AEC,
        wake_input=False,
    ),
    LegSpec("xvf_raw0_dtln", "xvf_raw0_dtln", 9890, LegKind.NEURAL_AEC, wake_input=False),
)


_BY_NAME = {leg.name: leg for leg in REGISTRY}
_BY_TOKEN = {leg.token: leg for leg in REGISTRY}


def by_name(name: str) -> LegSpec:
    """Look up a leg by its code slug. Raises ``KeyError`` on miss."""
    return _BY_NAME[name]


def by_token(token: str) -> LegSpec:
    """Look up a leg by its frozen wire / on-disk token. Raises ``KeyError``."""
    return _BY_TOKEN[token]


def wake_input_legs() -> tuple[LegSpec, ...]:
    """The production legs WakeLoop OR-gates, in priority order."""
    return tuple(leg for leg in REGISTRY if leg.wake_input)


def all_ports() -> dict[str, int]:
    """Every registered leg's ``token`` -> ``udp_port``.

    Sweep variants are parametric and live in ``jasper.aec_sweep``;
    ``jasper.wake_ports`` merges them on top of this for tooling.
    """
    return {leg.token: leg.udp_port for leg in REGISTRY}
