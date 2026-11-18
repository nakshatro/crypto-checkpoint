import unittest, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cloud-predictor' / 'worker'))
from market_structure import validate_levels

class MarketStructureTests(unittest.TestCase):
    def test_support_breakdown_retest_is_counted(self):
        rows=[{"h":101.2,"l":100.8,"c":101.0} for _ in range(8)]
        rows += [
            {"h":100.0,"l":99.5,"c":99.7},
            {"h":100.0,"l":99.4,"c":99.7},
            {"h":99.9,"l":99.2,"c":99.5},
        ]
        mtf={"timeframes":{"1H":{"support":[{"price":100}]}}}
        out=validate_levels(rows,mtf,lookback=72)
        level=out["levels"][0]
        self.assertGreaterEqual(level["close_breaks_lookback"],1)
        self.assertGreaterEqual(level["breakout_retests_lookback"],1)

    def test_empty_levels_returns_empty_validation(self):
        out=validate_levels([{"h":2,"l":1,"c":1.5}]*10,{"timeframes":{}})
        self.assertEqual(out["levels"],[])

if __name__=="__main__":
    unittest.main()
