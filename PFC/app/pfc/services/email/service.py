from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pfc.adapters.email.transports import (
    EmailMessage,
    EmailTransport,
    EmailTransportError,
    EmlHandoffTransport,
    build_eml,
)
from pfc.adapters.llm.providers import LLMProvider, PromptRequest
from pfc.config import Settings
from pfc.db import audit
from pfc.db.connection import database
from pfc.security.problems import Problem
from pfc.security.sessions import stamp
from pfc.services.pii import mask_emails_in

logger = logging.getLogger(__name__)
TEMPLATE_PATH = (
    Path(__file__).resolve().parents[4] / "templates" / "email" / "candidate_default.html"
)
EMAIL_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},
        "gaps": {"type": "string"},
        "pointers": {"type": "string"},
    },
    "required": ["analysis", "gaps", "pointers"],
    "additionalProperties": False,
}
UNRESOLVED_PLACEHOLDER = re.compile(r"\{[A-Za-z][A-Za-z0-9_]*\}")
SHORTLIST_POSITION_SQL = (
    "CASE WHEN mr.shortlisted=1 THEN (SELECT COUNT(*)+1 FROM match_results earlier "
    "WHERE earlier.run_id=mr.run_id AND earlier.demand_id=mr.demand_id "
    "AND earlier.shortlisted=1 AND (earlier.rank<mr.rank OR "
    "(earlier.rank=mr.rank AND earlier.result_id<mr.result_id))) END"
)


@dataclass(frozen=True)
class EmailDraft:
    message_id: int
    demand_id: int
    resource_id: int
    result_id: int | None
    run_id: int | None
    rank: int | None
    selected: bool
    to_address: str
    cc: tuple[str, ...]
    subject: str
    body_html: str
    status: str
    drafted_by_model: str | None
    created_at: str
    updated_at: str
    sent_at: str | None
    handoff_at: str | None
    last_error: str | None
    attachment_name: str | None


@dataclass(frozen=True)
class EmailDraftIssue:
    result_id: int
    resource_id: int
    code: str
    detail: str


@dataclass(frozen=True)
class EmailSendOutcome:
    message_id: int
    status: Literal["sent", "failed", "skipped"]
    detail: str | None = None


class _SafeValues(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class _EmailHTMLSanitizer(HTMLParser):
    allowed_tags = {"a", "b", "br", "em", "i", "li", "ol", "p", "strong", "u", "ul"}
    void_tags = {"br"}
    blocked_tags = {"iframe", "object", "script", "style", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[str] = []
        self.blocked_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.blocked_tags:
            self.blocked_depth += 1
            return
        if self.blocked_depth or tag not in self.allowed_tags:
            return
        if tag == "a":
            href = next((value for name, value in attrs if name == "href"), None)
            if href and _safe_link(href):
                self.parts.append(
                    f'<a href="{escape(href, quote=True)}" rel="noopener noreferrer">'
                )
            else:
                self.parts.append("<a>")
        else:
            self.parts.append(f"<{tag}>")
        if tag not in self.void_tags:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.blocked_tags:
            self.blocked_depth = max(0, self.blocked_depth - 1)
            return
        if self.blocked_depth or tag not in self.stack:
            return
        while self.stack:
            opened = self.stack.pop()
            self.parts.append(f"</{opened}>")
            if opened == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self.blocked_depth:
            self.parts.append(escape(data, quote=False))

    def finish(self) -> str:
        while self.stack:
            self.parts.append(f"</{self.stack.pop()}>")
        return "".join(self.parts)


def _safe_link(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme.lower() in {"http", "https", "mailto"}


def sanitize_email_html(value: str) -> str:
    parser = _EmailHTMLSanitizer()
    parser.feed(value)
    parser.close()
    return parser.finish()


def _plain_text(value: str | None) -> str:
    """REQ-OUT-03: stored evidence may hold emails masked under the old rule."""
    return mask_emails_in(_stored_text(value))


def _stored_text(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return value.strip()

    def flatten(item: Any) -> str:
        if isinstance(item, dict):
            values: list[str] = []
            for key, nested in item.items():
                text = flatten(nested)
                if text:
                    values.append(f"{key}: {text}")
            return "; ".join(values)
        if isinstance(item, list):
            return "; ".join(text for text in (flatten(entry) for entry in item) if text)
        if item is None:
            return ""
        return str(item)

    return flatten(parsed).strip()


def _html_text(value: str) -> str:
    return "<br>".join(escape(line, quote=False) for line in value.splitlines())


def _draft_from_row(row: sqlite3.Row) -> EmailDraft:
    cc_value = json.loads(row["cc_json"])
    cc = (
        tuple(item for item in cc_value if isinstance(item, str))
        if isinstance(cc_value, list)
        else ()
    )
    rank_value = row["result_rank"]
    shortlist_position = row["shortlist_position"]
    run_value = row["result_run_id"]
    attachment = row["jd_attachment_path"]
    return EmailDraft(
        message_id=int(row["message_id"]),
        demand_id=int(row["demand_id"]),
        resource_id=int(row["resource_id"]),
        result_id=int(row["result_id"]) if row["result_id"] is not None else None,
        run_id=int(run_value) if run_value is not None else None,
        rank=int(rank_value) if rank_value is not None else None,
        selected=bool(shortlist_position is not None and shortlist_position <= 4),
        to_address=str(row["to_address"]),
        cc=cc,
        subject=str(row["subject"]),
        body_html=str(row["body_html"]),
        status=str(row["status"]),
        drafted_by_model=row["drafted_by_model"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        sent_at=row["sent_at"],
        handoff_at=row["handoff_at"],
        last_error=row["last_error"],
        attachment_name=Path(attachment).name if attachment else None,
    )


class EmailService:
    def __init__(
        self,
        settings: Settings,
        transport: EmailTransport,
        llm_provider: LLMProvider | None,
        clock: Callable[[], datetime],
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.llm_provider = llm_provider
        self.clock = clock

    def list_drafts(
        self, *, status: str | None, demand_id: int | None, limit: int
    ) -> tuple[list[EmailDraft], dict[str, int], int]:
        conditions: list[str] = []
        parameters: list[object] = []
        if status is not None:
            conditions.append("em.status=?")
            parameters.append(status)
        if demand_id is not None:
            conditions.append("em.demand_id=?")
            parameters.append(demand_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        demand_where = "WHERE demand_id=?" if demand_id is not None else ""
        with database(self.settings) as db:
            rows = db.execute(
                f"SELECT em.*, mr.run_id AS result_run_id, mr.rank AS result_rank, "
                f"{SHORTLIST_POSITION_SQL} AS shortlist_position "
                f"FROM email_messages em LEFT JOIN match_results mr ON mr.result_id=em.result_id "
                f"{where} ORDER BY em.created_at DESC, em.message_id DESC LIMIT ?",
                (*parameters, limit),
            ).fetchall()
            count_parameters: tuple[object, ...] = (demand_id,) if demand_id is not None else ()
            counts = {
                str(row["status"]): int(row["count"])
                for row in db.execute(
                    f"SELECT status, count(*) AS count FROM email_messages {demand_where} "
                    "GROUP BY status",
                    count_parameters,
                )
            }
        return (
            [_draft_from_row(row) for row in rows],
            counts,
            counts.get("Sent", 0),
        )

    def get_draft(self, message_id: int) -> EmailDraft:
        with database(self.settings) as db:
            row = self._get_draft_row(db, message_id)
        if row is None:
            raise Problem(404, "EMAIL_DRAFT_NOT_FOUND", "Email draft not found")
        return _draft_from_row(row)

    async def create_drafts(
        self,
        *,
        run_id: int | None,
        demand_id: int | None,
        result_ids: Sequence[int] | None,
        user_id: int,
    ) -> tuple[list[EmailDraft], list[EmailDraftIssue]]:
        now = stamp(self.clock())
        with database(self.settings) as db:
            template = self._ensure_default_template(db, now)
            cc_addresses = self._resource_team(db)
            results = self._top_results(db, run_id=run_id, demand_id=demand_id)
        if not results:
            raise Problem(404, "MATCH_RESULTS_NOT_FOUND", "No match results found")
        if result_ids is not None:
            requested = set(result_ids)
            available = {int(row["result_id"]) for row in results}
            if requested - available:
                raise Problem(
                    422,
                    "EMAIL_RESULT_NOT_TOP_FIVE",
                    "Selected results must be in the top five for their demand",
                )
            results = [row for row in results if int(row["result_id"]) in requested]

        drafts: list[EmailDraft] = []
        issues: list[EmailDraftIssue] = []
        for result in results:
            raw_address = result["email_address"]
            result_id = int(result["result_id"])
            resource_id = int(result["resource_id"])
            if raw_address is None or not str(raw_address).strip():
                issues.append(
                    EmailDraftIssue(
                        result_id,
                        resource_id,
                        "missing_email",
                        "No candidate email is recorded in the resource data.",
                    )
                )
                continue

            existing = self._existing_pair(int(result["demand_id"]), resource_id)
            if existing is not None:
                drafts.append(existing)
                issues.append(
                    EmailDraftIssue(
                        result_id,
                        resource_id,
                        "already_exists",
                        "An email record already exists for this candidate and demand.",
                    )
                )
                continue

            sections, model = await self._draft_sections(result)
            subject, body_html = self._render(template, result, sections)
            attachment_rel, attachment_abs = self._write_jd_attachment(result)
            idempotency_key = self._idempotency_key(int(result["demand_id"]), resource_id, 1)
            created = False
            message_id: int | None = None
            with database(self.settings) as db:
                db.execute("BEGIN IMMEDIATE")
                cursor = db.execute(
                    "INSERT INTO email_messages (demand_id, resource_id, result_id, send_seq, "
                    "email_template_id, to_address, cc_json, subject, body_html, "
                    "jd_attachment_path, status, idempotency_key, drafted_by_model, created_by, "
                    "created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 'Draft', "
                    "?, ?, ?, ?, ?) ON CONFLICT(demand_id, resource_id, send_seq) DO NOTHING",
                    (
                        int(result["demand_id"]),
                        resource_id,
                        result_id,
                        int(template["email_template_id"]),
                        str(raw_address),
                        json.dumps(cc_addresses),
                        subject,
                        body_html,
                        attachment_rel,
                        idempotency_key,
                        model,
                        user_id,
                        now,
                        now,
                    ),
                )
                created = cursor.rowcount == 1
                if created:
                    message_id = int(cursor.lastrowid or 0)
                else:
                    row = db.execute(
                        "SELECT message_id FROM email_messages "
                        "WHERE demand_id=? AND resource_id=? AND send_seq=1",
                        (int(result["demand_id"]), resource_id),
                    ).fetchone()
                    message_id = int(row["message_id"]) if row else None
            if message_id is None:
                continue
            if created:
                try:
                    self.transport.create_draft(
                        EmailMessage(
                            idempotency_key,
                            str(raw_address),
                            "; ".join(cc_addresses),
                            subject,
                            body_html,
                            (str(attachment_abs),),
                        )
                    )
                except Exception:
                    logger.warning("Unable to create transport draft for message %s", message_id)
                    with database(self.settings) as db:
                        db.execute(
                            "UPDATE email_messages SET last_error=?, updated_at=? "
                            "WHERE message_id=?",
                            ("Transport draft creation failed", stamp(self.clock()), message_id),
                        )
                    issues.append(
                        EmailDraftIssue(
                            result_id,
                            resource_id,
                            "transport_draft_failed",
                            "The application draft was saved, but the Outlook draft "
                            "could not be created.",
                        )
                    )
            else:
                issues.append(
                    EmailDraftIssue(
                        result_id,
                        resource_id,
                        "already_exists",
                        "An email record already exists for this candidate and demand.",
                    )
                )
            drafts.append(self.get_draft(message_id))
        return drafts, issues

    def patch_draft(
        self,
        message_id: int,
        *,
        user_id: int,
        subject: str | None,
        body_html: str | None,
    ) -> EmailDraft:
        if subject is None and body_html is None:
            raise Problem(422, "EMAIL_EDIT_EMPTY", "Provide a subject or body to update")
        if subject is not None and ("\r" in subject or "\n" in subject):
            raise Problem(422, "EMAIL_SUBJECT_INVALID", "Subject cannot contain line breaks")
        safe_body = sanitize_email_html(body_html) if body_html is not None else None
        now = stamp(self.clock())
        with database(self.settings) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get_draft_row(db, message_id)
            if row is None:
                raise Problem(404, "EMAIL_DRAFT_NOT_FOUND", "Email draft not found")
            if row["status"] in {"Sent", "Sending", "Discarded"}:
                raise Problem(409, "EMAIL_DRAFT_NOT_EDITABLE", "Email draft cannot be edited")
            updated_status = "Draft" if row["status"] in {"Approved", "Failed"} else row["status"]
            cursor = db.execute(
                "UPDATE email_messages SET subject=COALESCE(?, subject), "
                "body_html=COALESCE(?, body_html), status=?, approved_by=NULL, approved_at=NULL, "
                "last_error=NULL, updated_at=? WHERE message_id=? "
                "AND status NOT IN ('Sent','Sending','Discarded')",
                (subject, safe_body, updated_status, now, message_id),
            )
            if cursor.rowcount == 0:
                raise Problem(409, "EMAIL_DRAFT_NOT_EDITABLE", "Email draft cannot be edited")
            audit.append_audit(
                db,
                ts=now,
                user_id=user_id,
                action="EMAIL_EDIT",
                entity_type="email_message",
                entity_id=str(message_id),
                details={
                    "fields": [
                        name
                        for name, value in (("subject", subject), ("body_html", body_html))
                        if value is not None
                    ]
                },
            )
            updated = self._get_draft_row(db, message_id)
        assert updated is not None
        return _draft_from_row(updated)

    async def regenerate(self, message_id: int, *, user_id: int) -> EmailDraft:
        with database(self.settings) as db:
            message = db.execute(
                "SELECT * FROM email_messages WHERE message_id=?", (message_id,)
            ).fetchone()
            if message is None:
                raise Problem(404, "EMAIL_DRAFT_NOT_FOUND", "Email draft not found")
            if message["status"] != "Draft":
                raise Problem(
                    409,
                    "EMAIL_DRAFT_NOT_REGENERATABLE",
                    "Only a draft email can be regenerated",
                )
            result = self._source_for_message(db, message)
            template = db.execute(
                "SELECT * FROM email_templates WHERE email_template_id=?",
                (message["email_template_id"],),
            ).fetchone()
            if template is None:
                raise Problem(409, "EMAIL_TEMPLATE_NOT_FOUND", "Email template not found")
        sections, model = await self._draft_sections(result)
        subject, body_html = self._render(template, result, sections)
        now = stamp(self.clock())
        with database(self.settings) as db:
            db.execute(
                "UPDATE email_messages SET subject=?, body_html=?, drafted_by_model=?, "
                "last_error=NULL, updated_at=? WHERE message_id=? AND status='Draft'",
                (subject, body_html, model, now, message_id),
            )
            audit.append_audit(
                db,
                ts=now,
                user_id=user_id,
                action="EMAIL_REGENERATE",
                entity_type="email_message",
                entity_id=str(message_id),
                details={"drafted_by_model": model},
            )
            updated = self._get_draft_row(db, message_id)
        assert updated is not None
        try:
            self.transport.create_draft(self._transport_message(updated))
        except Exception:
            logger.warning("Unable to refresh transport draft for message %s", message_id)
            with database(self.settings) as db:
                db.execute(
                    "UPDATE email_messages SET last_error=?, updated_at=? WHERE message_id=?",
                    ("Transport draft update failed", stamp(self.clock()), message_id),
                )
        return self.get_draft(message_id)

    def resend(self, message_id: int, *, user_id: int) -> EmailDraft:
        now = stamp(self.clock())
        created_id: int | None = None
        with database(self.settings) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT * FROM email_messages WHERE message_id=?", (message_id,)
            ).fetchone()
            if previous is None:
                raise Problem(404, "EMAIL_DRAFT_NOT_FOUND", "Email draft not found")
            if previous["status"] != "Sent":
                raise Problem(409, "EMAIL_NOT_SENT", "Only a sent email can be resent")
            send_seq = (
                int(
                    db.execute(
                        "SELECT MAX(send_seq) FROM email_messages "
                        "WHERE demand_id=? AND resource_id=?",
                        (previous["demand_id"], previous["resource_id"]),
                    ).fetchone()[0]
                )
                + 1
            )
            cursor = db.execute(
                "INSERT INTO email_messages (demand_id,resource_id,result_id,send_seq,"
                "email_template_id,to_address,cc_json,subject,body_html,jd_attachment_path,status,"
                "idempotency_key,drafted_by_model,created_by,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,'Draft',?,?,?,?,?)",
                (
                    previous["demand_id"],
                    previous["resource_id"],
                    previous["result_id"],
                    send_seq,
                    previous["email_template_id"],
                    previous["to_address"],
                    previous["cc_json"],
                    previous["subject"],
                    previous["body_html"],
                    previous["jd_attachment_path"],
                    self._idempotency_key(
                        int(previous["demand_id"]), int(previous["resource_id"]), send_seq
                    ),
                    previous["drafted_by_model"],
                    user_id,
                    now,
                    now,
                ),
            )
            created_id = int(cursor.lastrowid or 0)
            audit.append_audit(
                db,
                ts=now,
                user_id=user_id,
                action="EMAIL_RESEND_REQUEST",
                entity_type="email_message",
                entity_id=str(created_id),
                details={"previous_message_id": message_id, "send_seq": send_seq},
            )
        draft = self.get_draft(created_id)
        with database(self.settings) as db:
            row = self._get_draft_row(db, created_id)
        assert row is not None
        try:
            self.transport.create_draft(self._transport_message(row))
        except Exception:
            logger.warning("Unable to create resend draft for message %s", created_id)
        return draft

    def approve(self, message_id: int, *, user_id: int) -> EmailDraft:
        now = stamp(self.clock())
        with database(self.settings) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get_draft_row(db, message_id)
            if row is None:
                raise Problem(404, "EMAIL_DRAFT_NOT_FOUND", "Email draft not found")
            if row["status"] in {"Sent", "Sending", "Discarded"}:
                raise Problem(409, "EMAIL_NOT_APPROVABLE", "Email cannot be approved in this state")
            if not str(row["to_address"]).strip():
                raise Problem(409, "EMAIL_RECIPIENT_MISSING", "Candidate email is missing")
            if UNRESOLVED_PLACEHOLDER.search(str(row["subject"])) or UNRESOLVED_PLACEHOLDER.search(
                str(row["body_html"])
            ):
                raise Problem(
                    409,
                    "EMAIL_PLACEHOLDER_UNRESOLVED",
                    "Resolve all email template placeholders before approval",
                )
            cursor = db.execute(
                "UPDATE email_messages SET status='Approved', approved_by=?, approved_at=?, "
                "last_error=NULL, updated_at=? WHERE message_id=? "
                "AND status NOT IN ('Sent','Sending','Discarded')",
                (user_id, now, now, message_id),
            )
            if cursor.rowcount == 0:
                raise Problem(409, "EMAIL_NOT_APPROVABLE", "Email cannot be approved in this state")
            audit.append_audit(
                db,
                ts=now,
                user_id=user_id,
                action="EMAIL_APPROVE",
                entity_type="email_message",
                entity_id=str(message_id),
                details={"status": "Approved"},
            )
            updated = self._get_draft_row(db, message_id)
        assert updated is not None
        return _draft_from_row(updated)

    def handoff(self, message_id: int, user_id: int) -> tuple[EmailDraft, bool]:
        if not getattr(self.transport, "manual_send", False):
            raise Problem(
                409,
                "EMAIL_TRANSPORT_MISMATCH",
                "The active email transport does not support Outlook draft handoff",
            )

        now = stamp(self.clock())
        with database(self.settings) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get_draft_row(db, message_id)
            if row is None:
                raise Problem(404, "EMAIL_DRAFT_NOT_FOUND", "Email draft not found")

            repeat = row["status"] == "Sending" and row["handoff_at"] is not None
            if not repeat:
                if row["status"] in {"Sent", "Discarded"}:
                    raise Problem(409, "EMAIL_NOT_SENDABLE", "Email is not sendable")
                if row["status"] == "Sending":
                    raise Problem(
                        409,
                        "EMAIL_HANDOFF_NOT_ACTIVE",
                        "Email is sending through a different transport",
                    )
                if row["status"] == "Draft" or row["approved_by"] is None:
                    raise Problem(
                        409,
                        "EMAIL_NOT_APPROVED",
                        "Approve the email before opening an Outlook draft",
                    )
                if row["status"] not in {"Approved", "Failed"}:
                    raise Problem(409, "EMAIL_NOT_SENDABLE", "Email is not sendable")
                cursor = db.execute(
                    "UPDATE email_messages SET status='Sending', attempts=attempts+1, "
                    "handoff_at=?, last_error=NULL, transport=NULL, transport_ref=NULL, "
                    "updated_at=? WHERE message_id=? AND status IN ('Approved','Failed') "
                    "AND approved_by IS NOT NULL",
                    (now, now, message_id),
                )
                if cursor.rowcount == 0:
                    raise Problem(
                        409,
                        "EMAIL_NOT_SENDABLE",
                        "Email state changed before the Outlook draft was opened",
                    )

            audit.append_audit(
                db,
                ts=now,
                user_id=user_id,
                action="EMAIL_HANDOFF",
                entity_type="email_message",
                entity_id=str(message_id),
                details={"transport": "eml_handoff", "repeat": repeat},
            )
            updated = self._get_draft_row(db, message_id)

        assert updated is not None
        message = self._transport_message(updated, approved=True)
        path = self._handoff_path(message_id)
        transport = cast(EmlHandoffTransport, self.transport)
        try:
            transport.write_draft(message, path)
            opened = transport.open_draft(path)
        except (EmailTransportError, OSError) as exc:
            logger.warning(
                "Unable to create Outlook draft file for email_message_id=%s", message_id
            )
            raise Problem(
                503,
                "EMAIL_HANDOFF_FAILED",
                "PFC couldn't create the Outlook draft file. Choose Open again, or "
                "Not sent to return the email to Approved.",
            ) from exc
        return _draft_from_row(updated), opened

    def handoff_eml(self, message_id: int) -> bytes:
        with database(self.settings) as db:
            row = self._get_draft_row(db, message_id)
        if row is None:
            raise Problem(404, "EMAIL_DRAFT_NOT_FOUND", "Email draft not found")
        if row["status"] != "Sending" or row["handoff_at"] is None:
            raise Problem(
                409,
                "EMAIL_HANDOFF_NOT_ACTIVE",
                "Email does not have an active Outlook draft handoff",
            )
        try:
            return build_eml(self._transport_message(row, approved=True))
        except EmailTransportError as exc:
            logger.warning(
                "Unable to rebuild Outlook draft file for email_message_id=%s", message_id
            )
            raise Problem(
                503,
                "EMAIL_HANDOFF_FAILED",
                "PFC couldn't create the Outlook draft file. Choose Open again, or "
                "Not sent to return the email to Approved.",
            ) from exc

    def confirm_handoff(self, message_id: int, user_id: int, *, sent: bool) -> EmailDraft:
        now = stamp(self.clock())
        with database(self.settings) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get_draft_row(db, message_id)
            if row is None:
                raise Problem(404, "EMAIL_DRAFT_NOT_FOUND", "Email draft not found")
            if row["status"] != "Sending" or row["handoff_at"] is None:
                raise Problem(
                    409,
                    "EMAIL_HANDOFF_NOT_ACTIVE",
                    "Email does not have an active Outlook draft handoff",
                )

            if sent:
                db.execute(
                    "UPDATE email_messages SET status='Sent', sent_by=?, sent_at=?, "
                    "last_error=NULL, updated_at=? WHERE message_id=? AND status='Sending' "
                    "AND handoff_at IS NOT NULL",
                    (user_id, now, now, message_id),
                )
                action = "EMAIL_SENT"
                details = {
                    "status": "Sent",
                    "transport": "eml_handoff",
                    "confirmed_by_analyst": True,
                }
            else:
                db.execute(
                    "UPDATE email_messages SET status='Approved', handoff_at=NULL, "
                    "last_error=NULL, updated_at=? WHERE message_id=? AND status='Sending' "
                    "AND handoff_at IS NOT NULL",
                    (now, message_id),
                )
                action = "EMAIL_HANDOFF_CANCELLED"
                details = {"status": "Approved"}

            audit.append_audit(
                db,
                ts=now,
                user_id=user_id,
                action=action,
                entity_type="email_message",
                entity_id=str(message_id),
                details=details,
            )
            updated = self._get_draft_row(db, message_id)

        self._handoff_path(message_id).unlink(missing_ok=True)
        assert updated is not None
        return _draft_from_row(updated)

    def send_approved(
        self, *, user_id: int, message_ids: Sequence[int] | None = None
    ) -> list[EmailSendOutcome]:
        if message_ids is None:
            with database(self.settings) as db:
                rows = db.execute(
                    "SELECT message_id FROM email_messages "
                    "WHERE status IN ('Approved','Failed','Sending') ORDER BY message_id"
                ).fetchall()
            requested_ids = [int(row["message_id"]) for row in rows]
        else:
            requested_ids = list(dict.fromkeys(message_ids))

        outcomes: list[EmailSendOutcome] = []
        for message_id in requested_ids:
            with database(self.settings) as db:
                row = self._get_draft_row(db, message_id)
            if row is None:
                outcomes.append(EmailSendOutcome(message_id, "skipped", "Draft not found"))
                continue
            if row["status"] == "Sending" and row["handoff_at"] is not None:
                outcomes.append(
                    EmailSendOutcome(
                        message_id,
                        "skipped",
                        "Waiting for confirmation that the Outlook draft was sent",
                    )
                )
                continue
            if getattr(self.transport, "manual_send", False):
                outcomes.append(
                    EmailSendOutcome(
                        message_id,
                        "skipped",
                        "Use Open in Outlook for the draft file transport",
                    )
                )
                continue
            if row["status"] in {"Sent", "Discarded"}:
                outcomes.append(EmailSendOutcome(message_id, "skipped", "Email is not sendable"))
                continue
            if row["status"] == "Draft" or row["approved_by"] is None:
                outcomes.append(
                    EmailSendOutcome(message_id, "skipped", "Email has not been approved")
                )
                continue
            if row["status"] == "Sending":
                outcomes.append(self._reconcile_sending(row, user_id))
                continue
            if row["status"] not in {"Approved", "Failed"}:
                outcomes.append(EmailSendOutcome(message_id, "skipped", "Email is not sendable"))
                continue
            if row["status"] == "Failed":
                reconciled = self._reconcile_failed(row, user_id)
                if reconciled is not None:
                    outcomes.append(reconciled)
                    continue
                with database(self.settings) as db:
                    row = self._get_draft_row(db, message_id)
                if row is None:
                    outcomes.append(EmailSendOutcome(message_id, "skipped", "Draft not found"))
                    continue

            marked = self._mark_sending(message_id, user_id)
            if marked is None:
                outcomes.append(
                    EmailSendOutcome(message_id, "skipped", "Email state changed before sending")
                )
                continue
            try:
                result = self.transport.send(self._transport_message(marked, approved=True))
                if result.status != "sent":
                    raise EmailTransportError("Transport did not confirm the send")
            except Exception as exc:
                detail = str(exc) or "Email transport failed"
                self._finish_send(message_id, user_id, failed=detail)
                outcomes.append(EmailSendOutcome(message_id, "failed", detail))
            else:
                self._finish_send(message_id, user_id)
                outcomes.append(EmailSendOutcome(message_id, "sent"))
        return outcomes

    def _top_results(
        self, db: sqlite3.Connection, *, run_id: int | None, demand_id: int | None
    ) -> list[sqlite3.Row]:
        if run_id is None:
            if demand_id is None:
                raise Problem(422, "EMAIL_SCOPE_REQUIRED", "A run or demand is required")
            demand = db.execute("SELECT 1 FROM demands WHERE demand_id=?", (demand_id,)).fetchone()
            if demand is None:
                raise Problem(404, "DEMAND_NOT_FOUND", "Demand not found")
            run = db.execute(
                "SELECT mr.run_id FROM match_runs mr JOIN match_results res USING(run_id) "
                "WHERE res.demand_id=? AND mr.status='completed' "
                "ORDER BY mr.run_id DESC LIMIT 1",
                (demand_id,),
            ).fetchone()
            if run is None:
                raise Problem(404, "MATCH_RESULTS_NOT_FOUND", "No completed match results found")
            run_id = int(run["run_id"])
        else:
            run = db.execute("SELECT status FROM match_runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise Problem(404, "MATCH_RUN_NOT_FOUND", "Match run not found")
            if run["status"] != "completed":
                raise Problem(409, "MATCH_RUN_NOT_COMPLETE", "Match run is not complete")

        demand_clause = "AND mr.demand_id=?" if demand_id is not None else ""
        parameters: tuple[object, ...] = (run_id, demand_id) if demand_id is not None else (run_id,)
        rows = db.execute(
            "WITH profiles AS ("
            " SELECT mr.result_id, mr.run_id, mr.demand_id, mr.resource_id, "
            "COALESCE(mr.candidate_id, r.candidate_id) AS candidate_id, mr.score, mr.band, "
            "mr.rank, mr.matching_evidence, mr.gaps_checks, d.code_requisition, "
            "d.demand_request_name, r.email_address, r.short_name, c.display_name, "
            "dv.demand_version_id, dv.jd_text_raw, "
            "ROW_NUMBER() OVER (PARTITION BY mr.demand_id, "
            "COALESCE(mr.candidate_id, r.candidate_id, -mr.resource_id) "
            "ORDER BY mr.rank, mr.result_id) AS profile_position "
            "FROM match_results mr JOIN match_runs m ON m.run_id=mr.run_id "
            "JOIN demands d ON d.demand_id=mr.demand_id "
            "JOIN resources r ON r.resource_id=mr.resource_id "
            "LEFT JOIN candidates c ON c.candidate_id=COALESCE(mr.candidate_id,r.candidate_id) "
            "JOIN demand_versions dv ON dv.demand_version_id=mr.demand_version_id "
            "WHERE mr.run_id=? AND m.status='completed' AND mr.shortlisted=1 "
            f"{demand_clause}"
            "), shortlist AS ("
            " SELECT *, ROW_NUMBER() OVER (PARTITION BY demand_id ORDER BY rank, result_id) "
            "AS shortlist_position FROM profiles WHERE profile_position=1"
            ") SELECT * FROM shortlist WHERE shortlist_position<=5 "
            "ORDER BY demand_id, rank, result_id",
            parameters,
        ).fetchall()
        return list(rows)

    def _ensure_default_template(self, db: sqlite3.Connection, now: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM email_templates WHERE is_default=1 "
            "ORDER BY version DESC, email_template_id DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            return row
        subject = "Opportunity {requisition} – {role}"
        body_html = TEMPLATE_PATH.read_text(encoding="utf-8")
        cursor = db.execute(
            "INSERT INTO email_templates "
            "(name, version, subject, body_html, is_default, created_at) "
            "VALUES ('candidate_default', 1, ?, ?, 1, ?)",
            (subject, body_html, now),
        )
        row = db.execute(
            "SELECT * FROM email_templates WHERE email_template_id=?",
            (cursor.lastrowid,),
        ).fetchone()
        assert row is not None
        return row

    def _resource_team(self, db: sqlite3.Connection) -> list[str]:
        return [
            str(row["email"])
            for row in db.execute(
                "SELECT email FROM resource_team_recipients WHERE is_active=1 ORDER BY recipient_id"
            )
        ]

    async def _draft_sections(self, result: sqlite3.Row) -> tuple[dict[str, str], str]:
        fallback = self._fallback_sections(result)
        provider = self.llm_provider
        if provider is None:
            return fallback, "template"
        try:
            if not provider.capabilities().generate:
                return fallback, "template"
            request = PromptRequest(
                purpose="email_draft",
                template_id="email-default",
                template_version="1",
                json_schema=EMAIL_RESPONSE_SCHEMA,
                temperature=0,
                max_tokens=600,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Draft concise candidate-facing email sections using only the "
                            "provided match evidence, gaps, and job description. Do not invent "
                            "skills, experience, or qualifications. Return analysis, gaps, and "
                            "pointers as plain text."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Score: {result['score']} ({result['band']})\n"
                            f"Evidence: {_plain_text(result['matching_evidence'])}\n"
                            f"Gaps/checks: {_plain_text(result['gaps_checks'])}\n"
                            f"Job description: {result['jd_text_raw']}"
                        ),
                    },
                ],
            )
            generated = await provider.generate(request)
            value = generated.parsed_json
            if not isinstance(value, dict):
                value = json.loads(generated.text)
            if not all(
                isinstance(value.get(name), str) for name in ("analysis", "gaps", "pointers")
            ):
                raise ValueError("Email draft response is incomplete")
            return (
                {name: str(value[name]) for name in ("analysis", "gaps", "pointers")},
                f"{generated.served_by}:{generated.model}",
            )
        except Exception:
            logger.warning("Email LLM draft unavailable; using the deterministic template")
            return fallback, "template"

    def _fallback_sections(self, result: sqlite3.Row) -> dict[str, str]:
        evidence = _plain_text(result["matching_evidence"]) or "No match evidence was recorded."
        gaps = _plain_text(result["gaps_checks"]) or "No low-score gaps were recorded."
        pointers = (
            f"Add accurate profile or CV evidence for the following gaps: {gaps}."
            if gaps != "No low-score gaps were recorded."
            else (
                "Review the attached job description and ensure relevant skills and project "
                "experience already reflected in your profile are easy to find."
            )
        )
        return {
            "analysis": (
                f"Match score: {float(result['score']):.1f}% ({result['band']}). "
                f"Matching evidence: {evidence}"
            ),
            "gaps": gaps,
            "pointers": pointers,
        }

    def _render(
        self, template: sqlite3.Row, result: sqlite3.Row, sections: dict[str, str]
    ) -> tuple[str, str]:
        candidate_name = result["display_name"] or result["short_name"]
        subject_values = _SafeValues(
            candidate_name=str(candidate_name),
            requisition=str(result["code_requisition"]),
            role=str(result["demand_request_name"] or "the role"),
            match_pct=f"{float(result['score']):.1f}%",
            band=str(result["band"]),
            analysis=sections["analysis"],
            gaps=sections["gaps"],
            pointers=sections["pointers"],
            spoc_1="<SPOC-1>",
            spoc_2="<SPOC-2>",
        )
        body_values = _SafeValues(
            **{
                key: escape(value, quote=False)
                for key, value in subject_values.items()
                if key not in {"analysis", "gaps", "pointers"}
            },
            analysis=_html_text(sections["analysis"]),
            gaps=_html_text(sections["gaps"]),
            pointers=_html_text(sections["pointers"]),
        )
        with database(self.settings) as db:
            for key in ("email.spoc_1", "email.spoc_2"):
                row = db.execute(
                    "SELECT value_json FROM settings WHERE scope='system' AND user_id IS NULL "
                    "AND key=?",
                    (key,),
                ).fetchone()
                if row is not None:
                    try:
                        configured = json.loads(row["value_json"])
                    except (TypeError, ValueError):
                        configured = None
                    if isinstance(configured, str):
                        body_values[key.rsplit(".", 1)[1]] = escape(configured, quote=False)
                        subject_values[key.rsplit(".", 1)[1]] = configured
        subject = str(template["subject"]).format_map(subject_values)
        subject = " ".join(subject.splitlines())
        body = str(template["body_html"]).format_map(body_values)
        return subject, sanitize_email_html(body)

    def _write_jd_attachment(self, result: sqlite3.Row) -> tuple[str, Path]:
        relative = (
            Path("email")
            / "attachments"
            / f"demand-{int(result['demand_id'])}-version-{int(result['demand_version_id'])}.txt"
        )
        absolute = self.settings.data_dir / relative
        absolute.parent.mkdir(parents=True, exist_ok=True)
        if not absolute.exists():
            absolute.write_text(str(result["jd_text_raw"]), encoding="utf-8")
        return relative.as_posix(), absolute

    @staticmethod
    def _idempotency_key(demand_id: int, resource_id: int, send_seq: int) -> str:
        value = f"{demand_id}|{resource_id}|{send_seq}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _existing_pair(self, demand_id: int, resource_id: int) -> EmailDraft | None:
        with database(self.settings) as db:
            row = db.execute(
                f"SELECT em.*, mr.run_id AS result_run_id, mr.rank AS result_rank, "
                f"{SHORTLIST_POSITION_SQL} AS shortlist_position "
                "FROM email_messages em LEFT JOIN match_results mr ON mr.result_id=em.result_id "
                "WHERE em.demand_id=? AND em.resource_id=? AND em.send_seq=1",
                (demand_id, resource_id),
            ).fetchone()
        return _draft_from_row(row) if row else None

    def _get_draft_row(self, db: sqlite3.Connection, message_id: int) -> sqlite3.Row | None:
        return db.execute(
            f"SELECT em.*, mr.run_id AS result_run_id, mr.rank AS result_rank, "
            f"{SHORTLIST_POSITION_SQL} AS shortlist_position "
            "FROM email_messages em LEFT JOIN match_results mr ON mr.result_id=em.result_id "
            "WHERE em.message_id=?",
            (message_id,),
        ).fetchone()

    def _source_for_message(self, db: sqlite3.Connection, message: sqlite3.Row) -> sqlite3.Row:
        if message["result_id"] is None:
            raise Problem(409, "EMAIL_RESULT_NOT_FOUND", "Match result is unavailable")
        row = db.execute(
            "SELECT mr.result_id, mr.run_id, mr.demand_id, mr.resource_id, "
            "COALESCE(mr.candidate_id, r.candidate_id) AS candidate_id, mr.score, mr.band, "
            "mr.rank, mr.matching_evidence, mr.gaps_checks, d.code_requisition, "
            "d.demand_request_name, r.email_address, r.short_name, c.display_name, "
            "dv.demand_version_id, dv.jd_text_raw "
            "FROM match_results mr JOIN demands d ON d.demand_id=mr.demand_id "
            "JOIN resources r ON r.resource_id=mr.resource_id "
            "LEFT JOIN candidates c ON c.candidate_id=COALESCE(mr.candidate_id,r.candidate_id) "
            "JOIN demand_versions dv ON dv.demand_version_id=mr.demand_version_id "
            "WHERE mr.result_id=?",
            (message["result_id"],),
        ).fetchone()
        if row is None:
            raise Problem(409, "EMAIL_RESULT_NOT_FOUND", "Match result is unavailable")
        return row

    def _transport_message(self, row: sqlite3.Row, *, approved: bool = False) -> EmailMessage:
        cc_value = json.loads(row["cc_json"])
        cc = (
            [item for item in cc_value if isinstance(item, str)]
            if isinstance(cc_value, list)
            else []
        )
        attachment = row["jd_attachment_path"]
        attachments = (
            (str((self.settings.data_dir / str(attachment)).resolve()),) if attachment else ()
        )
        return EmailMessage(
            idempotency_key=str(row["idempotency_key"]),
            to=str(row["to_address"]),
            cc="; ".join(cc),
            subject=str(row["subject"]),
            html_body=str(row["body_html"]),
            attachments=attachments,
            approved=approved,
        )

    def _handoff_path(self, message_id: int) -> Path:
        return self.settings.data_dir / "email" / "outbox" / f"{message_id}.eml"

    def _mark_sending(self, message_id: int, user_id: int) -> sqlite3.Row | None:
        now = stamp(self.clock())
        with database(self.settings) as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "UPDATE email_messages SET status='Sending', attempts=attempts+1, "
                "transport=?, updated_at=? WHERE message_id=? AND status IN ('Approved','Failed') "
                "AND approved_by IS NOT NULL",
                (self._transport_kind(), now, message_id),
            )
            if not cursor.rowcount:
                return None
            audit.append_audit(
                db,
                ts=now,
                user_id=user_id,
                action="EMAIL_SEND",
                entity_type="email_message",
                entity_id=str(message_id),
                details={"phase": "sending"},
            )
            return self._get_draft_row(db, message_id)

    def _finish_send(self, message_id: int, user_id: int, *, failed: str | None = None) -> None:
        now = stamp(self.clock())
        with database(self.settings) as db:
            if failed is None:
                db.execute(
                    "UPDATE email_messages SET status='Sent', sent_by=?, sent_at=?, "
                    "last_error=NULL, updated_at=? WHERE message_id=? AND status='Sending'",
                    (user_id, now, now, message_id),
                )
                action = "EMAIL_SENT"
                details = {"status": "Sent"}
            else:
                db.execute(
                    "UPDATE email_messages SET status='Failed', last_error=?, updated_at=? "
                    "WHERE message_id=? AND status='Sending'",
                    (failed[:500], now, message_id),
                )
                action = "EMAIL_SEND_FAILED"
                details = {"status": "Failed", "error": failed[:200]}
            audit.append_audit(
                db,
                ts=now,
                user_id=user_id,
                action=action,
                entity_type="email_message",
                entity_id=str(message_id),
                details=details,
            )

    def _reconcile_failed(self, row: sqlite3.Row, user_id: int) -> EmailSendOutcome | None:
        message_id = int(row["message_id"])
        try:
            result = self.transport.reconcile(str(row["idempotency_key"]))
        except Exception as exc:
            detail = str(exc) or "Could not reconcile the previous send"
            self._record_reconcile_error(message_id, user_id, detail)
            return EmailSendOutcome(message_id, "failed", detail)
        if result.status == "sent":
            self._finish_reconciled_send(message_id, user_id)
            return EmailSendOutcome(message_id, "sent", "Previous send was confirmed")
        return None

    def _reconcile_sending(self, row: sqlite3.Row, user_id: int) -> EmailSendOutcome:
        message_id = int(row["message_id"])
        try:
            result = self.transport.reconcile(str(row["idempotency_key"]))
        except Exception as exc:
            detail = str(exc) or "Could not reconcile the previous send"
            self._record_reconcile_error(message_id, user_id, detail)
            return EmailSendOutcome(message_id, "failed", detail)
        now = stamp(self.clock())
        with database(self.settings) as db:
            if result.status == "sent":
                db.execute(
                    "UPDATE email_messages SET status='Sent', sent_by=?, sent_at=?, "
                    "last_error=NULL, updated_at=? WHERE message_id=? AND status='Sending'",
                    (user_id, now, now, message_id),
                )
                action = "EMAIL_SENT"
                detail = "Previous send was confirmed"
                audit_details = {"status": "Sent", "reconciled": True}
                outcome: Literal["sent", "failed", "skipped"] = "sent"
            else:
                db.execute(
                    "UPDATE email_messages SET status='Approved', last_error=?, updated_at=? "
                    "WHERE message_id=? AND status='Sending'",
                    ("Previous send was not found; retry requires a new request", now, message_id),
                )
                action = "EMAIL_SEND_RECONCILED"
                detail = "Previous send was not found; draft remains approved for retry"
                audit_details = {"status": "Approved", "reconciled": True}
                outcome = "skipped"
            audit.append_audit(
                db,
                ts=now,
                user_id=user_id,
                action=action,
                entity_type="email_message",
                entity_id=str(message_id),
                details=audit_details,
            )
        return EmailSendOutcome(message_id, outcome, detail)

    def _finish_reconciled_send(self, message_id: int, user_id: int) -> None:
        now = stamp(self.clock())
        with database(self.settings) as db:
            db.execute(
                "UPDATE email_messages SET status='Sent', sent_by=?, sent_at=?, "
                "last_error=NULL, updated_at=? WHERE message_id=? AND status='Failed'",
                (user_id, now, now, message_id),
            )
            audit.append_audit(
                db,
                ts=now,
                user_id=user_id,
                action="EMAIL_SENT",
                entity_type="email_message",
                entity_id=str(message_id),
                details={"status": "Sent", "reconciled": True},
            )

    def _record_reconcile_error(self, message_id: int, user_id: int, detail: str) -> None:
        now = stamp(self.clock())
        with database(self.settings) as db:
            db.execute(
                "UPDATE email_messages SET last_error=?, updated_at=? WHERE message_id=?",
                (detail[:500], now, message_id),
            )
            audit.append_audit(
                db,
                ts=now,
                user_id=user_id,
                action="EMAIL_SEND_RECONCILE_FAILED",
                entity_type="email_message",
                entity_id=str(message_id),
                details={"error": detail[:200]},
            )

    def _transport_kind(self) -> str | None:
        name = type(self.transport).__name__
        if name == "OutlookComTransport":
            return "outlook_com"
        if name == "SmtpTransport":
            return "smtp"
        return None
