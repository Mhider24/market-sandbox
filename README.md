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

## Open the trading workspace

With the virtual environment set up, run this from the repository folder in PowerShell.

```powershell
.\.venv\Scripts\python.exe -m uvicorn market_sandbox.api:app --reload
```

Open [the trading desk](http://127.0.0.1:8000/). The account summary and
watchlist sit beside a daily candlestick chart, paper-buy ticket, and exposure
panels. Select a symbol in the watchlist or search for it in the top bar.
The charts and price changes are illustrative. The account values, current
weights, and trade previews come from the local risk API.

The desk opens with the $500 QQQ preview, which exceeds the position and
group caps. Select SPY, enter $300, and click **Preview buy** to see a case
that reaches the group cap. **Edit portfolio** lets you change cash, marks,
holdings, and caps for the current page session.

## Connect Alpaca Paper

Create paper API credentials in the Alpaca dashboard. In the PowerShell window
that will run the desk, enter the values through prompts so the key and secret
stay in that shell's environment.

```powershell
$env:ALPACA_PAPER_KEY_ID = Read-Host "Alpaca Paper key ID"
$env:ALPACA_PAPER_SECRET_KEY = [System.Net.NetworkCredential]::new("", (Read-Host "Alpaca Paper secret key" -AsSecureString)).Password
.\.venv\Scripts\python.exe -m uvicorn market_sandbox.api:app --host 127.0.0.1 --port 8000
```

Open [the desk](http://127.0.0.1:8000/) and choose **Alpaca Paper**. The
broker routes are for a single local server process. The account panel and
Activity show Alpaca paper balances, positions, and order statuses.
Use **Refresh account** to update the broker view. The chart uses completed
IEX daily bars. The separate **Sandbox** view retains the illustrative portfolio.

**Run forecast** checks the selected symbols without submitting an order.
It reports the date of the latest included bar, each prediction, the Ridge
and zero-return RMSE, and the decision. The model refits on each review using
the rolling training window. The review uses sessions before the current UTC
date, including when run after the market closes.

**Arm bot** enables automatic checks while the server is running. The bot
checks once per minute and evaluates the first time it finds an open regular
US session and no pending orders. It may submit one automatic paper order per
session. Missing data or a failed forecast, cash, or exposure check can
result in a hold. The default automated order cap is $50, the default
position cap is 20%, and the default combined equity exposure cap is 40%.
The historical benchmark sections below document the model's results.

**Allow automatic sells of watched holdings** is off by default. When enabled,
the bot may sell the available shares of a watched long position, including
shares bought manually. An exit takes priority over a buy. The bot checks the
configured loss and gain thresholds against Alpaca's unrealized position return.
It can also exit when the model forecasts a decline of at least 10 basis points
and beats the zero-return baseline on trailing validation. The threshold check
runs as part of the session evaluation. Orders are market orders and the fill
price can differ from the price at the check.

**Pause** stops new automated submissions. The manual ticket supports
cash-funded dollar buys and sells by share quantity from long holdings.
Submitting a manual order or canceling an eligible Market Sandbox order
also pauses the bot. Existing orders have their own status in **Activity**.
Automation starts paused each time the server starts.

**Decision Journal** shows recent forecasts, holds, submissions, and pauses.
The history persists in `%USERPROFILE%\.market-sandbox\paper-audit.sqlite3` on
Windows and is scoped to the paper key ID used by the server. **Activity**
shows Alpaca's order status and reported fill quantity and average price.

Broker execution uses the [Alpaca Paper Trading API](https://docs.alpaca.markets/us/docs/trading-api).
Historical prices come from the [Alpaca Market Data API](https://docs.alpaca.markets/us/reference/stockbars).

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

## Check portfolio risk

In the local API docs, open `POST /risk` and paste this example. The prices are
illustrative. Supply one closing price per asset for the same session, including
an entry with zero shares for any new symbol you want to preview.

```json
{
  "as_of_session": "2026-11-06",
  "cash_cents": 630000,
  "max_stock_bps": 2000,
  "max_sector_bps": 4000,
  "assets": [
    {"symbol": "QQQ", "sector": "equity-etf", "shares": "6", "close_cents": 45000},
    {"symbol": "SPY", "sector": "equity-etf", "shares": "2", "close_cents": 50000}
  ],
  "paper_buy": {"symbol": "QQQ", "amount_cents": 50000}
}
```

The report marks share quantities to cents and includes cash in total portfolio
value. It returns current weights, concentration flags, and room under each cap.
Divide basis points by 100 to read them as percentages. Sectors are the groups
you assign to the assets. A cash-only snapshot works with an empty asset list.

In this example QQQ starts at 27%, seven percentage points over its cap. The
$500 preview moves it to 32% and moves the ETF group from 37% to 42%. The status
is `exceeds_limits`, with zero available room for a QQQ buy. An affordable
preview shows the hypothetical result even when a cap would be exceeded.
An `insufficient_cash` preview returns a null projection. Omit `paper_buy` to
see the current exposure only.

The preview assumes fractional shares, a fixed price, and zero fees.

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

## Historical paper-trading replay

The replay uses the same SPY and QQQ exports and 502 target sessions as the
forecast benchmark. At each prior close, the forecast gate uses the preceding
40 realized outcomes and the 10 bp forecast cutoff. This comparison scores
Ridge and zero return on the same sessions that passed the gate, before
portfolio limits are applied.

| ETF | Gate passed | Ridge RMSE (bps) | Zero-return RMSE (bps) |
| --- | ---: | ---: | ---: |
| SPY | 63 of 502 | 74.847 | **70.464** |
| QQQ | 42 of 502 | 105.458 | **93.549** |

Both profiles start with $10,000 in cash and cap each buy at $250, including
fees. Each chooses at most one proposal at the close and rechecks exposure at
the next open. SPY and QQQ share a single exposure bucket for this experiment.

| Profile | Per-ETF / combined cap | Fills | Ending equity | Max drawdown |
| --- | --- | ---: | ---: | ---: |
| Standard | 20% / 40% | 17 | $11,445.22 (+14.45%) | 8.52% |
| Tight | 5% / 10% | 6 | $10,418.28 (+4.18%) | 2.55% |

On filled orders, the baseline also had lower forecast error for both
profiles. The portfolio results describe the buy-only simulation, including
the effect of existing ETF exposure. Isolating the value of entry timing
would require a control portfolio with comparable exposure.

The simulator buys fractional adjusted units at the next adjusted open plus
5 bp slippage and a 1 bp fee rounded up to cents. Adjusted open is
`Open * Adj Close / Close`. Positions are marked at adjusted close and held
through the end. The adjustment approximates reinvested distributions.
Drawdown is measured from daily closing equity. The cost assumptions are
fixed for this run.

```powershell
.\.venv\Scripts\python.exe -m market_sandbox.paper_replay --spy-csv SPY.csv --qqq-csv QQQ.csv --start 2024-01-01 --end 2025-12-31 --log replay.csv
```

Use aligned daily exports with `Date`, `Open`, `Close`, and `Adj Close` and at
least 121 earlier trading closes. The optional decision log records the
forecast, gate result, portfolio decision, and fill cost for each candidate
and profile. Choose a fresh log filename for each run.

## Entry timing with matched exposure

To check the value of entry timing, the control takes each profile's filled
buys and samples new trading days within the same months. Each control keeps
the ETF, dollar amount, fee, and final valuation date for every buy. It uses the
same next-open fill model. A control qualifies when invested capital-days are
within 0.5% of the observed schedule for each ETF. Capital-days equal invested
notional times the number of trading sessions held.

| Profile | Observed ending equity | Median control | Control 5th–95th percentile | Controls below observed |
| --- | ---: | ---: | ---: | ---: |
| Standard | $11,445.22 | $11,456.77 | $11,443.29–$11,470.28 | 8.3% |
| Tight | $10,418.28 | $10,425.18 | $10,416.90–$10,435.50 | 8.8% |

Both observed schedules finished below most of the 2,000 controls per profile.
The comparison is retrospective and conditional on the fills that already
occurred. The original replay determines which orders passed the portfolio
limits. The controls move those orders and measure ending equity with similar
capital-days. This tests entry timing within a month on this historical path.

```powershell
.\.venv\Scripts\python.exe -m market_sandbox.timing_control --spy-csv SPY.csv --qqq-csv QQQ.csv --start 2024-01-01 --end 2025-12-31
```

The command uses the same CSVs as the paper replay and a fixed random seed of 42.
It reports the actual number of matches when the sampling budget runs out.

## Forward validation in 2026

The same model, signal cutoff, portfolio rules, cost assumptions, and 0.5%
matching tolerance were applied to a separate period. The run scored 214
sessions from January 2 through November 6, 2026, using the same data exports.
Each forecast still uses only observations available at the prior close.

| ETF | Ridge / zero-return RMSE, all sessions | Gate passed | Ridge / zero-return RMSE, gate passed |
| --- | ---: | ---: | ---: |
| SPY | 90.240 / **87.252** bps | 6 of 214 | 229.709 / **203.330** bps |
| QQQ | 146.232 / **138.444** bps | 9 of 214 | 205.755 / **150.451** bps |

| Profile | Fills | Ending equity | Max drawdown | Median control | Controls below observed |
| --- | ---: | ---: | ---: | ---: | ---: |
| Standard | 13 | $10,059.34 | 3.88% | $10,078.60 | 9.3% |
| Tight | 6 | $10,095.40 | 1.27% | $10,101.66 | 9.8% |

Zero return again had lower error on both ETFs, including the sessions that
passed the gate. Both portfolios also finished below most of their 2,000
matched control draws. The control comparison is conditional on the filled
orders and describes the observed historical path.

To reproduce, use start `2026-01-01` and end `2026-11-06` with the benchmark,
paper replay, and timing control commands above.

## Source map

- `market_sandbox/forecast.py` builds features, fits the model, and measures forecast error.
- `market_sandbox/policy.py` applies the portfolio and forecast limits.
- `market_sandbox/api.py` validates snapshots and selects a proposal.
- `market_sandbox/demo.py` provides synthetic inputs and a local demo.
- `market_sandbox/backtest.py` scores the model against a zero-return baseline.
- `market_sandbox/paper_replay.py` replays portfolio decisions and simulates fills.
- `market_sandbox/timing_control.py` compares entry timing with similar exposure.
