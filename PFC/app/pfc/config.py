import os
import sys
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LEGACY_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def default_data_dir(platform: str | None = None) -> Path:
    """REQ-AUTH-06 (S6): per-user profile storage, never inside the extracted package."""
    if (platform or sys.platform) == "win32":
        base, fallback = os.environ.get("LOCALAPPDATA"), Path.home() / "AppData" / "Local"
    else:
        base, fallback = os.environ.get("XDG_DATA_HOME"), Path.home() / ".local" / "share"
    root = Path(base) if base and Path(base).is_absolute() else fallback
    return root / "PFC" / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PFC_")

    data_dir: Path = Field(default_factory=lambda: default_data_dir())
    config_dir: Path = Path(__file__).resolve().parents[2] / "config"
    bundled_models_dir: Path = Field(
        default_factory=lambda data: data["config_dir"].parent / "models"
    )
    frontend_dir: Path = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    host: str = "127.0.0.1"
    port: int = 8765
    idle_timeout_min: int = 60
    absolute_timeout_h: int = 12
    lockout_threshold: int = 5
    lockout_minutes: int = 15
    argon_memory_cost: int = 65536
    argon_time_cost: int = 3
    argon_parallelism: int = 4
    allowed_hosts: list[str] = []
    job_workers: int = 4
    llm_enabled: bool = True
    llama_dir: Path = Path(__file__).resolve().parents[2] / "llama"
    models_dir: Path = Path(__file__).resolve().parents[2] / "models"
    llm_ctx_size: int = Field(default=4096, ge=1)
    llm_threads: int | None = Field(default=None, ge=1)
    llm_startup_timeout_s: float = Field(default=120, gt=0)
    llm_shutdown_timeout_s: float = Field(default=5, gt=0)

    @field_validator("host")
    @classmethod
    def loopback_only(cls, value: str) -> str:
        """REQ-AUTH-07: never expose the local service on external interfaces."""
        if value not in {"127.0.0.1", "localhost"}:
            raise ValueError("host must be 127.0.0.1 or localhost")
        return value
