"""No Outlook required: approval, verbatim recipients and deduplication (REQ-EML-01/04/05/06)."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from email import message_from_bytes
from email.policy import default
from pathlib import Path
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


def test_build_eml_includes_alternatives_and_attachment(
    message: transports.EmailMessage, tmp_path: Path
) -> None:
    attachment = tmp_path / "job-description.txt"
    attachment.write_bytes(b"Job description")
    message = replace(
        message,
        to="candidate@example.test",
        cc="team@example.test",
        subject="Opportunity",
        html_body="<p>Hello <strong>candidate</strong>.</p>",
        attachments=(str(attachment),),
    )

    parsed = message_from_bytes(transports.build_eml(message), policy=default)
    alternative = next(
        part for part in parsed.walk() if part.get_content_type() == "multipart/alternative"
    )
    plain = alternative.get_body(preferencelist=("plain",))
    html = alternative.get_body(preferencelist=("html",))
    attached = next(part for part in parsed.walk() if part.get_filename())

    assert parsed["X-Unsent"] == "1"
    assert parsed["To"] == "candidate@example.test"
    assert parsed["Cc"] == "team@example.test"
    assert parsed["Subject"] == "Opportunity"
    assert "From" not in parsed and "Date" not in parsed
    assert plain is not None and "Hello candidate." in plain.get_content()
    assert html is not None and "<strong>candidate</strong>" in html.get_content()
    assert attached.get_filename() == "job-description.txt"
    assert attached.get_payload(decode=True) == b"Job description"


def test_build_eml_omits_empty_cc_and_rejects_missing_attachment(
    message: transports.EmailMessage, tmp_path: Path
) -> None:
    message = replace(message, cc="", attachments=())
    parsed = message_from_bytes(transports.build_eml(message), policy=default)
    assert "Cc" not in parsed

    missing = replace(message, attachments=(str(tmp_path / "missing.txt"),))
    with pytest.raises(transports.EmailTransportError, match="attachment file is unavailable"):
        transports.build_eml(missing)


def test_eml_handoff_transport_never_sends_and_creates_drafts_without_side_effects(
    message: transports.EmailMessage, tmp_path: Path
) -> None:
    transport = transports.EmlHandoffTransport()

    result = transport.create_draft(message)

    assert result == transports.DeliveryResult(message.idempotency_key, "draft")
    assert not list(tmp_path.iterdir())
    with pytest.raises(
        transports.EmailTransportError,
        match="^Automatic sending is not available with the Outlook draft file transport$",
    ):
        transport.send(message)


def test_eml_handoff_write_and_open_draft(tmp_path: Path) -> None:
    message = transports.EmailMessage("key", "to@example.test", "", "subject", "<p>Body</p>")
    opened: list[Path] = []
    transport = transports.EmlHandoffTransport(opener=opened.append)
    path = tmp_path / "nested" / "draft.eml"

    transport.write_draft(message, path)

    assert path.is_file()
    assert message_from_bytes(path.read_bytes(), policy=default)["X-Unsent"] == "1"
    assert transport.open_draft(path) is True
    assert opened == [path]
    assert transports.EmlHandoffTransport().open_draft(path) is False


def test_eml_handoff_open_failure_is_reported_as_false(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def fail_to_open(path: Path) -> None:
        raise OSError("open failed")

    transport = transports.EmlHandoffTransport(opener=fail_to_open)

    assert transport.open_draft(tmp_path / "draft.eml") is False
    assert "Unable to open Outlook draft file" in caplog.text
    assert "open failed" not in caplog.text


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


def test_send_overwrites_outlook_draft_with_stored_content(
    message: transports.EmailMessage,
) -> None:
    """PFC's stored edits are authoritative when sending (REQ-EML-01/02)."""
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
        stored = replace(
            message,
            to="stored-to@example.test",
            cc="stored-cc@example.test",
            subject="PFC subject",
            html_body="<p>PFC body</p>",
            attachments=("/pfc.docx",),
            approved=True,
        )
        assert transport.send(stored).status == "sent"
        assert (mail.To, mail.CC, mail.Subject, mail.HTMLBody) == (
            stored.to,
            stored.cc,
            stored.subject,
            stored.html_body,
        )
        assert mail.paths == list(stored.attachments)
        assert len(app.created) == mail.sent == 1
        assert mail.saved == 2
    finally:
        transport.close()


def test_send_uses_pfc_edited_content_from_database_message(
    message: transports.EmailMessage,
) -> None:
    app = Application()
    transport = transports.OutlookComTransport(com_factory=lambda: app)
    stored = replace(
        message,
        to="pfc-edited@example.test",
        cc="pfc-team@example.test",
        subject="Updated in PFC",
        html_body="<p>Stored PFC content</p>",
        attachments=("/updated-jd.txt",),
        approved=True,
    )
    try:
        transport.create_draft(message)

        assert transport.send(stored).status == "sent"
        mail = app.created[0]
        assert (mail.To, mail.CC, mail.Subject, mail.HTMLBody) == (
            stored.to,
            stored.cc,
            stored.subject,
            stored.html_body,
        )
        assert mail.paths == list(stored.attachments)
        assert mail.sent == 1
    finally:
        transport.close()


@pytest.mark.parametrize("attachments", [("/replacement.docx", "/new.txt"), ()])
def test_regeneration_updates_unsent_draft_only(
    message: transports.EmailMessage, attachments: tuple[str, ...]
) -> None:
    """Regeneration replaces Draft content, but Sent is terminal (REQ-EML-02/06, TDD §10.4)."""
    app = Application()
    transport = transports.OutlookComTransport(com_factory=lambda: app)
    regenerated = replace(
        message,
        to="regenerated@example.test",
        cc="regenerated-team@example.test",
        subject="Regenerated subject",
        html_body="<p>Regenerated body</p>",
        attachments=attachments,
    )
    try:
        transport.create_draft(message)
        mail = app.created[0]
        assert transport.create_draft(regenerated, display=True).status == "displayed"
        assert (mail.To, mail.CC, mail.Subject, mail.HTMLBody) == (
            regenerated.to,
            regenerated.cc,
            regenerated.subject,
            regenerated.html_body,
        )
        assert mail.paths == list(attachments)
        assert mail.key == message.idempotency_key
        assert len(app.created) == 1
        assert mail.saved == 2
        assert mail.sent == 0
        transport.send(replace(regenerated, approved=True))
        assert transport.create_draft(message).status == "sent"
    finally:
        transport.close()

    restarted = transports.OutlookComTransport(com_factory=lambda: app)
    try:
        assert restarted.create_draft(message, display=True).status == "sent"
        assert (mail.To, mail.CC, mail.Subject, mail.HTMLBody) == (
            regenerated.to,
            regenerated.cc,
            regenerated.subject,
            regenerated.html_body,
        )
        assert mail.paths == list(attachments)
        assert mail.saved == 3
        assert len(app.created) == mail.sent == mail.displayed == 1
    finally:
        restarted.close()


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
