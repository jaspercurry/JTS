# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guard: only jasper-voice may read JASPER_VOICE_PROVIDER from the env.

AGENTS.md ("Reading the active provider in code — one reader, never
`os.environ`"): surfaces that display or aggregate the active provider
but are not jasper-voice (jasper-control's /state, the /system/
dashboard, wizards) MUST resolve it through
jasper.voice.provider_state (read_active_provider /
read_active_provider_and_model), which re-read the SSOT file
(/var/lib/jasper/voice_provider.env) fresh on every call. Long-lived
daemons load their env once at start and are NOT restarted on a
provider switch, so an os.environ read goes stale — that was the
"/system/ still shows the old provider after switching" bug.

The one legitimate env read is `Config.from_env` in jasper/config.py:
jasper-voice itself IS restarted on every switch, so its os.environ is
always fresh. (jasper/voice/provider_state.py reads the key out of the
*parsed SSOT file* mapping, not os.environ — not an env read, so the
patterns below deliberately don't match plain `env.get(...)`.)

Scope: direct env reads (`os.environ.get/[...]`, `os.getenv`, the
`_env*` config helpers) of the exact key. JASPER_VOICE_PROVIDER_FILE /
_IDS_FILE are different keys (path overrides) and stay out of scope.

The allowlist is two-sided: a file losing its allowed read (or growing
a second one) fails, so the list can't go stale silently.
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "jasper"


def code_only(source: str) -> str:
    """Return `source` with comments and standalone string expressions
    (docstrings and bare prose strings) blanked out, so a guard regex
    only sees executable code. A docstring *describing* the forbidden
    pattern (provider_state.py's does, deliberately) must not trip the
    guard that bans the pattern."""
    lines = source.splitlines()
    dead: set[int] = set()  # 0-based line indexes to blank
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            dead.update(range(node.lineno - 1, node.end_lineno))
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            lines[tok.start[0] - 1] = lines[tok.start[0] - 1][: tok.start[1]]
    return "\n".join("" if i in dead else line for i, line in enumerate(lines))

# Direct env reads of the provider key. `(?!_)` keeps the related path
# keys (JASPER_VOICE_PROVIDER_FILE, JASPER_VOICE_PROVIDER_IDS_FILE)
# out of scope.
_ENV_READ = re.compile(
    r"(?:os\.environ\.get\(|os\.getenv\(|os\.environ\[|_env[a-z_]*\()"
    r"\s*['\"]JASPER_VOICE_PROVIDER(?!_)['\"]"
)

# repo-relative file -> exact number of permitted env reads.
# jasper/config.py: Config.from_env, the jasper-voice daemon's single
# parse point (fresh on every switch because the daemon restarts).
_ALLOWED = {
    "jasper/config.py": 1,
}


def _read_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for py in sorted(PKG.rglob("*.py")):
        n = len(_ENV_READ.findall(code_only(py.read_text(encoding="utf-8"))))
        if n:
            counts[str(py.relative_to(ROOT))] = n
    return counts


def test_only_config_reads_provider_from_environ():
    counts = _read_counts()
    violations = {f: n for f, n in counts.items() if f not in _ALLOWED}
    assert not violations, (
        f"direct os.environ read(s) of JASPER_VOICE_PROVIDER outside the "
        f"allowlist: {violations}. Long-lived daemons are not restarted on "
        "a provider switch, so os.environ goes stale — resolve the active "
        "provider through jasper.voice.provider_state.read_active_provider() "
        "instead (see AGENTS.md 'one reader, never os.environ')."
    )


def test_allowlist_is_not_stale():
    counts = _read_counts()
    for f, expected in _ALLOWED.items():
        actual = counts.get(f, 0)
        assert actual == expected, (
            f"{f} has {actual} direct JASPER_VOICE_PROVIDER env read(s), "
            f"allowlist says {expected}. If the read moved or multiplied, "
            "update _ALLOWED in this test deliberately — extra reads in "
            "config.py are still one-per-daemon-start and must be reasoned "
            "about, and a removed read means the allowlist entry is dead."
        )
