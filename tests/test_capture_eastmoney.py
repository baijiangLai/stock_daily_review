import argparse
import contextlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import capture_eastmoney as capture
from capture_eastmoney import ensure_weekly_kline_screenshot, should_capture_weekly_kline


class CaptureEastmoneyTest(unittest.TestCase):
    def test_weekly_kline_is_captured_on_friday_and_weekend(self) -> None:
        self.assertTrue(should_capture_weekly_kline("2026-09-18"))
        self.assertTrue(should_capture_weekly_kline("2026-09-19"))
        self.assertTrue(should_capture_weekly_kline("2026-09-20"))
        self.assertFalse(should_capture_weekly_kline("2026-09-17"))
        self.assertFalse(should_capture_weekly_kline(None))
        self.assertFalse(should_capture_weekly_kline("invalid"))

    def test_existing_weekly_snapshot_is_reused_without_browser(self) -> None:
        args = argparse.Namespace(timeout=1, headed=False, auth_state=None)
        with tempfile.TemporaryDirectory() as directory:
            stock_dir = Path(directory)
            (stock_dir / "04_weekly_kline.png").write_bytes(b"weekly")
            (stock_dir / "metadata.json").write_text(
                json.dumps(
                    {
                        "review_date": "2026-09-18",
                        "screenshots": {
                            "weekly_kline": "04_weekly_kline.png"
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            captured = ensure_weekly_kline_screenshot(
                stock_dir,
                {"name": "示例股票", "code": "000001", "url": "fake"},
                args,
            )

        self.assertTrue(captured)

    def test_main_prints_weekly_file_without_name_error(self) -> None:
        output = Path("/tmp/fake-stock-capture")
        with mock.patch.object(
            capture,
            "run",
            return_value=output,
        ), mock.patch(
            "sys.argv",
            ["capture_eastmoney.py", "000001"],
        ), contextlib.redirect_stdout(io.StringIO()) as stdout:
            exit_code = capture.main()

        self.assertEqual(exit_code, 0)
        self.assertIn("04_daily_kline.png", stdout.getvalue())
        if capture.should_capture_weekly_kline(date.today().isoformat()):
            self.assertIn("04_weekly_kline.png", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
