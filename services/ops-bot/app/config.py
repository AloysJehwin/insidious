"""Settings for ops-bot. Everything comes from environment variables (names follow .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    """Env var (stripped). INSIDIOUS_<X> falls back to the legacy FESTIVEKIT_<X> when the new name is unset."""
    val = os.environ.get(name)
    if val is None and name.startswith("INSIDIOUS_"):
        val = os.environ.get("FESTIVEKIT_" + name[len("INSIDIOUS_"):])
    return (default if val is None else val).strip()


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def normalize_phone(raw: str | None) -> str:
    """E.164 without '+', digits only."""
    if not raw:
        return ""
    return "".join(ch for ch in str(raw) if ch.isdigit())


@dataclass
class Settings:
    # WhatsApp
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = "insidious-verify"
    whatsapp_app_secret: str = ""
    whatsapp_api_version: str = "v27.0"
    whatsapp_dry_run: bool = False
    whatsapp_base_url: str = "https://graph.facebook.com"
    dry_run_audio_dir: str = ""

    # Founder
    founder_name: str = "Ravi"
    founder_phone: str = ""
    founder_user_id: str = ""  # optional BSUID for username-only accounts
    founder_language: str = "ta-IN"
    founder_style: str = "tanglish"  # native | tanglish (Tamil in Latin letters mixed with English)

    # Sarvam
    sarvam_api_key: str = ""
    sarvam_base_url: str = "https://api.sarvam.ai"
    sarvam_stt_model: str = "saaras:v4"
    sarvam_tts_model: str = "bulbul:v4-flash"
    sarvam_llm_model: str = "sarvam-105b"
    sarvam_compose_model: str = "sarvam-105b-conversations"
    sarvam_translate_model: str = "sarvam-translate:v1"
    sarvam_mock: bool = False
    sarvam_rpm: int = 55
    sarvam_retry_base_s: float = 1.0
    sarvam_mock_transcript: str = "haan karo"
    sarvam_mock_language: str = "hi-IN"

    # NudgeBee
    nudgebee_url: str = "https://app.nudgebee.com"
    nudgebee_api_token: str = ""
    nudgebee_account_id: str = ""
    nudgebee_wf_incident_id: str = ""
    nudgebee_dry_run: bool = False

    # Flagsmith
    flagsmith_server_key: str = ""
    flagsmith_api_url: str = "https://edge.api.flagsmith.com/api/v1/"
    flagsmith_webhook_secret: str = ""

    # Wiring
    guardian_url: str = "http://guardian:8002"
    agent_url: str = "http://agent:8004"  # Claude ops agent (v2); empty disables plan decisions / ask
    agent_timeout_s: float = 90.0  # decision executes actions synchronously
    agent_ask_timeout_s: float = 60.0
    internal_token: str = ""
    data_dir: str = "./data"

    # Tunables (seconds) -- tests shrink these
    why_poll_interval_s: float = 3.0
    why_timeout_s: float = 180.0
    why_update_min_gap_s: float = 10.0
    why_max_updates: int = 5
    approval_resolve_attempts: int = 5
    approval_resolve_gap_s: float = 2.0

    extra: dict = field(default_factory=dict)

    @property
    def founder_identity(self) -> str:
        return f"founder:{self.founder_phone}"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            whatsapp_token=_env("WHATSAPP_TOKEN"),
            whatsapp_phone_number_id=_env("WHATSAPP_PHONE_NUMBER_ID"),
            whatsapp_verify_token=_env("WHATSAPP_VERIFY_TOKEN", "insidious-verify"),
            whatsapp_app_secret=_env("WHATSAPP_APP_SECRET"),
            whatsapp_api_version=_env("WHATSAPP_API_VERSION", "v27.0"),
            whatsapp_dry_run=_flag("WHATSAPP_DRY_RUN"),
            whatsapp_base_url=_env("WHATSAPP_BASE_URL", "https://graph.facebook.com").rstrip("/"),
            dry_run_audio_dir=_env("DRY_RUN_AUDIO_DIR"),
            founder_name=_env("FOUNDER_NAME", "Ravi"),
            founder_phone=normalize_phone(_env("FOUNDER_PHONE")),
            founder_user_id=_env("FOUNDER_USER_ID"),
            founder_language=_env("FOUNDER_LANGUAGE", "ta-IN") or "ta-IN",
            founder_style=_env("FOUNDER_STYLE", "tanglish").lower() or "tanglish",
            sarvam_api_key=_env("SARVAM_API_KEY"),
            sarvam_base_url=_env("SARVAM_BASE_URL", "https://api.sarvam.ai").rstrip("/"),
            sarvam_stt_model=_env("SARVAM_STT_MODEL", "saaras:v4") or "saaras:v4",
            sarvam_tts_model=_env("SARVAM_TTS_MODEL", "bulbul:v4-flash") or "bulbul:v4-flash",
            sarvam_llm_model=_env("SARVAM_LLM_MODEL", "sarvam-105b") or "sarvam-105b",
            sarvam_compose_model=_env("SARVAM_COMPOSE_MODEL", "sarvam-105b-conversations") or "sarvam-105b-conversations",
            sarvam_translate_model=_env("SARVAM_TRANSLATE_MODEL", "sarvam-translate:v1"),
            sarvam_mock=_flag("SARVAM_MOCK"),
            sarvam_rpm=int(_float("SARVAM_RPM", 55)),
            sarvam_retry_base_s=_float("SARVAM_RETRY_BASE_S", 1.0),
            sarvam_mock_transcript=_env("SARVAM_MOCK_TRANSCRIPT", "haan karo") or "haan karo",
            sarvam_mock_language=_env("SARVAM_MOCK_LANGUAGE", "hi-IN") or "hi-IN",
            nudgebee_url=_env("NUDGEBEE_URL", "https://app.nudgebee.com").rstrip("/"),
            nudgebee_api_token=_env("NUDGEBEE_API_TOKEN"),
            nudgebee_account_id=_env("NUDGEBEE_ACCOUNT_ID"),
            nudgebee_wf_incident_id=_env("NUDGEBEE_WF_INCIDENT_ID"),
            nudgebee_dry_run=_flag("NUDGEBEE_DRY_RUN"),
            flagsmith_server_key=_env("FLAGSMITH_SERVER_KEY"),
            flagsmith_api_url=_env("FLAGSMITH_API_URL", "https://edge.api.flagsmith.com/api/v1/"),
            flagsmith_webhook_secret=_env("FLAGSMITH_WEBHOOK_SECRET"),
            guardian_url=_env("GUARDIAN_URL", "http://guardian:8002").rstrip("/"),
            agent_url=_env("AGENT_URL", "http://agent:8004").rstrip("/"),
            agent_timeout_s=_float("AGENT_TIMEOUT_S", 90.0),
            agent_ask_timeout_s=_float("AGENT_ASK_TIMEOUT_S", 60.0),
            internal_token=_env("INSIDIOUS_INTERNAL_TOKEN"),
            data_dir=_env("DATA_DIR", "./data") or "./data",
            why_poll_interval_s=_float("WHY_POLL_INTERVAL_S", 3.0),
            why_timeout_s=_float("WHY_TIMEOUT_S", 180.0),
            why_update_min_gap_s=_float("WHY_UPDATE_MIN_GAP_S", 10.0),
        )
