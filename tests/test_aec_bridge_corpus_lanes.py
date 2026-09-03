# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the corpus lane set `build_corpus_lanes` produces per configuration.

`PINNED_LANES` was captured from the four `_build_*_paths` helpers as they
stood in `jasper.cli.aec_bridge` before they moved into
`jasper.cli.aec_bridge_corpus_lanes`, driven in that module's original
`_aec_loop` order. Every leg, UDP port, frame size, engine label and
registration position must survive the move unchanged.
"""

from __future__ import annotations

import os
from queue import Queue

import pytest

from jasper.cli import aec_bridge, aec_bridge_corpus_lanes

# name -> (env overlay, build flags)
SCENARIOS: dict[str, tuple[dict[str, str], dict[str, bool]]] = {
    "all_off": (
        {},
        {"xvf_webrtc": False, "xvf_dtln": False, "chip": False, "usb": False, "ref": False},
    ),
    "xvf_raw0_webrtc": (
        {},
        {"xvf_webrtc": True, "xvf_dtln": False, "chip": False, "usb": False, "ref": False},
    ),
    "xvf_raw0_both": (
        {},
        {"xvf_webrtc": True, "xvf_dtln": True, "chip": False, "usb": False, "ref": True},
    ),
    "usb_corpus": (
        {},
        {"xvf_webrtc": False, "xvf_dtln": False, "chip": False, "usb": True, "ref": False},
    ),
    "usb_corpus_dtln": (
        {"JASPER_AEC_CORPUS_USB_DTLN_ENABLED": "1"},
        {"xvf_webrtc": False, "xvf_dtln": False, "chip": False, "usb": True, "ref": False},
    ),
    "sweep_xvf": (
        {"JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED": "1"},
        {"xvf_webrtc": False, "xvf_dtln": False, "chip": False, "usb": False, "ref": False},
    ),
    "sweep_usb": (
        {
            "JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED": "1",
            "JASPER_AEC_CORPUS_AEC3_SWEEP_SOURCE": "usb",
        },
        {"xvf_webrtc": False, "xvf_dtln": False, "chip": False, "usb": True, "ref": True},
    ),
    "sweep_usb_no_queue": (
        {
            "JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED": "1",
            "JASPER_AEC_CORPUS_AEC3_SWEEP_SOURCE": "usb",
        },
        {"xvf_webrtc": False, "xvf_dtln": False, "chip": False, "usb": False, "ref": False},
    ),
    "dtln_observation": (
        {"JASPER_AEC_DTLN_ENABLED": "1", "JASPER_AEC_DTLN_SIZE": "512"},
        {"xvf_webrtc": False, "xvf_dtln": False, "chip": False, "usb": False, "ref": False},
    ),
    "chip_aec_suppresses": (
        {
            "JASPER_AEC_DTLN_ENABLED": "1",
            "JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED": "1",
        },
        {"xvf_webrtc": True, "xvf_dtln": True, "chip": True, "usb": True, "ref": False},
    ),
}

# emitters: (leg, UDP port, frame samples) in registration order — the order
# that reaches the stats snapshot's `ports` map and shutdown close order.
# lanes: populated CorpusLanes fields -> emitter leg / "aec3:<label>" / "dtln:<size>".
# sweep: (variant leg, input source, UDP port, engine label).
PINNED_LANES: dict[str, dict[str, object]] = {
    "all_off": {
        "emitters": (),
        "lanes": {},
        "sweep": (),
    },
    "chip_aec_suppresses": {
        "emitters": (
            ("xvf_raw0_webrtc_aec3", 9889, 1280),
            ("xvf_raw0_dtln", 9890, 1280),
            ("usb_raw", 9881, 1280),
            ("usb_webrtc", 9882, 1280),
        ),
        "lanes": {
            "usb_engine": "aec3:usb_webrtc/aec3_edge_combo_80",
            "usb_raw_emitter": "usb_raw",
            "usb_webrtc_emitter": "usb_webrtc",
            "xvf_raw0_dtln_emitter": "xvf_raw0_dtln",
            "xvf_raw0_dtln_engine": "dtln:256",
            "xvf_raw0_engine": "aec3:xvf_raw0_webrtc_aec3",
            "xvf_raw0_webrtc_emitter": "xvf_raw0_webrtc_aec3",
        },
        "sweep": (),
    },
    "dtln_observation": {
        "emitters": (
            ("dtln", 9878, 1280),
        ),
        "lanes": {
            "dtln_emitter": "dtln",
            "dtln_engine": "dtln:512",
        },
        "sweep": (),
    },
    "sweep_usb": {
        "emitters": (
            ("ref", 9880, 1280),
            ("usb_raw", 9881, 1280),
            ("usb_webrtc", 9882, 1280),
            ("aec3_variant_1", 9884, 1280),
            ("aec3_variant_2", 9885, 1280),
            ("aec3_variant_3", 9886, 1280),
        ),
        "lanes": {
            "ref_emitter": "ref",
            "usb_engine": "aec3:usb_webrtc/aec3_sweep_delay_40",
            "usb_raw_emitter": "usb_raw",
            "usb_webrtc_emitter": "usb_webrtc",
        },
        "sweep": (
            ("aec3_variant_1", "usb", 9884, "aec3_sweep/usb/aec3_variant_1"),
            ("aec3_variant_2", "usb", 9885, "aec3_sweep/usb/aec3_variant_2"),
            ("aec3_variant_3", "usb", 9886, "aec3_sweep/usb/aec3_variant_3"),
        ),
    },
    "sweep_usb_no_queue": {
        "emitters": (),
        "lanes": {},
        "sweep": (),
    },
    "sweep_xvf": {
        "emitters": (
            ("aec3_variant_1", 9884, 1280),
            ("aec3_variant_2", 9885, 1280),
            ("aec3_variant_3", 9886, 1280),
        ),
        "lanes": {},
        "sweep": (
            ("aec3_variant_1", "xvf", 9884, "aec3_sweep/xvf/aec3_variant_1"),
            ("aec3_variant_2", "xvf", 9885, "aec3_sweep/xvf/aec3_variant_2"),
            ("aec3_variant_3", "xvf", 9886, "aec3_sweep/xvf/aec3_variant_3"),
        ),
    },
    "usb_corpus": {
        "emitters": (
            ("usb_raw", 9881, 1280),
            ("usb_webrtc", 9882, 1280),
        ),
        "lanes": {
            "usb_engine": "aec3:usb_webrtc/aec3_edge_combo_80",
            "usb_raw_emitter": "usb_raw",
            "usb_webrtc_emitter": "usb_webrtc",
        },
        "sweep": (),
    },
    "usb_corpus_dtln": {
        "emitters": (
            ("usb_raw", 9881, 1280),
            ("usb_webrtc", 9882, 1280),
            ("usb_dtln", 9883, 1280),
        ),
        "lanes": {
            "usb_dtln_emitter": "usb_dtln",
            "usb_dtln_engine": "dtln:256",
            "usb_engine": "aec3:usb_webrtc/aec3_edge_combo_80",
            "usb_raw_emitter": "usb_raw",
            "usb_webrtc_emitter": "usb_webrtc",
        },
        "sweep": (),
    },
    "xvf_raw0_both": {
        "emitters": (
            ("xvf_raw0_webrtc_aec3", 9889, 1280),
            ("xvf_raw0_dtln", 9890, 1280),
            ("ref", 9880, 1280),
        ),
        "lanes": {
            "ref_emitter": "ref",
            "xvf_raw0_dtln_emitter": "xvf_raw0_dtln",
            "xvf_raw0_dtln_engine": "dtln:256",
            "xvf_raw0_engine": "aec3:xvf_raw0_webrtc_aec3",
            "xvf_raw0_webrtc_emitter": "xvf_raw0_webrtc_aec3",
        },
        "sweep": (),
    },
    "xvf_raw0_webrtc": {
        "emitters": (
            ("xvf_raw0_webrtc_aec3", 9889, 1280),
        ),
        "lanes": {
            "xvf_raw0_engine": "aec3:xvf_raw0_webrtc_aec3",
            "xvf_raw0_webrtc_emitter": "xvf_raw0_webrtc_aec3",
        },
        "sweep": (),
    },
}


class _StubAec3:
    def __init__(self, label: str | None) -> None:
        self.label = label

    def process(self, mic: bytes, ref: bytes) -> bytes:
        return b""

    def close(self) -> None:
        pass


class _StubDtln:
    def __init__(self, model_dir: object = None, model_size: int = 0) -> None:
        self.model_size = model_size


def _select_stub(
    overrides: dict[str, str] | None = None, label: str | None = None
) -> _StubAec3:
    return _StubAec3(label)


def _describe(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, _StubAec3):
        return f"aec3:{value.label}"
    if isinstance(value, _StubDtln):
        return f"dtln:{value.model_size}"
    return str(value.stats_key)  # type: ignore[attr-defined]


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_corpus_lane_set_matches_pin(scenario, monkeypatch, tmp_path):
    import jasper.aec_engines.dtln as dtln_mod

    monkeypatch.setattr(dtln_mod, "DTLNEngine", _StubDtln)
    monkeypatch.setattr(dtln_mod, "default_model_dir", lambda: tmp_path)

    for key in list(os.environ):
        if key.startswith("JASPER_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("JASPER_AEC_MIC_DEVICE", "test-card")
    monkeypatch.setenv(
        "JASPER_USB_MIC_INTENT_PATH", str(tmp_path / "no-usb-mic-intent")
    )
    monkeypatch.setenv(
        "JASPER_AEC_BRIDGE_STATS_PATH", str(tmp_path / "bridge-stats.json")
    )
    env, flags = SCENARIOS[scenario]
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    emitters: dict[str, aec_bridge.LegEmitter] = {}
    try:
        lanes = aec_bridge_corpus_lanes.build_corpus_lanes(
            emitters,
            aec_bridge.BridgeConfig.from_env(),
            select_engine=_select_stub,
            xvf_raw0_webrtc_enabled=flags["xvf_webrtc"],
            xvf_raw0_dtln_enabled=flags["xvf_dtln"],
            emit_ref=flags["ref"],
            production_chip_aec_enabled=flags["chip"],
            usb_raw_q=Queue() if flags["usb"] else None,
        )
        built = {
            "emitters": tuple(
                (emitter.stats_key, emitter.dest[1], emitter.frame_samples)
                for emitter in emitters.values()
            ),
            "lanes": {
                field: _describe(getattr(lanes, field))
                for field in vars(lanes)
                if field not in ("aec3_sweep_paths", "emit_aec3_sweep")
                and getattr(lanes, field) is not None
            },
            "sweep": tuple(
                (
                    path.variant.leg,
                    path.input_source,
                    path.emitter.dest[1],
                    path.engine.label,  # type: ignore[attr-defined]
                )
                for path in lanes.aec3_sweep_paths
            ),
        }
    finally:
        for emitter in emitters.values():
            emitter.close()

    assert built == PINNED_LANES[scenario]
