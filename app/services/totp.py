"""TOTP two-factor authentication helpers (stdlib only, RFC 4226 / RFC 6238).

No third-party dependencies: uses :mod:`hmac`, :mod:`hashlib`,
:mod:`base64`, and :mod:`secrets` from the standard library.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote
from typing import Callable

_DIGESTS: dict[str, Callable] = {
    "sha1": hashlib.sha1,
    "sha256": hashlib.sha256,
    "sha512": hashlib.sha512,
}


def generate_secret(num_bytes: int = 20) -> str:
    """Generate a random base32-encoded TOTP secret (no padding).

    Args:
        num_bytes: Random entropy in bytes. 20 bytes (160 bits) is the
            RFC 4226 recommended default.
    """
    if num_bytes < 10:
        raise ValueError("num_bytes must be >= 10")
    raw = secrets.token_bytes(num_bytes)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _decode_secret(secret: str) -> bytes:
    cleaned = "".join(secret.strip().replace(" ", "").replace("-", "").upper().split())
    # Re-add base32 padding.
    padding = "=" * (-len(cleaned) % 8)
    try:
        return base64.b32decode(cleaned + padding, casefold=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Invalid base32 TOTP secret: {exc}") from exc


def _digest_func(name: str) -> Callable:
    try:
        return _DIGESTS[name.lower()]
    except KeyError:
        raise ValueError(f"Unsupported digest: {name!r} (choose from sha1/sha256/sha512)") from None


def _hotp(key: bytes, counter: int, digits: int = 6, digest: str = "sha1") -> str:
    msg = struct.pack(">Q", counter)
    mac = hmac.new(key, msg, _digest_func(digest)).digest()
    offset = mac[-1] & 0x0F
    code_int = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code_int % (10**digits)).zfill(digits)


def generate_code(
    secret: str,
    at: float | None = None,
    interval: int = 30,
    digits: int = 6,
    digest: str = "sha1",
) -> str:
    """Generate the TOTP code for ``secret`` at Unix time ``at``."""
    if interval <= 0:
        raise ValueError("interval must be positive")
    if digits not in (6, 8):
        raise ValueError("digits must be 6 or 8")
    key = _decode_secret(secret)
    timestamp = time.time() if at is None else float(at)
    counter = int(timestamp // interval)
    return _hotp(key, counter, digits=digits, digest=digest)


def verify_code(
    secret: str,
    code: str,
    at: float | None = None,
    window: int = 1,
    interval: int = 30,
    digits: int = 6,
    digest: str = "sha1",
) -> bool:
    """Verify a TOTP ``code`` against ``secret`` within a time window.

    Args:
        secret: Base32-encoded shared secret.
        code: User-supplied code (whitespace tolerated).
        at: Unix time to verify against (defaults to now).
        window: Number of ``interval`` steps to check on each side of the
            current counter (``window=1`` accepts prev/current/next).
        interval: Time step in seconds.
        digits: Expected code length (6 or 8).
        digest: HMAC digest name.
    """
    if window < 0:
        raise ValueError("window must be >= 0")
    if interval <= 0:
        raise ValueError("interval must be positive")
    if digits not in (6, 8):
        raise ValueError("digits must be 6 or 8")
    candidate = "".join(str(code).strip().split())
    if not candidate.isdigit() or len(candidate) != digits:
        return False
    try:
        key = _decode_secret(secret)
    except ValueError:
        return False
    timestamp = time.time() if at is None else float(at)
    counter = int(timestamp // interval)
    digest_fn = _digest_func(digest)
    for offset in range(-window, window + 1):
        msg = struct.pack(">Q", counter + offset)
        mac = hmac.new(key, msg, digest_fn).digest()
        trunc = mac[-1] & 0x0F
        code_int = struct.unpack(">I", mac[trunc : trunc + 4])[0] & 0x7FFFFFFF
        expected = str(code_int % (10**digits)).zfill(digits)
        if hmac.compare_digest(expected, candidate):
            return True
    return False


# Backwards/forwards-compatible aliases.
def generate_totp(*args, **kwargs) -> str:
    return generate_code(*args, **kwargs)


def verify_totp(*args, **kwargs) -> bool:
    return verify_code(*args, **kwargs)


def verify(secret: str, code: str, *args, **kwargs) -> bool:
    return verify_code(secret, code, *args, **kwargs)


def provisioning_uri(
    secret: str,
    account: str,
    issuer: str = "VigilCam",
    interval: int = 30,
    digits: int = 6,
    digest: str = "SHA1",
) -> str:
    """Build an ``otpauth://`` provisioning URI for authenticator apps."""
    if not account:
        raise ValueError("account must be non-empty")
    cleaned = secret.strip().replace(" ", "").replace("-", "").upper()
    # Validate decodability early.
    _decode_secret(cleaned)
    digest_name = digest.upper()
    if digest_name not in ("SHA1", "SHA256", "SHA512"):
        raise ValueError("digest must be SHA1, SHA256, or SHA512")
    label = f"{quote(issuer, safe='')}:{quote(account, safe='')}" if issuer else quote(account, safe="")
    params = (
        f"secret={cleaned}"
        f"&issuer={quote(issuer, safe='')}"
        f"&algorithm={digest_name}"
        f"&digits={int(digits)}"
        f"&period={int(interval)}"
    )
    return f"otpauth://totp/{label}?{params}"


# Alias with the name some callers expect.
def get_provisioning_uri(*args, **kwargs) -> str:
    return provisioning_uri(*args, **kwargs)
