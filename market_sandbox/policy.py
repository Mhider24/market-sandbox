from dataclasses import dataclass
from typing import Literal

from .forecast import BPS_PER_UNIT, Forecast

MIN_FORECAST_BPS = 10.0


@dataclass(frozen=True)
class Candidate:
    symbol: str
    sector: str
    forecast: Forecast


@dataclass(frozen=True)
class Decision:
    symbol: str
    action: Literal["eligible", "hold"]
    reason: str
    max_buy_cents: int = 0


def check_candidate(
    candidate: Candidate,
    *,
    cash_cents: int,
    portfolio_cents: int,
    position_cents: int,
    sector_cents: int,
    order_budget_cents: int,
    max_stock_bps: int,
    max_sector_bps: int,
    excluded_sectors: set[str],
) -> Decision:
    symbol = candidate.symbol
    if candidate.sector in excluded_sectors:
        return Decision(symbol, "hold", "Sector excluded by the profile.")

    sector_room = portfolio_cents * max_sector_bps // BPS_PER_UNIT - sector_cents
    if sector_room <= 0:
        return Decision(symbol, "hold", "Sector exposure has reached its limit.")

    stock_room = portfolio_cents * max_stock_bps // BPS_PER_UNIT - position_cents
    if stock_room <= 0:
        return Decision(symbol, "hold", "Stock exposure has reached its limit.")

    max_buy = min(cash_cents, order_budget_cents, stock_room, sector_room)
    if max_buy <= 0:
        return Decision(symbol, "hold", "No virtual cash or order budget is available.")

    if not candidate.forecast.beats_baseline:
        return Decision(symbol, "hold", "Ridge did not beat the zero-return baseline.")

    if candidate.forecast.next_log_return_bps < MIN_FORECAST_BPS:
        return Decision(symbol, "hold", "Forecast is below the 10 basis point threshold.")

    return Decision(symbol, "eligible", "Forecast and portfolio limits passed.", max_buy)
