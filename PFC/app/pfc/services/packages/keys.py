"""Admin signing keys and separately provisioned Analyst trust (REQ-OCC-01/02)."""

from __future__ import annotations

import base64
import ctypes
import importlib
import json
import sys
from typing import Any, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from filelock import FileLock

from pfc.config import Settings
from pfc.security import package_signing as signing
from pfc.services.packages.common import PackageServiceError, atomic_write


class DPAPI(Protocol):
    def protect(self, data: bytes) -> bytes: ...
    def unprotect(self, data: bytes) -> bytes: ...


class KeyStoreError(PackageServiceError):
    pass


class _Win32DPAPI:
    def __init__(self, module: Any) -> None:
        self.module = module

    def protect(self, data: bytes) -> bytes:
        return bytes(self.module.CryptProtectData(data, "PFC signing key", None, None, None, 1))

    def unprotect(self, data: bytes) -> bytes:
        return bytes(self.module.CryptUnprotectData(data, None, None, None, 1)[1])


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


class _CtypesDPAPI:
    def __init__(self) -> None:
        loader = vars(ctypes).get("WinDLL")
        if loader is None:
            raise OSError("Windows DLL loading is unavailable")
        self.crypt = loader("crypt32", use_last_error=True)
        self.kernel = loader("kernel32", use_last_error=True)
        blob_ptr = ctypes.POINTER(_Blob)
        self.crypt.CryptProtectData.argtypes = [
            blob_ptr,
            ctypes.c_wchar_p,
            blob_ptr,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            blob_ptr,
        ]
        self.crypt.CryptUnprotectData.argtypes = [
            blob_ptr,
            ctypes.c_void_p,
            blob_ptr,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            blob_ptr,
        ]
        self.crypt.CryptProtectData.restype = ctypes.c_int
        self.crypt.CryptUnprotectData.restype = ctypes.c_int
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel.LocalFree.restype = ctypes.c_void_p

    def _transform(self, data: bytes, *, protect: bool) -> bytes:
        buffer = ctypes.create_string_buffer(data)
        source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        output = _Blob()
        operation = self.crypt.CryptProtectData if protect else self.crypt.CryptUnprotectData
        description = "PFC signing key" if protect else None
        if not operation(
            ctypes.byref(source), description, None, None, None, 1, ctypes.byref(output)
        ):
            raise KeyStoreError("dpapi_failed", "Windows DPAPI could not process the signing key")
        try:
            return ctypes.string_at(output.data, output.size)
        finally:
            self.kernel.LocalFree(output.data)

    def protect(self, data: bytes) -> bytes:
        return self._transform(data, protect=True)

    def unprotect(self, data: bytes) -> bytes:
        return self._transform(data, protect=False)


def discover_dpapi() -> DPAPI | None:
    if sys.platform != "win32":
        return None
    try:
        return _Win32DPAPI(importlib.import_module("win32crypt"))
    except (ImportError, OSError):
        try:
            return _CtypesDPAPI()
        except (AttributeError, OSError):
            return None


class SigningKeys:
    def __init__(
        self, settings: Settings, *, dpapi: DPAPI | None = None, use_dpapi: bool = True
    ) -> None:
        self.directory = settings.data_dir / "keys"
        self.dpapi = (dpapi if dpapi is not None else discover_dpapi()) if use_dpapi else None

    def get_or_create(self, passphrase: str) -> Ed25519PrivateKey:
        """Create once; never persist the passphrase or unencrypted key (REQ-OCC-01)."""
        if not passphrase:
            raise KeyStoreError("passphrase_required", "An Admin passphrase is required")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / "private.json"
        with FileLock(self.directory / ".keys.lock", timeout=30):
            if path.exists():
                try:
                    stored = json.loads(path.read_bytes())
                    pem = base64.b64decode(stored["encrypted_key"], validate=True)
                    if stored["dpapi"]:
                        if self.dpapi is None:
                            raise KeyStoreError(
                                "dpapi_unavailable", "This key requires Windows DPAPI"
                            )
                        pem = self.dpapi.unprotect(pem)
                except KeyStoreError:
                    raise
                except Exception as exc:
                    raise KeyStoreError(
                        "invalid_key_file", "Cannot read encrypted signing key"
                    ) from exc
                try:
                    private = signing.load_private_key(pem, passphrase.encode())
                except ValueError as exc:
                    raise KeyStoreError(
                        "wrong_passphrase", "Incorrect signing-key passphrase"
                    ) from exc
                if signing.serialise_public_key(
                    private.public_key()
                ) != signing.serialise_public_key(self.get_public_key()):
                    raise KeyStoreError("key_mismatch", "Signing and public keys do not match")
                return private
            private, public = signing.generate_keypair()
            pem = signing.serialise_private_key(private, passphrase.encode())
            encrypted = self.dpapi.protect(pem) if self.dpapi is not None else pem
            atomic_write(self.directory / "public.pub", signing.serialise_public_key(public))
            atomic_write(self.directory / "key_id.txt", signing.key_id(public).encode())
            atomic_write(
                path,
                signing.canonical_json(
                    {
                        "dpapi": self.dpapi is not None,
                        "encrypted_key": base64.b64encode(encrypted).decode("ascii"),
                    }
                ),
            )
            return private

    def get_public_key(self) -> Ed25519PublicKey:
        try:
            return signing.load_public_key((self.directory / "public.pub").read_bytes())
        except (OSError, ValueError) as exc:
            raise KeyStoreError(
                "public_key_unavailable", "Signing public key is unavailable"
            ) from exc


def load_trusted_keys(settings: Settings) -> list[Ed25519PublicKey]:
    """Trust only provisioned *.pub files, never the package's own key (REQ-OCC-02)."""
    keys: list[Ed25519PublicKey] = []
    seen: set[str] = set()
    for directory in (
        settings.data_dir / "keys" / "trusted",
        settings.config_dir / "keys" / "trusted",
    ):
        for path in sorted(directory.glob("*.pub")):
            try:
                public = signing.load_public_key(path.read_bytes())
                identifier = signing.key_id(public)
                if identifier not in seen:
                    keys.append(public)
                    seen.add(identifier)
            except (OSError, ValueError) as exc:
                raise KeyStoreError(
                    "invalid_trust_store", f"Invalid trusted key file: {path.name}"
                ) from exc
    return keys
