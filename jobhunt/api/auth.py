"""Password hashing and JWT helpers. Stdlib scrypt + PyJWT; nothing hand-rolled."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

import jwt

ALGORITHM = "HS256"          # pinned on decode, so an "alg: none" token is rejected
MIN_SECRET_LEN = 32
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1


class AuthNotConfigured(RuntimeError):
    """JWT_SECRET is missing or too short."""


@dataclass(frozen=True)
class AuthSettings:
    secret: str
    idle_seconds: int       # inactivity before a session dies
    max_seconds: int        # hard cap on a session, however active
    max_failed_logins: int
    lockout_seconds: int


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, default)))
    except ValueError:
        return default


def load_settings() -> AuthSettings:
    secret = os.getenv("JWT_SECRET", "")
    if len(secret) < MIN_SECRET_LEN:
        raise AuthNotConfigured(
            f"JWT_SECRET must be set to at least {MIN_SECRET_LEN} characters")
    return AuthSettings(
        secret=secret,
        idle_seconds=_env_int("SESSION_IDLE_MINUTES", 30) * 60,
        max_seconds=_env_int("SESSION_MAX_HOURS", 12) * 3600,
        max_failed_logins=_env_int("MAX_FAILED_LOGINS", 5),
        lockout_seconds=_env_int("LOCKOUT_MINUTES", 15) * 60,
    )


# --------------------------------------------------------------- passwords --

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


_DUMMY_HASH: str | None = None


def burn_password_check(password: str) -> None:
    """Spend the same time as a real check, so an unknown email is not
    distinguishable from a wrong password by response time."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("dummy-password-for-timing")
    verify_password(password, _DUMMY_HASH)


# ------------------------------------------------------------------- tokens --

def create_token(settings: AuthSettings, user_id: int, session_id: str) -> tuple[str, int]:
    """Returns (token, absolute expiry as a unix timestamp)."""
    now = int(time.time())
    exp = now + settings.max_seconds
    token = jwt.encode({"sub": str(user_id), "sid": session_id, "iat": now, "exp": exp},
                       settings.secret, algorithm=ALGORITHM)
    return token, exp


def decode_token(settings: AuthSettings, token: str) -> dict[str, Any]:
    """Raises jwt.PyJWTError for a bad signature, a missing claim or an expired token."""
    return jwt.decode(token, settings.secret, algorithms=[ALGORITHM],
                      options={"require": ["exp", "sub", "sid"]})
