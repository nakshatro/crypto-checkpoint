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
