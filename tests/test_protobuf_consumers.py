# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Import-time compatibility smoke for the shared protobuf runtime."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_all_protobuf_consumers_import_in_one_interpreter() -> None:
    """Catch namespace/runtime conflicts that isolated mocks cannot see."""
    script = "\n".join(
        (
            "import google.protobuf",
            "import onnxruntime",
            "import google.api_core",
            "import google.api.annotations_pb2",
            "import proto",
            "from google import genai",
            "from google.transit import gtfs_realtime_pb2",
        )
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


def test_production_dtln_models_load_with_shared_protobuf_runtime() -> None:
    """Load and execute the real two-stage DTLN bundle when provided.

    The 15 MB release assets intentionally stay outside git. Point the test
    at a downloaded bundle on a laptop or at /var/lib/jasper/dtln on a Pi:
    ``JASPER_DTLN_TEST_MODEL_DIR=/path pytest tests/test_protobuf_consumers.py``.
    """
    model_dir_value = os.environ.get("JASPER_DTLN_TEST_MODEL_DIR")
    if not model_dir_value:
        pytest.skip("set JASPER_DTLN_TEST_MODEL_DIR to the production model bundle")

    from jasper.aec_engines.dtln import BLOCK_SHIFT, DTLNEngine

    engine = DTLNEngine(Path(model_dir_value))
    try:
        silence = bytes(BLOCK_SHIFT * 2)
        assert len(engine.process(silence, silence)) == len(silence)
    finally:
        engine.close()
