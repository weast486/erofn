import os
import sys
import tempfile
import unittest

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import investor_flow as f  # noqa: E402


class TossInvestorFlowTest(unittest.TestCase):
    def test_volume_times_close_and_classify(self):
        days = pd.bdate_range("2024-03-01", periods=15)
        closes = {"000001": pd.Series(1000.0, index=days)}

        def fake(client, code, until, count=20):
            recs = [{"date": d.strftime("%Y-%m-%d"),
                     "institution": {"netBuyVolume": "-100"},
                     "foreigner": {"netBuyVolume": "30000"},
                     "individual": None}  # 당일 잠정치는 개인이 null
                    for d in days if d <= pd.Timestamp(until)]
            return f.toss_records_to_df(recs)

        with tempfile.TemporaryDirectory() as tmp:
            flow = f.load_flow_toss(None, "000001", days[7], closes, tmp, fake)
            self.assertEqual(flow.loc[days[7], "외국인합계"], 30_000_000.0)
            self.assertEqual(flow.loc[days[7], "개인"], 0.0)
            c = f.classify(flow, days[7])
            self.assertEqual(c["전일5일_주체"], "외국인")
            self.assertEqual(c["포함5일_외국인(억)"], 1.5)
            # 두 번째 호출은 캐시에서 읽는다
            again = f.load_flow_toss(None, "000001", days[7], closes, tmp,
                                     lambda *a, **k: self.fail("캐시 미사용"))
            self.assertEqual(len(again), len(flow))


if __name__ == "__main__":
    unittest.main()
