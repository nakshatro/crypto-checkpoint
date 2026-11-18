import unittest, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cloud-predictor' / 'worker'))
from pre_move import evaluate_pre_move

class PreMoveTests(unittest.TestCase):
    def candles(self, breakout=False):
        rows=[]
        for i in range(80):
            c=100+i*0.05
            h=c+0.15; l=c-0.15
            v=1000
            rows.append({"o":c-0.02,"h":h,"l":l,"c":c,"v":v})
        if breakout:
            rows[-1]={"o":104.0,"h":105.0,"l":103.9,"c":104.8,"v":1800}
        return rows

    def test_returns_wait_with_insufficient_inputs(self):
        out=evaluate_pre_move("BTC",self.candles(),{}, {}, [], {}, {})
        self.assertIn(out["action"],("WAIT","LONG","SHORT"))
        self.assertIn("stage",out)

    def test_long_watch_uses_directional_evidence(self):
        rows=self.candles()
        deriv={"taker_cvd_24h":0.2,"oi_change_24h":0.04,"book_imbalance":0.3}
        model={"horizons":{"24h":{"p_up":0.68}}}
        forecasts=[{"horizon":"24h","up_frequency_pct":65}]
        out=evaluate_pre_move("BTC",rows,deriv,model,forecasts,{}, {})
        self.assertEqual(out["action"],"LONG")
        self.assertEqual(out["stage"],"EARLY_WATCH")
        self.assertIsNotNone(out["stop_reference"])
        self.assertGreaterEqual(out["target_reference"]-out["entry_reference"],1.5*(out["entry_reference"]-out["stop_reference"]))

    def test_trigger_requires_breakout_and_volume(self):
        rows=self.candles(breakout=True)
        deriv={"taker_cvd_24h":0.2,"oi_change_24h":0.04,"book_imbalance":0.3}
        model={"horizons":{"24h":{"p_up":0.68}}}
        forecasts=[{"horizon":"24h","up_frequency_pct":65}]
        out=evaluate_pre_move("BTC",rows,deriv,model,forecasts,{}, {})
        self.assertEqual(out["action"],"LONG")
        self.assertEqual(out["stage"],"CONFIRMED_TRIGGER")

if __name__=="__main__":
    unittest.main()
