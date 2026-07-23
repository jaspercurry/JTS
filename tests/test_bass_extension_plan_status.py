# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "docs" / "HANDOFF-bass-extension-plan.md"
README = ROOT / "README.md"


def test_bass_extension_plan_pins_merged_waves_and_remaining_gate() -> None:
    text = PLAN.read_text(encoding="utf-8")

    for merge_sha in (
        "0670540654a6684f8ac98fb2e70b2e643d65d82f",
        "9f39c70e418cf64316c23de535f322d21f825c8e",
        "bb2919383b408d630f9d70ef24c14fe38ca98be0",
    ):
        assert merge_sha in text

    assert "The transaction has\n> no production caller" in text
    assert "crossover-program hardware burn-in prerequisite is **met**" in text
    assert "contract rev 8 freezes limiter protocol revision `2026-07-19b`" in text
    assert "reviewed bench runner/temporary activation owner" in text
    assert "no real\n> target-specific limiter result is established yet" in text
    # Honesty reconciliation: the plan records the merged bench-runner software
    # (#1611 producer, #1630 runner/activation-owner/context-builder) and the
    # landed rev-9 `ladder.py` slice, while the live on-device tap executor and
    # the accepted bundle stay named as the real remaining blockers. Pin the
    # true claim so it cannot silently regress to "runner unbuilt".
    assert "jasper/bass_extension/ladder.py" in text
    assert "live pre/post-limiter tap executor" in text
    assert "No implementation is authorized by revision 6" not in text
    assert "no frozen contract maps ladder/sustain" not in text
    assert "no frozen\n  wave defines" not in text
    assert "Wave 4 revision 6" not in text
    assert "missing deterministic limiter producer" not in text
    assert "runtime/audio emission has not shipped" not in text
    assert "hardware-unvalidated" not in text


def test_readme_does_not_claim_bass_extension_has_no_code() -> None:
    text = README.read_text(encoding="utf-8")
    entry = re.search(
        r"- \[`HANDOFF-bass-extension-plan\.md`\].*?"
        r"(?=\n- \[`HANDOFF-correction\.md`\])",
        text,
        flags=re.DOTALL,
    )

    assert entry is not None
    assert "Waves 1–3 are merged" in entry.group()
    assert "No code exists yet" not in entry.group()
    # `ladder.py` landed with the Wave 4 rev-9 hardware-free commissioning
    # slice; the backend/scheduler/runtime surfaces remain unshipped (blocked
    # behind Jasper's accepted bench bundle and later reviewed revisions).
    for unshipped_surface in (
        "jasper/web/bassext_backend.py",
        "jasper/bass_extension/scheduler.py",
        "jasper/bass_extension/runtime.py",
    ):
        assert not (ROOT / unshipped_surface).exists(), unshipped_surface


def test_wave3_transactions_have_no_production_callers() -> None:
    owner = ROOT / "jasper" / "bass_extension" / "__init__.py"
    entry_points = {
        "apply_bass_extension",
        "bypass_bass_extension",
        "recover_pending_bass_extension_apply",
    }
    for path in (ROOT / "jasper").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        allowed_owner_uses: set[int] = set()
        if path == owner:
            bypass = next(
                node
                for node in tree.body
                if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "bypass_bass_extension"
            )
            delegation_calls = [
                node
                for node in ast.walk(bypass)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "apply_bass_extension"
            ]
            assert len(delegation_calls) == 1
            allowed_owner_uses.add(id(delegation_calls[0].func))

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert entry_points.isdisjoint(alias.name for alias in node.names), path
            elif (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in entry_points
            ):
                assert id(node) in allowed_owner_uses, path
            elif isinstance(node, ast.Attribute) and node.attr in entry_points:
                assert False, path
