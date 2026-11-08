from collections import Counter
from dataclasses import asdict
from datetime import date
from typing import Annotated

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .forecast import MIN_CLOSES, forecast_next_close
from .policy import Candidate, check_candidate, select_candidate

app = FastAPI(title="Market Sandbox", version="0.1.0")

Symbol = Annotated[
    str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=1, max_length=12)
]
Sector = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=1, max_length=40)
]
Cents = Annotated[int, Field(ge=0, strict=True)]
BasisPoints = Annotated[int, Field(ge=1, le=10_000, strict=True)]
Close = Annotated[float, Field(gt=0, strict=True, allow_inf_nan=False)]


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
    max_stock_bps: BasisPoints = 2_000
    max_sector_bps: BasisPoints = 4_000


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
