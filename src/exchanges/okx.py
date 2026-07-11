"""OKX USDT Swap: канал tickers, батч-підписка на всі інструменти."""
from __future__ import annotations

import asyncio
import json
import logging

import ccxt.async_support as ccxt
import websockets

from src.exchanges.base import ExchangeAdapter, split_base
from src.models import PairMeta, SymbolInfo

log = logging.getLogger(__name__)

WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
SUBSCRIBE_CHUNK = 100
# OKX закриває з'єднання після 30с тиші; ping-фрейми не рахуються — потрібен текстовий 'ping'
KEEPALIVE_SEC = 20


class OkxAdapter(ExchangeAdapter):
    name = "OKX"

    def __init__(self) -> None:
        super().__init__()
        self._rest = ccxt.okx({"enableRateLimit": True})
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
            existing = self.symbols.get(base)
            if existing is None or (existing.multiplier != 1.0 and mult == 1.0):
                self.symbols[base] = info
        self._by_id = {s.exchange_id: s for s in self.symbols.values()}
        log.info("OKX: %d USDT-перпетуалів", len(self.symbols))

    async def _connect_and_listen(self) -> None:
        async with websockets.connect(WS_URL, ping_interval=None) as ws:
            inst_ids = list(self._by_id.keys())
            for i in range(0, len(inst_ids), SUBSCRIBE_CHUNK):
                chunk = inst_ids[i : i + SUBSCRIBE_CHUNK]
                await ws.send(json.dumps({
                    "op": "subscribe",
                    "args": [{"channel": "tickers", "instId": x} for x in chunk],
                }))
            await self._set_connected(True)

            keepalive = asyncio.create_task(self._keepalive(ws))
            try:
                async for raw in ws:
                    if raw == "pong":
                        continue
                    msg = json.loads(raw)
                    if msg.get("event"):  # subscribe-підтвердження або error
                        if msg["event"] == "error":
                            log.warning("OKX WS error: %s", msg)
                        continue
                    for row in msg.get("data", []):
                        info = self._by_id.get(row.get("instId", ""))
                        if info is None:
                            continue
                        bid, ask = row.get("bidPx"), row.get("askPx")
                        if not bid or not ask:
                            continue
                        self._store_quote(info.base, float(bid), float(ask), info.multiplier)
            finally:
                keepalive.cancel()

    async def _keepalive(self, ws) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_SEC)
            await ws.send("ping")

    async def refresh_meta(self) -> None:
        tickers = await self._rest.fetch_tickers(params={"type": "swap"})
        fundings = await self._rest.fetch_funding_rates()
        for info in self.symbols.values():
            meta = self.meta.setdefault(info.base, PairMeta())
            t = tickers.get(info.ccxt_symbol)
            if t:
                vol = t.get("quoteVolume")
                if vol is None and t.get("baseVolume") is not None and t.get("last"):
                    vol = float(t["baseVolume"]) * float(t["last"])
                if vol is not None:
                    meta.volume_24h_usd = float(vol)
            f = fundings.get(info.ccxt_symbol)
            if f and f.get("fundingRate") is not None:
                meta.funding_rate = float(f["fundingRate"])

    async def close(self) -> None:
        await self._rest.close()
