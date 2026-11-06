from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PFC_")

    data_dir: Path = Path(__file__).resolve().parents[2] / "data"
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

    @field_validator("host")
    @classmethod
    def loopback_only(cls, value: str) -> str:
        """REQ-AUTH-07: never expose the local service on external interfaces."""
        if value not in {"127.0.0.1", "localhost"}:
            raise ValueError("host must be 127.0.0.1 or localhost")
        return value
