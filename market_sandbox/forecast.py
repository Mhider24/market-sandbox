from dataclasses import dataclass

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

LONG_LOOKBACK = 20
TRAINING_ROWS = 60
VALIDATION_ROWS = 40
MIN_TRAINING_CLOSES = LONG_LOOKBACK + TRAINING_ROWS + 1
MIN_CLOSES = MIN_TRAINING_CLOSES + VALIDATION_ROWS
RIDGE_ALPHA = 10.0
VOLATILITY_DECAY = 0.94
BPS_PER_UNIT = 10_000


@dataclass(frozen=True)
class Forecast:
    next_log_return_bps: float
    volatility_bps: float
    model_rmse_bps: float
    baseline_rmse_bps: float
    validation_sessions: int

    @property
    def beats_baseline(self) -> bool:
        return self.model_rmse_bps < self.baseline_rmse_bps


def _features(log_returns: np.ndarray) -> np.ndarray:
    variance = log_returns[0] ** 2
    rows = []
    for index, log_return in enumerate(log_returns):
        variance = VOLATILITY_DECAY * variance + (1 - VOLATILITY_DECAY) * log_return**2
        if index + 1 >= LONG_LOOKBACK:
            end = index + 1
            rows.append(
                [
                    log_return,
                    np.mean(log_returns[end - 5 : end]),
                    np.mean(log_returns[end - LONG_LOOKBACK : end]),
                    np.sqrt(variance),
                ]
            )
    return np.asarray(rows)


def _predict(features: np.ndarray, targets: np.ndarray, index: int) -> float:
    training = slice(index - TRAINING_ROWS, index)
    model = make_pipeline(StandardScaler(), Ridge(alpha=RIDGE_ALPHA))
    model.fit(features[training], targets[training])
    return float(model.predict(features[index : index + 1])[0])


def _prepare_series(closes: list[float], min_closes: int) -> tuple[np.ndarray, np.ndarray]:
    prices = np.asarray(closes, dtype=float)
    if (
        prices.ndim != 1
        or len(prices) < min_closes
        or not np.isfinite(prices).all()
        or np.any(prices <= 0)
    ):
        raise ValueError(f"Supply at least {min_closes} finite positive closing prices.")

    log_returns = np.diff(np.log(prices))
    # The row ending at close t is labeled with the return from t to t+1.
    # The most recent feature row has no observed target yet.
    return _features(log_returns), log_returns[LONG_LOOKBACK:]


def _replay(features: np.ndarray, targets: np.ndarray, start: int) -> np.ndarray:
    return np.asarray(
        [_predict(features, targets, index) for index in range(start, len(targets))]
    )


def walk_forward_returns(
    closes: list[float], first_target_close: int
) -> tuple[np.ndarray, np.ndarray]:
    """Return actual and predicted log returns from the first target close onward."""
    if not MIN_TRAINING_CLOSES <= first_target_close < len(closes):
        raise ValueError(
            f"The first target needs {MIN_TRAINING_CLOSES} earlier closes and a known close."
        )
    features, targets = _prepare_series(closes, MIN_TRAINING_CLOSES + 1)
    start = first_target_close - LONG_LOOKBACK - 1
    return targets[start:], _replay(features, targets, start)


def forecast_next_close(closes: list[float]) -> Forecast:
    features, targets = _prepare_series(closes, MIN_CLOSES)
    forecast_index = len(targets)
    start = forecast_index - VALIDATION_ROWS
    validation_targets = targets[start:forecast_index]
    predictions = _replay(features, targets, start)
    model_rmse = np.sqrt(np.mean((validation_targets - predictions) ** 2))
    baseline_rmse = np.sqrt(np.mean(validation_targets**2))
    return Forecast(
        next_log_return_bps=_predict(features, targets, forecast_index) * BPS_PER_UNIT,
        volatility_bps=float(features[-1, -1]) * BPS_PER_UNIT,
        model_rmse_bps=float(model_rmse) * BPS_PER_UNIT,
        baseline_rmse_bps=float(baseline_rmse) * BPS_PER_UNIT,
        validation_sessions=VALIDATION_ROWS,
    )
