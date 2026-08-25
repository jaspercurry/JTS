# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Doc-pin guards for `docs/HANDOFF-crossover-measurement-v2.md` (#2400 item 1).

Two promises the canonical doc's own prose makes about itself, each pinned
here because both of #2291 Phase 5c-v's defect rounds were "no test pinned
the promise":

* **Tier numbers.** "What it is" states `TIER_FULL`/`TIER_EXPRESS`/`TIER_REMOTE`'s capture
  counts as prose, right next to its own instruction not to: "Do not restate
  those numbers anywhere a plan change cannot reach them --
  `tier_display_info()` derives them from the plans themselves." So this
  guard asserts the doc's prose against `tier_display_info()`'s DERIVED
  values, never against a second literal copy typed into the test.
* **Event names.** The "Debugging" section's `journalctl | grep -E` recipes
  name `event=` values as a triage aid. This guard extracts every name from
  those recipes (regex over the doc text, not a hand-typed list -- so the
  doc, not the test, is what's being pinned) and resolves each against what
  the code actually logs.

**Why the event-name resolver needs FOUR emission paths, not one.** A naive
scan for `log_event(logger, "literal", ...)` call sites misses most of the
vocabulary and false-positives on names the doc is right to cite. Verified
against the real doc + the real `jasper/` tree (ablating each path in turn
and checking which of the doc's 31 recipe names drop out of the resolved
set):

1. **`log_event`'s own `name` argument** (positional index 1 -- `logger` is
   index 0, and both are positional-only). Covers 26 of the 31 -- the
   direct-call majority (`correction.crossover_v2_authorized`,
   `..._applied`, `..._delta_probe`, the `session_volume_*` family, etc).
2. **The organs' `emit(event, fields, level=...)` closure** (arg-0 --
   `intervention.py`'s local disclosure port, wraps into a `JournalRecord`).
   Without this path, `correction.crossover_v2_realized_level_match` --
   emitted via `emit("correction.crossover_v2_realized_level_match", ...)`
   in `intervention.py`, never through `log_event` directly -- resolves to
   nothing, and the guard false-positives exactly as both #2291 5c-v
   rebuilt sweeps initially did on this name (the issue's own history).
3. **A module-level `NAME = "literal"` constant binding.** Several event
   names are constants (`accountability.py`'s `EVENT_LEVEL_FRAME_FINDING`
   etc), referenced by NAME at the construction call site -- sometimes
   inside a ternary (`EVENT_LEVEL_FRAME_FINDING if banked else
   EVENT_LEVEL_FRAME_REFUSED`), sometimes by keyword
   (`GateRecord(event=SOME_EVENT, ...)` by keyword). Resolved
   per-module (a name is looked up in the SAME file's own constants --
   every case found ties a reference to a same-file definition; this
   deliberately does not chase imports).
4. **`GateRecord`/`FailureRecord`/`JournalRecord` construction sites.**
   These three record types are the ONLY way `CrossoverV2Session.
   _journal_linearization` (`crossover_v2_flow.py:9611`,
   `log_event(logger, record.event, ...)`) ever says anything -- but
   `record.event` is an opaque attribute read at that call site, invisible
   to static analysis. So this resolver does not try to resolve it THERE;
   it instead treats each record type's constructor as its own emission
   source (arg-0, or a keyword named `event`), which is where the literal
   or constant-reference actually lives. Without this path,
   `correction.crossover_v2_level_frame_finding`,
   `..._level_frame_refused`, `..._level_match_finding`, and
   `..._prediction_gate` -- each constructed as a `GateRecord` in
   `accountability.py` and drained only through that opaque `record.event`
   call -- all resolve to nothing (jointly with path 3, since each is
   referenced there by constant name, not a literal).

Path 1 ablated: 26/31 recipe names drop out. Path 2 ablated:
`realized_level_match` drops out alone. Paths 3 and 4 ablated (each
independently): the same four `level_frame`/`level_match`/`prediction_gate`
names drop out. All four are load-bearing for the doc's OWN current content,
not merely hypothetically required.
"""
from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

from jasper.active_speaker.crossover_v2_flow import (
    TIER_EXPRESS,
    TIER_FULL,
    TIER_REMOTE,
    tier_display_info,
)

ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = ROOT / "docs" / "HANDOFF-crossover-measurement-v2.md"
_JASPER_ROOT = ROOT / "jasper"

# The doc is a live spine followed by a "tagged historical" appendix
# (AGENTS.md doc rule 10) whose specific facts are deliberately NOT kept in
# sync with the code. The event-name guard only scans grep recipes ABOVE
# this heading -- scanning the appendix too would fail the guard on facts
# the doc's own convention says are allowed to drift.
_APPENDIX_MARKER = "## Appendix — the campaign record (historical)"

# One `journalctl ... | grep -E 'event=<prefix>\.(alt1|alt2|...)'` recipe
# line: group 1 is the backslash-escaped dotted prefix, group 2 the
# pipe-separated alternatives.
_RECIPE_RE = re.compile(r"event=([\w\\.]+)\(([\w|]+)\)")


def _extract_recipe_event_names(doc_text: str) -> set[str]:
    """Every full event name named by a live-spine `grep -E` recipe."""
    # A missing marker makes str.partition() return the WHOLE text as
    # "spine" with no error -- silently re-including the appendix instead
    # of failing loudly on a heading rename.
    assert _APPENDIX_MARKER in doc_text, (
        "appendix heading not found -- did it get renamed? the live-spine/"
        "appendix split below would silently scan the whole doc instead"
    )
    spine, _, _ = doc_text.partition(_APPENDIX_MARKER)
    names: set[str] = set()
    for prefix, alternatives in _RECIPE_RE.findall(spine):
        prefix = prefix.replace("\\", "")
        for alt in alternatives.split("|"):
            names.add(f"{prefix}{alt}")
    return names


# Callable name -> the 0-indexed positional slot its event-name argument
# occupies (emission paths 1, 2 and 4's three record constructors).
_EVENT_ARG_INDEX = {
    "log_event": 1,
    "emit": 0,
    "GateRecord": 0,
    "FailureRecord": 0,
    "JournalRecord": 0,
}


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _event_call_argument(node: ast.Call, index: int) -> ast.AST | None:
    """The AST node holding a call's event-name argument, positional or kw.

    `log_event`'s `name` parameter is positional-only (can never be a
    keyword), but `emit()` and the three record dataclasses are ordinary
    Python -- some modules call `GateRecord(event=SOME_EVENT, ...)` by
    keyword, so a positional-only lookup would silently miss it.
    """
    if len(node.args) > index:
        return node.args[index]
    for kw in node.keywords:
        if kw.arg == "event":
            return kw.value
    return None


def _resolve_event_expr(node: ast.AST, constants: dict[str, str]) -> set[str]:
    """Resolve one event-name expression to the literal(s) it can be.

    A bare string literal resolves to itself (paths 1/2/4's common case); a
    NAME resolves through the same file's own constant table (path 3); an
    `X if cond else Y` ternary -- the accountability gate's banked/refused
    choice -- recurses into both arms, since either can be said depending on
    a runtime condition this static scan does not evaluate. Anything else
    (the `record.event` attribute read at the one drain call site this
    resolver deliberately does not chase -- see the module docstring)
    resolves to nothing, which is the correct, fail-closed answer for an
    expression this resolver was not asked to understand.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name) and node.id in constants:
        return {constants[node.id]}
    if isinstance(node, ast.IfExp):
        return _resolve_event_expr(node.body, constants) | _resolve_event_expr(
            node.orelse, constants
        )
    return set()


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"` string bindings (emission path 3)."""
    out: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            out[node.targets[0].id] = node.value.value
    return out


@functools.lru_cache(maxsize=None)
def logged_event_names(root: Path = _JASPER_ROOT) -> frozenset[str]:
    """Every event name reachable through the four emission paths above.

    Whole-tree discovery (every `.py` under `jasper/`), not a curated file
    list naming the crossover_v2 package -- the doc's calibration events
    (`correction.crossover_v2_calibration_resolve_failed` etc) are said from
    `jasper/web/correction_crossover_v2.py`, outside that package, so a
    narrower scope would have missed them.

    The `emit` matcher is by BARE NAME, not by verifying the call resolves
    to `intervention.py`'s specific closure, so the returned set is a
    superset of the true crossover_v2 vocabulary by construction -- it also
    picks up unrelated `emit(...)` calls elsewhere in `jasper/` (e.g.
    `jasper/voice/trace.py`'s `emit(kind, payload)`). Harmless for every
    caller here: each only asserts a name IS in the set (recipe names must
    be a subset) or IS NOT (the fabricated-name/decoy controls), and a wider
    set can only make the first kind of assertion easier to satisfy, never
    the second.
    """
    names: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        constants = _module_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            index = _EVENT_ARG_INDEX.get(_call_name(node))
            if index is None:
                continue
            argument = _event_call_argument(node, index)
            if argument is None:
                continue
            names |= _resolve_event_expr(argument, constants)
    return frozenset(names)


def test_doc_tier_capture_counts_match_tier_display_info():
    """The "What it is" capture-count prose, pinned to the derived numbers.

    Built from `tier_display_info()`'s own dict rather than re-typed as a
    literal -- exactly the second-copy-drift the doc's own instruction
    warns against ("Do not restate those numbers anywhere a plan change
    cannot reach them").
    """
    info = tier_display_info()
    full, express = info[TIER_FULL], info[TIER_EXPRESS]
    remote = info[TIER_REMOTE]
    doc = DOC_PATH.read_text(encoding="utf-8")

    assert (
        f"`TIER_FULL` is **{full['capture_target']} captures "
        f"— {full['stage1_captures']} then {full['stage2_captures']}**"
    ) in doc
    assert (
        f"`TIER_EXPRESS` is **{express['capture_target']} — the same "
        f"{express['stage1_captures']}, then {express['stage2_captures']}**"
    ) in doc
    # The remote tier's line follows the same derived-numbers rule, so its
    # prose cannot outlive a change to its shape either.
    assert (
        f"It is\n  **{remote['capture_target']} — the same "
        f"{remote['stage1_captures']}, then {remote['stage2_captures']}**"
    ) in doc


def test_doc_event_grep_recipes_name_real_events():
    """Every `event=` name in the doc's live-spine grep recipes is real.

    Extracted from the doc's own text (not a hand-typed 31-name list), so a
    future recipe edit re-checks itself automatically instead of only
    re-checking whatever names this test happened to enumerate today.
    """
    doc = DOC_PATH.read_text(encoding="utf-8")
    recipe_names = _extract_recipe_event_names(doc)
    assert len(recipe_names) >= 20, (
        "recipe extraction found suspiciously few names -- the grep-recipe "
        "regex may no longer match the doc's current formatting"
    )

    found = logged_event_names()
    missing = sorted(recipe_names - found)
    assert not missing, (
        "doc grep recipe names an event the four-path resolver cannot find "
        "logged anywhere under jasper/ (see this module's docstring for "
        "the paths considered):\n" + "\n".join(missing)
    )


def test_realized_level_match_resolves_through_the_emit_indirection():
    """Named regression pin (#2400): the case that broke both prior sweeps.

    `correction.crossover_v2_realized_level_match` is said via
    `intervention.py`'s local `emit()` closure, never `log_event` directly
    -- a resolver limited to direct `log_event` calls false-positives on
    this exact name, which is what happened twice before this file existed.
    """
    assert (
        "correction.crossover_v2_realized_level_match" in logged_event_names()
    )


def test_a_fabricated_event_name_does_not_resolve():
    """No-op control: the resolver is discriminating, not vacuously true.

    Without this, a resolver that (by bug) returned "everything is logged"
    would pass the two guards above just as well as a correct one.
    """
    found = logged_event_names()
    assert "correction.crossover_v2_totally_made_up_event_xyz" not in found
    # A near-miss of a real name (one character off) must not resolve
    # either -- guards against a resolver that's accidentally doing prefix
    # or fuzzy matching instead of exact string resolution.
    assert "correction.crossover_v2_realzed_level_match" not in found


def test_resolver_rejects_names_a_string_presence_scan_would_accept(tmp_path):
    """Discriminates the campaign's most-recurred defect class.

    Every test above exercises a REAL, correctly-emitted event name, so a
    resolver reduced to "does this literal appear anywhere in the corpus"
    would pass all of them too -- that shape is what broke both #2291
    Phase 5c-v rebuilt sweeps, and a third time in a gate harness. A reason
    code, a dict field, and an ordinary function argument can all carry a
    string that LOOKS like an event name without it ever being logged;
    only a name that reaches one of the four call shapes should resolve.
    """
    (tmp_path / "decoy.py").write_text(
        'from jasper.log_event import log_event\n'
        'REASON_CODE = "correction.crossover_v2_looks_like_an_event"\n'
        '_RESULT = {"code": "correction.crossover_v2_also_looks_like_one"}\n'
        'str("correction.crossover_v2_mentioned_but_not_logged")\n'
        'log_event(logger, "correction.crossover_v2_the_real_one")\n',
        encoding="utf-8",
    )
    assert logged_event_names(root=tmp_path) == {
        "correction.crossover_v2_the_real_one"
    }
