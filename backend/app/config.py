"""Application settings, loaded from environment variables / the .env file."""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # The .env file normally lives in the repo root; backend/.env also works.
        env_file=(str(REPO_DIR / ".env"), str(BACKEND_DIR / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- admin login -------------------------------------------------------
    admin_username: str = "admin"
    # argon2 hash. Stored base64-encoded with a "b64:" prefix so the "$" signs
    # in the hash cannot be mangled by shells or docker compose.
    admin_password_hash: str = ""
    session_secret: str = ""
    session_max_age_s: int = 12 * 3600

    # --- encryption of broker credentials (Fernet key) ---------------------
    encryption_key: str = ""

    # --- server ------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8000
    database_url: str = f"sqlite+aiosqlite:///{(BACKEND_DIR / 'data' / 'tradecopier.db').as_posix()}"
    log_level: str = "INFO"
    # Browser origins allowed to open the WebSocket (besides same-origin requests).
    allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:8000", "http://127.0.0.1:8000",
                                 "http://localhost:5173", "http://127.0.0.1:5173"]
    )

    # --- copier / simulator --------------------------------------------------
    seed_demo_data: bool = True
    sim_random_walk: bool = True
    sim_tick_interval_s: float = 0.5
    reconcile_interval_s: float = 3.0

    @field_validator("admin_password_hash")
    @classmethod
    def _decode_hash(cls, value: str) -> str:
        value = value.strip()
        if value.startswith("b64:"):
            return base64.b64decode(value[4:]).decode()
        return value

    def missing(self) -> list[str]:
        problems = []
        if not self.admin_password_hash:
            problems.append("ADMIN_PASSWORD_HASH")
        if not self.session_secret:
            problems.append("SESSION_SECRET")
        if not self.encryption_key:
            problems.append("ENCRYPTION_KEY")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
