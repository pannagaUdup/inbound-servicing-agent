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
def _load_dotenv() -> None:
    """Load REPO_ROOT/.env into os.environ (without overriding already-set vars).

    Keeps secrets (SA_WHATSAPP_TOKEN, ...) in a gitignored file instead of the repo.
    No dependency; a shell-exported var or one passed on the command line still wins.
    """
    envf = REPO_ROOT / ".env"
    if not envf.exists():
        return
    for raw in envf.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("SA_DATA_DIR", REPO_ROOT / "data/generated")))
    database_url: str = field(default_factory=lambda: os.environ.get("SA_DATABASE_URL", "sqlite:///./servicing_agent.db"))
    otp_enabled: bool = field(default_factory=lambda: _bool("SA_OTP_ENABLED", True))
    otp_stub_code: str = field(default_factory=lambda: os.environ.get("SA_OTP_STUB_CODE", "123456"))
    # Agent OTP: "auto" = real (Redis + email) whenever the live WhatsApp channel is configured,
    # stub otherwise; "real" / "stub" force it. The stub code must never work on a live channel.
    otp_mode: str = field(default_factory=lambda: os.environ.get("SA_OTP_MODE", "auto"))
    helpline: str = field(default_factory=lambda: os.environ.get("SA_HELPLINE", "1800-000-0000"))
    planner: str = field(default_factory=lambda: os.environ.get("SA_PLANNER", "rules"))
    # Payment reminders can only go to these numbers (comma separated); empty = numbers that have chatted with us.
    demo_reminder_numbers: str = field(default_factory=lambda: os.environ.get("SA_DEMO_REMINDER_NUMBERS", ""))
    # Where the agent reads loan data (accounts, contacts, payments): "csv" (data/generated) or "db" (Postgres,
    # filled by `make db-load`, re-read every SA_DATA_REFRESH_SECONDS so edits apply without a restart).
    data_source: str = field(default_factory=lambda: os.environ.get("SA_DATA_SOURCE", "csv"))
    data_refresh_seconds: float = field(default_factory=lambda: float(os.environ.get("SA_DATA_REFRESH_SECONDS", "5")))
    # Waivers / settlements as a last resort: encourage payment first, offer them only per the policy (HRD-03..08).
    hardship_ladder: bool = field(default_factory=lambda: _bool("SA_HARDSHIP_LADDER", True))
    # Verified session: ends after this many idle minutes, and in any case this long after verifying.
    session_idle_minutes: int = field(default_factory=lambda: int(os.environ.get("SA_SESSION_IDLE_MINUTES", "30")))
    session_max_minutes: int = field(default_factory=lambda: int(os.environ.get("SA_SESSION_MAX_MINUTES", "120")))
    # Per-node trace spans (input / output / timing / tool calls) for the Traces console. Off by default.
    tracing: bool = field(default_factory=lambda: _bool("SA_TRACING", False))
    planner_model: str = field(default_factory=lambda: os.environ.get("SA_PLANNER_MODEL", "claude-opus-5-5"))
    settlement_window_hours: int = field(default_factory=lambda: int(os.environ.get("SA_SETTLEMENT_WINDOW_HOURS", "24")))
    # A payment that isn't in the ledger (LMS) is given this long after the borrower sends it, then
    # rechecked once: still missing -> "couldn't confirm, escalation filed". Demo: 2 minutes.
    # Policy retrieval: "fastembed" (local, BAAI/bge-small-en-v1.5), "sarvam" (/v2/embeddings, needs access)
    # or "off" (keyword BM25 only). Hybrid score = alpha * BM25 + (1 - alpha) * cosine.
    embeddings: str = field(default_factory=lambda: os.environ.get("SA_EMBEDDINGS", "fastembed"))
    embedding_model: str = field(default_factory=lambda: os.environ.get("SA_EMBEDDING_MODEL", ""))
    # CLM-02: a payment not in our records is first double-checked with the borrower ("is this the right
    # screenshot?"); the window starts on YES. Off: the window starts straight away (older tests).
    claim_confirm_missing: bool = field(default_factory=lambda: os.environ.get("SA_CLAIM_CONFIRM_MISSING", "1")
                                        not in ("0", "false", "no"))
    # Optional override; the window itself is policy (clause CLM-02, wait_seconds).
    claim_window_seconds: int | None = field(default_factory=lambda: int(os.environ["SA_CLAIM_WINDOW_SECONDS"])
                                             if os.environ.get("SA_CLAIM_WINDOW_SECONDS") else None)
    # "stub": /simulate takes pre-extracted receipt/transcript hints (no network).
    # "sarvam": real media goes to Sarvam speech-to-text and Document AI (needs SARVAM_API_KEY).
    perception: str = field(default_factory=lambda: os.environ.get("SA_PERCEPTION", "stub"))
    # Translate-and-normalize for regional / code-mixed text: "off" or "sarvam" (needs SARVAM_API_KEY).
    normalizer: str = field(default_factory=lambda: os.environ.get("SA_NORMALIZER", "off"))
    normalizer_model: str = field(default_factory=lambda: os.environ.get("SA_NORMALIZER_MODEL", "sarvam-105b-conversations"))
    # Model that labels messages the keyword rules can't (intent classifier). Empty = the normalizer's models.
    # e.g. SA_INTENT_MODEL=deepseekv4.1-flash-1017 to test DeepSeek; SA_INTENT_FALLBACK_MODEL=none for no fallback.
    intent_model: str = field(default_factory=lambda: os.environ.get("SA_INTENT_MODEL", ""))
    intent_fallback_model: str = field(default_factory=lambda: os.environ.get("SA_INTENT_FALLBACK_MODEL", ""))
    normalizer_fallback_model: str = field(
        default_factory=lambda: os.environ.get("SA_NORMALIZER_FALLBACK_MODEL", "deepseekv4.1-flash-1017"))
    # Where uploaded images and voice notes are kept (local stand-in for S3). Git-ignored: real borrower data.
    media_dir: Path = field(default_factory=lambda: Path(os.environ.get("SA_MEDIA_DIR", REPO_ROOT / "data/media")))
    # Where generated PDFs (receipts, statements, quotes, NOCs) are written before upload to WhatsApp.
    docs_dir: Path = field(default_factory=lambda: Path(os.environ.get("SA_DOCS_DIR", REPO_ROOT / "data/docs")))
    # WhatsApp Cloud API (Meta Graph). Secrets come from the environment, never the repo.
    whatsapp_token: str = field(default_factory=lambda: os.environ.get("SA_WHATSAPP_TOKEN", ""))
    wa_phone_number_id: str = field(default_factory=lambda: os.environ.get("SA_WA_PHONE_NUMBER_ID", ""))
    wa_api_version: str = field(default_factory=lambda: os.environ.get("SA_WA_API_VERSION", "v25.0"))
    wa_verify_token: str = field(default_factory=lambda: os.environ.get("SA_WA_VERIFY_TOKEN", ""))
    graph_base_url: str = field(default_factory=lambda: os.environ.get("SA_GRAPH_BASE_URL", "https://graph.facebook.com"))
    # Public base URL the mock pay-link page is reachable at (must be https for WhatsApp). Falls back
    # to the ngrok static domain, then localhost for dev.
    public_base_url: str = field(default_factory=lambda: (
        os.environ.get("SA_PUBLIC_BASE_URL")
        or (f"https://{os.environ['NGROK_DOMAIN']}" if os.environ.get("NGROK_DOMAIN") else "http://localhost:8000")))
    # Async backbone (Redis Streams ingress + worker pool). Blank/unreachable -> sync fallback.
    redis_url: str = field(default_factory=lambda: os.environ.get("SA_REDIS_URL", "redis://localhost:6382/0"))
    workers: int = field(default_factory=lambda: int(os.environ.get("SA_WORKERS", "4")))
    batch_seconds: float = field(default_factory=lambda: float(os.environ.get("SA_BATCH_SECONDS", "3")))
    # At startup, queued messages older than this are dropped instead of answered late.
    queue_recover_max_age_s: float = field(default_factory=lambda: float(os.environ.get("SA_QUEUE_RECOVER_MAX_AGE_S", "600")))
    # Email OTP (inbound menu flow). No SMTP host -> MockEmail (logs the OTP) for dev.
    smtp_host: str = field(default_factory=lambda: os.environ.get("SA_SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.environ.get("SA_SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.environ.get("SA_SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.environ.get("SA_SMTP_PASSWORD", ""))
    smtp_tls: bool = field(default_factory=lambda: _bool("SA_SMTP_TLS", True))
    email_from: str = field(default_factory=lambda: os.environ.get("SA_EMAIL_FROM", "CreditNirvana <no-reply@creditnirvana.ai>"))
    email_domain: str = field(default_factory=lambda: os.environ.get("SA_EMAIL_DOMAIN", "example.com"))
    # Dev: route every OTP email to this real address (synthetic customer emails aren't real).
    otp_email_override: str = field(default_factory=lambda: os.environ.get("SA_OTP_EMAIL_OVERRIDE", ""))
    otp_ttl_seconds: int = field(default_factory=lambda: int(os.environ.get("SA_OTP_TTL_SECONDS", "600")))
    # Single ops user that every ticket and case is assigned to for now (one-queue model).
    ops_assignee: str = field(default_factory=lambda: os.environ.get("SA_OPS_ASSIGNEE", "ops@creditnirvana.ai"))
    max_otp_attempts: int = 3
    max_failed_verifications_per_sender_24h: int = 5
    restarts_flag_threshold_7d: int = 3


def offline(settings: "Settings") -> "Settings":
    """The same settings with every live integration off: no WhatsApp sends, no SMTP email,
    stub OTP. Tests, the eval harness and batch replays use this so a developer's .env (real
    tokens, SMTP, OTP inbox) is never touched by an automated run."""
    from dataclasses import replace
    return replace(settings, whatsapp_token="", wa_phone_number_id="", smtp_host="", otp_mode="stub", embeddings="off",
                   data_source="csv", demo_reminder_numbers="")


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
