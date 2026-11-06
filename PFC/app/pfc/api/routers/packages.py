"""Signed package HTTP boundary, mounted by the caller (REQ-OCC-01..03)."""

from __future__ import annotations

import base64
import hashlib
import zipfile
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Body, Depends, Request, Response
from fastapi import Path as PathParameter
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.types import Message

from pfc.api.schemas.packages import (
    ApplyRequest,
    CountItem,
    ExportRequest,
    ExportResponse,
    HistoryResponse,
    ModelApplyResponse,
    ModelPreviewResponse,
    OneTimePassword,
    PackageChange,
    PackageInfo,
    PendingResponse,
    ReembedResponse,
    SettingsPreviewResponse,
    SigningKeyResponse,
)
from pfc.security.dependencies import Principal, current_user, require_role
from pfc.security.package_signing import Manifest, key_id, serialise_public_key, verify_package
from pfc.services.packages.common import (
    PackageServiceError,
    active_package,
    app_version,
    package_lock,
    readonly_database,
)
from pfc.services.packages.export import export_settings
from pfc.services.packages.http_errors import package_errors, problem
from pfc.services.packages.importer import SettingsImporter, SettingsPreview
from pfc.services.packages.inbox import PackageInbox, PendingPackage
from pfc.services.packages.keys import SigningKeys
from pfc.services.packages.models import JobSubmitter, ModelApplyResult, ModelImporter, ModelPreview
from pfc.services.packages.staging import Kind, UploadStaging, upload_limit


class BoundedUploadRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            if not self.path.endswith(
                ("/settings/settings-package/preview", "/occ/embedding-models/preview")
            ):
                return await handler(request)
            current_user(request)
            kind: Kind = (
                "model" if self.path.endswith("/occ/embedding-models/preview") else "settings"
            )
            if kind == "model":
                require_role("Admin")(request)
            maximum = upload_limit(kind)
            length = request.headers.get("content-length")
            if length is not None:
                try:
                    size = int(length)
                except ValueError as exc:
                    raise problem("invalid_upload") from exc
                if size < 0:
                    raise problem("invalid_upload")
                if size > maximum:
                    raise problem("size_limit")
            received = 0

            async def receive() -> Message:
                nonlocal received
                message = await request.receive()
                received += len(message.get("body", b""))
                if received > maximum:
                    raise problem("size_limit")
                return message

            return await handler(Request(request.scope, receive=receive))

        return bounded


router = APIRouter(route_class=BoundedUploadRoute, tags=["packages"])
Authenticated = Annotated[Principal, Depends(current_user)]
Admin = Annotated[Principal, Depends(require_role("Admin"))]


def _now(request: Request) -> int:
    return int(request.app.state.clock().timestamp())


def _jobs(request: Request) -> JobSubmitter | None:
    return cast(JobSubmitter | None, getattr(request.app.state, "job_runner", None))


def _pending_id(item: PendingPackage) -> str:
    return hashlib.sha256(f"{item.path}\0{item.sha256}".encode()).hexdigest()


def _pending(request: Request, identifier: str, kind: Kind) -> tuple[PackageInbox, Path]:
    inbox = PackageInbox(request.app.state.settings, jobs=_jobs(request))
    item = next((item for item in inbox.list_pending() if _pending_id(item) == identifier), None)
    if item is None:
        raise PackageServiceError("not_pending", "Pending item not found")
    if item.kind != kind:
        raise PackageServiceError("wrong_kind", "Use the matching package endpoint")
    return inbox, Path(item.path)


async def _upload(
    request: Request, principal: Principal, kind: Kind
) -> tuple[str, SettingsPreview | ModelPreview, str]:
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise problem("upload_media_type")
    with package_errors():
        try:
            async with request.form(
                max_files=1, max_fields=0, max_part_size=upload_limit(kind)
            ) as form:
                upload = form.get("file")
                if len(form.multi_items()) != 1 or not isinstance(upload, UploadFile):
                    raise problem("invalid_upload")
                token, result = await run_in_threadpool(
                    UploadStaging(request.app.state.settings, clock=lambda: _now(request)).preview,
                    upload.file,
                    kind=kind,
                    user_id=principal.user.user_id,
                    session_id=principal.session.session_id,
                    now=_now(request),
                )
                return (
                    token,
                    result,
                    (upload.filename or "package").replace("\\", "/").rsplit("/", 1)[-1],
                )
        except HTTPException as exc:
            raise problem("invalid_upload") from exc


def _export_info(request: Request, path: Path) -> PackageInfo:
    with path.open("rb") as stream:
        with zipfile.ZipFile(stream) as archive:
            manifest = Manifest.model_validate_json(archive.read("manifest.json"))
        stream.seek(0)
        verified = verify_package(
            stream,
            trusted_keys=[SigningKeys(request.app.state.settings).get_public_key()],
            app_version=manifest.min_app_version or manifest.max_app_version or app_version(),
            current_version=0,
        )
    return PackageInfo(
        file_name=path.name,
        version=verified.manifest.package_version,
        created_at=verified.manifest.created_at,
        contents=[
            CountItem(label=name.split(".v", 1)[0].replace("_", " ").title(), count=len(rows))
            for name, rows in verified.settings_payloads.items()
        ],
    )


@router.get("/occ/settings-package", response_model=PackageInfo | None)
def last_export(request: Request, response: Response, principal: Admin) -> PackageInfo | None:
    response.headers["Cache-Control"] = "no-store"
    with package_errors(), package_lock(request.app.state.settings):
        paths = [
            path
            for path in (request.app.state.settings.data_dir / "exports").glob(
                "PFC-settings-v*.pfcpkg"
            )
            if path.stem.removeprefix("PFC-settings-v").isdigit()
        ]
        if not paths:
            return None
        latest = max(paths, key=lambda path: int(path.stem.removeprefix("PFC-settings-v")))
        return _export_info(request, latest)


@router.post("/occ/settings-package/export", response_model=ExportResponse)
def export(
    request: Request,
    response: Response,
    principal: Admin,
    body: Annotated[ExportRequest | None, Body()] = None,
) -> ExportResponse:
    """Admin only; OTPs are returned with this export response only (REQ-OCC-01)."""
    with package_errors():
        if body is None or body.passphrase is None:
            raise problem("passphrase_required")
        result = export_settings(
            request.app.state.settings,
            passphrase=body.passphrase,
            actor_id=principal.user.user_id,
            min_app_version=body.min_app_version,
            max_app_version=body.max_app_version,
        )
        response.headers["Cache-Control"] = "no-store"
        return ExportResponse(
            **_export_info(request, result.path).model_dump(by_alias=False),
            content_base64=base64.b64encode(result.path.read_bytes()).decode("ascii"),
            one_time_passwords=[
                OneTimePassword(email=email, otp=otp)
                for email, otp in result.one_time_passwords.items()
            ],
        )


@router.get("/occ/settings-package/signing-key", response_model=SigningKeyResponse)
def signing_key(request: Request, principal: Admin) -> SigningKeyResponse:
    with package_errors():
        public = SigningKeys(request.app.state.settings).get_public_key()
        return SigningKeyResponse(
            key_id=key_id(public),
            public_key_base64=base64.b64encode(serialise_public_key(public)).decode("ascii"),
        )


@router.post("/settings/settings-package/preview", response_model=SettingsPreviewResponse)
async def preview(request: Request, principal: Authenticated) -> SettingsPreviewResponse:
    token, result, file_name = await _upload(request, principal, "settings")
    assert isinstance(result, SettingsPreview)
    with readonly_database(request.app.state.settings) as db:
        active = active_package(db)
        installed_version = active["package_version"] if active else 0
    return SettingsPreviewResponse(
        preview_id=token,
        file_name=file_name,
        package_version=result.manifest.package_version,
        installed_version=installed_version,
        imported_accounts=result.imported_accounts,
        changes=[
            PackageChange(
                area=name, change=cast(Literal["added", "changed", "removed"], kind), item=item
            )
            for name, change in result.changes.items()
            for kind in ("added", "changed", "removed")
            for item in getattr(change, kind)
        ],
    )


@router.post("/settings/settings-package/import", response_model=list[HistoryResponse])
def apply(body: ApplyRequest, request: Request, principal: Authenticated) -> list[HistoryResponse]:
    with package_errors():
        if body.pending_id is not None:
            inbox, path = _pending(request, body.pending_id, "settings")
            result = inbox.apply_pending(path, actor_id=principal.user.user_id)
            assert isinstance(result, SettingsPreview)
        else:
            assert body.preview_id is not None
            result = UploadStaging(request.app.state.settings, clock=lambda: _now(request)).apply(
                body.preview_id,
                kind="settings",
                user_id=principal.user.user_id,
                session_id=principal.session.session_id,
                now=_now(request),
                operation=lambda path: SettingsImporter(request.app.state.settings).apply(
                    path, actor_id=principal.user.user_id
                ),
            )
        return history(request, principal)


@router.post("/settings/settings-package/rollback", response_model=list[HistoryResponse])
def rollback(request: Request, principal: Authenticated) -> list[HistoryResponse]:
    with package_errors():
        SettingsImporter(request.app.state.settings).rollback(actor_id=principal.user.user_id)
        return history(request, principal)


@router.get("/settings/settings-package/pending", response_model=list[PendingResponse])
def pending(request: Request, principal: Authenticated) -> list[PendingResponse]:
    with package_errors():
        return [
            PendingResponse(
                id=_pending_id(item),
                file_name=Path(item.path).name,
                kind=cast(Kind, item.kind),
                version=item.version,
                valid=item.valid,
                error=item.error,
            )
            for item in PackageInbox(request.app.state.settings).scan()
        ]


@router.post("/occ/embedding-models/preview", response_model=ModelPreviewResponse)
async def model_preview(request: Request, principal: Admin) -> ModelPreviewResponse:
    """OCC Admin model import: REQ-OCC-03 and TDD §12 route marked Adm."""
    token, result, _ = await _upload(request, principal, "model")
    assert isinstance(result, ModelPreview)
    return ModelPreviewResponse(
        preview_id=token,
        package_version=result.manifest.package_version,
        manifest=result.manifest,
        metadata=result.metadata,
    )


@router.post("/occ/embedding-models/import", response_model=ModelApplyResponse)
def model_apply(body: ApplyRequest, request: Request, principal: Admin) -> ModelApplyResponse:
    with package_errors():
        if body.pending_id is not None:
            inbox, path = _pending(request, body.pending_id, "model")
            result = inbox.apply_pending(path, actor_id=principal.user.user_id)
            assert isinstance(result, ModelApplyResult)
        else:
            assert body.preview_id is not None
            result = UploadStaging(request.app.state.settings, clock=lambda: _now(request)).apply(
                body.preview_id,
                kind="model",
                user_id=principal.user.user_id,
                session_id=principal.session.session_id,
                now=_now(request),
                operation=lambda path: ModelImporter(
                    request.app.state.settings, jobs=_jobs(request)
                ).apply(path, actor_id=principal.user.user_id),
            )
        return ModelApplyResponse(
            version=result.version, model_id=result.model_id, job_id=result.job_id
        )


@router.post("/occ/embedding-models/{version}/reembed", response_model=ReembedResponse)
def reembed(
    version: Annotated[int, PathParameter(ge=1)], request: Request, principal: Admin
) -> ReembedResponse:
    """Explicit Admin retry of committed model installation (REQ-OCC-03)."""
    with package_errors():
        job_id = ModelImporter(request.app.state.settings, jobs=_jobs(request)).retry_reembedding(
            version, actor_id=principal.user.user_id
        )
        return ReembedResponse(version=version, job_id=job_id)


@router.get("/settings/settings-package/history", response_model=list[HistoryResponse])
def history(request: Request, principal: Authenticated) -> list[HistoryResponse]:
    """Current and rolled-away package versions (REQ-OCC-02)."""
    with package_errors(), readonly_database(request.app.state.settings) as db:
        rolled_back = {
            row["entity_id"]
            for row in db.execute(
                "SELECT DISTINCT entity_id FROM audit_log WHERE action='settings_package.rollback' "
                "AND entity_type='settings_package'"
            )
        }
        return [
            HistoryResponse(
                version=row["package_version"],
                imported_at=row["imported_at"],
                imported_by=row["applied_by"],
                file_name=f"PFC-settings-v{row['package_version']}.pfcpkg",
                can_rollback=bool(row["is_active"] and row["previous_package_id"] is not None),
                is_current=bool(row["is_active"]),
                status="rolled_back" if str(row["package_version"]) in rolled_back else "applied",
            )
            for row in db.execute(
                "SELECT p.*, COALESCE(NULLIF(u.display_name, ''), u.email) AS applied_by "
                "FROM settings_packages p JOIN users u ON u.user_id=p.imported_by "
                "ORDER BY p.package_version DESC"
            )
        ]
