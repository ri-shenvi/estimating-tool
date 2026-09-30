"""Envelope encryption for stored secrets and HMAC helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _key(secret_key: str) -> bytes:
    return hashlib.sha256(secret_key.encode("utf-8")).digest()


def encrypt(plaintext: str, secret_key: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(_key(secret_key)).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt(token: str, secret_key: str) -> str:
    raw = base64.urlsafe_b64decode(token.encode("ascii"))
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(_key(secret_key)).decrypt(nonce, ct, None).decode("utf-8")


def sign(payload: bytes, secret_key: str) -> str:
    return (
        base64.urlsafe_b64encode(hmac.new(_key(secret_key), payload, hashlib.sha256).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


def verify(payload: bytes, signature: str, secret_key: str) -> bool:
    return hmac.compare_digest(sign(payload, secret_key), signature)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
