# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The emit-loop CLI's argument contract and its no-binary preflight.

``--dry-run`` is the only path exercised here: the rendering path resolves the
binary from ``jasper-camilla.service`` and belongs to the on-device run, while
the loop itself is covered against a stand-in binary in
``tests/test_active_speaker_emit_bench_loop.py``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from jasper.cli import active_speaker_emit_bench as cli

SHELF_Q = 1.0 / math.sqrt(2.0)


def _fit_file(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "linearization.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _fits() -> dict[str, object]:
    """The persisted ``{role: LinearizationFit.to_dict()}`` shape."""

    return {
        "tweeter": {
            "role": "tweeter",
            "filters": [
                {
                    "biquad_type": "Lowshelf",
                    "freq": 8400.0,
                    "q": SHELF_Q,
                    "gain": -11.0,
                }
            ],
        }
    }


def _args(tmp_path: Path, *extra: str, fits: object | None = None) -> list[str]:
    return [
        "--linearization",
        str(_fit_file(tmp_path, _fits() if fits is None else fits)),
        "--playback-device",
        "hw:CARD=DAC8x,DEV=0",
        "--out",
        str(tmp_path / "bundle"),
        *extra,
    ]


def test_dry_run_emits_and_derives_both_arms_but_renders_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--dry-run`` is a preflight, not an echo of the arguments.

    It runs the real emitter and the real derivation, so an emitter refusal or
    an inadmissible stage surfaces here — on a laptop — instead of on the Pi.
    """

    rc = cli.main(_args(tmp_path, "--dry-run"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "tweeter: 1 filter(s) Lowshelf@8400Hz/-11.0dB" in out
    assert "analysis band:" in out
    assert "derived both arms:" in out
    assert "no binary resolved, nothing rendered" in out

    bundle = tmp_path / "bundle"
    control, treated = bundle / "control.yml", bundle / "treated.yml"
    assert control.is_file() and treated.is_file()
    assert "as_tweeter_linearization_shelf" in treated.read_text()
    assert "as_tweeter_linearization_shelf" not in control.read_text()
    assert "type: WavFile" in control.read_text()
    # Nothing was rendered, and the multi-megabyte stimulus was not written.
    assert not (bundle / "stimulus.wav").exists()
    assert not list(bundle.glob("*.raw"))


def test_dry_run_surfaces_an_emitter_refusal_without_a_binary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The documented value of the preflight, exercised.

    A positive-gain-shaped shelf run the emitter's own validation rejects must
    be caught by ``--dry-run``, not discovered after an SSH round trip.
    """

    bad = {
        "tweeter": {
            "filters": [
                {"biquad_type": "Notch", "freq": 8400.0, "q": SHELF_Q, "gain": -11.0}
            ]
        }
    }
    rc = cli.main(_args(tmp_path, "--dry-run", fits=bad))
    assert rc == 2
    assert "REFUSED" in capsys.readouterr().err


def test_a_stale_report_is_removed_before_a_run_that_may_refuse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refusing run must not leave a previous verdict beside fresh configs."""

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    stale = bundle / "report.json"
    stale.write_text('{"outcome": "matched"}', encoding="utf-8")
    assert cli.main(_args(tmp_path, "--dry-run")) == 0
    capsys.readouterr()
    assert not stale.exists()


def test_an_already_reduced_linearization_shape_is_refused(tmp_path: Path) -> None:
    """The reducer returns ``{}`` — not an error — on the reduced shape.

    A CLI that sniffed between the two shapes would silently grade nothing, so
    one shape is accepted and the other is named in the refusal.
    """

    reduced = {"tweeter": [{"biquad_type": "Lowshelf", "freq": 8400.0, "q": SHELF_Q, "gain": -11.0}]}
    with pytest.raises(SystemExit, match="already-reduced"):
        cli.main(
            [
                "--linearization",
                str(_fit_file(tmp_path, reduced)),
                "--playback-device",
                "hw:CARD=DAC8x,DEV=0",
                "--dry-run",
            ]
        )


def test_a_missing_or_malformed_linearization_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="could not read"):
        cli.main(
            [
                "--linearization",
                str(tmp_path / "absent.json"),
                "--playback-device",
                "x",
                "--dry-run",
            ]
        )
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(SystemExit, match="keyed by driver role"):
        cli.main(
            ["--linearization", str(bad), "--playback-device", "x", "--dry-run"]
        )


def test_playback_device_and_linearization_are_required() -> None:
    with pytest.raises(SystemExit):
        cli.main([])


class _StubReport:
    """The three fields the CLI's exit-code mapping reads."""

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.branches = ()
        self.binary = {"binary_path": "/x", "version_output": "CamillaDSP 4.1.3"}
        self.expected_offset_db = 0.0

    def to_dict(self) -> dict[str, object]:
        return {"outcome": self.outcome}


@pytest.mark.parametrize(
    "outcome, expected_rc",
    [("matched", 0), ("mismatched", 1), ("unavailable", 2)],
)
def test_exit_code_is_three_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
    expected_rc: int,
) -> None:
    """``unavailable`` is neither a pass nor a mismatch, and the shell sees that.

    Folding it into 0 would grant permission on no evidence; folding it into 1
    would report a mismatch on a branch that was measured cleanly and simply had
    nothing to verify — the ordinary shape of a single-driver correction.
    """

    monkeypatch.setattr(cli, "resolve_render_binary", lambda: object())
    monkeypatch.setattr(cli, "run_emit_loop", lambda *a, **k: _StubReport(outcome))
    (tmp_path / "bundle").mkdir()
    rc = cli.main(_args(tmp_path))
    captured = capsys.readouterr()
    assert rc == expected_rc
    if outcome == "unavailable":
        assert "no branch could be graded" in captured.err
    assert (tmp_path / "bundle" / "report.json").is_file()


def test_there_is_no_binary_override_flag() -> None:
    """R5: the render binary is resolved from the running unit, never overridden.

    A ``--binary`` flag would be the obvious convenience and is deliberately
    absent — it would let a bench run grade a build that is not the one the
    speaker actually runs.
    """

    with pytest.raises(SystemExit):
        cli.main(
            [
                "--linearization",
                "x.json",
                "--playback-device",
                "y",
                "--binary",
                "/usr/bin/camilladsp",
            ]
        )
