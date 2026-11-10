"""Approval-gated, idempotent email transports (REQ-EML-01/04/05/06)."""

from __future__ import annotations

import importlib
import logging
import mimetypes
import os
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from email.message import EmailMessage as MIMEEmailMessage
from html.parser import HTMLParser
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Any, Literal, Protocol, cast

logger = logging.getLogger(__name__)

IDEMPOTENCY_PROPERTY = (
    "http://schemas.microsoft.com/mapi/string/"
    "{00020329-0000-0000-C000-000000000046}/PFC-IdempotencyKey"
)


@dataclass(frozen=True)
class EmailMessage:
    idempotency_key: str
    to: str
    cc: str
    subject: str
    html_body: str
    attachments: tuple[str, ...] = ()
    approved: bool = False


@dataclass(frozen=True)
class DeliveryResult:
    idempotency_key: str
    status: Literal["draft", "displayed", "sent", "missing"]


class EmailTransport(Protocol):
    def create_draft(self, message: EmailMessage, *, display: bool = False) -> DeliveryResult: ...
    def send(self, message: EmailMessage) -> DeliveryResult: ...
    def reconcile(self, idempotency_key: str) -> DeliveryResult: ...


class EmailTransportError(RuntimeError):
    pass


def validate(message: EmailMessage, *, sending: bool = False) -> None:
    if not message.idempotency_key or not message.to:
        raise ValueError("Idempotency key and verbatim To recipient are required")
    if sending and not message.approved:
        raise EmailTransportError("Explicit per-message approval is required (REQ-EML-01)")


class _PlainTextHTMLParser(HTMLParser):
    _block_tags = {"br", "div", "li", "ol", "p", "tr", "ul"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._block_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_text(html: str) -> str:
    parser = _PlainTextHTMLParser()
    parser.feed(html)
    parser.close()
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def build_eml(message: EmailMessage) -> bytes:
    """Build an editable RFC 5322 draft for analyst handoff."""
    mime_message = MIMEEmailMessage()
    mime_message["To"] = message.to
    if message.cc:
        mime_message["Cc"] = message.cc
    mime_message["Subject"] = message.subject
    mime_message["X-Unsent"] = "1"
    mime_message.set_content(_plain_text(message.html_body))
    mime_message.add_alternative(message.html_body, subtype="html")
    for attachment in message.attachments:
        path = Path(attachment)
        if not path.is_file():
            raise EmailTransportError("Email attachment file is unavailable")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise EmailTransportError("Email attachment file is unavailable") from exc
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        maintype, subtype = mime_type.split("/", maxsplit=1)
        mime_message.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )
    return bytes(mime_message)


class EmlHandoffTransport:
    """Writes an analyst-reviewed draft file without sending email automatically."""

    manual_send = True

    def __init__(self, *, opener: Callable[[Path], None] | None = None) -> None:
        default_opener = getattr(os, "startfile", None) if sys.platform == "win32" else None
        self._opener = (
            opener if opener is not None else cast(Callable[[Path], None] | None, default_opener)
        )

    def create_draft(self, message: EmailMessage, *, display: bool = False) -> DeliveryResult:
        validate(message)
        return DeliveryResult(message.idempotency_key, "draft")

    def send(self, message: EmailMessage) -> DeliveryResult:
        raise EmailTransportError(
            "Automatic sending is not available with the Outlook draft file transport"
        )

    def reconcile(self, idempotency_key: str) -> DeliveryResult:
        return DeliveryResult(idempotency_key, "missing")

    def write_draft(self, message: EmailMessage, path: Path) -> None:
        content = build_eml(message)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def open_draft(self, path: Path) -> bool:
        if self._opener is None:
            return False
        try:
            self._opener(path)
        except Exception:
            logger.warning("Unable to open Outlook draft file")
            return False
        return True


class FakeTransport:
    """Records exact messages; one send per key (REQ-EML-05/06)."""

    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []
        self.drafts: dict[str, EmailMessage] = {}
        self._sent: set[str] = set()
        self._lock = Lock()

    def create_draft(self, message: EmailMessage, *, display: bool = False) -> DeliveryResult:
        validate(message)
        with self._lock:
            if message.idempotency_key in self._sent:
                return DeliveryResult(message.idempotency_key, "sent")
            self.drafts[message.idempotency_key] = message
            return DeliveryResult(message.idempotency_key, "displayed" if display else "draft")

    def send(self, message: EmailMessage) -> DeliveryResult:
        validate(message, sending=True)
        with self._lock:
            if message.idempotency_key not in self._sent:
                self.messages.append(message)
                self._sent.add(message.idempotency_key)
                self.drafts.pop(message.idempotency_key, None)
            return DeliveryResult(message.idempotency_key, "sent")

    def reconcile(self, idempotency_key: str) -> DeliveryResult:
        with self._lock:
            return DeliveryResult(
                idempotency_key, "sent" if idempotency_key in self._sent else "missing"
            )


class OutlookComTransport:
    """All COM work is on one STA worker (REQ-EML-04).

    The service must commit Sending before send and persist the result afterwards.
    Reconciliation never sends; missing returns control to human approval (REQ-EML-06).
    """

    def __init__(self, *, com_factory: Callable[[], Any] | None = None) -> None:
        self._factory = com_factory
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pfc-outlook-sta")
        self._outlook: Any = None
        self._com: Any = None
        self._sent: set[str] = set()
        self._uncertain: set[str] = set()

    def _application(self) -> Any:
        if self._outlook is None:
            if self._factory is not None:
                self._outlook = self._factory()
            else:
                if sys.platform != "win32":
                    raise EmailTransportError("Outlook COM transport is available only on Windows")
                pythoncom = importlib.import_module("pythoncom")
                win32 = importlib.import_module("win32com.client")
                if self._com is None:
                    pythoncom.CoInitialize()
                    self._com = pythoncom
                self._outlook = win32.Dispatch("Outlook.Application")
        return self._outlook

    def _call(self, action: Callable[[], DeliveryResult]) -> DeliveryResult:
        try:
            return self._executor.submit(action).result()
        except (EmailTransportError, ValueError):
            raise
        except Exception as exc:
            raise EmailTransportError(
                "Outlook operation failed; check installation, profile, policy "
                "and attachment size. "
                "Reconcile before retrying a send."
            ) from exc

    def _find(self, key: str, folders: tuple[int, ...]) -> Any:
        namespace = self._application().GetNamespace("MAPI")
        escaped = key.replace("'", "''")
        query = f"@SQL=\"{IDEMPOTENCY_PROPERTY}\" = '{escaped}'"
        for folder in folders:
            items = namespace.GetDefaultFolder(folder).Items.Restrict(query)
            if items.Count:
                return items.Item(1)
        return None

    def _already_sent(self, key: str) -> bool:
        if key in self._sent:
            return True
        if self._find(key, (4, 5)) is not None:
            self._sent.add(key)
            return True
        return False

    def _mail(self, message: EmailMessage, *, regenerate: bool = False) -> Any:
        mail = self._find(message.idempotency_key, (16,))
        if mail is not None and not regenerate:
            return mail
        if mail is None:
            mail = self._application().CreateItem(0)
        mail.To = message.to
        mail.CC = message.cc
        mail.Subject = message.subject
        mail.HTMLBody = message.html_body
        while mail.Attachments.Count:
            mail.Attachments.Remove(1)
        for path in message.attachments:
            mail.Attachments.Add(path)
        mail.PropertyAccessor.SetProperty(IDEMPOTENCY_PROPERTY, message.idempotency_key)
        mail.Save()
        return mail

    def create_draft(self, message: EmailMessage, *, display: bool = False) -> DeliveryResult:
        validate(message)

        def action() -> DeliveryResult:
            if self._already_sent(message.idempotency_key):
                return DeliveryResult(message.idempotency_key, "sent")
            mail = self._mail(message, regenerate=True)
            if display:
                mail.Display()
            return DeliveryResult(message.idempotency_key, "displayed" if display else "draft")

        return self._call(action)

    def send(self, message: EmailMessage) -> DeliveryResult:
        validate(message, sending=True)

        def action() -> DeliveryResult:
            key = message.idempotency_key
            if self._already_sent(key):
                return DeliveryResult(key, "sent")
            if key in self._uncertain:
                raise EmailTransportError("Previous send outcome uncertain; reconcile before retry")
            mail = self._mail(message, regenerate=True)
            self._uncertain.add(key)
            mail.Send()
            self._sent.add(key)
            self._uncertain.discard(key)
            return DeliveryResult(key, "sent")

        return self._call(action)

    def reconcile(self, idempotency_key: str) -> DeliveryResult:
        def action() -> DeliveryResult:
            sent = self._already_sent(idempotency_key)
            self._uncertain.discard(idempotency_key)
            return DeliveryResult(idempotency_key, "sent" if sent else "missing")

        return self._call(action)

    def close(self) -> None:
        def cleanup() -> None:
            self._outlook = None
            if self._com is not None:
                self._com.CoUninitialize()
                self._com = None

        try:
            self._executor.submit(cleanup).result()
        finally:
            self._executor.shutdown(wait=True)
