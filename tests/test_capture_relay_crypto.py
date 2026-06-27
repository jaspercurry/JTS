# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Pi-side E2E decrypt + integrity (phone-mic relay step 5).

Two layers:

  - Python-only correctness (always runs): self round-trip through pyca
    ``cryptography`` in the page's wire format, plus the loud-failure paths
    (wrong key, tampered ciphertext, length/SHA-256 mismatch).
  - Cross-language proof (skips if node or the capture page is absent on this
    branch; runs on ``main`` once the page + this module are both present): the
    page's WebCrypto encryptor (capture-page/js/crypto.js) encrypts a payload and
    this module decrypts it **bit-identically** — directly the plan §15 "the
    decrypted WAV is bit-identical (verify via the integrity hash)" criterion.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from jasper.capture_relay import crypto

_REPO = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")
_EMITTER = _REPO / "tests" / "js" / "capture_crypto_emit.mjs"
_PAGE_CRYPTO = _REPO / "capture-page" / "js" / "crypto.js"


def _page_format_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """Encrypt exactly as the page does: 12-byte IV prepended to the GCM output."""
    iv = os.urandom(crypto.IV_BYTES)
    ciphertext = AESGCM(key).encrypt(iv, plaintext, None)
    return iv + ciphertext


# --- Python-only correctness --------------------------------------------------


def test_self_round_trip_in_page_format():
    key = crypto.generate_content_key()
    plaintext = bytes(range(256)) * 4
    blob = _page_format_encrypt(key, plaintext)
    expected_sha = __import__("hashlib").sha256(plaintext).hexdigest()
    recovered = crypto.decrypt_and_verify(key, blob, len(plaintext), expected_sha)
    assert recovered == plaintext


def test_generate_content_key_is_32_random_bytes():
    a = crypto.generate_content_key()
    b = crypto.generate_content_key()
    assert len(a) == 32 and len(b) == 32
    assert a != b  # CSPRNG


def test_content_key_b64url_round_trip():
    key = crypto.generate_content_key()
    encoded = crypto.content_key_to_b64url(key)
    assert "=" not in encoded  # unpadded for the URL fragment
    assert crypto.content_key_from_b64url(encoded) == key


def test_wrong_key_fails_loud():
    key = crypto.generate_content_key()
    blob = _page_format_encrypt(key, b"hello world")
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt_blob(crypto.generate_content_key(), blob)


def test_tampered_ciphertext_fails_loud():
    key = crypto.generate_content_key()
    blob = bytearray(_page_format_encrypt(key, b"hello world"))
    blob[-1] ^= 0x01  # flip a tag byte
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt_blob(key, bytes(blob))


def test_short_blob_fails_loud():
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt_blob(crypto.generate_content_key(), b"tooshort")


def test_integrity_length_mismatch_fails_loud():
    key = crypto.generate_content_key()
    plaintext = b"abcdef"
    blob = _page_format_encrypt(key, plaintext)
    sha = __import__("hashlib").sha256(plaintext).hexdigest()
    with pytest.raises(crypto.IntegrityError, match="length"):
        crypto.decrypt_and_verify(key, blob, len(plaintext) + 1, sha)


def test_integrity_sha_mismatch_fails_loud():
    key = crypto.generate_content_key()
    plaintext = b"abcdef"
    blob = _page_format_encrypt(key, plaintext)
    with pytest.raises(crypto.IntegrityError, match="SHA-256"):
        crypto.decrypt_and_verify(key, blob, len(plaintext), "0" * 64)


# --- Cross-language proof (page WebCrypto encrypt -> Pi decrypt) ---------------


def test_page_encrypt_pi_decrypt_bit_identical():
    if _NODE is None:
        pytest.skip("node not on PATH")
    if not _PAGE_CRYPTO.exists() or not _EMITTER.exists():
        pytest.skip("capture page not present on this branch (lands with step 3)")
    proc = subprocess.run(
        [_NODE, str(_EMITTER)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout.strip().splitlines()[-1])

    key = crypto.content_key_from_b64url(data["key_b64url"])
    blob = base64.b64decode(data["blob_b64"])
    expected = base64.b64decode(data["plaintext_b64"])

    recovered = crypto.decrypt_and_verify(
        key, blob, data["plaintext_len"], data["sha256"]
    )
    # Bit-identical — the plan §15 acceptance criterion.
    assert recovered == expected
