from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class MissingRevision(RuntimeError):
    """Raised when work is attempted without a pinned schedule version."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # A classification is only valid against a schedule version. Everything
    # downstream reads this, and it is stamped onto every trace and report.
    hts_revision: str = "unset"

    embedding_backend: str = "local"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    llm_model: str = "claude-opus-5-5"

    cost_ceiling_usd_per_request: float = 0.05

    anthropic_api_key: str | None = None
    openai_api_key: str | None = None

    project_root: Path = PROJECT_ROOT

    @property
    def data_dir(self) -> Path:
        return self.project_root / "data"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw" / self.require_revision()

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed" / self.require_revision()

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "manifests" / f"{self.require_revision()}.json"

    @property
    def reports_dir(self) -> Path:
        return self.project_root / "reports"

    @property
    def traces_dir(self) -> Path:
        return self.project_root / "traces"

    def require_revision(self) -> str:
        if self.hts_revision in ("", "unset"):
            raise MissingRevision(
                "HTS_REVISION is unset. Set it in .env to the release you downloaded "
                "(e.g. 2026HTSRev21) - a code without a schedule version is unusable."
            )
        return self.hts_revision

    def manifest(self) -> dict:
        path = self.manifest_path
        if not path.exists():
            raise FileNotFoundError(
                f"No manifest at {path}. Run the download step for {self.hts_revision} first."
            )
        return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
