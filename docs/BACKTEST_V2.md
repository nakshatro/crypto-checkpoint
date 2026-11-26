# Second-generation Crypto Checkpoint backtest

## Implementation status

This branch adds `scripts/backtest_v2.py`, an event-driven portfolio backtest harness with separate reversal and trend strategies and separate spot/futures reports. It does not modify the live collector, forecast worker, dashboard, or Telegram workflows.

## Repository preflight findings

- Existing `scripts/walk_forward_backtest.py` evaluates daily spot direction and a funding-feature ablation. It is not a replay of the five-layer production model.
- Existing archived price files are daily (`{coin}_spot_1d.csv.gz`); the report records 2,099 daily rows per coin.
- Funding archives exist (`{coin}_funding.csv.gz`), but no archived 1H OHLCV files were found.
- The live collector fetches only a rolling set of completed hourly candles and queries current derivatives/order-book/liquidation/news/on-chain/ETF context. These historical feature snapshots are not retained as timestamped training/backtest rows.
- Consequently, a defensible 1H second-generation run and a true production five-layer replay cannot yet be reported from the current archive.

## Required hourly data

Place one gzip CSV per coin at:

```
data/historical/btc_spot_1h.csv.gz
data/historical/eth_spot_1h.csv.gz
data/historical/xrp_spot_1h.csv.gz
data/historical/sol_spot_1h.csv.gz
```

Required columns: `open_time_ms,open,high,low,close,volume`. At least 500 valid hourly candles per coin are required by the initial smoke threshold; longer history is strongly preferred. Funding data is read from the existing `{coin}_funding.csv.gz` files using `funding_time_ms,funding_rate`.

## Strategy and execution model

- Starting capital: 1,000 USDT per independently evaluated portfolio.
- Strategy families: reversal/capitulation-reclaim and trend/breakout.
- Spot: long-only; short trades are excluded because spot cannot create a short position.
- Futures: long and short, simulated at 1x notional (no leverage) pending a separately approved leverage and liquidation model.
- Risk budget: 0.5% of current capital per trade; aggregate open stop-risk capped at 2%; position notional capped at 10% of capital.
- Stops/targets: 1.5 ATR stop and 2R target; maximum holding period 72 hours.
- Costs: configurable fixed fee and slippage assumptions; funding is applied only when an archived funding timestamp exactly matches an hourly bar.
- Conservative candle ordering: if stop and target are both touched within one candle, stop is assumed to execute first.
- Metrics: net P&L, return, win rate, profit factor, maximum drawdown, annualized hourly Sharpe, trade ledger and equity curve.

## Important limitations

This is an OHLCV-only proxy inspired by CMI reversal and trend requirements. It does not reproduce the five-layer model or its pre-move scoring because historical OI, taker CVD, liquidation flow, order-book imbalance, ETF flow, on-chain, macro/news and sentiment snapshots are not archived alongside each hourly forecast. No backtest output should be described as a validation of the live AI model.

Staged entries are simulated as two equal tranches: 50% at the next bar open, then the remaining 50% at the following bar open only if the prior completed candle still confirms the signal and price has not crossed the stop. The signal and execution assumptions still require independent unit tests and review.

## Run

```bash
python -m py_compile scripts/backtest_v2.py
python scripts/backtest_v2.py
```

If hourly files are missing, the script writes a blocked preflight report instead of inventing performance results:
`data/historical/backtest/backtest_v2_report.json`.
