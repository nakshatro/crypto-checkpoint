# Crypto Checkpoint Forecast Lab

This additive Forecast Lab lives under `cloud-predictor/`; it does not replace the separate root dashboard. It combines a historical-analogue scenario view (4h/24h/72h) with an experimental five-layer model (24h/1w/1m) for BTC, ETH, XRP and SOL.

## Architecture

1. GitHub Actions refreshes the public-market snapshots on a best-effort 15-minute schedule.
2. The worker reads completed OKX hourly and daily candles and public derivatives endpoints where available.
3. It scores structure, derivatives, liquidity, on-chain flows and news; missing components are excluded and reported.
4. Hourly model snapshots are retained in `docs/data/history.json` and evaluated in `docs/data/calibration.json`.
5. The static dashboard renders forecasts, layer evidence, confidence, missing inputs and calibration metrics.

## Model layers

| Layer | Intended evidence | Current implementation notes |
|---|---|---|
| Structure | Daily EMA20/50/200, RSI14, alt relative strength vs BTC | Requires 200 completed daily candles; otherwise score is missing |
| Derivatives | Funding crowding, price vs OI change, long/short ratio, perp basis | OKX funding history and OI changes are used when endpoints return data; long/short history and basis may be unavailable |
| Liquidity | 24h taker CVD, liquidation magnet/flow, book imbalance | 24h taker CVD is used when available; liquidation map/flow and book imbalance are not yet implemented |
| On-chain | 30d stablecoin supply, ETH/SOL chain TVL, optional ETF flow | ETF flows are manual only; unknown values remain null |
| News | Asset-matched headlines and Fear & Greed | Keyword-based headlines; not semantic or fully time-filtered news analysis |

The model detects the UPTREND, DOWNTREND, RANGE and HIGH_VOL regimes, adjusts layer weights by horizon and regime, applies supported HEALTHY_TREND / EXHAUSTION interactions, and shrinks directional estimates toward 50%. Confidence is reduced by layer disagreement, low layer coverage and configured events within 24 hours.

## Reading the dashboard

- **Bullish / bearish model lean** means the model has a directional bias, not a verified trade setup.
- **P(up)** is an uncalibrated model estimate, not a reliable real-world probability until sufficient out-of-sample observations accumulate.
- **Confidence** summarizes data coverage and agreement; it is not the chance of a profitable trade.
- **Historical analogue forecasts** are a separate reference method and should not be confused with the five-layer model.
- **Missing inputs** are displayed instead of being filled with fabricated values.
- Calibration shows Brier score, skill against a 50/50 baseline and hit rate. Treat early results cautiously; aim for 100+ evaluated outcomes per horizon.

## Manual inputs

Edit `cloud-predictor/manual_inputs.json` only with verified data:
- `etf_flow_5d_usd`: net five-day spot-ETF flow in USD. Leave null when unknown.
- `events`: scheduled catalysts with a verified `time_utc` in ISO-8601 UTC format, e.g. `2026-12-09T19:00:00Z`. Events within 24 hours reduce model confidence and appear as a countdown.

Do not add unverified dates or estimates.

## Schedule

The current workflow runs at minutes 7, 22, 37 and 52 of each UTC hour (about every 15 minutes). GitHub cron can be delayed. Edit `.github/workflows/crypto-data.yml` to change it; for example `0,30 * * * *` is twice hourly and `0 */3 * * *` is every three hours.

## Limitations and safety

Educational probabilistic analytics only—not financial advice. No strategy profitability has been demonstrated. Public endpoints can fail or return incomplete histories; hosted runners may be geo-blocked; the liquidation map/flow and order-book imbalance are not yet implemented; keyword sentiment is simplistic; ETF flow and event inputs require manual verification; and calibration remains unreliable until enough future outcomes exist. The dashboard refreshes saved data and does not itself fetch fresh market data every minute.
