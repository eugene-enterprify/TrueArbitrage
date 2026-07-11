"""Точка входу: запускає адаптери бірж, розрахунок спредів, бота і запис у базу."""
from __future__ import annotations

import asyncio
import logging
import sys

from src.alert_engine import AlertEngine
from src.config import Secrets, load_config
from src.exchanges.binance import BinanceAdapter
from src.exchanges.okx import OkxAdapter
from src.spread_engine import SpreadEngine
from src.storage import Storage
from src.telegram_bot import TelegramService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("aiogram").setLevel(logging.WARNING)
log = logging.getLogger("main")

TICK_SEC = 1.0  # частота перерахунку спредів


async def apply_saved_settings(config, storage: Storage) -> int | None:
    """Налаштування, змінені ботом раніше, мають пріоритет над config.yaml."""
    saved = await storage.get_settings()
    if "threshold_pct" in saved:
        config.threshold_pct = float(saved["threshold_pct"])
    if "min_volume_musd" in saved:
        config.min_volume_musd = float(saved["min_volume_musd"])
    if "muted_bases" in saved and saved["muted_bases"]:
        config.muted_bases = set(saved["muted_bases"].split(","))
    return int(saved["chat_id"]) if "chat_id" in saved else None


async def spread_loop(engine, alert_engine, storage, telegram, config) -> None:
    while True:
        await asyncio.sleep(TICK_SEC)
        try:
            views = engine.compute_all()
            if not views:
                continue
            events = alert_engine.process(views)
            for event in events:
                await telegram.send_alert(event)
            await storage.record_spreads(views, config.sampling)
        except Exception:
            log.exception("Помилка в циклі розрахунку спредів")


async def main() -> None:
    config = load_config()
    secrets = Secrets()

    storage = Storage(config.db_path)
    await storage.open()
    saved_chat_id = await apply_saved_settings(config, storage)

    adapters = [BinanceAdapter(), OkxAdapter()]
    log.info("Завантаження списків інструментів...")
    await asyncio.gather(*(a.load_symbols() for a in adapters))

    engine = SpreadEngine(adapters, config)
    engine.build_common_bases()
    log.info("Спільних монет: %d", len(engine.common_bases))

    alert_engine = AlertEngine(config)
    telegram = TelegramService(
        secrets.telegram_bot_token, config, storage, engine, alert_engine, adapters
    )
    telegram.chat_id = saved_chat_id

    for a in adapters:
        a.on_status = telegram.send_health

    log.info("Перше завантаження funding/обсягів...")
    await asyncio.gather(*(a.refresh_meta() for a in adapters))

    tasks = [
        asyncio.create_task(telegram.run_polling(), name="telegram"),
        asyncio.create_task(
            spread_loop(engine, alert_engine, storage, telegram, config), name="spreads"
        ),
    ]
    for a in adapters:
        tasks.append(asyncio.create_task(a.run_ws(), name=f"ws-{a.name}"))
        tasks.append(
            asyncio.create_task(a.run_meta_loop(config.meta_refresh_sec), name=f"meta-{a.name}")
        )

    log.info("Монітор запущено. Надішліть боту /start для реєстрації чату.")
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        for a in adapters:
            await a.close()
        await storage.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        # кирилиця в консолі Windows
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Зупинено")
