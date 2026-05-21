"""Local Argon2id auth + PBKDF2 bootstrap compatibility (PLAN §9.2)."""
from __future__ import annotations

import base64
import hashlib
import hmac

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

_ph = PasswordHasher()


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(pw: str, stored: str) -> bool:
    """Accepts deploy.sh bootstrap PBKDF2 and Argon2id."""
    if stored.startswith("pbkdf2$"):
        return _verify_pbkdf2(pw, stored)
    try:
        return _ph.verify(stored, pw)
    except (VerifyMismatchError, VerificationError):
        return False


def _verify_pbkdf2(pw: str, stored: str) -> bool:
    try:
        _, iters_s, salt_b64, dk_b64 = stored.split("$")
        iters = int(iters_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
    except Exception:  # noqa: BLE001
        return False
    got = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iters)
    return hmac.compare_digest(got, expected)


def needs_rehash(stored: str) -> bool:
    return stored.startswith("pbkdf2$") or _ph.check_needs_rehash(stored)
