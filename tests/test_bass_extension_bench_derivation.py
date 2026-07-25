# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""R1/R2/R3/R7 — pure config derivation for the offline tap render.

Pins the amendment's Tests section: the three-part prefix proof (devices
allowlist diff; filters+mixers identity incl. the -6.02 dB split-mixer
source; pipeline truncated at the owner step); the exact ``names[:i]`` /
``names[:i+1]`` boundary math including the ``names[:i-1]`` refusal; the
distinct pre-limiter re-proof predicate; the R7 owner-path allowlist
(including the Async-resampler and bypassed-step refusals); channel-width
preservation + owner-channel-index recording; ``devices.capture``/
``devices.playback`` key handling; and the fader-reproduction receipt shape.
"""

from __future__ import annotations

import pytest
import yaml

from jasper.bass_extension.bench import derivation

LIMITER_NAME = "as_woofer_baseline_limiter"
OWNER_CHANNELS = (2, 3)

# One realistic live active_config_raw: a stereo split mixer (the -6.02 dB
# MONO_SUM_GAIN_DB hazard R2(ii) must catch) feeding a stereo woofer owner
# step (bass_ext_lt -> bass_ext_subsonic -> the baseline limiter).
LIVE_CONFIG: dict = {
    "devices": {
        "samplerate": 48000,
        "chunksize": 1024,
        "capture": {
            "type": "Alsa",
            "channels": 2,
            "device": "hw:Loopback,1",
            "format": "S32LE",
        },
        "playback": {
            "type": "Alsa",
            "channels": 4,
            "device": "hw:0",
            "format": "S32LE",
        },
        "enable_rate_adjust": True,
        "queuelimit": 4,
        "target_level": 1024,
        "volume_limit": 0.0,
    },
    "filters": {
        "bass_ext_lt": {
            "type": "Biquad",
            "parameters": {
                "type": "LinkwitzTransform",
                "freq_act": 40.0,
                "q_act": 0.7,
                "freq_target": 40.0,
                "q_target": 0.7,
            },
        },
        "bass_ext_subsonic": {
            "type": "BiquadCombo",
            "parameters": {
                "type": "ButterworthHighpass",
                "freq": 30.0,
                "order": 4,
            },
        },
        LIMITER_NAME: {
            "type": "Limiter",
            "parameters": {"clip_limit": -1.0, "soft_clip": True},
        },
    },
    "mixers": {
        "split_2way": {
            "channels": {"in": 2, "out": 4},
            "mapping": [
                {"dest": 0, "sources": [{"channel": 0, "gain": 0.0, "inverted": False}]},
                {"dest": 1, "sources": [{"channel": 1, "gain": 0.0, "inverted": False}]},
                {
                    "dest": 2,
                    "sources": [
                        {"channel": 0, "gain": -6.02, "inverted": False},
                        {"channel": 1, "gain": -6.02, "inverted": False},
                    ],
                },
                {
                    "dest": 3,
                    "sources": [
                        {"channel": 0, "gain": -6.02, "inverted": False},
                        {"channel": 1, "gain": -6.02, "inverted": False},
                    ],
                },
            ],
        }
    },
    "pipeline": [
        {"type": "Mixer", "name": "split_2way"},
        {
            "type": "Filter",
            "channels": [2, 3],
            "names": ["bass_ext_lt", "bass_ext_subsonic", LIMITER_NAME],
        },
    ],
}


def _live_yaml() -> str:
    return yaml.safe_dump(LIVE_CONFIG, sort_keys=False)


def _header(**overrides: object) -> derivation.ArtifactHeader:
    values = {"sample_rate_hz": 48000, "channels": 2, "bits_per_sample": 16}
    values.update(overrides)
    return derivation.ArtifactHeader(**values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _stub_post_limiter_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass activation._prove_active_graph's bass_extension_block_valid
    check (already pinned by its own tests) so these tests focus on
    derivation's OWN truncation/allowlist logic."""

    monkeypatch.setattr(
        derivation, "_prove_active_graph", lambda config, proof: proof.expected_clip_limit_dbfs
    )


def _derive(boundary: derivation.Boundary, **overrides: object) -> derivation.DerivedConfig:
    kwargs: dict = dict(
        live_active_config_raw=_live_yaml(),
        boundary=boundary,
        limiter_name=LIMITER_NAME,
        owner_channels=OWNER_CHANNELS,
        profile_summary={"runtime_block_required": True},
        expected_clip_limit_dbfs=-1.0,
        capture_header=_header(),
        capture_filename="/tmp/bundle/target/pre-input.wav",
        playback_filename="/tmp/bundle/target/pre-output.raw",
        processing_precision="F64_LE",
    )
    kwargs.update(overrides)
    return derivation.derive_truncated_config(**kwargs)


# --------------------------------------------------------------------------- #
# R2 boundary math
# --------------------------------------------------------------------------- #


def test_boundary_index_finds_the_limiter_position() -> None:
    names = ["bass_ext_lt", "bass_ext_subsonic", LIMITER_NAME]
    assert derivation.boundary_index(names, LIMITER_NAME) == 2


def test_boundary_index_refuses_when_limiter_absent() -> None:
    with pytest.raises(derivation.DerivationError):
        derivation.boundary_index(["bass_ext_lt", "bass_ext_subsonic"], LIMITER_NAME)


def test_post_limiter_retains_names_through_i_plus_1() -> None:
    derived = _derive("post_limiter")
    parsed = yaml.safe_load(derived.yaml_text)
    owner_step = parsed["pipeline"][-1]
    assert owner_step["names"] == ["bass_ext_lt", "bass_ext_subsonic", LIMITER_NAME]


def test_pre_limiter_retains_names_up_to_i() -> None:
    derived = _derive("pre_limiter")
    parsed = yaml.safe_load(derived.yaml_text)
    owner_step = parsed["pipeline"][-1]
    assert owner_step["names"] == ["bass_ext_lt", "bass_ext_subsonic"]


def test_names_i_minus_1_refuses_the_boundary_invariant() -> None:
    """The exact boundary-math invariant, pinned directly (not just through
    derive_truncated_config's own correct-by-construction call)."""

    live_names = ["bass_ext_lt", "bass_ext_subsonic", LIMITER_NAME]
    with pytest.raises(derivation.DerivationError):
        derivation.validate_boundary(
            live_names=live_names,
            retained_names=live_names[:1],  # names[:i-1] where i=2
            index=2,
            boundary="pre_limiter",
        )


def test_boundary_validate_accepts_the_two_true_lengths() -> None:
    live_names = ["bass_ext_lt", "bass_ext_subsonic", LIMITER_NAME]
    derivation.validate_boundary(
        live_names=live_names, retained_names=live_names[:2], index=2, boundary="pre_limiter"
    )
    derivation.validate_boundary(
        live_names=live_names, retained_names=live_names[:3], index=2, boundary="post_limiter"
    )


# --------------------------------------------------------------------------- #
# R2's distinct pre-limiter re-proof predicate
# --------------------------------------------------------------------------- #


def test_pre_limiter_reproof_requires_lt_before_subsonic() -> None:
    from jasper.active_speaker.graph_safety import GraphPipelineStep, GraphView

    owner = frozenset(OWNER_CHANNELS)
    view = GraphView(
        parsed_ok=True,
        filters={},
        pipeline_steps=(GraphPipelineStep(owner, ("bass_ext_subsonic", "bass_ext_lt")),),
    )
    assert not derivation._prove_pre_limiter_prefix(
        view, owner_channels=owner, limiter_name=LIMITER_NAME
    )


def test_pre_limiter_reproof_rejects_limiter_present() -> None:
    from jasper.active_speaker.graph_safety import GraphPipelineStep, GraphView

    owner = frozenset(OWNER_CHANNELS)
    view = GraphView(
        parsed_ok=True,
        filters={},
        pipeline_steps=(
            GraphPipelineStep(owner, ("bass_ext_lt", "bass_ext_subsonic", LIMITER_NAME)),
        ),
    )
    assert not derivation._prove_pre_limiter_prefix(
        view, owner_channels=owner, limiter_name=LIMITER_NAME
    )


def test_pre_limiter_reproof_accepts_the_valid_prefix() -> None:
    from jasper.active_speaker.graph_safety import GraphPipelineStep, GraphView

    owner = frozenset(OWNER_CHANNELS)
    view = GraphView(
        parsed_ok=True,
        filters={},
        pipeline_steps=(GraphPipelineStep(owner, ("bass_ext_lt", "bass_ext_subsonic")),),
    )
    assert derivation._prove_pre_limiter_prefix(
        view, owner_channels=owner, limiter_name=LIMITER_NAME
    )


# --------------------------------------------------------------------------- #
# R2(i)/(ii)/(iii) three-part prefix proof
# --------------------------------------------------------------------------- #


def test_filters_and_mixers_survive_verbatim_including_split_mixer_gain() -> None:
    derived = _derive("post_limiter")
    parsed = yaml.safe_load(derived.yaml_text)
    assert parsed["filters"] == LIVE_CONFIG["filters"]
    assert parsed["mixers"] == LIVE_CONFIG["mixers"]
    # The -6.02 dB split-mixer hazard specifically: an altered source gain
    # would be caught by this exact equality.
    assert (
        parsed["mixers"]["split_2way"]["mapping"][2]["sources"][0]["gain"] == -6.02
    )


def test_mismatched_split_mixer_gain_is_caught_by_filters_mixers_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hypothetical bug that alters the retained mixers block during
    derivation must be caught — simulate by asserting the identity check
    itself refuses on a real mismatch."""

    tampered_mixers = yaml.safe_load(yaml.safe_dump(LIVE_CONFIG["mixers"]))
    tampered_mixers["split_2way"]["mapping"][2]["sources"][0]["gain"] = 0.0
    with pytest.raises(derivation.DerivationError):
        derivation._assert_filters_mixers_identical(
            _live_yaml(), {**LIVE_CONFIG, "mixers": tampered_mixers}
        )


def test_filters_mixers_identity_catches_in_place_mutation_of_a_shared_object() -> None:
    """N1: ``derive_truncated_config``'s ``derived[key] = value`` loop copies
    live's ``filters``/``mixers`` values by REFERENCE (never re-serializing
    them), so ``derived["mixers"]`` and a same-session ``live["mixers"]`` can
    be the literal SAME dict object. The OLD check compared ``derived``
    against that ``live`` mapping directly — an in-place mutation of the
    SHARED object is invisible to that comparison (both sides see the same
    mutated data, so ``==`` stays true). Re-parsing the immutable SOURCE TEXT
    independently, as this function now does, still catches it."""

    live_dict = yaml.safe_load(_live_yaml())
    shared_mixers = live_dict["mixers"]  # what `derived["mixers"] = value` would alias
    derived = {**LIVE_CONFIG, "mixers": shared_mixers}
    # In-place mutation of the SHARED object — live_dict and derived both see
    # it, because it is literally the same object; only re-parsing the
    # untouched source text catches this.
    shared_mixers["split_2way"]["mapping"][2]["sources"][0]["gain"] = 0.0
    with pytest.raises(derivation.DerivationError):
        derivation._assert_filters_mixers_identical(_live_yaml(), derived)


def test_derived_pipeline_equals_live_pipeline_truncated_at_owner_step() -> None:
    derived = _derive("post_limiter")
    parsed = yaml.safe_load(derived.yaml_text)
    assert len(parsed["pipeline"]) == 2
    assert parsed["pipeline"][0] == LIVE_CONFIG["pipeline"][0]  # the Mixer step, verbatim


def test_devices_diff_is_exactly_the_r3_allowlist() -> None:
    derived = _derive("post_limiter")
    parsed = yaml.safe_load(derived.yaml_text)
    devices = parsed["devices"]
    assert devices["capture"]["type"] == "WavFile"
    assert "device" not in devices["capture"]
    assert "format" not in devices["capture"]
    assert "channels" not in devices["capture"]  # R3's verified WavFile correction
    assert devices["playback"]["type"] == "File"
    assert devices["playback"]["channels"] == 4  # unchanged: live pipeline width
    assert devices["playback"]["format"] == "F64_LE"
    assert "device" not in devices["playback"]
    assert devices["enable_rate_adjust"] is False
    # Unchanged-by-R3 keys survive verbatim.
    assert devices["samplerate"] == 48000
    assert devices["chunksize"] == 1024
    assert devices["volume_limit"] == 0.0
    assert devices["queuelimit"] == 4
    assert devices["target_level"] == 1024


def test_receipt_carries_citations_and_concrete_values_not_just_docstring_prose() -> None:
    """S3: receipts are bundle evidence a reviewer or the producer's own
    tests can inspect without reading source comments — every recorded
    obligation carries a file+symbol citation, and device_diff records the
    queuelimit/target_level values even though they are unchanged."""

    derived = _derive("post_limiter")
    receipt = derived.receipt
    obligations = receipt["recorded_obligations"]
    for key in (
        "wav_capture_semantics",
        "processing_precision",
        "fader_application_point",
        "queuelimit_target_level",
    ):
        entry = obligations[key]
        assert entry["file"]
        assert entry["symbol"]
        assert entry["fact"]
    # The fader obligation is the one R10(b) safety-load-bearing citation —
    # its offset_sign field must say the render carries it (offset 0), never
    # the old "-recorded_main_volume_db" branch.
    assert "reproduce the recorded fader gain" in obligations["fader_application_point"][
        "offset_sign"
    ]
    assert "0 dB" in obligations["fader_application_point"]["offset_sign"]

    device_diff = receipt["device_diff"]
    assert device_diff["queuelimit_live"] == 4
    assert device_diff["queuelimit_derived"] == 4
    assert device_diff["target_level_live"] == 1024
    assert device_diff["target_level_derived"] == 1024


def test_artifact_header_mismatch_refuses_before_any_render() -> None:
    with pytest.raises(derivation.DerivationError):
        _derive("post_limiter", capture_header=_header(sample_rate_hz=44100))
    with pytest.raises(derivation.DerivationError):
        _derive("post_limiter", capture_header=_header(channels=1))
    with pytest.raises(derivation.DerivationError):
        _derive("post_limiter", capture_header=_header(bits_per_sample=24))


def test_more_than_one_owner_step_refuses() -> None:
    live = yaml.safe_load(_live_yaml())
    live["pipeline"].append(dict(live["pipeline"][-1]))
    with pytest.raises(derivation.DerivationError):
        _derive("post_limiter", live_active_config_raw=yaml.safe_dump(live, sort_keys=False))


# --------------------------------------------------------------------------- #
# R7 owner-path stage allowlist
# --------------------------------------------------------------------------- #


def test_bypassed_retained_step_refuses() -> None:
    live = yaml.safe_load(_live_yaml())
    live["pipeline"][0]["bypassed"] = True
    with pytest.raises(derivation.DerivationError):
        _derive("post_limiter", live_active_config_raw=yaml.safe_dump(live, sort_keys=False))


def test_non_allowlisted_filter_type_refuses() -> None:
    live = yaml.safe_load(_live_yaml())
    live["filters"]["bass_ext_lt"]["type"] = "Volume"
    with pytest.raises(derivation.DerivationError):
        _derive("post_limiter", live_active_config_raw=yaml.safe_dump(live, sort_keys=False))


def test_processor_type_pipeline_step_refuses() -> None:
    live = yaml.safe_load(_live_yaml())
    live["pipeline"].insert(0, {"type": "Processor", "name": "some_compressor"})
    live["pipeline"][2]["channels"] = [2, 3]  # keep owner step findable
    with pytest.raises(derivation.DerivationError):
        _derive("post_limiter", live_active_config_raw=yaml.safe_dump(live, sort_keys=False))


def test_async_resampler_block_refuses() -> None:
    live = yaml.safe_load(_live_yaml())
    live["devices"]["resampler"] = {"type": "AsyncSinc"}
    with pytest.raises(derivation.DerivationError):
        _derive("post_limiter", live_active_config_raw=yaml.safe_dump(live, sort_keys=False))


def test_enable_rate_adjust_is_not_a_refusal_trigger() -> None:
    """enable_rate_adjust: true on the live config is normalized to false by
    R3, never a refusal — distinct from an actual devices.resampler block."""

    derived = _derive("post_limiter")  # LIVE_CONFIG already has enable_rate_adjust: true
    parsed = yaml.safe_load(derived.yaml_text)
    assert parsed["devices"]["enable_rate_adjust"] is False


@pytest.mark.parametrize(
    "allowed_type", ["Biquad", "BiquadCombo", "Conv", "Delay", "Gain", "Limiter"]
)
def test_every_r7_allowlisted_filter_type_is_admitted(allowed_type: str) -> None:
    live = yaml.safe_load(_live_yaml())
    live["filters"]["bass_ext_lt"]["type"] = allowed_type
    if allowed_type == "Conv":
        live["filters"]["bass_ext_lt"]["parameters"] = {"type": "Values", "values": [1.0]}
    elif allowed_type == "Delay":
        live["filters"]["bass_ext_lt"]["parameters"] = {"delay": 1.0, "unit": "ms"}
    elif allowed_type == "Gain":
        live["filters"]["bass_ext_lt"]["parameters"] = {"gain": 0.0}
    # Biquad/BiquadCombo/Limiter keep their existing (already-allowed) shape.
    _derive("post_limiter", live_active_config_raw=yaml.safe_dump(live, sort_keys=False))


# --------------------------------------------------------------------------- #
# owner_path_stages — the narrower backward-walked subset (R6's delay/impulse
# math), independent of the blanket R7 admission gate above.
# --------------------------------------------------------------------------- #


def test_owner_path_stages_walks_the_split_mixer_backward() -> None:
    live = yaml.safe_load(_live_yaml())
    pipeline = live["pipeline"]
    owner_step_index = len(pipeline) - 1
    retained = pipeline[: owner_step_index + 1]
    stages = derivation.owner_path_stages(
        retained, live["mixers"], owner_channels=frozenset(OWNER_CHANNELS)
    )
    kinds = [step["type"] for step in stages]
    assert "Mixer" in kinds
    assert "Filter" in kinds


def test_owner_path_stages_excludes_unrelated_channels() -> None:
    """A Filter step on channels the backward walk never reaches (e.g. a
    tweeter path on channels the split mixer's sources never trace back to)
    is not part of the owner path, even though R7's admission gate above
    still requires it to be allowlisted if retained."""

    live = yaml.safe_load(_live_yaml())
    # The split mixer's sources for channels 2/3 trace back to capture
    # channels {0, 1} — channel 5 is reached by neither the owner step nor
    # that backward walk, so a step confined to it is genuinely unrelated.
    unrelated = {
        "type": "Filter",
        "channels": [5],
        "names": ["bass_ext_lt"],  # reuse an allowlisted name; content irrelevant here
    }
    pipeline = [unrelated, *live["pipeline"]]
    owner_step_index = len(pipeline) - 1
    retained = pipeline[: owner_step_index + 1]
    stages = derivation.owner_path_stages(
        retained, live["mixers"], owner_channels=frozenset(OWNER_CHANNELS)
    )
    assert unrelated not in stages
