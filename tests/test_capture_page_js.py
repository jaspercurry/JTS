# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Execute the capture-page modules and verify the published bundle."""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from jasper.audio_measurement.program import COURTESY_TONE_BEEP_COUNT
from jasper.capture_relay.session import RETAKE_TOO_LATE_MESSAGE


_JS_DIR = Path(__file__).resolve().parent / "js"
_NODE = shutil.which("node")
_REPO = Path(__file__).resolve().parents[1]

_CAPTURE_PAGE_JS_DIGEST = (
    "ba79c733416fcae806a2964dd3749f185b34824c343bbf262fb30feb966c5a0b"
)
_CAPTURE_PAGE_JS_DIGEST_BUILD = "20260830.1"

_HARNESSES = [
    "capture_render_test.mjs",
    "capture_crypto_test.mjs",
    "capture_relay_client_test.mjs",
    "capture_fragment_test.mjs",
    "capture_constraints_test.mjs",
    "capture_wakelock_test.mjs",
    "capture_return_url_test.mjs",
    "capture_level_events_test.mjs",
    "capture_setup_store_test.mjs",
    "capture_calibration_model_test.mjs",
    "capture_protocol_test.mjs",
    "capture_transport_integrity_test.mjs",
    "capture_host_stop_lifecycle_test.mjs",
    "capture_ambient_stats_test.mjs",
    "capture_defect_fixes_test.mjs",
    "capture_time_budget_test.mjs",
    "capture_integrity_test.mjs",
    "capture_boot_contract_test.mjs",
]


class _CaptureIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.csp = ""

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.add(str(attributes["id"]))
        if tag == "meta" and attributes.get("http-equiv") == "Content-Security-Policy":
            self.csp = str(attributes.get("content") or "")


def _assert_capture_index_contract(index: str) -> None:
    parser = _CaptureIndexParser()
    parser.feed(index)
    assert {
        "screen",
        "status",
        "wakelock-hint",
        "stop-confirm",
        "stop-confirm-cancel",
        "stop-confirm-accept",
    } <= parser.ids
    directives = {
        parts[0]: set(parts[1:])
        for raw in parser.csp.split(";")
        if (parts := raw.split())
    }
    assert {"'self'", "https://relay.jasper.tech"} <= directives["connect-src"]
    assert {"'self'", "blob:"} <= directives["script-src"]


def _run_capture_page_harness(
    harness: str,
    *,
    extra_env: dict[str, str] | None = None,
) -> dict[str, object]:
    if _NODE is None:
        if os.environ.get("CI"):
            pytest.fail("node is not on PATH in CI")
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [_NODE, str(_JS_DIR / harness)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **(extra_env or {})},
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"] is True, result
    assert result["passed"] >= 1, result
    return result


@pytest.mark.parametrize("harness", _HARNESSES)
def test_capture_page_harness(harness: str) -> None:
    _run_capture_page_harness(harness)


def test_capture_page_upload_never_declares_a_sign_convention() -> None:
    _run_capture_page_harness("capture_calibration_confirm_test.mjs")


def test_capture_page_beep_copy_matches_the_composed_beep_count() -> None:
    _run_capture_page_harness(
        "capture_stop_and_ambient_countdown_test.mjs",
        extra_env={
            "JTS_EXPECTED_COURTESY_BEEP_COUNT": str(COURTESY_TONE_BEEP_COUNT),
        },
    )


def test_a_late_retake_reads_the_same_whichever_side_answers_it() -> None:
    _run_capture_page_harness(
        "capture_plan_loop_test.mjs",
        extra_env={"JTS_EXPECTED_RETAKE_TOO_LATE_MESSAGE": RETAKE_TOO_LATE_MESSAGE},
    )


def test_shared_runner_terminates_promptly_when_a_failed_test_leaks_a_handle() -> None:
    if _NODE is None:
        pytest.skip("node not on PATH")
    helper_uri = (_JS_DIR / "run_test_functions.mjs").resolve().as_uri()
    script = f"""
import {{ runTestFunctions }} from {json.dumps(helper_uri)};
await runTestFunctions(
  [function leaksHandle() {{
    setInterval(() => {{}}, 10_000);
    throw new Error("expected failure");
  }}],
  () => 0,
);
"""
    proc = subprocess.run(
        [_NODE, "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        timeout=2,
    )
    assert proc.returncode == 1
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"] is False
    assert result["test"] == "leaksHandle"


def _build_capture_page_bundle(
    tmp_path: Path,
    *,
    include_shared_helper: bool = True,
) -> tuple[Path, subprocess.CompletedProcess[str], Path]:
    fake_repo = tmp_path / "repo"
    shutil.copytree(
        _REPO / "capture-page",
        fake_repo / "capture-page",
        ignore=shutil.ignore_patterns("dist"),
    )
    shared = fake_repo / "deploy/assets/shared/js/measurement-audio.js"
    if include_shared_helper:
        shared.parent.mkdir(parents=True)
        shutil.copy2(
            _REPO / "deploy/assets/shared/js/measurement-audio.js",
            shared,
        )
    proc = subprocess.run(
        ["bash", str(fake_repo / "capture-page/build.sh")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return fake_repo / "capture-page/dist", proc, shared


def _published_javascript() -> dict[str, Path]:
    files = {path.name: path for path in (_REPO / "capture-page/js").glob("*.js")}
    shared = _REPO / "deploy/assets/shared/js/measurement-audio.js"
    assert shared.name not in files
    files[shared.name] = shared
    return files


def _published_javascript_digest() -> str:
    digest = hashlib.sha256()
    for name, path in sorted(_published_javascript().items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def test_build_emits_the_complete_browser_bundle(tmp_path: Path) -> None:
    dist, proc, shared = _build_capture_page_bundle(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (dist / "index.html").read_bytes() == (
        _REPO / "capture-page/index.html"
    ).read_bytes()
    assert json.loads((dist / "version.json").read_text(encoding="utf-8")) == (
        json.loads((_REPO / "capture-page/version.json").read_text(encoding="utf-8"))
    )
    expected_js = _published_javascript()
    assert shared.name in expected_js
    assert {path.name for path in (dist / "js").iterdir()} == set(expected_js)
    for name, source in expected_js.items():
        assert (dist / "js" / name).read_bytes() == source.read_bytes()


def test_built_page_contains_the_dom_and_relay_policy_used_by_main(
    tmp_path: Path,
) -> None:
    dist, proc, _shared = _build_capture_page_bundle(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    _assert_capture_index_contract((dist / "index.html").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "source",
    [
        'import { safeReturnUrl } from "./missing-return-url.js";',
        'import { resolveTheme } from "./theme.js";',
        'import { safeReturnUrl } from "./config.js";',
        'import { missingReturnUrl } from "./return-url.js";',
    ],
)
def test_capture_module_loader_rejects_wrong_dependency_ownership(
    tmp_path: Path,
    source: str,
) -> None:
    if _NODE is None:
        pytest.skip("node not on PATH")
    main = tmp_path / "synthetic-main.mjs"
    main.write_text(source, encoding="utf-8")
    loader_uri = (_JS_DIR / "_capture_page_module.mjs").resolve().as_uri()
    script = f"""
import {{ loadCapturePage }} from {json.dumps(loader_uri)};
await loadCapturePage({{ mainUrl: {json.dumps(main.as_uri())} }});
"""
    proc = subprocess.run(
        [_NODE, "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode != 0


def test_build_refuses_to_publish_without_the_shared_recorder(tmp_path: Path) -> None:
    dist, proc, _shared = _build_capture_page_bundle(
        tmp_path,
        include_shared_helper=False,
    )
    assert proc.returncode != 0
    assert not dist.exists()


def test_capture_page_build_stamp_matches_the_main_module_cache_key() -> None:
    version = json.loads(
        (_REPO / "capture-page/version.json").read_text(encoding="utf-8")
    )
    index = (_REPO / "capture-page/index.html").read_text(encoding="utf-8")
    match = re.search(r'<script type="module" src="\./js/main\.js\?v=([^"]+)"', index)
    assert match
    assert match.group(1) == version["capture_page_build"].replace(".", "-")


def test_published_version_identifies_the_live_capture_protocol() -> None:
    version = json.loads(
        (_REPO / "capture-page/version.json").read_text(encoding="utf-8")
    )
    assert {
        "schema_version": version["schema_version"],
        "capture_protocol_version": version["capture_protocol_version"],
        "supported_capture_protocol_versions": version[
            "supported_capture_protocol_versions"
        ],
    } == {
        "schema_version": 1,
        "capture_protocol_version": 3,
        "supported_capture_protocol_versions": [3],
    }


def test_published_javascript_change_requires_a_build_stamp_decision() -> None:
    version = json.loads(
        (_REPO / "capture-page/version.json").read_text(encoding="utf-8")
    )
    assert version["capture_page_build"] == _CAPTURE_PAGE_JS_DIGEST_BUILD
    assert _published_javascript_digest() == _CAPTURE_PAGE_JS_DIGEST


def test_shared_recorder_participates_in_the_published_digest() -> None:
    published = _published_javascript()
    assert published["measurement-audio.js"] == (
        _REPO / "deploy/assets/shared/js/measurement-audio.js"
    )


def test_published_modules_have_one_cache_key_each() -> None:
    pattern = re.compile(r'from\s+["\']\./([\w-]+\.js)\?v=([\w.\-]+)["\']')
    inventory: dict[str, dict[str, list[str]]] = {}
    for path in sorted((_REPO / "capture-page/js").glob("*.js")):
        for module, stamp in pattern.findall(path.read_text(encoding="utf-8")):
            inventory.setdefault(module, {}).setdefault(stamp, []).append(path.name)
    assert inventory
    split = {module: stamps for module, stamps in inventory.items() if len(stamps) > 1}
    assert not split, split
