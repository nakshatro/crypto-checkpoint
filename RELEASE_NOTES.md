# Crypto Checkpoint v1.0.0 — Research Release

This release packages the Crypto Checkpoint dashboard and reproducible historical evaluation pipeline.

## Included
- BTC, ETH, XRP and SOL market-analysis dashboard and data workflows.
- Walk-forward comparison of base-rate, momentum, moving-average, combined technical and funding-augmented forecasts.
- Directional accuracy, Brier score, approximate block-bootstrap intervals and regime-level reporting.
- Selective-signal sensitivity under 5, 10 and 20 basis-point assumed round-trip costs.
- Fixed reference-capital trade PnL illustration using 1,000 USDT capital and 100 USDT (10%) notional per signal.
- Automated Python syntax and smoke tests.

## Important limitations
- This is a research/analytics release, not a claim of validated forecasting skill or profitability.
- Production five-layer probabilities remain experimental and uncalibrated until independently evaluated on sufficient future outcomes.
- The 1,000 USDT illustration is fixed-notional per signal, not a compounding account simulation. Trades may overlap; summed PnL is not a realizable account equity curve.
- The historical funding feature test does not model funding cash flows, margin, liquidation or perpetual-futures execution.
- Public datasets may have missing periods or source limitations. Review the report's coverage and limitations before relying on it.
- No orders are placed by this repository.
