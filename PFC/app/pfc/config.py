from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PFC_")

    data_dir: Path = Path(__file__).resolve().parents[2] / "data"
    host: str = "127.0.0.1"
    port: int = 8765
