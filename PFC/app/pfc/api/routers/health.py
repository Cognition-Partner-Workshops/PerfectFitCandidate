from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    """Liveness for the launcher; details are informational and never gate start-up."""
    encryption = getattr(request.app.state, "data_volume_encryption", "Unknown")
    return {"status": "ok", "details": {"dataVolumeEncryption": encryption}}
