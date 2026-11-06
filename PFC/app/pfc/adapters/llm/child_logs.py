"""Diagnostic-only, bounded persistence of untrusted llama-server output (G6)."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)
MAX_LINE_CHARS = 2048
MAX_READ_BYTES = 64 * 1024
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 2

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_BEARER = re.compile(r"\bBearer\s+[^\s\"'<>,;\[\](){}]+", re.IGNORECASE)
_KEY_FIELD = re.compile(r"\b(?:api[_ -]?keys?|LLAMA_API_KEY|LLAMA_ARG_API_KEY)\s*[:=].*", re.I)
_PROFILE = re.compile(
    r"%USERPROFILE%|[a-z]:[\\/]+Users[\\/]+[^\\/\"'<>\r\n]+|/home/[^/\"'<>\r\n]+", re.I
)
_PAYLOAD = re.compile(
    r"\b(?:prompt|completion|messages?|content|request[_ ]?body|response[_ ]?body|"
    r"generated[_ ]?text|chat[_ ]?template|example_format)\b|[{}]",
    re.IGNORECASE,
)
_DIAGNOSTIC = re.compile(
    r"^(?:(?:load_backend|build_info|system_info|start|main|gguf_init_from_file|"
    r"llama_model_load(?:_from_file_impl)?|common_fit_params|common_init_from_params|"
    r"common_init_result):|srv\s+(?:load_model|operator\(\)):\s|"
    r"init:\s+(?:api_keys:|using \d+ threads)|Unauthorized:\s+Invalid API Key\s*$)"
)


class ChildLogFilter:
    def __init__(self, secrets: Iterable[str], profiles: Iterable[str] = ()) -> None:
        self.secrets = sorted({value for value in secrets if value}, key=len, reverse=True)
        homes = {str(Path.home()), *profiles}
        homes |= {value.replace("\\", "/") for value in homes}
        self.profiles = [
            re.compile(re.escape(value) + r"(?=$|[\\/\s\"':;,\)\]])", re.I)
            for value in sorted(homes, key=len, reverse=True)
            if value
        ]

    def line(self, text: str) -> str | None:
        text = _ANSI.sub("", text)
        text = "".join(char for char in text if char.isprintable() or char == "\t").strip()
        if not _DIAGNOSTIC.match(text) or _PAYLOAD.search(text):
            return None
        for secret in self.secrets:
            text = text.replace(secret, "<redacted>")
        text = _BEARER.sub("Bearer <redacted>", text)
        text = _KEY_FIELD.sub("api_keys: <redacted>", text)
        for profile in self.profiles:
            text = profile.sub("<profile>", text)
        text = _PROFILE.sub("<profile>", text)
        if len(text) > MAX_LINE_CHARS:
            text = text[: MAX_LINE_CHARS - len(" [truncated]")] + " [truncated]"
        return text


class RotatingChildLog:
    def __init__(self, path: Path, *, max_bytes: int = MAX_LOG_BYTES) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.stream = path.open("ab")

    def write(self, text: str) -> None:
        data = (text + "\n").encode("utf-8")
        if len(data) > self.max_bytes:
            return
        if self.stream.tell() + len(data) > self.max_bytes:
            self.stream.close()
            for index in range(LOG_BACKUPS, 0, -1):
                source = (
                    self.path
                    if index == 1
                    else self.path.with_name(f"{self.path.name}.{index - 1}")
                )
                if source.exists():
                    source.replace(self.path.with_name(f"{self.path.name}.{index}"))
            self.stream = self.path.open("ab")
        self.stream.write(data)
        self.stream.flush()

    def close(self) -> None:
        self.stream.close()


class ChildLogCapture:
    def __init__(self, path: Path, secrets: Iterable[str], profiles: Iterable[str]) -> None:
        self.filter = ChildLogFilter(secrets, profiles)
        self.log = RotatingChildLog(path)
        self.thread: threading.Thread | None = None

    def start(self, stream: IO[bytes]) -> None:
        self.thread = threading.Thread(
            target=self._drain, args=(stream,), daemon=True, name="pfc-llm-log"
        )
        self.thread.start()

    def _drain(self, stream: IO[bytes]) -> None:
        writable = True
        try:
            with stream:
                while raw := stream.readline(MAX_READ_BYTES):
                    if len(raw) == MAX_READ_BYTES and not raw.endswith(b"\n"):
                        while raw and not raw.endswith(b"\n"):
                            raw = stream.readline(MAX_READ_BYTES)
                        continue
                    line = self.filter.line(raw.decode("utf-8", errors="replace"))
                    if line is not None and writable:
                        try:
                            self.log.write(line)
                        except OSError:
                            writable = False
                            logger.warning("Local LLM diagnostic log unavailable")
        except OSError:
            logger.warning("Local LLM diagnostic log unavailable")
        finally:
            with suppress(OSError):
                self.log.close()

    def close(self) -> None:
        if self.thread is not None:
            self.thread.join()
        else:
            self.log.close()
