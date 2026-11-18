import unittest, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cloud-predictor' / 'worker'))
from pre_move_review import evaluate

class PreMoveReviewTests(unittest.TestCase):
    def test_forward_only_long_signal_is_scored_after_horizon(self):
        history=[
            {"updated_at":"2026-01-01T00:00:00+00:00","coins":{"BTC":{"price":100,"pre_move":{"action":"LONG","stage":"EARLY_WATCH"}}}},
            {"updated_at":"2026-01-01T04:00:00+00:00","coins":{"BTC":{"price":102}}},
        ]
        out=evaluate(history,horizons=(4,))
        self.assertEqual(out["horizons"]["4h"]["all"]["n"],1)
        self.assertEqual(out["horizons"]["4h"]["all"]["hit_rate_pct"],100.0)

    def test_unmatured_signal_is_not_scored(self):
        history=[{"updated_at":"2026-01-01T00:00:00+00:00","coins":{"ETH":{"price":100,"pre_move":{"action":"SHORT","stage":"CONFIRMED_TRIGGER"}}}}]
        out=evaluate(history,horizons=(4,))
        self.assertEqual(out["horizons"]["4h"]["all"]["n"],0)
        self.assertIsNone(out["horizons"]["4h"]["all"]["hit_rate_pct"])

if __name__=="__main__":
    unittest.main()
