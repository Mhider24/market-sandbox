from collections import Counter
from dataclasses import asdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .forecast import BPS_PER_UNIT, MIN_CLOSES, forecast_next_close
from .policy import (
    DEFAULT_SECTOR_CAP_BPS,
    DEFAULT_STOCK_CAP_BPS,
    Candidate,
    check_candidate,
    concentration_room_cents,
    select_candidate,
)

app = FastAPI(title="Market Sandbox", version="0.1.0")
WORKSPACE_DIRECTORY = Path(__file__).with_name("web")
app.mount("/workspace-assets", StaticFiles(directory=WORKSPACE_DIRECTORY), name="workspace-assets")


@app.get("/", include_in_schema=False)
def trading_workspace() -> FileResponse:
    return FileResponse(WORKSPACE_DIRECTORY / "index.html")

Symbol = Annotated[
    str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=1, max_length=12)
]
Sector = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=1, max_length=40)
]
Cents = Annotated[int, Field(ge=0, strict=True)]
BasisPoints = Annotated[int, Field(ge=1, le=10_000, strict=True)]
Close = Annotated[float, Field(gt=0, strict=True, allow_inf_nan=False)]
PositiveCents = Annotated[int, Field(gt=0, strict=True)]
ClosingPrice = Annotated[int, Field(gt=0, lt=10_000_000_000, strict=True)]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Asset(InputModel):
    symbol: Symbol
    sector: Sector
    closes: list[Close] = Field(min_length=MIN_CLOSES, max_length=3000)


class Profile(InputModel):
    cash_cents: Cents
    order_budget_cents: Cents
    holdings_cents: dict[Symbol, Cents] = Field(default_factory=dict)
    preferred_sectors: set[Sector] = Field(default_factory=set)
    excluded_sectors: set[Sector] = Field(default_factory=set)
    max_stock_bps: BasisPoints = DEFAULT_STOCK_CAP_BPS
    max_sector_bps: BasisPoints = DEFAULT_SECTOR_CAP_BPS


class Simulation(InputModel):
    sessions: list[date] = Field(min_length=MIN_CLOSES, max_length=3000)
    assets: list[Asset] = Field(min_length=1, max_length=20)
    profile: Profile

    @model_validator(mode="after")
    def validate_market_snapshot(self) -> "Simulation":
        if self.sessions != sorted(set(self.sessions)):
            raise ValueError("Sessions must be unique and ordered from oldest to newest.")
        symbols = {asset.symbol for asset in self.assets}
        if len(symbols) != len(self.assets):
            raise ValueError("Asset symbols must be unique.")
        if self.profile.holdings_cents.keys() - symbols:
            raise ValueError("Every holding needs a matching asset in the snapshot.")
        if any(len(asset.closes) != len(self.sessions) for asset in self.assets):
            raise ValueError("Each asset must have one close per session.")
        if self.profile.preferred_sectors & self.profile.excluded_sectors:
            raise ValueError("A sector cannot be both preferred and excluded.")
        return self


class RiskAsset(InputModel):
    symbol: Symbol
    sector: Sector
    shares: Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=18, decimal_places=8)]
    close_cents: ClosingPrice


class PaperBuy(InputModel):
    symbol: Symbol
    amount_cents: PositiveCents


class RiskRequest(InputModel):
    as_of_session: date
    cash_cents: Cents
    assets: list[RiskAsset] = Field(default_factory=list, max_length=20)
    max_stock_bps: BasisPoints = DEFAULT_STOCK_CAP_BPS
    max_sector_bps: BasisPoints = DEFAULT_SECTOR_CAP_BPS
    paper_buy: PaperBuy | None = None

    @model_validator(mode="after")
    def validate_symbols(self) -> "RiskRequest":
        symbols = {asset.symbol for asset in self.assets}
        if len(symbols) != len(self.assets):
            raise ValueError("Asset symbols must be unique.")
        if self.paper_buy and self.paper_buy.symbol not in symbols:
            raise ValueError("The paper buy needs a matching asset with a closing price.")
        return self


@app.post("/simulate")
def simulate(request: Simulation) -> dict:
    profile = request.profile
    holdings = profile.holdings_cents
    portfolio_cents = profile.cash_cents + sum(holdings.values())
    sector_totals = Counter()
    for asset in request.assets:
        sector_totals[asset.sector] += holdings.get(asset.symbol, 0)

    assessments = []
    for asset in request.assets:
        candidate = Candidate(asset.symbol, asset.sector, forecast_next_close(asset.closes))
        decision = check_candidate(
            candidate,
            cash_cents=profile.cash_cents,
            portfolio_cents=portfolio_cents,
            position_cents=holdings.get(asset.symbol, 0),
            sector_cents=sector_totals[asset.sector],
            order_budget_cents=profile.order_budget_cents,
            max_stock_bps=profile.max_stock_bps,
            max_sector_bps=profile.max_sector_bps,
            excluded_sectors=profile.excluded_sectors,
        )
        assessments.append((candidate, decision))

    selected = select_candidate(assessments, profile.preferred_sectors)
    proposal = {"action": "hold", "reason": "No candidate passed the checks."}
    if selected:
        candidate, decision = selected
        proposal = {
            "action": "paper_buy",
            "symbol": candidate.symbol,
            "amount_cents": decision.max_buy_cents,
            "reason": (
                "Highest eligible forecast among preferred sectors."
                if candidate.sector in profile.preferred_sectors
                else "Highest eligible forecast."
            ),
        }

    return {
        "mode": "simulation",
        "as_of_session": request.sessions[-1],
        "proposal": proposal,
        "candidates": [
            {
                "sector": candidate.sector,
                "forecast": asdict(candidate.forecast),
                "beats_baseline": candidate.forecast.beats_baseline,
                **asdict(decision),
            }
            for candidate, decision in assessments
        ],
    }


def _weight_bps(value_cents: int, total_cents: int) -> float | None:
    if total_cents == 0:
        return None
    return round(value_cents * BPS_PER_UNIT / total_cents, 4)


def _exposure(value_cents: int, total_cents: int, limit_bps: int) -> dict:
    room = concentration_room_cents(total_cents, value_cents, limit_bps)
    excess = max(value_cents * BPS_PER_UNIT - total_cents * limit_bps, 0)
    return {
        "market_value_cents": value_cents,
        "weight_bps": _weight_bps(value_cents, total_cents),
        "limit_bps": limit_bps,
        "over_limit": room < 0,
        "excess_bps": round(excess / total_cents, 4) if total_cents else 0.0,
        "room_cents": max(room, 0),
    }


def _risk_snapshot(request: RiskRequest, cash_cents: int, marked_cents: dict[str, int]) -> dict:
    total = cash_cents + sum(marked_cents.values())
    sector_totals = Counter()
    for asset in request.assets:
        sector_totals[asset.sector] += marked_cents[asset.symbol]
    return {
        "total_cents": total,
        "cash_cents": cash_cents,
        "cash_weight_bps": _weight_bps(cash_cents, total),
        "positions": [
            {
                "symbol": asset.symbol,
                "sector": asset.sector,
                **_exposure(marked_cents[asset.symbol], total, request.max_stock_bps),
            }
            for asset in request.assets
        ],
        "sectors": [
            {"sector": sector, **_exposure(value, total, request.max_sector_bps)}
            for sector, value in sorted(sector_totals.items())
        ],
    }


@app.post("/risk")
def risk_report(request: RiskRequest) -> dict:
    """Report concentration and preview an optional paper buy at the supplied close."""
    marked = {
        asset.symbol: int((asset.shares * asset.close_cents).to_integral_value(rounding=ROUND_HALF_UP))
        for asset in request.assets
    }
    current = _risk_snapshot(request, request.cash_cents, marked)
    preview = None
    buy = request.paper_buy
    if buy is not None:
        position = next(row for row in current["positions"] if row["symbol"] == buy.symbol)
        sector = next(row for row in current["sectors"] if row["sector"] == position["sector"])
        max_buy = min(request.cash_cents, position["room_cents"], sector["room_cents"])
        projected = None
        status = "insufficient_cash"
        if buy.amount_cents <= request.cash_cents:
            after_buy = {**marked, buy.symbol: marked[buy.symbol] + buy.amount_cents}
            projected = _risk_snapshot(request, request.cash_cents - buy.amount_cents, after_buy)
            status = "within_limits" if buy.amount_cents <= max_buy else "exceeds_limits"
        preview = {
            "symbol": buy.symbol,
            "amount_cents": buy.amount_cents,
            "max_buy_cents": max_buy,
            "status": status,
            "projected": projected,
        }
    return {
        "mode": "paper",
        "as_of_session": request.as_of_session,
        "current": current,
        "preview": preview,
    }
