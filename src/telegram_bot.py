"""Telegram-бот: сповіщення про спреди та команди керування."""
from __future__ import annotations

import logging
import time

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from src.alert_engine import AlertEngine, AlertEvent
from src.config import AppConfig
from src.exchanges.base import ExchangeAdapter
from src.models import SpreadView
from src.spread_engine import SpreadEngine
from src.storage import Storage

log = logging.getLogger(__name__)


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "?"
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    return f"${v / 1e6:.0f}M"


def _fmt_funding(v: float | None) -> str:
    return "?" if v is None else f"{v * 100:+.4f}%"


def _fmt_duration(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}г {m}хв" if h else (f"{m}хв {s}с" if m else f"{s}с")


def format_spread_message(v: SpreadView) -> str:
    lines = [
        f"🔔 <b>{v.base}/USDT</b> — спред <b>{v.gross_pct:.2f}%</b> (чистий {v.net_pct:.2f}%)",
        f"LONG  {v.long_exchange}: ask {v.long_ask:g}  funding {_fmt_funding(v.funding_long)}",
        f"SHORT {v.short_exchange}: bid {v.short_bid:g}  funding {_fmt_funding(v.funding_short)}",
        f"Обсяг 24h: {v.long_exchange} {_fmt_money(v.vol_long_usd)} / "
        f"{v.short_exchange} {_fmt_money(v.vol_short_usd)}",
    ]
    edge = v.funding_edge_pct
    if edge is not None:
        direction = "працює на вас" if edge > 0 else ("проти вас" if edge < 0 else "нейтральний")
        lines.append(f"Фандинг {direction}: {edge:+.4f}% за інтервал")
    return "\n".join(lines)


def format_close_message(event: AlertEvent) -> str:
    v = event.view
    return (
        f"✅ <b>{v.base}/USDT</b> — спред закрився (зараз {v.gross_pct:.2f}%)\n"
        f"Тривав {_fmt_duration(event.duration_sec or 0)}, "
        f"максимум {event.max_gross_pct:.2f}%"
    )


class TelegramService:
    def __init__(
        self,
        token: str,
        config: AppConfig,
        storage: Storage,
        engine: SpreadEngine,
        alert_engine: AlertEngine,
        adapters: list[ExchangeAdapter],
    ) -> None:
        self.bot = Bot(token=token)
        self.dp = Dispatcher()
        self.config = config
        self.storage = storage
        self.engine = engine
        self.alert_engine = alert_engine
        self.adapters = adapters
        self.chat_id: int | None = None
        self._register_handlers()

    # ---- вихідні повідомлення ----

    async def send(self, text: str, **kwargs) -> None:
        if self.chat_id is None:
            log.info("Чат ще не зареєстровано (/start), повідомлення пропущено")
            return
        try:
            await self.bot.send_message(self.chat_id, text, parse_mode="HTML", **kwargs)
        except Exception:
            log.exception("Не вдалося надіслати повідомлення в Telegram")

    async def send_alert(self, event: AlertEvent) -> None:
        if event.kind == "open":
            await self.send(format_spread_message(event.view))
        else:
            await self.send(format_close_message(event))

    async def send_health(self, exchange: str, connected: bool) -> None:
        icon = "🟢" if connected else "🔴"
        state = "відновлено" if connected else "втрачено"
        await self.send(f"{icon} З'єднання з {exchange} {state}")

    # ---- команди ----

    def _register_handlers(self) -> None:
        dp = self.dp

        @dp.message(Command("start"))
        async def cmd_start(message: Message) -> None:
            self.chat_id = message.chat.id
            await self.storage.set_setting("chat_id", str(message.chat.id))
            await message.answer(
                "Монітор арбітражу Binance–OKX запущено.\n"
                f"Поріг: {self.config.threshold_pct}% | "
                f"Мін. обсяг: ${self.config.min_volume_musd:.0f}M | "
                f"Cooldown: {self.config.cooldown_min} хв\n"
                "Команди: /status /top /threshold /minvolume /mute /unmute /export"
            )

        @dp.message(Command("status"))
        async def cmd_status(message: Message) -> None:
            lines = ["<b>Статус</b>"]
            for a in self.adapters:
                age = a.data_age_sec()
                age_s = "немає даних" if age is None else f"дані {age:.0f}с тому"
                icon = "🟢" if a.connected else "🔴"
                lines.append(f"{icon} {a.name}: {len(a.symbols)} пар, {age_s}")
            lines.append(f"Спільних монет: {len(self.engine.common_bases)}")
            lines.append(
                f"Поріг {self.config.threshold_pct}% | мін. обсяг ${self.config.min_volume_musd:.0f}M"
                f" | cooldown {self.config.cooldown_min} хв"
            )
            if self.config.muted_bases:
                lines.append("Mute: " + ", ".join(sorted(self.config.muted_bases)))
            lines.append(f"Записів у базі: {await self.storage.count_rows()}")
            open_now = self.alert_engine.open_spreads
            if open_now:
                lines.append("Відкриті спреди: " + ", ".join(
                    f"{b} {s.view.gross_pct:.2f}%" for b, s in sorted(open_now.items())
                ))
            await message.answer("\n".join(lines), parse_mode="HTML")

        @dp.message(Command("top"))
        async def cmd_top(message: Message) -> None:
            views = [v for v in self.engine.compute_all() if v.liquid]
            views.sort(key=lambda v: v.gross_pct, reverse=True)
            if not views:
                await message.answer("Немає даних (перевірте /status)")
                return
            lines = ["<b>Топ-10 спредів (з фільтром обсягу)</b>"]
            for v in views[:10]:
                lines.append(
                    f"{v.base}: {v.gross_pct:.2f}% (чистий {v.net_pct:.2f}%) "
                    f"long {v.long_exchange} / short {v.short_exchange}"
                )
            await message.answer("\n".join(lines), parse_mode="HTML")

        @dp.message(Command("threshold"))
        async def cmd_threshold(message: Message, command: CommandObject) -> None:
            if not command.args:
                await message.answer(f"Поточний поріг: {self.config.threshold_pct}%")
                return
            try:
                value = float(command.args.replace(",", "."))
                if not 0 < value <= 100:
                    raise ValueError
            except ValueError:
                await message.answer("Приклад: /threshold 2.5")
                return
            self.config.threshold_pct = value
            await self.storage.set_setting("threshold_pct", str(value))
            await message.answer(f"Поріг спреду: {value}%")

        @dp.message(Command("minvolume"))
        async def cmd_minvolume(message: Message, command: CommandObject) -> None:
            if not command.args:
                await message.answer(f"Поточний мін. обсяг: ${self.config.min_volume_musd:.0f}M")
                return
            try:
                value = float(command.args.replace(",", "."))
                if value < 0:
                    raise ValueError
            except ValueError:
                await message.answer("Приклад: /minvolume 20 (у млн $)")
                return
            self.config.min_volume_musd = value
            await self.storage.set_setting("min_volume_musd", str(value))
            await message.answer(f"Мін. обсяг 24h: ${value:.0f}M")

        @dp.message(Command("mute"))
        async def cmd_mute(message: Message, command: CommandObject) -> None:
            if not command.args:
                await message.answer("Приклад: /mute BTC")
                return
            base = command.args.strip().upper()
            self.config.muted_bases.add(base)
            await self.storage.set_setting("muted_bases", ",".join(sorted(self.config.muted_bases)))
            await message.answer(f"{base}: сповіщення вимкнено")

        @dp.message(Command("unmute"))
        async def cmd_unmute(message: Message, command: CommandObject) -> None:
            if not command.args:
                await message.answer("Приклад: /unmute BTC")
                return
            base = command.args.strip().upper()
            self.config.muted_bases.discard(base)
            await self.storage.set_setting("muted_bases", ",".join(sorted(self.config.muted_bases)))
            await message.answer(f"{base}: сповіщення увімкнено")

        @dp.message(Command("export"))
        async def cmd_export(message: Message, command: CommandObject) -> None:
            hours = 24.0
            if command.args:
                try:
                    hours = float(command.args)
                except ValueError:
                    await message.answer("Приклад: /export 24 (годин)")
                    return
            data, n = await self.storage.export_csv(hours)
            if n == 0:
                await message.answer("За цей період записів немає")
                return
            filename = f"spreads_{time.strftime('%Y%m%d_%H%M')}.csv"
            await message.answer_document(
                BufferedInputFile(data, filename=filename),
                caption=f"{n} записів за останні {hours:g} год",
            )

        @dp.message(F.text)
        async def fallback(message: Message) -> None:
            await message.answer("Невідома команда. Доступні: /status /top /threshold /minvolume /mute /unmute /export")

    async def run_polling(self) -> None:
        await self.dp.start_polling(self.bot, handle_signals=False)
