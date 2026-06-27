# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pi-side end-to-end decryption + integrity for the capture relay (step 5).

The phone encrypts the recorded WAV under a `content_key` the **Pi** minted and
delivered only in the page's URL fragment — so the relay stores ciphertext only
and never sees the key (plan §8). This module is the Pi's matching half: it mints
the key, decrypts the relay-pulled blob, and verifies the **plaintext** integrity
the phone attached, **before** the WAV reaches analysis. An intact-but-corrupt or
truncated capture fails loud here (plan §9, §15), never a silently-wrong number.

Wire format — the contract with the page (capture-page/js/crypto.js):

  key   : 32 raw bytes, base64url in the fragment
  blob  : IV(12 bytes) ‖ AES-256-GCM(ciphertext ‖ 16-byte tag)
  hash  : lowercase hex SHA-256 over the PLAINTEXT WAV bytes
  length: plaintext WAV byte count

AES-GCM already authenticates the ciphertext (a tampered blob fails to decrypt);
the explicit plaintext length + SHA-256 is the second, plan-mandated check that
also catches a valid-but-wrong-length payload and is the "decrypted WAV is
bit-identical" proof (pinned cross-language by tests/test_capture_relay_crypto.py
against the page's WebCrypto encryptor).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CONTENT_KEY_BYTES = 32  # AES-256
IV_BYTES = 12
GCM_TAG_BYTES = 16


class DecryptError(ValueError):
    """The relay blob could not be decrypted (wrong key or tampered ciphertext)."""


class IntegrityError(ValueError):
    """Decryption succeeded but the plaintext failed its length/SHA-256 check."""


# --- key lifecycle ------------------------------------------------------------


def generate_content_key() -> bytes:
    """Mint a fresh AES-256-GCM content key with a CSPRNG (plan §11)."""
    return secrets.token_bytes(CONTENT_KEY_BYTES)


def content_key_to_b64url(key: bytes) -> str:
    """Encode the key for the URL fragment (unpadded base64url)."""
    if len(key) != CONTENT_KEY_BYTES:
        raise ValueError(f"content_key must be {CONTENT_KEY_BYTES} bytes")
    return base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii")


def content_key_from_b64url(value: str) -> bytes:
    """Decode a base64url content key (padding-tolerant)."""
    padded = value + "=" * (-len(value) % 4)
    raw = base64.urlsafe_b64decode(padded)
    if len(raw) != CONTENT_KEY_BYTES:
        raise ValueError(f"content_key must decode to {CONTENT_KEY_BYTES} bytes")
    return raw


# --- decrypt + verify ---------------------------------------------------------


def decrypt_blob(content_key: bytes, blob: bytes) -> bytes:
    """Decrypt an ``IV ‖ ciphertext`` relay blob. Raises DecryptError on failure."""
    if len(content_key) != CONTENT_KEY_BYTES:
        raise DecryptError(f"content_key must be {CONTENT_KEY_BYTES} bytes")
    if len(blob) < IV_BYTES + GCM_TAG_BYTES:
        raise DecryptError("blob too short to contain IV + GCM tag")
    iv = blob[:IV_BYTES]
    ciphertext = blob[IV_BYTES:]
    try:
        return AESGCM(content_key).decrypt(iv, ciphertext, None)
    except InvalidTag as exc:
        raise DecryptError(
            "AES-GCM authentication failed (wrong key or tampered ciphertext)"
        ) from exc


def verify_integrity(
    plaintext: bytes, expected_len: int, expected_sha256_hex: str
) -> None:
    """Verify the plaintext length + SHA-256 the phone attached. Loud on mismatch."""
    if len(plaintext) != expected_len:
        raise IntegrityError(
            f"plaintext length {len(plaintext)} != expected {expected_len}"
        )
    actual = hashlib.sha256(plaintext).hexdigest()
    if not hmac.compare_digest(actual, (expected_sha256_hex or "").lower()):
        raise IntegrityError("plaintext SHA-256 mismatch")


def decrypt_and_verify(
    content_key: bytes,
    blob: bytes,
    expected_len: int,
    expected_sha256_hex: str,
) -> bytes:
    """Decrypt then verify integrity, returning the WAV bytes. The single call
    the Pi pull path uses before handing the WAV to analysis."""
    plaintext = decrypt_blob(content_key, blob)
    verify_integrity(plaintext, expected_len, expected_sha256_hex)
    return plaintext
