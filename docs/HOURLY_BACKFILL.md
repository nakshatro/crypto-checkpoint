# Hourly history backfill

The research branch includes a public-data backfill for 1-hour spot and linear USDT candles for BTC, ETH, XRP and SOL.

## Source and pagination

Uses Bybit V5 `GET /v5/market/kline`, categories `spot` and `linear`, interval `60`. The API returns newest-first rows and supports up to 1,000 candles per request. The script requests bounded 999-hour windows, deduplicates by candle open timestamp, sorts ascending, and writes only closed hourly candles. See the [official Bybit endpoint documentation](https://bybit-exchange.github.io/docs/v5/market/kline).

## Output

- `data/historical/{coin}_spot_1h.csv.gz` — consumed by `scripts/backtest_v2.py`
- `data/historical/{coin}_linear_1h.csv.gz` — separate linear futures-market candles
- `data/historical/backtest/hourly_backfill_report.json` — exact coverage and gap audit

CSV columns: `open_time_ms,open,high,low,close,volume`.

## Run locally

```bash
python scripts/backfill_ohlcv_1h.py --days 730 --minimum-coverage 99.5
python scripts/backtest_v2.py
```

## GitHub Actions

Open Actions → **Backfill hourly history and run Backtest V2** → Run workflow. The default is 730 days. The workflow uploads the compressed candle archives and JSON reports as a 30-day artifact; it does not commit large market-data files to Git.

A weekly scheduled run refreshes the same rolling 730-day window. The manual workflow can also be run at any time.

## Important limitations

This creates an OHLCV-based proxy backtest, not a historical replay of the full CMI production model. Historical point-in-time OI, taker CVD, liquidations, order-book imbalance, ETF flows, on-chain, macro/news/sentiment snapshots and timestamped model forecasts are still required to test those layers without look-ahead bias. Spot and linear candles are separate market feeds; the current V2 execution harness is still the authority on which files/markets it actually consumes. No trading recommendation is produced by this data pipeline.
