# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator CLI preflight: the dry run composes what the live run activates.

The box state (topology, applied snapshot, selected graph) and the ONE composer
are stubbed; everything between them — the manifest authoring, the real
per-rung plans, and the refusal exits — is this CLI's own.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from jasper.bass_extension.bench import plan as plan_module
from jasper.bass_extension.bench.manifest import (
    STIMULUS_ROLES,
    author_campaign_manifest,
)
from jasper.bass_extension.bench.runner import BenchRefused
from jasper.cli import bass_extension_bench
from tests.test_bass_extension_bench_plan import (
    BOOSTED_ID,
    applied_profile,
    graph_text,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _request() -> dict[str, Any]:
    return {
        "requested_stimulus_band_hz": [30.0, 200.0],
        "requested_stimulus_effective_peak_dbfs": -30.0,
        "requested_commanded_main_volume_db": -35.0,
        "requested_hold_duration_s": 12.0,
        "requested_cooldown_s": 4.0,
        "requested_repeat_count": 2,
        "stimulus_generator_identity": "gen-v1",
        "render_timeout_s": 30.0,
        "render_rlimit_as_bytes": 536_870_912,
        "render_rlimit_cpu_s": 60,
        "render_nice": 10,
        "cross_check_poll_interval_s": 0.25,
        "cross_check_read_count": 40,
        "cross_check_tolerance_db": 1.5,
    }


def _inputs(*target_ids: str) -> dict[str, Any]:
    return {
        "driver_safety_fingerprint": _sha("ds"),
        "margin_policy_name": "conservative",
        "margin_policy_fingerprint": _sha("mp"),
        "requests": {tid: {role: _request() for role in STIMULUS_ROLES} for tid in target_ids},
    }


def _write(tmp_path: Path, inputs: dict[str, Any]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(inputs), encoding="utf-8")
    return path


@pytest.fixture
def box(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A commissioned box: a family to bench and a graph to compose onto."""

    state: dict[str, Any] = {"applied": applied_profile()}
    monkeypatch.setattr(
        bass_extension_bench,
        "_box_state",
        lambda: (object(), state["applied"], Path("/var/lib/camilladsp/selected.yml")),
    )
    monkeypatch.setattr(
        plan_module,
        "recompose_active_baseline_for_bass_extension",
        lambda topology, **kwargs: graph_text(target_id=str(kwargs["bass_target_id"])),
    )
    return state


def test_dry_run_authors_the_manifest_and_composes_every_rung(
    tmp_path: Path, box: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, _inputs(BOOSTED_ID, "natural"))
    rc = bass_extension_bench.main([str(path), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "margin=conservative" in out
    assert f"{BOOSTED_ID}, natural" in out
    assert "jts_bass_extension_limiter_evidence" in out
    assert "[-120.0, 0.0] dBFS" in out
    # The composed plans, target by target: what the live run would activate.
    assert "composed 2 rung graph(s)" in out
    assert "as_woofer_baseline_limiter at -12 dBFS" in out
    assert "owner channels [0, 1]" in out
    assert "dry run: no device opened" in out


def test_missing_input_refuses_with_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    inputs = _inputs("deep")
    del inputs["requests"]["deep"]["sweep_transparency"]["requested_hold_duration_s"]
    path = _write(tmp_path, inputs)
    rc = bass_extension_bench.main([str(path), "--dry-run"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "requests.deep.sweep_transparency.requested_hold_duration_s" in err


def test_unknown_margin_is_rejected(tmp_path: Path) -> None:
    inputs = _inputs("deep")
    inputs["margin_policy_name"] = "reckless"
    path = _write(tmp_path, inputs)
    with pytest.raises(SystemExit):
        bass_extension_bench.main([str(path), "--dry-run"])


@pytest.mark.parametrize("argv", [[], ["--dry-run", "--live"]])
def test_the_safe_dry_run_posture_is_the_default_and_wins(
    tmp_path: Path,
    box: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    path = _write(tmp_path, _inputs("natural"))
    rc = bass_extension_bench.main([str(path), *argv])
    assert rc == 0
    assert "dry run: no device opened" in capsys.readouterr().out


def test_a_box_with_no_applied_family_refuses_before_any_device(
    tmp_path: Path, box: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The campaign is composed from what the box APPLIED; without a family
    there is no rung to play, and nothing is opened to find that out."""

    box["applied"] = {}
    path = _write(tmp_path, _inputs("natural"))

    rc = bass_extension_bench.main([str(path), "--live"])

    assert rc == 2
    assert "bench_no_applied_family" in capsys.readouterr().err


def test_live_run_refuses_when_the_render_binary_cannot_be_resolved(
    tmp_path: Path, box: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.bass_extension.bench.render import RenderError

    def _raise() -> None:
        raise RenderError("no jasper-camilla.service on this host")

    monkeypatch.setattr(bass_extension_bench, "resolve_render_binary", _raise)
    path = _write(tmp_path, _inputs("natural"))
    assert bass_extension_bench.main([str(path), "--live"]) == 2


async def test_a_box_that_moved_off_the_composed_graph_refuses_before_activation(
    tmp_path: Path, box: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rungs are composed onto ONE installed graph; a box now running a
    different file would be measured against overlays it no longer carries, so
    the campaign refuses before the first activation."""

    from types import SimpleNamespace

    from jasper import camilla
    from jasper.bass_extension.bench import activation
    from jasper.bass_extension.bench.runner import Stop

    async def _snapshot(controller: Any) -> activation.PredecessorSnapshot:
        return activation.PredecessorSnapshot(
            active_config_raw="devices: {}\n",
            config_file_path="/var/lib/camilladsp/configs/somewhere-else.yml",
            graph_fingerprint="f" * 64,
        )

    monkeypatch.setattr(camilla, "primary_controller", lambda: object())
    monkeypatch.setattr(activation, "snapshot_predecessor", _snapshot)
    path = _write(tmp_path, _inputs("natural"))
    manifest = author_campaign_manifest(_inputs("natural"), target_ids=("natural",))
    campaign = bass_extension_bench._compose(manifest, ("natural",))

    with pytest.raises(BenchRefused) as raised:
        await bass_extension_bench._campaign(
            SimpleNamespace(bundle_dir=tmp_path / "bundle", manifest=path),
            manifest,
            campaign,
            binary=None,
            stop=Stop(),
        )

    assert raised.value.reason == "bench_selected_graph_mismatch"
