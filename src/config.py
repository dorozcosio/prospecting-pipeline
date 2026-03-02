"""
Load and validate YAML configs + .env, exposing a single Config dataclass.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent


@dataclass
class Settings:
    scholar_cache_days: int = 90
    haiku_batch_size: int = 40
    sonnet_batch_size: int = 15
    sonnet_call_delay: float = 13.0
    haiku_call_delay: float = 13.0
    scholar_delay_range: list = field(default_factory=lambda: [10, 30])
    scholar_backend: str = "serpapi"
    scholar_flush_interval: int = 25
    client_timeout_seconds: int = 120


@dataclass
class Institution:
    name: str
    domain: str


@dataclass
class Config:
    institutions: list
    settings: Settings
    google_service_account_key_path: str
    google_sheet_id: str
    anthropic_api_key: str
    serpapi_api_key: str


_config: Optional[Config] = None


def load_config() -> Config:
    global _config
    if _config is not None:
        return _config

    load_dotenv(ROOT / ".env")

    # Load institutions
    with open(ROOT / "config" / "institutions.yaml") as f:
        inst_data = yaml.safe_load(f)
    institutions = [
        Institution(name=i["name"], domain=i["domain"])
        for i in inst_data["institutions"]
    ]

    # Load settings, applying defaults for any missing keys
    settings = Settings()
    settings_path = ROOT / "config" / "settings.yaml"
    if settings_path.exists():
        with open(settings_path) as f:
            raw = yaml.safe_load(f) or {}
        for field_name in ("scholar_cache_days", "haiku_batch_size",
                           "sonnet_batch_size", "sonnet_call_delay", "haiku_call_delay",
                           "scholar_delay_range", "scholar_backend",
                           "scholar_flush_interval", "client_timeout_seconds"):
            if field_name in raw:
                setattr(settings, field_name, raw[field_name])

    missing = [k for k in ("GOOGLE_SERVICE_ACCOUNT_KEY_PATH", "GOOGLE_SHEET_ID",
                            "ANTHROPIC_API_KEY", "SERPAPI_API_KEY")
               if not os.getenv(k)]
    if missing:
        raise EnvironmentError(f"Missing required env vars: {', '.join(missing)}")

    _config = Config(
        institutions=institutions,
        settings=settings,
        google_service_account_key_path=os.environ["GOOGLE_SERVICE_ACCOUNT_KEY_PATH"],
        google_sheet_id=os.environ["GOOGLE_SHEET_ID"],
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        serpapi_api_key=os.environ["SERPAPI_API_KEY"],
    )
    return _config


# Convenience alias used across modules
get_config = load_config
