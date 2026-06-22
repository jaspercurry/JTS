# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free sanity tests for the DTLN model registry.

Mirrors the shape of tests/test_wake_setup.py's "Registry sanity"
section. Catches the easy mistakes — a typo in the URL, a hash
that's the wrong length, a registry that doesn't include the
DEFAULT_SIZE — at unit-test time rather than at install time on a
real Pi.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jasper.aec_engines import dtln_models


def test_registry_is_nonempty():
    assert len(dtln_models.REGISTRY) > 0


def test_registry_sizes_are_unique():
    sizes = [e.size for e in dtln_models.REGISTRY]
    assert len(sizes) == len(set(sizes)), f"duplicate sizes: {sizes}"


def test_default_size_in_registry():
    """DEFAULT_SIZE must map to an entry — `default()` would crash otherwise."""
    assert dtln_models.by_size(dtln_models.DEFAULT_SIZE) is not None


def test_default_returns_registry_entry():
    entry = dtln_models.default()
    assert entry is dtln_models.by_size(dtln_models.DEFAULT_SIZE)


@pytest.mark.parametrize("entry", dtln_models.REGISTRY)
def test_entry_urls_are_https(entry: dtln_models.DTLNModelEntry):
    """Anonymous download requires https; raw github URLs are always https
    anyway, but catch a hand-edited http:// before it surprises us."""
    assert entry.stage1_url.startswith("https://"), entry.stage1_url
    assert entry.stage2_url.startswith("https://"), entry.stage2_url


@pytest.mark.parametrize("entry", dtln_models.REGISTRY)
def test_entry_hashes_are_64_hex(entry: dtln_models.DTLNModelEntry):
    """SHA-256 hex strings are exactly 64 lowercase hex characters.
    A 40-char string here would be SHA-1 (different algorithm); empty
    would mean someone forgot to fill it in."""
    for sha in (entry.stage1_sha256, entry.stage2_sha256):
        assert len(sha) == 64, f"not a sha256 hex: {sha!r}"
        assert all(c in "0123456789abcdef" for c in sha), f"not lowercase hex: {sha!r}"


@pytest.mark.parametrize("entry", dtln_models.REGISTRY)
def test_entry_files_method_returns_two_files(entry: dtln_models.DTLNModelEntry):
    """files() must return both stages — engine init requires both."""
    files = entry.files()
    assert len(files) == 2
    paths = {p.name for p, _, _ in files}
    assert paths == {entry.stage1_filename, entry.stage2_filename}


@pytest.mark.parametrize("entry", dtln_models.REGISTRY)
def test_entry_files_paths_are_under_models_dir(entry: dtln_models.DTLNModelEntry):
    """Both files land under DTLN_MODELS_DIR by default — the bridge reads
    from JASPER_DTLN_MODEL_DIR which falls through to this constant."""
    base = Path(dtln_models.DTLN_MODELS_DIR)
    for path, _, _ in entry.files():
        assert path.parent == base, f"{path} not under {base}"


@pytest.mark.parametrize("entry", dtln_models.REGISTRY)
def test_entry_filenames_match_engine_glob(entry: dtln_models.DTLNModelEntry):
    """DTLNEngine constructs filenames as `dtln_aec_{size}_{1,2}.onnx`.
    A registry entry whose filenames don't match wouldn't be found by the
    engine even after install.sh successfully downloads them."""
    assert entry.stage1_filename == f"dtln_aec_{entry.size}_1.onnx"
    assert entry.stage2_filename == f"dtln_aec_{entry.size}_2.onnx"


def test_files_method_honors_explicit_base_dir(tmp_path: Path):
    """files(base_dir=...) must let callers (e.g. tests, or a custom
    install path) override the default location."""
    entry = dtln_models.default()
    files = entry.files(base_dir=tmp_path)
    assert all(p.parent == tmp_path for p, _, _ in files)
