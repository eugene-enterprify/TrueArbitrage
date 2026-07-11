"""Розрахунок спредів між усіма парами бірж."""
from __future__ import annotations

import logging
import time
from itertools import combinations

from src.config import AppConfig
from src.exchanges.base import ExchangeAdapter
from src.models import SpreadView

log = logging.getLogger(__name__)

# Якщо ціни двох ніг відрізняються більш ніж у стільки разів — це майже напевно
# різні монети з однаковим тикером (напр. ON на Binance і ON на OKX) або
# невраховане нормування. Пара назавжди виключається з розрахунку.
MISMATCH_RATIO = 2.0


class SpreadEngine:
    def __init__(self, adapters: list[ExchangeAdapter], config: AppConfig) -> None:
        self.adapters = adapters
        self.config = config
        self.common_bases: set[str] = set()
        self.mismatched_bases: set[str] = set()

    def build_common_bases(self) -> None:
        sets = [set(a.symbols.keys()) for a in self.adapters]
        self.common_bases = set.intersection(*sets) if sets else set()

    def compute_all(self) -> list[SpreadView]:
        """Поточний найкращий спред по кожній спільній монеті."""
        now = time.time()
        max_age = self.config.max_quote_age_sec
        views: list[SpreadView] = []

        for base in self.common_bases:
            best: SpreadView | None = None
            for a, b in combinations(self.adapters, 2):
                qa, qb = a.quotes.get(base), b.quotes.get(base)
                if qa is None or qb is None:
                    continue
                # захист від застарілих котирувань
                if now - qa.ts > max_age or now - qb.ts > max_age:
                    continue
                # захист від різних монет під однаковим тикером
                if base not in self.mismatched_bases:
                    ratio = max(qa.ask, qb.ask) / min(qa.ask, qb.ask)
                    if ratio > MISMATCH_RATIO:
                        self.mismatched_bases.add(base)
                        log.warning(
                            "%s: ціни різняться у %.0f разів (%s %.6g vs %s %.6g) — "
                            "пара виключена як різні активи",
                            base, ratio, a.name, qa.ask, b.name, qb.ask,
                        )
                if base in self.mismatched_bases:
                    continue

                # напрямок: long там, де нижчий ask; short там, де вищий bid
                for long_ex, long_q, short_ex, short_q in (
                    (a, qa, b, qb),
                    (b, qb, a, qa),
                ):
                    if long_q.ask <= 0:
                        continue
                    gross = (short_q.bid - long_q.ask) / long_q.ask * 100
                    if best is not None and gross <= best.gross_pct:
                        continue
                    fee = self.config.round_trip_fee_pct(long_ex.name, short_ex.name)
                    meta_long = long_ex.meta.get(base)
                    meta_short = short_ex.meta.get(base)
                    vol_long = meta_long.volume_24h_usd if meta_long else None
                    vol_short = meta_short.volume_24h_usd if meta_short else None
                    liquid = (
                        vol_long is not None
                        and vol_short is not None
                        and min(vol_long, vol_short) >= self.config.min_volume_usd
                    )
                    best = SpreadView(
                        base=base,
                        long_exchange=long_ex.name,
                        short_exchange=short_ex.name,
                        long_ask=long_q.ask,
                        short_bid=short_q.bid,
                        long_bid=long_q.bid,
                        short_ask=short_q.ask,
                        gross_pct=gross,
                        net_pct=gross - fee,
                        funding_long=meta_long.funding_rate if meta_long else None,
                        funding_short=meta_short.funding_rate if meta_short else None,
                        vol_long_usd=vol_long,
                        vol_short_usd=vol_short,
                        liquid=liquid,
                    )
            if best is not None:
                views.append(best)
        return views
