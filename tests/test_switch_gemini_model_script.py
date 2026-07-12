# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contracts for the installed-catalog Gemini model switcher."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
from typing import cast

import pytest

from jasper.voice import catalog
from jasper.voice.catalog import ModelStatus, PROVIDERS, ProviderCatalogEntry


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "switch-gemini-model.sh"


def _resolver_source() -> str:
    source = SCRIPT.read_text(encoding="utf-8")
    return source.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]


def _switch_remote_source() -> str:
    source = SCRIPT.read_text(encoding="utf-8")
    return source.split("<<'REMOTE'\n", 1)[1].split("\nREMOTE\n", 1)[0]


def _execute_resolver(
    monkeypatch: pytest.MonkeyPatch,
    providers: tuple[ProviderCatalogEntry, ...],
    *,
    alias: str = "3.1",
) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    monkeypatch.setattr(catalog, "PROVIDERS", providers)
    monkeypatch.setattr(sys, "argv", ["catalog-resolver", alias])
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exec(compile(_resolver_source(), "<catalog-resolver>", "exec"), {})
    except SystemExit as exc:
        status = exc.code if isinstance(exc.code, int) else 1
        return status, stdout.getvalue(), stderr.getvalue()
    return 0, stdout.getvalue(), stderr.getvalue()


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def script_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPT, scripts / SCRIPT.name)
    shutil.copy2(ROOT / "scripts" / "_lib.sh", scripts / "_lib.sh")
    (repo / ".env.local").write_text(
        "PI_HOST=checkout.invalid\nPI_USER=checkout-user\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "ssh.log"
    _write_executable(
        fake_bin / "ssh",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf 'ssh' >> "$FAKE_SSH_LOG"
            for arg in "$@"; do
                flat="${arg//$'\n'/\\n}"
                printf '\t%s' "$flat" >> "$FAKE_SSH_LOG"
            done
            printf '\n' >> "$FAKE_SSH_LOG"

            remote="${*: -1}"
            if [[ "$remote" == *"/opt/jasper/.venv/bin/python - 3.1"* ||
                  "$remote" == *"/opt/jasper/.venv/bin/python - 2.5"* ]]; then
                case "${FAKE_CATALOG_MODE:-ok}" in
                    ok)
                        case "$remote" in
                            *" 3.1") printf 'catalog-3.1.test\n' ;;
                            *" 2.5") printf 'catalog-2.5.test\n' ;;
                            *) exit 29 ;;
                        esac
                        ;;
                    malformed-output)
                        printf 'first-model\nsecond-model\n'
                        ;;
                    unsafe-output)
                        printf 'unsafe model id\n'
                        ;;
                    *)
                        printf 'catalog error: synthetic %s\n' "$FAKE_CATALOG_MODE" >&2
                        exit 23
                        ;;
                esac
            fi
            """
        ),
    )
    foreign_cwd = tmp_path / "foreign-cwd"
    foreign_cwd.mkdir()
    return repo, fake_bin, log


def _run(
    script_repo: tuple[Path, Path, Path],
    args: list[str],
    *,
    mode: str = "ok",
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    repo, fake_bin, log = script_repo
    log.unlink(missing_ok=True)
    env = os.environ.copy()
    for key in ("PI_HOST", "PI_USER", "JASPER_HOSTNAME"):
        env.pop(key, None)
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "FAKE_SSH_LOG": str(log),
            "FAKE_CATALOG_MODE": mode,
        }
    )
    result = subprocess.run(
        ["bash", str(repo / "scripts" / SCRIPT.name), *args],
        cwd=repo.parent / "foreign-cwd",
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls


def test_script_reads_installed_catalog_without_hardcoded_model_ids() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "from jasper.voice.catalog import ModelStatus, PROVIDERS" in source
    assert "gemini-3.1-flash-live-preview" not in source
    assert "gemini-2.5-flash-native-audio-preview-12-2025" not in source
    assert not any(
        line.lstrip().startswith('MODEL="gemini-') for line in source.splitlines()
    )


def test_catalog_resolver_pins_alias_uniqueness_and_lifecycle_contract() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    for contract in (
        "len(gemini_entries) != 1",
        "type(model.default) is not bool",
        "not isinstance(model.status, ModelStatus)",
        "len(matches) != 1",
        "ModelStatus.TESTED, True",
        "ModelStatus.FALLBACK, False",
        "len(defaults) != 1",
    ):
        assert contract in source


@pytest.mark.parametrize("alias", ("3.1", "2.5"))
def test_embedded_resolver_executes_against_current_catalog(alias: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _resolver_source(), alias],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    model_id = result.stdout.strip()
    gemini = next(provider for provider in PROVIDERS if provider.id == "gemini")
    selected = next(model for model in gemini.models if model.id == model_id)
    assert alias in selected.id or alias in selected.label
    assert selected.status is (
        ModelStatus.TESTED if alias == "3.1" else ModelStatus.FALLBACK
    )
    assert selected.default is (alias == "3.1")


def test_embedded_resolver_rejects_catalog_contract_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gemini = next(provider for provider in PROVIDERS if provider.id == "gemini")
    default = next(model for model in gemini.models if model.default)
    fallback = next(
        model for model in gemini.models if model.status is ModelStatus.FALLBACK
    )
    other_providers = tuple(
        provider for provider in PROVIDERS if provider.id != "gemini"
    )
    duplicate_alias = replace(
        default,
        id="synthetic-3.1-duplicate",
        label="Synthetic 3.1 duplicate",
        status=ModelStatus.EXPERIMENTAL,
        default=False,
    )
    cases = {
        "missing Gemini": other_providers,
        "duplicate Gemini": (*PROVIDERS, gemini),
        "missing alias": (
            replace(
                gemini,
                models=(
                    default,
                    replace(fallback, id="synthetic-fallback", label="Fallback"),
                ),
            ),
            *other_providers,
        ),
        "duplicate alias": (
            replace(gemini, models=(*gemini.models, duplicate_alias)),
            *other_providers,
        ),
        "malformed status": (
            replace(
                gemini,
                models=(
                    replace(default, status=cast(ModelStatus, "tested")),
                    fallback,
                ),
            ),
            *other_providers,
        ),
        "malformed default": (
            replace(
                gemini,
                models=(replace(default, default=cast(bool, 1)), fallback),
            ),
            *other_providers,
        ),
        "wrong default": (
            replace(
                gemini,
                models=(
                    replace(default, default=False),
                    replace(fallback, default=True),
                ),
            ),
            *other_providers,
        ),
        "wrong model env": (
            replace(gemini, model_env="JASPER_WRONG_MODEL"),
            *other_providers,
        ),
        "unsafe model id": (
            replace(
                gemini,
                models=(replace(default, id="synthetic-3.1\tunsafe"), fallback),
            ),
            *other_providers,
        ),
    }

    for label, providers in cases.items():
        with monkeypatch.context() as isolated:
            status, stdout, stderr = _execute_resolver(isolated, providers)
        assert status == 2, label
        assert stdout == "", label
        assert "catalog error:" in stderr, label


@pytest.mark.parametrize(
    ("alias", "selected", "not_selected"),
    (
        ("3.1", "catalog-3.1.test", "catalog-2.5.test"),
        ("3", "catalog-3.1.test", "catalog-2.5.test"),
        ("2.5", "catalog-2.5.test", "catalog-3.1.test"),
        ("2", "catalog-2.5.test", "catalog-3.1.test"),
    ),
)
def test_alias_resolves_from_catalog_before_env_mutation(
    script_repo: tuple[Path, Path, Path],
    alias: str,
    selected: str,
    not_selected: str,
) -> None:
    result, calls = _run(script_repo, [alias])

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 2
    assert "checkout-user@checkout.invalid" in calls[0]
    canonical_alias = alias if "." in alias else {"3": "3.1", "2": "2.5"}[alias]
    assert f"/opt/jasper/.venv/bin/python - {canonical_alias}" in calls[0]
    assert "sudo sh -s --" in calls[1]
    assert "/var/lib/jasper/voice_provider.env" in calls[1]
    assert "sed -i" not in calls[1]
    assert selected in calls[1]
    assert not_selected not in calls[1]


def test_show_current_preserves_single_read_only_query(
    script_repo: tuple[Path, Path, Path],
) -> None:
    result, calls = _run(script_repo, [], mode="unreadable")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Current model on checkout.invalid:" in result.stdout
    assert len(calls) == 1
    assert "^JASPER_GEMINI_MODEL=" in calls[0]
    assert "/etc/jasper/jasper.env" in calls[0]
    assert "/var/lib/jasper/voice_provider.env" in calls[0]
    assert "tail -1" in calls[0]
    assert "jasper.voice.catalog" not in calls[0]
    assert "sed -i" not in calls[0]
    assert "systemctl restart" not in calls[0]


@pytest.mark.parametrize(
    ("runtime_model", "ownership_fails", "filter_fails", "expected_status"),
    (
        ("catalog-3.1.test", False, False, 0),
        ("runtime-stale.test", False, False, 1),
        ("catalog-3.1.test", True, False, 7),
        ("catalog-3.1.test", False, True, 9),
    ),
)
def test_remote_switch_overrides_later_file_and_checks_restarted_environment(
    tmp_path: Path,
    runtime_model: str,
    ownership_fails: bool,
    filter_fails: bool,
    expected_status: int,
) -> None:
    selected = "catalog-3.1.test"
    tmp_path.chmod(0o770)
    operator_env = tmp_path / "jasper.env"
    provider_env = tmp_path / "voice_provider.env"
    operator_env.write_text(
        "JASPER_GEMINI_MODEL=operator-old\n",
        encoding="utf-8",
    )
    original_provider = (
        "JASPER_VOICE_PROVIDER=gemini\n"
        "JASPER_GEMINI_MODEL=wizard-old\n"
        "JASPER_GEMINI_VOICE=Aoede\n"
    )
    provider_env.write_text(
        original_provider,
        encoding="utf-8",
    )

    fake_bin = tmp_path / "remote-bin"
    fake_bin.mkdir()
    systemctl_log = tmp_path / "systemctl.log"
    _write_executable(
        fake_bin / "systemctl",
        textwrap.dedent(
            """\
            #!/bin/sh
            printf '%s\n' "$*" >> "$FAKE_SYSTEMCTL_LOG"
            case "$1" in
                restart) exit 0 ;;
                is-active) printf 'active\n' ;;
                show) printf '4242\n' ;;
                *) exit 29 ;;
            esac
            """
        ),
    )
    chown_status = 7 if ownership_fails else 0
    _write_executable(fake_bin / "chown", f"#!/bin/sh\nexit {chown_status}\n")
    if filter_fails:
        _write_executable(fake_bin / "awk", "#!/bin/sh\nexit 9\n")
    for command in ("sleep", "journalctl"):
        _write_executable(fake_bin / command, "#!/bin/sh\nexit 0\n")

    proc_root = tmp_path / "proc"
    process_dir = proc_root / "4242"
    process_dir.mkdir(parents=True)
    (process_dir / "environ").write_bytes(
        b"PATH=/usr/bin\0JASPER_GEMINI_MODEL=" + runtime_model.encode() + b"\0"
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "FAKE_SYSTEMCTL_LOG": str(systemctl_log),
        }
    )

    result = subprocess.run(
        [
            "sh",
            "-s",
            "--",
            selected,
            str(operator_env),
            str(provider_env),
            str(proc_root),
        ],
        input=_switch_remote_source(),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == expected_status, result.stdout + result.stderr
    assert tmp_path.stat().st_mode & 0o777 == 0o770
    provider_lines = provider_env.read_text(encoding="utf-8").splitlines()
    if ownership_fails or filter_fails:
        assert provider_env.read_text(encoding="utf-8") == original_provider
        assert provider_lines == [
            "JASPER_VOICE_PROVIDER=gemini",
            "JASPER_GEMINI_MODEL=wizard-old",
            "JASPER_GEMINI_VOICE=Aoede",
        ]
        assert not systemctl_log.exists()
        return
    assert provider_lines == [
        "JASPER_VOICE_PROVIDER=gemini",
        "JASPER_GEMINI_VOICE=Aoede",
        f"JASPER_GEMINI_MODEL={selected}",
    ]
    effective_lines = [
        line
        for path in (operator_env, provider_env)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("JASPER_GEMINI_MODEL=")
    ]
    assert effective_lines[-1] == f"JASPER_GEMINI_MODEL={selected}"
    assert provider_env.stat().st_mode & 0o777 == 0o640
    assert systemctl_log.read_text(encoding="utf-8").splitlines() == [
        "restart jasper-voice",
        "is-active jasper-voice",
        "show jasper-voice --property=MainPID --value",
    ]
    if expected_status == 0:
        assert f"JASPER_GEMINI_MODEL={selected}" in result.stdout
        assert "does not have" not in result.stderr
    else:
        assert f"does not have JASPER_GEMINI_MODEL={selected}" in result.stderr


def test_unknown_alias_exits_without_network(
    script_repo: tuple[Path, Path, Path],
) -> None:
    result, calls = _run(script_repo, ["unknown"], mode="unreadable")

    assert result.returncode == 2
    assert "unknown model alias" in result.stderr
    assert calls == []


@pytest.mark.parametrize(
    "mode",
    (
        "unreadable",
        "gemini-missing",
        "gemini-duplicate",
        "alias-missing",
        "alias-duplicate",
        "malformed-status",
        "malformed-default",
        "wrong-default",
        "malformed-output",
        "unsafe-output",
    ),
)
def test_catalog_failure_is_read_only_and_fail_closed(
    script_repo: tuple[Path, Path, Path],
    mode: str,
) -> None:
    result, calls = _run(script_repo, ["3.1"], mode=mode)

    assert result.returncode == 3
    assert "catalog" in result.stderr
    assert len(calls) == 1
    assert "/opt/jasper/.venv/bin/python - 3.1" in calls[0]
    assert "sed -i" not in calls[0]
    assert "JASPER_GEMINI_MODEL=.*" not in calls[0]
    assert "systemctl restart" not in calls[0]


def test_script_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
