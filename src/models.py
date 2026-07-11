"""Спільні структури даних."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Quote:
    """Найкращі bid/ask, нормалізовані до ціни за 1 монету."""
    bid: float
    ask: float
    ts: float  # epoch-час отримання, для фільтра застарілих котирувань


@dataclass
class SymbolInfo:
    """Опис інструмента на конкретній біржі."""
    exchange_id: str      # нативний id, напр. 1000PEPEUSDT або PEPE-USDT-SWAP
    ccxt_symbol: str      # символ ccxt, напр. 1000PEPE/USDT:USDT
    base: str             # нормалізована монета без множника, напр. PEPE
    multiplier: float     # 1000 якщо котирування йде за 1000 монет (Binance 1000PEPE)


@dataclass
class PairMeta:
    """Метадані пари: фандинг і обсяг, оновлюються раз на кілька хвилин."""
    funding_rate: float | None = None   # десятковий дріб за інтервал, напр. 0.0001
    volume_24h_usd: float | None = None


@dataclass
class SpreadView:
    """Розрахований спред по одній монеті в найкращому напрямку."""
    base: str
    long_exchange: str    # де купуємо (нижчий ask)
    short_exchange: str   # де шортимо (вищий bid)
    long_ask: float
    short_bid: float
    long_bid: float       # ціна продажу лонга (закриття long-ноги)
    short_ask: float      # ціна відкупу шорта (закриття short-ноги)
    gross_pct: float      # брудний спред, %
    net_pct: float        # мінус 4 тейкер-комісії, %
    funding_long: float | None    # ставка фандингу на біржі long-ноги
    funding_short: float | None
    vol_long_usd: float | None
    vol_short_usd: float | None
    liquid: bool          # пройшов фільтр мінімального обсягу

    @property
    def funding_edge_pct(self) -> float | None:
        """Очікуваний фандинг-ефект позиції за інтервал, % (плюс — на нашу користь).

        Long платить фандинг (якщо ставка додатна), short — отримує.
        """
        if self.funding_long is None or self.funding_short is None:
            return None
        return (self.funding_short - self.funding_long) * 100


@dataclass
class OpenSpread:
    """Стан відкритого (поміченого) спреду для відстеження закриття."""
    view: SpreadView
    opened_ts: float
    max_gross_pct: float
    last_seen_ts: float = field(default=0.0)
