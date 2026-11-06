"""Expired confirmations are reclaimed without touching live uploads (REQ-OCC-02)."""

import io

import pytest

from pfc.config import Settings
from pfc.services.packages import staging
from pfc.services.packages.common import PackageServiceError
from pfc.services.packages.inbox import scan_inbox
from pfc.services.packages.staging import UploadStaging

from .test_settings import exported


@pytest.mark.parametrize("action", ["construct", "stage", "lookup", "startup"])
def test_expired_uploads_are_purged(
    pair: tuple[Settings, Settings], action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = pair
    now = 100
    store = UploadStaging(target, clock=lambda: now)
    package = exported(source).path.read_bytes()
    expired, _ = store.preview(
        io.BytesIO(package), kind="settings", user_id=1, session_id=1, now=now
    )
    live, _ = store.preview(io.BytesIO(package), kind="settings", user_id=1, session_id=1, now=200)
    now = 3700
    if action == "construct":
        UploadStaging(target, clock=lambda: now)
    elif action == "stage":
        store.preview(io.BytesIO(package), kind="settings", user_id=1, session_id=1, now=now)
    elif action == "lookup":
        with pytest.raises(PackageServiceError) as error:
            store.apply(
                expired,
                kind="settings",
                user_id=1,
                session_id=1,
                now=now,
                operation=lambda _: pytest.fail("Expired package must not be applied"),
            )
        assert error.value.reason == "preview_expired"
    else:
        monkeypatch.setattr(staging.time, "time", lambda: now)
        scan_inbox(target)
    for extension in ("json", "upload"):
        assert not (store.directory / f"{expired}.{extension}").exists()
        assert (store.directory / f"{live}.{extension}").exists()
