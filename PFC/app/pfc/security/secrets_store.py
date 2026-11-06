"""REQ-AUTH-06: current-user DPAPI; file-key Fernet for non-Windows development/tests."""

from __future__ import annotations

import base64
import ctypes
import hmac
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from filelock import FileLock

PREFIX = "pfc-secret-v1:"


class SecretStoreError(Exception):
    pass


def sensitive_key(key: str) -> bool:
    words = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key)
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", words)
    segments = re.split(r"[_\-.]+", words.casefold())
    names = set(segments)
    if names & {"max", "min", "num", "count", "limit", "budget", "length", "size", "per"}:
        return False
    if names & {
        "token",
        "tokens",
        "auth",
        "session",
        "password",
        "passwords",
        "passwd",
        "passphrase",
        "passphrases",
        "secret",
        "secrets",
        "credential",
        "credentials",
        "authorization",
        "bearer",
        "cookie",
        "cookies",
        "apikey",
        "privatekey",
        "sessionkey",
        "accesskey",
        "signingkey",
        "clientsecret",
    }:
        return True
    if any(
        pair
        in {
            ("api", "key"),
            ("api", "keys"),
            ("private", "key"),
            ("private", "keys"),
            ("session", "key"),
            ("session", "keys"),
            ("access", "key"),
            ("access", "keys"),
            ("signing", "key"),
            ("signing", "keys"),
        }
        for pair in zip(segments, segments[1:], strict=False)
    ):
        return True
    return False


def sensitive_setting(key: str, value: Any) -> bool:
    if sensitive_key(key):
        return True
    if isinstance(value, dict):
        return any(sensitive_setting(str(k), v) for k, v in value.items())
    if isinstance(value, list):
        return any(sensitive_setting("", v) for v in value)
    return isinstance(value, str) and value.startswith(PREFIX)


def identity_scalar(key: str, value: Any) -> bool:
    return key in {"id", "name", "key", "provider"} and (
        value is None or isinstance(value, (str, int, float, bool))
    )


def redaction_fingerprint(value: Any) -> str | None:
    match = re.fullmatch(r"\[REDACTED:([0-9a-f]{12})\]", value) if isinstance(value, str) else None
    return match[1] if match else None


def redacted_marker(value: Any) -> bool:
    return isinstance(value, str) and (value == "[REDACTED]" or value.startswith("[REDACTED:"))


def redact_sensitive(value: Any, key: str = "", *, store: SecretStore | None = None) -> Any:
    def redact(node: Any, sensitive: bool, field: str) -> Any:
        if isinstance(node, dict):
            return {
                k: redact(v, sensitive or sensitive_key(str(k)), str(k)) for k, v in node.items()
            }
        if isinstance(node, list):
            return [redact(v, sensitive, "") for v in node]
        if sensitive or (isinstance(node, str) and node.startswith(PREFIX)):
            if identity_scalar(field, node) and store is not None:
                return f"[REDACTED:{store.fingerprint(node)}]"
            return "[REDACTED]"
        return node

    return redact(value, sensitive_key(key), key)


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt_windows(data: bytes, *, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise SecretStoreError("Windows DPAPI is required")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    ptr = ctypes.POINTER(_Blob)
    crypt.CryptProtectData.argtypes = [
        ptr,
        ctypes.c_wchar_p,
        ptr,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ptr,
    ]
    crypt.CryptUnprotectData.argtypes = [
        ptr,
        ctypes.c_void_p,
        ptr,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ptr,
    ]
    crypt.CryptProtectData.restype = crypt.CryptUnprotectData.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    source_buffer = ctypes.create_string_buffer(data)
    entropy_buffer = ctypes.create_string_buffer(b"PerfectFitCandidate secrets v1")
    source = _Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = _Blob(
        len(entropy_buffer.raw) - 1, ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    output = _Blob()
    operation = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    if not operation(
        ctypes.byref(source),
        "PFC secret" if protect else None,
        ctypes.byref(entropy),
        None,
        None,
        1,
        ctypes.byref(output),
    ):
        raise SecretStoreError("DPAPI could not process the secret")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


class SecretStore:
    def __init__(self, data_dir: Path, *, file_key_fallback: bool = False) -> None:
        self.directory = data_dir / "keys"
        self.use_dpapi = sys.platform == "win32" and not file_key_fallback
        self._identity_key: bytes | None = None

    def fingerprint(self, value: Any) -> str:
        try:
            if self._identity_key is None:
                self.directory.mkdir(parents=True, exist_ok=True)
                path = self.directory / "identity.key"
                with FileLock(self.directory / ".identity-key.lock", timeout=10):
                    if not path.exists():
                        encrypted = self.encrypt(os.urandom(32)).encode("ascii")
                        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, "wb") as output:
                            output.write(encrypted)
                    self._identity_key = self.decrypt(path.read_text(encoding="ascii"))
                if len(self._identity_key) != 32:
                    self._identity_key = None
                    raise SecretStoreError("Invalid fingerprint key")
            canonical = json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
            return hmac.digest(self._identity_key, canonical, "sha256").hex()[:12]
        except (OSError, ValueError) as exc:
            raise SecretStoreError("Secret fingerprint unavailable") from exc

    def _fernet(self, *, create: bool) -> Fernet:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / "settings.key"
        with FileLock(self.directory / ".settings-key.lock", timeout=10):
            if create and not path.exists():
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as output:
                    output.write(Fernet.generate_key())
            return Fernet(path.read_bytes())

    def encrypt(self, value: bytes) -> str:
        try:
            if self.use_dpapi:
                encrypted = base64.b64encode(_crypt_windows(value, protect=True)).decode("ascii")
                return PREFIX + "dpapi:" + encrypted
            return PREFIX + "fernet:" + self._fernet(create=True).encrypt(value).decode("ascii")
        except (OSError, ValueError) as exc:
            raise SecretStoreError("Secret encryption unavailable") from exc

    def decrypt(self, value: str) -> bytes:
        try:
            if self.use_dpapi and value.startswith(PREFIX + "dpapi:"):
                return _crypt_windows(
                    base64.b64decode(value.removeprefix(PREFIX + "dpapi:"), validate=True),
                    protect=False,
                )
            if not self.use_dpapi and value.startswith(PREFIX + "fernet:"):
                return self._fernet(create=False).decrypt(
                    value.removeprefix(PREFIX + "fernet:").encode("ascii")
                )
        except (OSError, ValueError, InvalidToken) as exc:
            raise SecretStoreError("Secret decryption unavailable") from exc
        raise SecretStoreError("Unsupported secret format or platform")
