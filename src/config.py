"""Конфігурація: config.yaml (дефолти) + .env (секрети).

Значення, змінені через команди бота, зберігаються в таблиці settings
у SQLite і при старті перекривають config.yaml.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    telegram_bot_token: str


class SamplingConfig(BaseModel):
    delta_pct: float = 0.1
    interval_sec: int = 60
    min_record_gross_pct: float = 0.5  # писати лише спреди від цього значення, %
    liquid_only: bool = True           # писати лише пари, що пройшли фільтр обсягу


class AppConfig(BaseModel):
    threshold_pct: float = 2.0
    min_volume_musd: float = 20.0
    cooldown_min: int = 15
    close_factor: float = 0.5
    max_quote_age_sec: float = 10.0
    fees_pct: dict[str, float] = {"Binance": 0.05, "OKX": 0.05}
    meta_refresh_sec: int = 300
    sampling: SamplingConfig = SamplingConfig()
    db_path: str = "data/spreads.db"

    # runtime-стан, керується ботом
    muted_bases: set[str] = set()

    @property
    def min_volume_usd(self) -> float:
        return self.min_volume_musd * 1_000_000

    def round_trip_fee_pct(self, exchange_a: str, exchange_b: str) -> float:
        """Сумарна комісія повного циклу: вхід і вихід на обох ногах."""
        return 2 * self.fees_pct.get(exchange_a, 0.05) + 2 * self.fees_pct.get(exchange_b, 0.05)


def load_config() -> AppConfig:
    path = PROJECT_ROOT / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return AppConfig(**(data or {}))
