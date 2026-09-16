"""Runtime settings, read once from environment variables."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(_env(name, str(default)))
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(_env(name, str(default)))
    except ValueError:
        value = default
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class Settings:
    app_password: str = field(default_factory=lambda: _env("APP_PASSWORD"))
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-2.5-flash"))
    fallback_model: str = field(default_factory=lambda: _env("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash-lite"))
    search_model: str = field(default_factory=lambda: _env("GEMINI_SEARCH_MODEL", "gemini-2.5-flash"))
    web_research: str = field(default_factory=lambda: _env("WEB_RESEARCH", "fallback"))  # fallback | always | off
    brain_mode: str = field(default_factory=lambda: _env("BRAIN_MODE", "deep"))  # deep | fast
    signoff: str = field(default_factory=lambda: _env("SIGNOFF"))
    # News older than this is never used. Capped at 90 days by design.
    max_news_age_days: int = field(default_factory=lambda: _env_int("MAX_NEWS_AGE_DAYS", 90, 7, 90))
    min_fit: float = field(default_factory=lambda: _env_float("MIN_FIT", 0.30, 0.0, 1.0))
    max_repairs: int = field(default_factory=lambda: _env_int("MAX_REPAIRS", 1, 0, 2))
    gemini_min_interval: float = field(default_factory=lambda: _env_float("GEMINI_MIN_INTERVAL_SECONDS", 4.0, 0.0, 60.0))
    row_delay_seconds: float = field(default_factory=lambda: _env_float("ROW_DELAY_SECONDS", 4.0, 0.0, 120.0))
    # Vercel Hobby functions stop at 300 s; leave headroom for the sheet write.
    row_time_budget: float = field(default_factory=lambda: _env_float("ROW_TIME_BUDGET_SECONDS", 240.0, 60.0, 780.0))

    def service_account_info(self) -> dict | None:
        raw = _env("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not raw:
            b64 = _env("GOOGLE_SERVICE_ACCOUNT_B64")
            if b64:
                raw = base64.b64decode(b64).decode("utf-8")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON.") from exc


def get_settings() -> Settings:
    return Settings()


# Email length and shape limits (words).
LIMITS = {
    "subject_max_words": 5,
    "e1_min": 45, "e1_max": 150,
    "e2_min": 30, "e2_max": 90,
    "e3_max": 32,
    "e4_min": 18, "e4_max": 60,
    "sentence_max_words": 32,
}

MAX_SIGNALS = 8
BRITISH_SPELLING_COUNTRIES = {
    "united kingdom", "uk", "england", "scotland", "wales", "northern ireland", "ireland", "india",
    "australia", "new zealand", "singapore", "south africa", "united arab emirates", "uae",
    "pakistan", "malaysia", "nigeria", "kenya", "hong kong", "sri lanka", "bangladesh",
}
