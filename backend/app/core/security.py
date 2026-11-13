"""Password hashing, credential encryption and log redaction."""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken

_hasher = PasswordHasher()


# --------------------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def encode_hash_for_env(hashed: str) -> str:
    """argon2 hashes contain '$'; base64 keeps them intact in .env / docker compose."""
    return "b64:" + base64.b64encode(hashed.encode()).decode()


def verify_password(hashed: str, password: str) -> bool:
    if not hashed:
        return False
    try:
        return _hasher.verify(hashed, password)
    except (VerificationError, InvalidHashError):
        return False


# --------------------------------------------------------------------------- encryption


class CredentialCipher:
    """Encrypts broker credentials (a dict) with Fernet (AES-128-CBC + HMAC)."""

    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError("ENCRYPTION_KEY is not set")
        self._fernet = Fernet(key.encode())

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()

    def encrypt(self, credentials: dict[str, Any]) -> str:
        return self._fernet.encrypt(json.dumps(credentials).encode()).decode()

    def decrypt(self, token: str) -> dict[str, Any]:
        try:
            data = json.loads(self._fernet.decrypt(token.encode()).decode())
        except InvalidToken as exc:
            raise ValueError("Cannot decrypt credentials (wrong ENCRYPTION_KEY?)") from exc
        register_secrets(data)
        return data


# --------------------------------------------------------------------------- log redaction

_SECRET_KEYS = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?token|sec|cid)\b(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}&]+)"
)


class SecretRegistry:
    """Known secret values (decrypted credentials) that must never reach a log."""

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = threading.Lock()

    def add(self, value: str) -> None:
        if value and len(value) >= 4:
            with self._lock:
                self._values.add(value)

    def redact(self, text: str) -> str:
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for value in values:
            if value in text:
                text = text.replace(value, "***")
        return _SECRET_KEYS.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


SECRETS = SecretRegistry()


def register_secrets(credentials: dict[str, Any]) -> None:
    for key, value in credentials.items():
        if isinstance(value, str) and key.lower() not in {"username", "user", "environment", "account"}:
            SECRETS.add(value)


class RedactingFilter(logging.Filter):
    """Masks secrets in every log record that passes through a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = SECRETS.redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True
