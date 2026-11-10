import os

import pytest


def _raise_oserror(*_args: object, **_kwargs: object) -> None:
    raise OSError("disabled in tests")


@pytest.fixture(autouse=True)
def disable_os_startfile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "startfile", _raise_oserror, raising=False)
