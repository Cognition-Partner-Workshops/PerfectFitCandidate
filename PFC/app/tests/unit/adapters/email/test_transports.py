"""No Outlook required: approval, verbatim recipients and deduplication (REQ-EML-01/04/05/06)."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pfc.adapters.email import transports


class Items:
    def __init__(self, mails: Iterable[Mail] = ()) -> None:
        self.mails = list(mails)

    @property
    def Count(self) -> int:
        return len(self.mails)

    def Item(self, index: int) -> Mail:
        return self.mails[index - 1]

    def Restrict(self, query: str) -> Items:
        key = query.split(" = '")[1][:-1].replace("''", "'")
        return Items([mail for mail in self.mails if mail.key == key])


class Mail:
    To: str
    CC: str
    Subject: str
    HTMLBody: str

    def __init__(self, app: Application) -> None:
        self.app = app
        self.key: str | None = None
        self.sent = self.displayed = self.saved = 0
        self.paths: list[str] = []
        self.PropertyAccessor = SimpleNamespace(SetProperty=self.set_property)

    @property
    def Attachments(self) -> SimpleNamespace:
        return SimpleNamespace(
            Add=self.paths.append, Remove=lambda i: self.paths.pop(i - 1), Count=len(self.paths)
        )

    def set_property(self, name: str, value: str) -> None:
        assert name == transports.IDEMPOTENCY_PROPERTY
        self.key = value

    def Save(self) -> None:
        self.saved += 1
        if self not in self.app.folders[16].Items.mails:
            self.app.folders[16].Items.mails.append(self)

    def Display(self) -> None:
        self.displayed += 1

    def Send(self) -> None:
        self.sent += 1
        if self.app.fail:
            raise RuntimeError("blocked by policy")
        self.app.folders[4].Items.mails.append(self)


class Application:
    def __init__(self) -> None:
        self.folders = {id: SimpleNamespace(Items=Items()) for id in (4, 5, 16)}
        self.created: list[Mail] = []
        self.threads: set[int] = set()
        self.fail = False

    def CreateItem(self, kind: int) -> Mail:
        assert kind == 0
        self.threads.add(threading.get_ident())
        mail = Mail(self)
        self.created.append(mail)
        return mail

    def GetNamespace(self, name: str) -> Application:
        assert name == "MAPI"
        self.threads.add(threading.get_ident())
        return self

    def GetDefaultFolder(self, id: int) -> SimpleNamespace:
        return self.folders[id]


@pytest.fixture
def message() -> transports.EmailMessage:
    return transports.EmailMessage(
        "key'1",
        " Candidate <exact@example.test> ",
        "Team <team@example.test>;Other@example.test",
        "subject",
        "<b>body</b>",
        ("/jd.docx",),
    )


@pytest.mark.parametrize("kind", ["fake", "com"])
def test_contract(kind: str, message: transports.EmailMessage) -> None:
    app = Application()
    transport = (
        transports.FakeTransport()
        if kind == "fake"
        else transports.OutlookComTransport(com_factory=lambda: app)
    )
    try:
        assert transport.reconcile(message.idempotency_key).status == "missing"
        with pytest.raises(transports.EmailTransportError, match="approval"):
            transport.send(message)
        assert transport.create_draft(message).status == "draft"
        assert transport.create_draft(message, display=True).status == "displayed"
        assert transport.reconcile(message.idempotency_key).status == "missing"
        approved = replace(message, approved=True)
        assert transport.send(approved).status == "sent"
        assert transport.send(approved).status == "sent"
        assert transport.create_draft(message).status == "sent"
        assert transport.reconcile(message.idempotency_key).status == "sent"
        if isinstance(transport, transports.FakeTransport):
            assert transport.messages == [approved]
        else:
            assert len(app.created) == 1
            mail = app.created[0]
            assert (mail.To, mail.CC, mail.Subject, mail.HTMLBody) == (
                message.to,
                message.cc,
                message.subject,
                message.html_body,
            )
            assert mail.paths == list(message.attachments)
            assert mail.sent == mail.displayed == 1
            assert len(app.threads) == 1
            assert threading.get_ident() not in app.threads
    finally:
        if isinstance(transport, transports.OutlookComTransport):
            transport.close()


def test_reconcile_after_restart_and_manual_send(message: transports.EmailMessage) -> None:
    app = Application()
    first = transports.OutlookComTransport(com_factory=lambda: app)
    first.create_draft(message, display=True)
    app.created[0].Send()
    first.close()
    second = transports.OutlookComTransport(com_factory=lambda: app)
    try:
        assert second.reconcile(message.idempotency_key).status == "sent"
        second.send(replace(message, approved=True))
        assert app.created[0].sent == 1
    finally:
        second.close()


def test_send_preserves_analyst_draft_edits(message: transports.EmailMessage) -> None:
    """Approved Outlook edits, not original generated fields, are sent (REQ-EML-01/02)."""
    app = Application()
    transport = transports.OutlookComTransport(com_factory=lambda: app)
    try:
        transport.create_draft(message, display=True)
        mail = app.created[0]
        mail.To = "edited-to@example.test"
        mail.CC = "edited-cc@example.test"
        mail.Subject = "Analyst subject"
        mail.HTMLBody = "<p>Analyst-edited body</p>"
        mail.paths[:] = ["/analyst-added.docx"]
        assert transport.send(replace(message, approved=True)).status == "sent"
        assert mail.To == "edited-to@example.test"
        assert mail.CC == "edited-cc@example.test"
        assert mail.Subject == "Analyst subject"
        assert mail.HTMLBody == "<p>Analyst-edited body</p>"
        assert mail.paths == ["/analyst-added.docx"]
        assert len(app.created) == mail.sent == mail.saved == 1
    finally:
        transport.close()


def test_uncertain_failure_requires_reconciliation(message: transports.EmailMessage) -> None:
    app = Application()
    app.fail = True
    transport = transports.OutlookComTransport(com_factory=lambda: app)
    approved = replace(message, approved=True)
    try:
        with pytest.raises(transports.EmailTransportError, match="Outlook operation failed"):
            transport.send(approved)
        with pytest.raises(transports.EmailTransportError, match="uncertain"):
            transport.send(approved)
        assert app.created[0].sent == 1
        assert transport.reconcile(message.idempotency_key).status == "missing"
        assert app.created[0].sent == 1
        app.fail = False
        transport.send(approved)
        assert app.created[0].sent == 2
    finally:
        transport.close()


def test_non_windows_and_empty(
    monkeypatch: pytest.MonkeyPatch, message: transports.EmailMessage
) -> None:
    monkeypatch.setattr(transports.sys, "platform", "linux")
    transport = transports.OutlookComTransport()
    try:
        with pytest.raises(transports.EmailTransportError, match="only on Windows"):
            transport.create_draft(message)
        with pytest.raises(ValueError):
            transport.create_draft(replace(message, to=""))
        with pytest.raises(ValueError):
            transport.send(replace(message, idempotency_key="", approved=True))
    finally:
        transport.close()


@pytest.mark.parametrize("kind", ["fake", "com"])
def test_concurrent_duplicates_and_explicit_resend(
    kind: str, message: transports.EmailMessage
) -> None:
    app = Application()
    transport = (
        transports.FakeTransport()
        if kind == "fake"
        else transports.OutlookComTransport(com_factory=lambda: app)
    )
    approved = replace(message, approved=True)
    try:
        with ThreadPoolExecutor(max_workers=4) as callers:
            results = list(callers.map(transport.send, [approved] * 4))
        assert all(result.status == "sent" for result in results)
        transport.send(replace(approved, idempotency_key="explicit-resend-2"))
        if isinstance(transport, transports.FakeTransport):
            assert len(transport.messages) == 2
        else:
            assert sum(mail.sent for mail in app.created) == 2
    finally:
        if isinstance(transport, transports.OutlookComTransport):
            transport.close()
