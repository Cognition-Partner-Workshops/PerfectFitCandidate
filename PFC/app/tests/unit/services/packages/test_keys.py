"""Encrypted signing keys, injectable DPAPI and provisioned trust (REQ-OCC-01/02)."""

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pfc.config import Settings
from pfc.security import package_signing as signing
from pfc.services.packages import keys


class FakeDPAPI:
    def __init__(self) -> None:
        self.protected = 0
        self.unprotected = 0

    def protect(self, data: bytes) -> bytes:
        assert data.startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----")
        self.protected += 1
        return b"windows:" + data[::-1]

    def unprotect(self, data: bytes) -> bytes:
        assert data.startswith(b"windows:")
        self.unprotected += 1
        return data[len(b"windows:") :][::-1]


@pytest.mark.parametrize("wrapped", [False, True])
def test_key_creation_reload_and_wrong_passphrase(tmp_path: Path, wrapped: bool) -> None:
    settings = Settings(data_dir=tmp_path)
    dpapi = FakeDPAPI()
    store = keys.SigningKeys(settings, dpapi=dpapi, use_dpapi=wrapped)
    passphrase = "signing passphrase only in memory"
    private = store.get_or_create(passphrase)
    second = store.get_or_create(passphrase)
    assert signing.key_id(second.public_key()) == signing.key_id(private.public_key())
    assert signing.key_id(store.get_public_key()) == (tmp_path / "keys/key_id.txt").read_text()
    stored = json.loads((tmp_path / "keys/private.json").read_bytes())
    assert stored["dpapi"] is wrapped
    encrypted = base64.b64decode(stored["encrypted_key"])
    if not wrapped:
        assert signing.load_private_key(encrypted, passphrase.encode())
    else:
        assert (dpapi.protected, dpapi.unprotected) == (1, 1)
        with pytest.raises(keys.KeyStoreError) as unavailable:
            keys.SigningKeys(settings, use_dpapi=False).get_or_create(passphrase)
        assert unavailable.value.reason == "dpapi_unavailable"
    with pytest.raises(keys.KeyStoreError) as wrong:
        store.get_or_create("incorrect passphrase")
    assert wrong.value.reason == "wrong_passphrase"
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert passphrase.encode() not in path.read_bytes()
    assert keys.load_trusted_keys(settings) == []
    trusted = tmp_path / "keys/trusted"
    trusted.mkdir()
    (trusted / "admin.pub").write_bytes(signing.serialise_public_key(store.get_public_key()))
    assert signing.key_id(keys.load_trusted_keys(settings)[0]) == signing.key_id(
        private.public_key()
    )
    (trusted / "bad.pub").write_bytes(b"invalid")
    with pytest.raises(keys.KeyStoreError, match="Invalid trusted"):
        keys.load_trusted_keys(settings)


def test_missing_public_key_and_empty_passphrase(tmp_path: Path) -> None:
    store = keys.SigningKeys(Settings(data_dir=tmp_path), use_dpapi=False)
    with pytest.raises(keys.KeyStoreError, match="unavailable"):
        store.get_public_key()
    with pytest.raises(keys.KeyStoreError, match="passphrase is required"):
        store.get_or_create("")


def test_windows_dpapi_discovery_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keys.sys, "platform", "win32")
    module = SimpleNamespace(
        CryptProtectData=lambda data, *_: b"win:" + data,
        CryptUnprotectData=lambda data, *_: ("description", data[4:]),
    )
    monkeypatch.setattr(keys.importlib, "import_module", lambda _: module)
    dpapi = keys.discover_dpapi()
    assert dpapi is not None
    assert dpapi.unprotect(dpapi.protect(b"encrypted")) == b"encrypted"

    def unavailable(_: str) -> Any:
        raise ImportError("pywin32 unavailable")

    monkeypatch.setattr(keys.importlib, "import_module", unavailable)
    fallback = FakeDPAPI()
    monkeypatch.setattr(keys, "_CtypesDPAPI", lambda: fallback)
    assert keys.discover_dpapi() is fallback

    def missing_ctypes() -> Any:
        raise OSError("crypt32 unavailable")

    monkeypatch.setattr(keys, "_CtypesDPAPI", missing_ctypes)
    assert keys.discover_dpapi() is None
    monkeypatch.setattr(keys.sys, "platform", "linux")
    assert keys.discover_dpapi() is None
