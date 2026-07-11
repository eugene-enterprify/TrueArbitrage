"""Базовий інтерфейс адаптера біржі.

Нова біржа = новий файл з підкласом ExchangeAdapter, який реалізує
load_symbols(), _connect_and_listen() і refresh_meta(). Решта (reconnect,
health-колбеки, кеш котирувань) — спільна.
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from src.models import PairMeta, Quote, SymbolInfo

log = logging.getLogger(__name__)

# Префікси-множники в назвах контрактів (Binance: 1000PEPEUSDT = ціна за 1000 PEPE).
# Порядок важливий: довші префікси перевіряються першими.
_MULTIPLIER_PREFIXES: tuple[tuple[str, float], ...] = (
    ("1000000", 1e6),
    ("1M", 1e6),
    ("10000", 1e4),
    ("1000", 1e3),
)
# Реальні тикери, що починаються з цифр і НЕ є множниками
_LITERAL_BASES = {"1INCH", "1000X"}


def split_base(raw_base: str) -> tuple[str, float]:
    """'1000PEPE' -> ('PEPE', 1000.0); 'BTC' -> ('BTC', 1.0)."""
    if raw_base in _LITERAL_BASES:
        return raw_base, 1.0
    for prefix, mult in _MULTIPLIER_PREFIXES:
        if raw_base.startswith(prefix) and len(raw_base) > len(prefix):
            return raw_base[len(prefix):], mult
    return raw_base, 1.0


StatusCallback = Callable[[str, bool], Awaitable[None]]


class ExchangeAdapter(ABC):
    name: str = "?"

    def __init__(self) -> None:
        self.symbols: dict[str, SymbolInfo] = {}   # нормалізована монета -> інструмент
        self.quotes: dict[str, Quote] = {}         # монета -> bid/ask за 1 шт
        self.meta: dict[str, PairMeta] = {}        # монета -> funding/обсяг
        self.connected: bool = False
        self.last_msg_ts: float = 0.0
        self.on_status: StatusCallback | None = None

    # ---- реалізується підкласом ----

    @abstractmethod
    async def load_symbols(self) -> None:
        """Заповнити self.symbols списком активних USDT-перпетуалів."""

    @abstractmethod
    async def _connect_and_listen(self) -> None:
        """Одне WS-з'єднання: підписатись і обробляти повідомлення до розриву."""

    @abstractmethod
    async def refresh_meta(self) -> None:
        """Оновити funding rates і обсяги 24h (у USD) у self.meta."""

    # ---- спільна логіка ----

    async def run_ws(self) -> None:
        """Вічний цикл WS з reconnect та exponential backoff."""
        backoff = 1.0
        while True:
            started = time.monotonic()
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("%s: WS розрив: %r", self.name, exc)
            await self._set_connected(False)
            # якщо з'єднання жило довго — скидаємо backoff
            if time.monotonic() - started > 120:
                backoff = 1.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def run_meta_loop(self, interval_sec: int) -> None:
        while True:
            try:
                await self.refresh_meta()
                log.info("%s: метадані оновлено (%d пар)", self.name, len(self.meta))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("%s: помилка оновлення метаданих: %r", self.name, exc)
            await asyncio.sleep(interval_sec)

    async def _set_connected(self, value: bool) -> None:
        if value == self.connected:
            return
        self.connected = value
        log.info("%s: %s", self.name, "з'єднано" if value else "з'єднання втрачено")
        if self.on_status is not None:
            try:
                await self.on_status(self.name, value)
            except Exception:
                log.exception("%s: помилка status-колбека", self.name)

    def _store_quote(self, base: str, bid: float, ask: float, multiplier: float) -> None:
        if bid <= 0 or ask <= 0:
            return
        self.quotes[base] = Quote(bid=bid / multiplier, ask=ask / multiplier, ts=time.time())
        self.last_msg_ts = time.time()

    def data_age_sec(self) -> float | None:
        if self.last_msg_ts == 0:
            return None
        return time.time() - self.last_msg_ts
