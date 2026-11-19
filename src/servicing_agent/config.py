"""Runtime settings (env vars) and the YAML config files under config/."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def load_dotenv(path: Path | None = None) -> None:
    """Read KEY=VALUE lines from the repo's .env into os.environ (existing env vars win).

    Kept dependency-free on purpose. .env is git-ignored; secrets such as SARVAM_API_KEY live there.
    """
    path = path or REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("SA_DATA_DIR", REPO_ROOT / "data/generated")))
    database_url: str = field(default_factory=lambda: os.environ.get("SA_DATABASE_URL", "sqlite:///./servicing_agent.db"))
    otp_enabled: bool = field(default_factory=lambda: _bool("SA_OTP_ENABLED", True))
    otp_stub_code: str = field(default_factory=lambda: os.environ.get("SA_OTP_STUB_CODE", "123456"))
    helpline: str = field(default_factory=lambda: os.environ.get("SA_HELPLINE", "1800-000-0000"))
    planner: str = field(default_factory=lambda: os.environ.get("SA_PLANNER", "rules"))
    planner_model: str = field(default_factory=lambda: os.environ.get("SA_PLANNER_MODEL", "claude-opus-5-5"))
    settlement_window_hours: int = field(default_factory=lambda: int(os.environ.get("SA_SETTLEMENT_WINDOW_HOURS", "24")))
    # "stub": /simulate takes pre-extracted receipt/transcript hints (no network).
    # "sarvam": real media goes to Sarvam speech-to-text and Document AI (needs SARVAM_API_KEY).
    perception: str = field(default_factory=lambda: os.environ.get("SA_PERCEPTION", "stub"))
    # Where uploaded images and voice notes are kept (local stand-in for S3). Git-ignored: real borrower data.
    media_dir: Path = field(default_factory=lambda: Path(os.environ.get("SA_MEDIA_DIR", REPO_ROOT / "data/media")))
    max_otp_attempts: int = 3
    max_failed_verifications_per_sender_24h: int = 5
    restarts_flag_threshold_7d: int = 3


def get_settings() -> Settings:
    load_dotenv()
    return Settings()


@lru_cache
def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def routing_matrix() -> dict[str, dict]:
    return load_yaml("routing_matrix.yaml")["routes"]


def intent_fields() -> dict[str, list[str]]:
    return load_yaml("intent_fields.yaml")["intents"]


def policy_bands() -> dict:
    return load_yaml("policy_bands.yaml")
