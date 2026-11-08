"""Run two paper profiles against the same intentionally predictable synthetic series."""

import argparse
import json
from datetime import date, timedelta

import numpy as np
from fastapi.encoders import jsonable_encoder

from .api import Asset, Profile, Simulation, simulate


def demo_market() -> tuple[list[date], list[Asset]]:
    sessions = [
        date(2025, 1, 1) + timedelta(days=offset)
        for offset in range(300)
        if (date(2025, 1, 1) + timedelta(days=offset)).weekday() < 5
    ][:200]
    rng = np.random.default_rng(19)
    returns = [0.0]
    for innovation in rng.normal(0.0, 0.004, len(sessions) - 1):
        returns.append(0.85 * returns[-1] + innovation)
    returns[-1] = 0.015
    closes = (100 * np.exp(np.cumsum(returns))).tolist()
    return sessions, [Asset(symbol="DEMO-TECH", sector="technology", closes=closes)]


def demo_requests() -> dict[str, Simulation]:
    sessions, assets = demo_market()
    profiles = {
        "room_for_tech": Profile(
            cash_cents=100_000, order_budget_cents=5_000, preferred_sectors={"technology"}
        ),
        "tech_at_limit": Profile(
            cash_cents=60_000,
            order_budget_cents=5_000,
            holdings_cents={"DEMO-TECH": 40_000},
            preferred_sectors={"technology"},
        ),
    }
    return {
        name: Simulation(sessions=sessions, assets=assets, profile=profile)
        for name, profile in profiles.items()
    }


def run_demo() -> dict:
    return {name: simulate(request) for name, request in demo_requests().items()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", choices=demo_requests(), help="Print sample POST JSON.")
    args = parser.parse_args()
    output = demo_requests()[args.request] if args.request else run_demo()
    print(json.dumps(jsonable_encoder(output), indent=2))
