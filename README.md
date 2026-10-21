# Crypto Checkpoint

**Release line:** v1.0.0 — research/analytics release  
**Markets:** BTC, ETH, XRP and SOL  
**Historical evaluation:** daily spot-price baseline with funding-rate feature ablation  
**Reference capital for trade illustrations:** 1,000 USDT

Crypto Checkpoint provides market monitoring, historical analogue scenarios, an experimental five-layer forecast, and a reproducible historical evaluation pipeline. It is a research and decision-support tool, not an automated execution system.

## Current status

- The dashboard and data collection workflows are maintained separately from the historical backtest.
- The five-layer production forecast remains **experimental and uncalibrated** until sufficient forward outcomes are evaluated.
- Historical baseline results are not evidence of a durable or profitable strategy.
- Public data endpoints can be incomplete, unavailable, delayed or restricted by geography.
- No order is placed by this repository.

## Historical backtest

Run locally with Python 3.12 and the standard library:

```bash
python -m py_compile scripts/walk_forward_backtest.py tests/test_backtest.py
python -m unittest discover -s tests -v
python scripts/walk_forward_backtest.py
```

Outputs:
- `data/historical/backtest/walk_forward_report.json`
- `data/historical/backtest/walk_forward_predictions.csv.gz`

The report compares a training-period base rate, momentum-only, moving-average, combined technical and funding-augmented probability estimates. It uses chronological walk-forward evaluation and includes directional accuracy, Brier scores, approximate block-bootstrap accuracy intervals, regime breakdowns and selective-trade sensitivity to assumed round-trip costs.

## 1,000 USDT trade illustration

The report uses **1,000 USDT reference capital** and **10% fixed notional per signal (100 USDT)** to express illustrative gross and net trade PnL in USDT. It does not compound returns, does not create a portfolio equity curve, and may count overlapping signals independently. Therefore the summed PnL is not a forecast of account growth or a result achievable by a 1,000 USDT account. No leverage is assumed.

Cost scenarios are assumptions, not measured fees from a user's exchange account. The simple funding feature comparison does not model actual funding cash flows, margin, liquidation or perpetual-contract execution.

## Data and methodology

The historical backfill uses free/public sources where available, including Binance public candle/funding archives and a public BTC ETF flow dataset. Coverage differs by dataset and asset. The report should be checked for row counts and date coverage before interpreting results.

Forecast probabilities are not considered calibrated unless out-of-sample Brier score and calibration tests demonstrate improvement over relevant benchmarks. The backtest's price-only baseline does not validate the production five-layer model.

## Risk notice

Educational and analytical use only; not financial advice. Crypto assets can lose substantial value. Do not use a forecast as a trade instruction without independent checks, position sizing, stop/exit rules and an understanding of fees and execution risks. Historical performance does not guarantee future results.

## Release acceptance

A version tag denotes a tested software snapshot, not certification of predictive skill or profitability. CI must pass syntax checks, unit/smoke tests and report generation before a release is considered technically accepted.
