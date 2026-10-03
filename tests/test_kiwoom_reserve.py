import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tossbot.config import KST
from kiwoombot.config import KiwoomConfig
from kiwoombot.daytrade import DayTrader
from kiwoombot.reserve import ReserveBook


class ReserveBookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.book = ReserveBook(self.tmp.name, pct=50, floor=1_000_000)

    def test_profit_day_reserves_half_then_loss_day_tops_up(self):
        self.book.ensure_base(1_000_000, "20261005")
        st = self.book.settle(1_040_000, "20261005")  # +4만원 → 적립 2만원
        self.assertEqual(st.reserve, 20_000)
        self.assertEqual(self.book.operating(1_040_000), 1_020_000)
        self.book.mark_flows(1_040_000, "20261005")
        st = self.book.settle(990_000, "20261006")  # -5만원 → 운용금 97만 → 적립금 2만원 꺼내 99만
        self.assertEqual(st.reserve, 0)
        self.assertEqual(self.book.operating(990_000), 990_000)

    def test_loss_day_keeps_reserve_when_operating_above_floor(self):
        self.book.ensure_base(1_500_000, "20261005")
        self.book.set_reserve(200_000)
        st = self.book.settle(1_450_000, "20261005")  # 손실, 운용금 125만 ≥ 100만
        self.assertEqual(st.reserve, 200_000)
        self.assertEqual(self.book.operating(1_450_000), 1_250_000)

    def test_top_up_only_fills_shortfall(self):
        self.book.ensure_base(1_300_000, "20261005")
        self.book.set_reserve(300_000)
        st = self.book.settle(1_200_000, "20261005")  # 운용금 90만 → 10만원만 보충
        self.assertEqual(st.reserve, 200_000)

    def test_settle_runs_once_per_day(self):
        self.book.ensure_base(1_000_000, "20261005")
        self.book.settle(1_100_000, "20261005")
        st = self.book.settle(1_200_000, "20261005")
        self.assertEqual(st.reserve, 50_000)

    def test_withdrawal_after_noon_comes_from_reserve_first(self):
        self.book.ensure_base(2_000_000, "20261005")
        self.book.set_reserve(300_000)
        self.book.settle(2_000_000, "20261005")
        st = self.book.mark_flows(1_800_000, "20261005")  # 20만원 출금
        self.assertEqual(st.reserve, 100_000)
        self.assertEqual(self.book.operating(1_800_000), 1_700_000)  # 운용금 그대로
        st = self.book.settle(1_800_000, "20261006")  # 다음 날 손익 0 → 적립 없음
        self.assertEqual(st.reserve, 100_000)

    def test_withdrawal_larger_than_reserve_reduces_operating(self):
        self.book.ensure_base(2_000_000, "20261005")
        self.book.set_reserve(100_000)
        self.book.settle(2_000_000, "20261005")
        st = self.book.mark_flows(1_700_000, "20261005")
        self.assertEqual(st.reserve, 0)
        self.assertEqual(self.book.operating(1_700_000), 1_700_000)

    def test_deposit_after_noon_goes_to_operating_not_profit(self):
        self.book.ensure_base(1_000_000, "20261005")
        self.book.settle(1_000_000, "20261005")
        st = self.book.mark_flows(1_500_000, "20261005")  # 50만원 입금
        self.assertEqual(st.reserve, 0)
        st = self.book.settle(1_500_000, "20261006")
        self.assertEqual(st.reserve, 0)  # 입금은 수익으로 잡히지 않음

    def test_small_change_after_noon_is_not_a_flow(self):
        self.book.ensure_base(2_000_000, "20261005")
        self.book.set_reserve(100_000)
        self.book.settle(2_000_000, "20261005")
        st = self.book.mark_flows(1_999_700, "20261005")
        self.assertEqual(st.reserve, 100_000)

    def test_disabled_and_dry_run(self):
        off = ReserveBook(self.tmp.name, pct=0, floor=1_000_000)
        self.assertEqual(off.operating(1_234_000), 1_234_000)
        dry = ReserveBook(self.tmp.name, pct=50, floor=1_000_000, dry_run=True)
        dry.ensure_base(1_000_000, "20261005")
        self.assertEqual(dry.settle(1_100_000, "20261005").reserve, 50_000)
        self.assertFalse((Path(self.tmp.name) / "reserve.json").exists())  # 드라이런은 파일을 바꾸지 않음


class ReserveDefaultOffTest(unittest.TestCase):
    def test_default_config_has_reserve_off(self):
        self.assertEqual(KiwoomConfig().reserve_pct, 0.0)  # 기본은 적립 없음 (전액 복리)


class FakeBalanceClient:
    def __init__(self, equity):
        self.equity = equity

    def balance(self):
        return {"prsm_dpst_aset_amt": str(self.equity)}

    def change_rate_ranking(self, max_pages=5):
        return []


class DayTraderReserveTest(unittest.TestCase):
    def test_order_size_uses_operating_equity_and_settles_after_exit_time(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = KiwoomConfig(app_key="x", secret_key="y", dry_run=False, state_dir=tmp.name, swing_state_file="",
                           reserve_pct=50.0)
        ReserveBook(tmp.name, 50, 1_000_000).set_reserve(300_000)
        client = FakeBalanceClient(1_500_000)
        clock = [datetime(2026, 10, 5, 8, 50, tzinfo=KST)]
        trader = DayTrader(client, cfg, now_fn=lambda: clock[0], sleep_fn=lambda s: None)
        trader.prepare()
        self.assertEqual(trader.state.equity, 1_200_000)  # 150만 - 적립금 30만
        clock[0] = datetime(2026, 10, 5, 12, 1, tzinfo=KST)
        client.equity = 1_560_000  # 하루 +6만원
        trader.settle_reserve()
        self.assertEqual(trader.reserve.load().reserve, 330_000)


if __name__ == "__main__":
    unittest.main()
