"""Measure next-session forecast error on a chronological holdout."""

import argparse
import csv
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date
from math import isfinite
from pathlib import Path

import numpy as np

from .forecast import BPS_PER_UNIT, MIN_TRAINING_CLOSES, walk_forward_returns


@dataclass(frozen=True)
class Benchmark:
    first_target: date
    last_target: date
    sessions: int
    ridge_rmse_bps: float
    baseline_rmse_bps: float


def load_prices(path: Path, close_column: str) -> list[tuple[date, float]]:
    rows = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"Date", close_column}.issubset(reader.fieldnames or []):
            raise ValueError(f"CSV must contain Date and {close_column!r} columns.")

        for row in reader:
            try:
                session = date.fromisoformat(row["Date"].strip())
                close = float(row[close_column])
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError(f"Invalid date or price on CSV line {reader.line_num}.") from exc
            if not isfinite(close) or close <= 0:
                raise ValueError(f"Price must be finite and positive on CSV line {reader.line_num}.")
            if session in rows:
                raise ValueError(f"Duplicate trading date: {session}.")
            rows[session] = close
    return sorted(rows.items())


def run_benchmark(
    prices: list[tuple[date, float]], holdout_start: date, holdout_end: date
) -> Benchmark:
    if holdout_end < holdout_start:
        raise ValueError("Holdout end must be on or after the start.")

    dates = [session for session, _ in prices]
    start = bisect_left(dates, holdout_start)
    end = bisect_right(dates, holdout_end)
    if start >= end:
        raise ValueError("The CSV has no trading sessions in the requested holdout.")
    if start < MIN_TRAINING_CLOSES:
        raise ValueError(
            f"Need at least {MIN_TRAINING_CLOSES} trading closes before the first holdout target."
        )

    closes = [close for _, close in prices[:end]]
    actual, predicted = walk_forward_returns(closes, start)
    return Benchmark(
        first_target=dates[start],
        last_target=dates[end - 1],
        sessions=len(actual),
        ridge_rmse_bps=float(np.sqrt(np.mean((actual - predicted) ** 2))) * BPS_PER_UNIT,
        baseline_rmse_bps=float(np.sqrt(np.mean(actual**2))) * BPS_PER_UNIT,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path, help="Daily price CSV for one symbol.")
    parser.add_argument("--holdout-start", required=True, type=date.fromisoformat)
    parser.add_argument("--holdout-end", required=True, type=date.fromisoformat)
    parser.add_argument("--close-column", default="Close", help="Price column to score.")
    args = parser.parse_args()
    try:
        if args.close_column == "Date":
            raise ValueError("The price column must be different from Date.")
        report = run_benchmark(
            load_prices(args.csv, args.close_column), args.holdout_start, args.holdout_end
        )
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        parser.error(str(exc))

    print(f"Input: {args.csv} ({args.close_column})")
    print(f"Target sessions: {report.first_target} to {report.last_target} ({report.sessions})")
    print(f"Ridge RMSE:       {report.ridge_rmse_bps:9.3f} bps")
    print(f"Zero-return RMSE: {report.baseline_rmse_bps:9.3f} bps")
    print(f"Ridge minus baseline: {report.ridge_rmse_bps - report.baseline_rmse_bps:+.3f} bps")


if __name__ == "__main__":
    main()
