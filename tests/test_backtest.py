import importlib.util
import math
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "walk_forward_backtest.py"
SPEC = importlib.util.spec_from_file_location("walk_forward_backtest", SCRIPT)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

class BacktestSmokeTests(unittest.TestCase):
    def test_features_handle_short_warmup(self):
        rows = [(i * 86_400_000, 100.0 + i) for i in range(5)]
        features = mod.features(rows, [])
        self.assertEqual(len(features), 5)
        self.assertTrue(math.isfinite(features[0]["g20"]))

    def test_metric_pack_has_correct_base_rate_brier_label(self):
        predictions = [
            {"p_up": 0.6, "p_base": 0.5, "actual_up": 1, "horizon_days": 1},
            {"p_up": 0.4, "p_base": 0.5, "actual_up": 0, "horizon_days": 1},
            {"p_up": 0.6, "p_base": 0.5, "actual_up": 0, "horizon_days": 1},
            {"p_up": 0.4, "p_base": 0.5, "actual_up": 1, "horizon_days": 1},
        ]
        result = mod.metric_pack(predictions)
        self.assertEqual(result["n"], 4)
        self.assertAlmostEqual(result["constant_train_rate_brier"], 0.25, places=5)
        self.assertIsNotNone(result["accuracy_ci95_pct"])

    def test_1000_usdt_reference_allocation(self):
        ps = [
            {"p_up": 0.7, "actual_return_pct": 2.0, "date": "2026-01-01", "regime": "bull"},
            {"p_up": 0.3, "actual_return_pct": -1.0, "date": "2026-01-02", "regime": "bear"},
        ]
        result = mod.strategy_metrics(ps, "p_up", 0.6, 10)
        self.assertEqual(result["reference_capital_usdt"], 1000.0)
        self.assertEqual(result["position_notional_usdt"], 100.0)
        self.assertEqual(result["trades"], 2)
        self.assertAlmostEqual(result["gross_pnl_usdt"], 3.0, places=2)
        self.assertAlmostEqual(result["net_pnl_usdt"], 2.8, places=2)

if __name__ == "__main__":
    unittest.main()


class ForecastAuditRepairTests(unittest.TestCase):
    @staticmethod
    def collector():
        path = Path(__file__).resolve().parents[1] / "cloud-predictor" / "worker" / "collector.py"
        spec = importlib.util.spec_from_file_location("crypto_collector", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_distant_events_do_not_reduce_confidence(self):
        from datetime import datetime, timezone
        factor, notes = self.collector().event_confidence_adjustment([
            {"name": "CPI", "time_exact": True, "time_utc": "2026-11-17T13:30:00Z"},
            {"name": "FOMC", "time_exact": False, "date_utc": "2026-11-20"},
        ], datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(factor, 1.0)
        self.assertEqual(notes, [])

    def test_exact_event_within_24h_reduces_confidence(self):
        from datetime import datetime, timezone
        factor, notes = self.collector().event_confidence_adjustment([
            {"name": "CPI", "time_exact": True, "time_utc": "2026-11-16T00:00:00Z"},
        ], datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(factor, 0.8)
        self.assertTrue(any("CPI" in note for note in notes))

    def test_date_only_event_today_gets_smaller_caution(self):
        from datetime import datetime, timezone
        factor, notes = self.collector().event_confidence_adjustment([
            {"name": "FOMC meeting", "time_exact": False, "time_utc": None, "date_utc": "2026-11-15"},
        ], datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(factor, 0.9)
        self.assertTrue(notes)

    def test_collector_calibration_uses_non_overlapping_origins(self):
        from datetime import datetime, timezone, timedelta
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        history = []
        for hour in range(24):
            history.append({"updated_at": (base + timedelta(hours=hour)).isoformat(), "coins": {"BTC": {
                "price": 100 + (hour // 4),
                "model": {"horizons": {"4h": {"p_up": 0.6}}}
            }}})
        result = self.collector().evaluate_model_calibration(history)["4h"]
        self.assertLessEqual(result["n"], 6)
        self.assertIn("skill_vs_past_base_rate", result)

    def test_calibration_review_selects_non_overlapping_forecasts(self):
        path = Path(__file__).resolve().parents[1] / "cloud-predictor" / "worker" / "calibration_review.py"
        spec = importlib.util.spec_from_file_location("calibration_review", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from datetime import datetime, timezone, timedelta
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [{"forecast_time_utc": (base + timedelta(hours=h)).isoformat()} for h in range(12)]
        selected = module.select_non_overlapping_forecasts(rows, 4)
        self.assertEqual(len(selected), 3)
        self.assertEqual([x["forecast_time_utc"] for x in selected], [rows[i]["forecast_time_utc"] for i in (0, 4, 8)])
