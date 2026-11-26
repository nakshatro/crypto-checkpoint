import importlib.util
import math
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backtest_v2.py"
SPEC = importlib.util.spec_from_file_location("backtest_v2", SCRIPT)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class BacktestV2Tests(unittest.TestCase):
    def make_rows(self, n=100):
        rows = []
        for i in range(n):
            c = 100 + i * 0.15 + math.sin(i / 4)
            rows.append({"t": i * 3_600_000, "o": c - 0.1, "h": c + 0.5,
                         "l": c - 0.5, "c": c, "v": 1000 + (i % 7) * 10})
        return rows

    def test_warmup_returns_no_signal(self):
        side, atr = mod.signal(self.make_rows(40), "trend")
        self.assertEqual(side, 0)
        self.assertIsNone(atr)

    def test_atr_is_positive_after_warmup(self):
        value = mod.atr(self.make_rows(40))
        self.assertIsNotNone(value)
        self.assertGreater(value, 0)

    def test_rsi_is_bounded(self):
        value = mod.rsi([100 + i for i in range(20)])
        self.assertGreaterEqual(value, 0)
        self.assertLessEqual(value, 100)

    def test_risk_and_capital_defaults(self):
        self.assertEqual(mod.START_CAPITAL, 1000.0)
        self.assertEqual(mod.RISK_PER_TRADE, 0.005)
        self.assertEqual(mod.MAX_PORTFOLIO_RISK_PCT, 0.02)


if __name__ == "__main__":
    unittest.main()
