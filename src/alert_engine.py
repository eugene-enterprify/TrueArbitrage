"""Логіка сповіщень: поріг, фільтр ліквідності, cooldown, відкриття/закриття спреду."""
from __future__ import annotations

import time
from dataclasses import dataclass

from src.config import AppConfig
from src.models import OpenSpread, SpreadView


@dataclass
class AlertEvent:
    kind: str  # "open" | "close"
    view: SpreadView
    duration_sec: float | None = None
    max_gross_pct: float | None = None


class AlertEngine:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.open_spreads: dict[str, OpenSpread] = {}
        self._last_alert_ts: dict[str, float] = {}

    def process(self, views: list[SpreadView]) -> list[AlertEvent]:
        now = time.time()
        events: list[AlertEvent] = []
        seen: set[str] = set()

        for v in views:
            seen.add(v.base)
            open_state = self.open_spreads.get(v.base)

            if open_state is None:
                if (
                    v.gross_pct >= self.config.threshold_pct
                    and v.liquid
                    and v.base not in self.config.muted_bases
                ):
                    self.open_spreads[v.base] = OpenSpread(
                        view=v, opened_ts=now, max_gross_pct=v.gross_pct, last_seen_ts=now
                    )
                    last = self._last_alert_ts.get(v.base, 0.0)
                    if now - last >= self.config.cooldown_min * 60:
                        self._last_alert_ts[v.base] = now
                        events.append(AlertEvent(kind="open", view=v))
            else:
                open_state.last_seen_ts = now
                open_state.view = v
                open_state.max_gross_pct = max(open_state.max_gross_pct, v.gross_pct)
                close_level = self.config.threshold_pct * self.config.close_factor
                if v.gross_pct <= close_level:
                    del self.open_spreads[v.base]
                    if v.base not in self.config.muted_bases:
                        events.append(AlertEvent(
                            kind="close",
                            view=v,
                            duration_sec=now - open_state.opened_ts,
                            max_gross_pct=open_state.max_gross_pct,
                        ))

        # монета зникла з розрахунку (застарілі котирування) понад 5 хв — прибираємо стан
        stale_cutoff = now - 300
        for base in list(self.open_spreads):
            if base not in seen and self.open_spreads[base].last_seen_ts < stale_cutoff:
                del self.open_spreads[base]

        return events
