import tempfile
import unittest
from pathlib import Path

from kiwoombot.__main__ import update_settings


class UpdateSettingsTest(unittest.TestCase):
    def test_keeps_keys_and_mode_updates_strategy(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env.kiwoom"
            ex = Path(d) / "example"
            env.write_text("KIWOOM_APP_KEY=abc\nKIWOOM_SECRET_KEY=def\nKIWOOM_MOCK=false\nKIWOOM_DRY_RUN=false\n"
                           "KW_MIN_PREV_CHANGE=15\nKW_POSITION_PCT=20\nMY_EXTRA=1\n", encoding="utf-8")
            ex.write_text("# 설명\nKIWOOM_APP_KEY=\nKIWOOM_SECRET_KEY=\nKIWOOM_MOCK=true\nKIWOOM_DRY_RUN=true\n"
                          "KW_MIN_PREV_CHANGE=20\nKW_POSITION_PCT=33\nKW_ETF_ENABLED=true\n", encoding="utf-8")
            changes = update_settings(env, ex)
            text = env.read_text(encoding="utf-8")
            for line in ["KIWOOM_APP_KEY=abc", "KIWOOM_SECRET_KEY=def", "KIWOOM_MOCK=false", "KIWOOM_DRY_RUN=false",
                         "KW_MIN_PREV_CHANGE=20", "KW_POSITION_PCT=33", "KW_ETF_ENABLED=true", "MY_EXTRA=1", "# 설명"]:
                self.assertIn(line, text)
            self.assertTrue((Path(d) / ".env.kiwoom.bak").exists())
            self.assertEqual({c[0] for c in changes}, {"KW_MIN_PREV_CHANGE", "KW_POSITION_PCT", "KW_ETF_ENABLED"})


if __name__ == "__main__":
    unittest.main()
