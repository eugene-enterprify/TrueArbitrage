"""Binance USDT-M Futures: усі bookTicker одним WS-стрімом."""
from __future__ import annotations

import json
import logging

import ccxt.async_support as ccxt
import websockets

from src.exchanges.base import ExchangeAdapter, split_base
from src.models import PairMeta, SymbolInfo

log = logging.getLogger(__name__)

WS_URL = "wss://fstream.binance.com/ws/!bookTicker"


class BinanceAdapter(ExchangeAdapter):
    name = "Binance"

    def __init__(self) -> None:
        super().__init__()
        self._rest = ccxt.binanceusdm({"enableRateLimit": True})
        self._by_id: dict[str, SymbolInfo] = {}

    async def load_symbols(self) -> None:
        markets = await self._rest.load_markets()
        for m in markets.values():
            if not (m.get("swap") and m.get("linear") and m.get("active")):
                continue
            if m.get("settle") != "USDT" or m.get("quote") != "USDT":
                continue
            base, mult = split_base(m["base"])
            info = SymbolInfo(
                exchange_id=m["id"], ccxt_symbol=m["symbol"], base=base, multiplier=mult
            )
            # при колізії (PEPE і 1000PEPE) лишаємо контракт без множника
            existing = self.symbols.get(base)
            if existing is None or (existing.multiplier != 1.0 and mult == 1.0):
                self.symbols[base] = info
        self._by_id = {s.exchange_id: s for s in self.symbols.values()}
        log.info("Binance: %d USDT-перпетуалів", len(self.symbols))

    async def _connect_and_listen(self) -> None:
        async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
            await self._set_connected(True)
            async for raw in ws:
                msg = json.loads(raw)
                info = self._by_id.get(msg.get("s", ""))
                if info is None:
                    continue
                self._store_quote(
                    info.base, float(msg["b"]), float(msg["a"]), info.multiplier
                )

    async def refresh_meta(self) -> None:
        ccxt_symbols = [s.ccxt_symbol for s in self.symbols.values()]
        tickers = await self._rest.fetch_tickers(ccxt_symbols)
        fundings = await self._rest.fetch_funding_rates()
        for info in self.symbols.values():
            meta = self.meta.setdefault(info.base, PairMeta())
            t = tickers.get(info.ccxt_symbol)
            if t and t.get("quoteVolume") is not None:
                meta.volume_24h_usd = float(t["quoteVolume"])
            f = fundings.get(info.ccxt_symbol)
            if f and f.get("fundingRate") is not None:
                meta.funding_rate = float(f["fundingRate"])

    async def close(self) -> None:
        await self._rest.close()
