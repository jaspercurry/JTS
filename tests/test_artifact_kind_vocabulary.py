# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one artifact-``kind`` vocabulary: namespaced writes, tolerant reads.

``ArtifactEntry.kind`` was opaque free text shared by three features writing
into one flat space. This holds both halves of the rule
:mod:`jasper.audio_measurement.bundles` now states:

* a NEW kind carries its owner (``jts_<owner>_<name>``), while the kinds
  written before the rule are grandfathered and never renamed — banked
  bundles are durable evidence with no migration mechanism;
* a reader that meets an unknown kind degrades readably and never raises,
  which is what makes tightening the write side safe without touching a
  single bundle on disk.

The reader half was already true by construction — no consumer branched on a
kind value in a way that could raise. True by construction is not the same as
true on purpose: nothing stopped the next reader from writing ``KINDS[kind]``.
These tests are what makes it a contract.

**Known bound:** the source scan walks ``jasper/`` only and reads ``kind=``
literals only, so a manifest writer living outside that tree, or one passing a
computed kind, is invisible to it. Verified empty as of 2026-08-22 — every
writer is under ``jasper/``, and the single computed site
(``correction/artifacts.py``'s three-value capture-kind dict) is matched from
the source text instead. The runtime guard in :func:`validate_artifact_kind`
is what covers a computed kind on a path this scan cannot see.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from jasper.audio_measurement import bundles

REPO_ROOT = Path(__file__).resolve().parents[1]

# Functions whose `kind=` keyword argument becomes ArtifactEntry.kind.
_KIND_WRITERS = {
    "record_artifact",
    "write_json_artifact",
    "record_existing",
    "write_json",
    "write_bytes",
}

UNKNOWN_KIND = "jts_some_future_feature_artifact"


# --------------------------------------------------------------------------
# Write side: a new kind carries its owner.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        "jts_bass_extension_bench_receipt",
        "jts_room_capture",
        "jts_active_round_receipt",
        UNKNOWN_KIND,
    ],
)
def test_namespaced_kinds_are_accepted(kind: str) -> None:
    assert bundles.is_namespaced_kind(kind)
    assert bundles.validate_artifact_kind(kind) == kind


@pytest.mark.parametrize(
    "kind",
    [
        "capture",
        "derived",
        "replay_response",
        "evidence_packet",
        "jts_room",  # a prefix with no artifact name is not a namespace
        "jts__capture",  # an empty owner segment is not an owner
        "notjts_room_capture",
    ],
)
def test_unnamespaced_new_kinds_are_refused(kind: str) -> None:
    assert not bundles.is_namespaced_kind(kind)
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.validate_artifact_kind(kind)
    # The refusal names the offending kind and the shape that fixes it.
    assert kind in str(excinfo.value)
    assert "jts_<owner>_<name>" in str(excinfo.value)


@pytest.mark.parametrize("kind", sorted(bundles.LEGACY_UNNAMESPACED_KINDS))
def test_every_grandfathered_kind_is_still_writable(kind: str) -> None:
    """The exemption is what keeps banked bundles un-migrated."""

    assert bundles.validate_artifact_kind(kind) == kind


@pytest.mark.parametrize("kind", ["", None, 7])
def test_a_kind_must_be_a_non_empty_string(kind: object) -> None:
    with pytest.raises(bundles.BundleError):
        bundles.validate_artifact_kind(kind)  # type: ignore[arg-type]


def _kind_literals_written_under(package: Path) -> dict[str, str]:
    """Every literal `kind=` a manifest writer is handed, by AST.

    Static rather than runtime: a code path no test exercises would otherwise
    ship an un-namespaced kind that the write-time guard never sees.
    """

    found: dict[str, str] = {}
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else (func.id if isinstance(func, ast.Name) else None)
            )
            if name not in _KIND_WRITERS:
                continue
            for keyword in node.keywords:
                if keyword.arg != "kind":
                    continue
                value = keyword.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.setdefault(
                        value.value,
                        f"{path.relative_to(REPO_ROOT)}:{keyword.lineno}",
                    )
    return found


def test_no_production_writer_adds_an_unnamespaced_kind() -> None:
    """The closed legacy set stays closed, across paths no test runs."""

    written = _kind_literals_written_under(REPO_ROOT / "jasper")
    assert written, "found no kind= writers — the AST walk is looking wrong"

    offenders = {
        kind: site
        for kind, site in written.items()
        if not bundles.is_namespaced_kind(kind)
        and kind not in bundles.LEGACY_UNNAMESPACED_KINDS
    }
    assert offenders == {}, (
        "new artifact kinds must be namespaced as jts_<owner>_<name>: "
        + ", ".join(f"{kind!r} at {site}" for kind, site in sorted(offenders.items()))
    )


def test_the_legacy_set_has_no_entry_nothing_writes() -> None:
    """Grandfathering a kind no writer emits would be dead vocabulary.

    `jasper/correction/artifacts.py` picks three of these from a dict rather
    than a literal keyword, so those are matched from the source text.
    """

    written = set(_kind_literals_written_under(REPO_ROOT / "jasper"))
    dispatched = (REPO_ROOT / "jasper/correction/artifacts.py").read_text(
        encoding="utf-8"
    )
    unwritten = {
        kind
        for kind in bundles.LEGACY_UNNAMESPACED_KINDS
        if kind not in written and f'"{kind}"' not in dispatched
    }
    assert unwritten == set(), f"grandfathered but never written: {sorted(unwritten)}"


# --------------------------------------------------------------------------
# Read side: an unknown kind is data, not an error.
# --------------------------------------------------------------------------


def _bundle_with_unknown_kind(tmp_path: Path) -> Path:
    """A bundle whose manifest carries a kind no reader in this tree knows."""

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    bundles.write_json_artifact(
        bundle,
        "info.json",
        {"bundle_schema_version": 5},
        kind="session_metadata",
        sensitivity="debug_safe",
        recomputable=False,
        generated_by="tests",
    )
    payload = bundle / "mystery.json"
    payload.write_text(json.dumps({"from": "a newer JTS"}))
    bundles.record_artifact(
        bundle,
        payload,
        kind=UNKNOWN_KIND,
        sensitivity="debug_safe",
        recomputable=True,
        generated_by="tests",
    )
    return bundle


def _manifest_kinds(bundle: Path) -> list[str]:
    manifest: dict[str, Any] = bundles.read_artifact_manifest(bundle)
    return [entry.get("kind") for entry in manifest["artifacts"]]


def test_the_neutral_reader_returns_an_unknown_kind_untouched(tmp_path: Path) -> None:
    bundle = _bundle_with_unknown_kind(tmp_path)
    assert UNKNOWN_KIND in _manifest_kinds(bundle)


def test_bundle_validation_does_not_fault_an_unknown_kind(tmp_path: Path) -> None:
    """The manifest validator checks that `kind` is PRESENT, never which one."""

    from jasper.correction import bundles as correction_bundles

    bundle = _bundle_with_unknown_kind(tmp_path)
    issues: list[correction_bundles.BundleIssue] = []
    correction_bundles._validate_artifact_manifest(
        bundle,
        issues,
        info_bundle_schema_version=5,
        require_manifest=True,
    )
    assert not [
        issue for issue in issues if UNKNOWN_KIND in str(issue)
    ], f"an unknown kind raised a bundle issue: {issues}"


def test_artifact_counts_gives_an_unknown_kind_its_own_bucket(tmp_path: Path) -> None:
    """A histogram over kinds must not collapse or drop what it cannot name."""

    from jasper.correction import bundle_tools

    bundle = _bundle_with_unknown_kind(tmp_path)
    counts = bundle_tools._artifact_counts(bundles.read_artifact_manifest(bundle))
    assert counts[UNKNOWN_KIND] == 1
    # "unknown" is reserved for an entry with no kind at all, not for one
    # whose kind this build has never heard of.
    assert "unknown" not in counts


def test_fir_readiness_reports_a_missing_kind_rather_than_raising(
    tmp_path: Path,
) -> None:
    from jasper.correction import bundle_tools

    bundle = _bundle_with_unknown_kind(tmp_path)
    readiness = bundle_tools.fir_readiness(bundle)
    # The two kinds it looks up by name are absent, so it reports zero and
    # names what is missing. An unknown kind is neither counted as one of
    # them nor allowed to abort the summary.
    assert readiness["derived_impulse_response_count"] == 0
    assert readiness["ready_for_runtime_import"] is False
    assert "derived impulse-response artifacts" in readiness["missing"]


def test_advisor_manifest_context_survives_an_unknown_kind(tmp_path: Path) -> None:
    from jasper.calibration_agent import advisor_context

    bundle = _bundle_with_unknown_kind(tmp_path)
    context = advisor_context._manifest_context(bundle)
    assert context["available"] is True
    assert context["artifact_count"] == 2
    # Unknown, and not audio by sensitivity — counted as an artifact, not as
    # private audio, and above all not an error.
    assert context["private_audio_count"] == 0


def test_private_audio_allowlist_names_only_real_kinds() -> None:
    """Every entry must be a kind something writes — the drift that bit here.

    Four of this set's five entries once matched nothing at all, so the clause
    read as coverage while `sensitivity` did all the work.
    """

    from jasper.calibration_agent import advisor_context

    written = set(_kind_literals_written_under(REPO_ROOT / "jasper"))
    dispatched = (REPO_ROOT / "jasper/correction/artifacts.py").read_text(
        encoding="utf-8"
    )
    phantom = {
        kind
        for kind in advisor_context._PRIVATE_AUDIO_KINDS
        if kind not in written and f'"{kind}"' not in dispatched
    }
    assert phantom == set(), (
        f"private-audio allowlist names kinds nothing writes: {sorted(phantom)}"
    )
