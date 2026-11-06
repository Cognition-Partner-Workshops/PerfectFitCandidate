"""REQ-AUTH-06: development fallback and mocked DPAPI dispatch; not Windows validation."""

import sys
from pathlib import Path

import pytest

from pfc.security.secrets_store import (
    SecretStore,
    SecretStoreError,
    redact_sensitive,
    sensitive_setting,
)


@pytest.mark.parametrize("value", [b"", b"secret smtp password", b"non-ascii \xc3\xa9"])
def test_file_key_roundtrip(tmp_path: Path, value: bytes) -> None:
    store = SecretStore(tmp_path, file_key_fallback=True)
    encrypted = store.encrypt(value)
    assert encrypted.startswith("pfc-secret-v1:fernet:")
    assert SecretStore(tmp_path, file_key_fallback=True).decrypt(encrypted) == value
    assert encrypted != value.decode("utf-8")
    if sys.platform != "win32":
        assert (tmp_path / "keys" / "settings.key").stat().st_mode & 0o777 == 0o600


def test_decryption_fails_closed(tmp_path: Path) -> None:
    store = SecretStore(tmp_path, file_key_fallback=True)
    encrypted = store.encrypt(b"private")
    other = SecretStore(tmp_path / "other", file_key_fallback=True)
    other.encrypt(b"different")
    for target, blob in [
        (other, encrypted),
        (store, "plaintext"),
        (store, encrypted[:-10] + "AAAAAAAAAA"),
        (store, "pfc-secret-v1:dpapi:wrong-platform"),
    ]:
        with pytest.raises(SecretStoreError):
            target.decrypt(blob)
    (tmp_path / "keys" / "settings.key").unlink()
    with pytest.raises(SecretStoreError):
        store.decrypt(encrypted)
    assert not (tmp_path / "keys" / "settings.key").exists()


def test_dpapi_dispatch_without_file_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def dpapi(data: bytes, *, protect: bool) -> bytes:
        calls.append(protect)
        return b"protected:" + data if protect else data.removeprefix(b"protected:")

    monkeypatch.setattr("pfc.security.secrets_store._crypt_windows", dpapi)
    store = SecretStore(tmp_path)
    store.use_dpapi = True
    encrypted = store.encrypt(b"private")
    assert encrypted.startswith("pfc-secret-v1:dpapi:")
    assert store.decrypt(encrypted) == b"private"
    assert calls == [True, False]
    assert not (tmp_path / "keys" / "settings.key").exists()


def test_nested_secret_detection() -> None:
    assert sensitive_setting("smtp.password", "private")
    assert sensitive_setting("llm.api_key", "private")
    assert sensitive_setting("provider.options", [{"apiKey": "private"}])
    assert not sensitive_setting("matching.top_n", 5)


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "access_token",
        "apiKey",
        "Authorization",
        "PRIVATE.KEY",
        "clientSecret",
        "HTTPAuthorization",
        "session-key",
        "signing_key",
        "client.credentials",
        "headers.cookies",
        "PRIVATE.KEYS",
        "sessionKeys",
    ],
)
def test_sensitive_key_segments(key: str) -> None:
    assert sensitive_setting(key, "private")


@pytest.mark.parametrize(
    "key",
    [
        "max_tokens",
        "token_limit",
        "num_tokens",
        "maxTokenCount",
        "access_token_limit",
        "tokenizer",
        "author",
        "secretary",
    ],
)
def test_noncredential_segments(key: str) -> None:
    assert not sensitive_setting(key, 64)


def test_redaction_preserves_empty_and_sensitive_containers() -> None:
    value = {
        "private_key": {"parts": ["private", None], "empty": []},
        "options": [{}, [], {"max_tokens": 64, "token": "private"}],
    }
    assert redact_sensitive(value) == {
        "private_key": {"parts": ["[REDACTED]", "[REDACTED]"], "empty": []},
        "options": [{}, [], {"max_tokens": 64, "token": "[REDACTED]"}],
    }
    assert value["private_key"]["parts"] == ["private", None]
