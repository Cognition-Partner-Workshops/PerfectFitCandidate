"""REQ-ING-04: configuration only in Phase 1; never auto-commit uploads."""

from pathlib import Path

from pydantic import BaseModel, Field


class WatchFolderSource(BaseModel):
    path: Path
    dl_glob: str = "*DL*.xlsx"
    ct_glob: str = "*CT*.xlsx"
    poll_minutes: int = Field(default=5, gt=0)
    enabled: bool = False

    def poll(self) -> list[Path]:
        if self.enabled:
            raise NotImplementedError("Watch folder auto-pickup is available in Phase 2 only")
        return []
