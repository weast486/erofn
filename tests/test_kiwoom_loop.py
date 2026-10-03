import unittest
from datetime import datetime, timedelta
from unittest import mock

from tossbot.config import KST
from kiwoombot import __main__ as kmain
from kiwoombot.__main__ import next_start
from kiwoombot.config import KiwoomConfig


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=KST)


class KiwoomLoopNextStartTest(unittest.TestCase):
    def test_saturday_waits_until_monday_morning(self):
        self.assertEqual(next_start(at(2026, 10, 3, 14, 30)), at(2026, 10, 5, 8, 40))

    def test_sunday_night_waits_until_monday_morning(self):
        self.assertEqual(next_start(at(2026, 10, 4, 23, 50)), at(2026, 10, 5, 8, 40))

    def test_weekday_before_start_waits_same_day(self):
        self.assertEqual(next_start(at(2026, 10, 5, 2, 0)), at(2026, 10, 5, 8, 40))

    def test_weekday_during_market_starts_now(self):
        now = at(2026, 10, 5, 10, 15)
        self.assertEqual(next_start(now), now)

    def test_weekday_after_close_waits_next_day(self):
        self.assertEqual(next_start(at(2026, 10, 5, 16, 0)), at(2026, 10, 6, 8, 40))

    def test_already_ran_today_waits_next_day(self):
        self.assertEqual(next_start(at(2026, 10, 5, 12, 5), "20261005"), at(2026, 10, 6, 8, 40))

    def test_friday_after_run_waits_until_monday(self):
        self.assertEqual(next_start(at(2026, 10, 9, 15, 31), "20261009"), at(2026, 10, 12, 8, 40))


class KiwoomLoopRunTest(unittest.TestCase):
    def test_waits_for_monday_then_runs_once_per_day_and_resumes_after_error(self):
        clock = [at(2026, 10, 3, 14, 30)]  # 토요일 오후에 켬
        runs = []

        def sleep(sec):
            clock[0] += timedelta(seconds=sec)

        class FakeTrader:
            def __init__(self, client, cfg):
                pass

            def run(self):
                runs.append(clock[0])
                if len(runs) == 1:
                    raise RuntimeError("일시 오류")  # 같은 날 30초 뒤 이어서 다시
                if len(runs) == 2:
                    clock[0] = clock[0].replace(hour=15, minute=31)  # 하루 끝
                    return
                raise KeyboardInterrupt  # 다음 거래일 실행에서 테스트 끝

        cfg = KiwoomConfig(app_key="x", secret_key="y")
        with mock.patch.object(kmain, "DayTrader", FakeTrader), self.assertLogs("kiwoombot", level="INFO"):
            with self.assertRaises(KeyboardInterrupt):
                kmain.loop(cfg, now_fn=lambda: clock[0], sleep_fn=sleep)
        self.assertEqual(runs[0], at(2026, 10, 5, 8, 40))
        self.assertEqual(runs[1], at(2026, 10, 5, 8, 40) + timedelta(seconds=30))
        self.assertEqual(runs[2], at(2026, 10, 6, 8, 40))


if __name__ == "__main__":
    unittest.main()
