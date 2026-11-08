# Market Sandbox

Market Sandbox is a paper investing prototype. It forecasts the next session's
close-to-close log return and proposes one simulated buy or holds cash. The same
forecast can lead to different decisions for users with different holdings, budgets,
or sector preferences.

## Run the demo

Use Python 3.12. In Windows PowerShell, from the repository folder:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m market_sandbox.demo
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

On macOS or Linux, use `python3.12 -m venv .venv` and replace
`.\.venv\Scripts\python.exe` with `./.venv/bin/python`.

The demo gives both profiles the same fictional stock and price history. The first
gets a $50 paper buy proposal. The second holds because its tech exposure is already
at its cap. The fixture has deliberately predictable returns so the demo exercises
the buy path. Its results describe the fixture only.

## Try the API

Run `python -m uvicorn market_sandbox.api:app --reload` from the activated environment
and open [the local API docs](http://127.0.0.1:8000/docs). In a second terminal, run
`python -m market_sandbox.demo --request room_for_tech` and paste its JSON into
`POST /simulate` in the API docs. Use `tech_at_limit` for the second profile.

For your own research data, supply at least 121 aligned, strictly ascending trading
session dates and one positive close per asset per session. Use split-adjusted closes
from the same source and avoid gaps in the exchange's trading calendar. Include every
held symbol, its sector, and the market value of the position as of the last supplied
session. Cash, holdings, and order budget use integer cents. The engine consumes the
snapshot and returns a proposal with the latest session, model errors, and individual
reasons. Each request is independent.

## Forecast equations

| Step | Method |
| --- | --- |
| Target | `r[t+1] = ln(P[t+1]) - ln(P[t])`. A forecast made at close `t` targets the next close. |
| Inputs | Latest log return, trailing 5- and 20-session mean log returns, and daily EWMA volatility. |
| Volatility | `v[t] = 0.94 * v[t-1] + 0.06 * r[t]^2`, initialized with the first squared return. The input is `sqrt(v[t])`. |
| Learner | Ridge regression minimizes `sum((y - intercept - Xw)^2) + 10 * sum(w^2)`. The features are standardized using the current training window. |
| Update | Fit on the latest 60 examples with observed targets. Submitting the next close adds a newly labeled example, drops the oldest row from the fit, and produces a new forecast. |
| Check | For the last 40 known targets, refit at each historical origin using only its preceding 60 labeled examples. Compare root mean squared error against `r_hat = 0`, which implies an unchanged closing price. |

The next-session forecast must reach 10 basis points and the model must have a
strictly lower rolling RMSE than the baseline. The 10 bp cutoff and window sizes
are prototype settings. A basis point is 0.01 percentage point. Preferred sectors
rank first among eligible candidates, with larger forecasts ranking first within
each group. Excluded sectors and breached limits force a hold. The proposal uses
the smallest of available cash, order budget, and remaining stock and sector room.

The RMSE comparison measures forecast error. Testing investment performance
requires an untouched historical period and an execution simulation that handles
next-session fills, slippage, and fees. The prototype accepts snapshots on demand
and returns virtual order proposals without filling them.

Methods are described in [Ridge regression](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.Ridge.html),
[rolling-origin validation and naïve forecasts](https://otexts.com/fpppy/05-toolbox.html),
[RiskMetrics EWMA](https://www.msci.com/documents/10199/5915b101-4206-4ba0-aee2-3449d5c7e95a).

## Historical forecast benchmark

The original Ridge settings were frozen before scoring the January 2024 through
December 2025 holdout. At each session the model refits using only targets
observed by the previous close. The report scores every holdout session against
an unchanged-price forecast. Lower root mean squared error (RMSE) is better.

| ETF | Scored sessions | Ridge RMSE (bps) | Zero-return RMSE (bps) |
| --- | ---: | ---: | ---: |
| SPY | 502 | 115.595 | **102.734** |
| QQQ | 502 | 143.071 | **131.508** |

The zero-return baseline had lower error on both ETFs. The first scored close was
January 2, 2024 and the last was December 31, 2025. The inputs were the `Adj Close`
columns from the daily CSV exports at [StockQuote SPY](https://www.stockquote.io/stocks/SPY/history)
and [StockQuote QQQ](https://www.stockquote.io/stocks/QQQ/history), downloaded on
November 7, 2026. The exports started on November 8, 2021. The figures measure
forecast accuracy.

To rerun, save one daily price CSV per ticker with `Date` and `Adj Close` columns
and at least 81 trading closes before the holdout. Include every trading session
in the period. The runner sorts dates, rejects duplicates, and prints the actual
range and session count. The source data may be revised between downloads.

```powershell
.\.venv\Scripts\python.exe -m market_sandbox.backtest --csv SPY.csv --close-column "Adj Close" --holdout-start 2024-01-01 --holdout-end 2025-12-31
```

Use `QQQ.csv` for the second run. The runner uses `Close` if you omit `--close-column`.

## Source map

- `market_sandbox/forecast.py` builds features, fits the model, and measures forecast error.
- `market_sandbox/policy.py` applies the portfolio and forecast limits.
- `market_sandbox/api.py` validates snapshots and selects a proposal.
- `market_sandbox/demo.py` provides synthetic inputs and a local demo.
- `market_sandbox/backtest.py` scores the model against a zero-return baseline.
