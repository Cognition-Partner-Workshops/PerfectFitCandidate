import io
import threading
from collections.abc import Iterator

import pytest


class HeldPipe(io.BytesIO):
    def __init__(self) -> None:
        super().__init__(b"main: model loaded\n")
        self.blocked = threading.Event()
        self.release = threading.Event()

    def readline(self, size: int = -1) -> bytes:
        data = super().readline(size)
        if data:
            return data
        self.blocked.set()
        self.release.wait()
        return b"main: model loaded LATE_OUTPUT\n"


@pytest.fixture
def held_pipe() -> Iterator[HeldPipe]:
    stream = HeldPipe()
    try:
        yield stream
    finally:
        stream.release.set()
