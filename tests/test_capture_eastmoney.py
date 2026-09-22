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
from capture_eastmoney import (
    LoginRequiredError,
    PersistentOcclusionError,
    capture_area,
    capture_with_recapture,
    clear_occlusions,
    ensure_logged_in,
    ensure_weekly_kline_screenshot,
    find_login_dialog,
    open_stock_page,
    should_capture_weekly_kline,
)


def _page_with_login_dialog(selector: str = "#login-mask"):
    handle = mock.MagicMock()
    handle.is_visible.return_value = True
    page = mock.MagicMock()
    page.query_selector_all.side_effect = lambda query: (
        [handle] if query == selector else []
    )
    return page, handle


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


class LoginStateGuardTest(unittest.TestCase):
    def test_find_login_dialog_detects_visible_dialog(self) -> None:
        page, _ = _page_with_login_dialog()
        self.assertEqual(find_login_dialog(page), "#login-mask")

    def test_find_login_dialog_ignores_hidden_dialog(self) -> None:
        handle = mock.MagicMock()
        handle.is_visible.return_value = False
        page = mock.MagicMock()
        page.query_selector_all.side_effect = lambda query: (
            [handle] if query == "#login-mask" else []
        )
        self.assertIsNone(find_login_dialog(page))

    def test_ensure_logged_in_raises_when_dialog_visible(self) -> None:
        page, _ = _page_with_login_dialog()
        with self.assertRaises(LoginRequiredError) as caught:
            ensure_logged_in(page)
        self.assertIn("--login", str(caught.exception))

    def test_ensure_logged_in_passes_without_dialog(self) -> None:
        page = mock.MagicMock()
        page.query_selector_all.return_value = []
        ensure_logged_in(page)  # 不应抛出异常

    def test_open_stock_page_fails_fast_without_retry_on_login_expired(self) -> None:
        page, _ = _page_with_login_dialog()
        context = mock.MagicMock()
        context.new_page.return_value = page
        with mock.patch.object(
            capture,
            "create_eastmoney_context",
            return_value=context,
        ) as create_context:
            with self.assertRaises(LoginRequiredError):
                open_stock_page(
                    mock.MagicMock(),
                    {"url": "https://quote.eastmoney.com/unify/r/0.000001"},
                    argparse.Namespace(auth_state=None, device_scale_factor=2.0, timeout=5),
                    5.0,
                )
        # 登录过期重试无效：上下文只创建一次，不再走第二次尝试
        self.assertEqual(create_context.call_count, 1)
        context.close.assert_called_once()


class OcclusionGuardTest(unittest.TestCase):
    def _page_without_login(self) -> mock.MagicMock:
        page = mock.MagicMock()
        page.query_selector_all.return_value = []
        return page

    def test_clear_occlusions_hides_overlay_then_proceeds(self) -> None:
        page = self._page_without_login()
        occluder = {"tag": "DIV", "id": "em-window-ads", "className": "popup ads"}
        page.evaluate.side_effect = [occluder, None]

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            clear_occlusions(page, [".k_chart"])

        self.assertEqual(page.evaluate.call_count, 2)
        self.assertIn("em-window-ads", stdout.getvalue())

    def test_clear_occlusions_raises_when_overlay_persists(self) -> None:
        page = self._page_without_login()
        occluder = {"tag": "DIV", "id": "sticky-ad", "className": ""}
        page.evaluate.return_value = occluder

        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(capture.CaptureError) as caught:
                clear_occlusions(page, [".k_chart"])

        self.assertIn("sticky-ad", str(caught.exception))
        self.assertEqual(page.evaluate.call_count, 3)

    def test_clear_occlusions_fails_fast_on_login_dialog(self) -> None:
        page, _ = _page_with_login_dialog()
        page.evaluate.return_value = None

        with self.assertRaises(LoginRequiredError):
            clear_occlusions(page, [".k_chart"])

        # 登录弹窗禁止静默隐藏：不应触发任何遮挡清理
        page.evaluate.assert_not_called()

    def test_clear_occlusions_tolerates_probe_errors(self) -> None:
        page = self._page_without_login()
        page.evaluate.side_effect = RuntimeError("navigating away")
        clear_occlusions(page, [".k_chart"])  # 不应抛出异常

    def test_capture_with_recapture_reloads_on_persistent_occlusion(self) -> None:
        """持续遮挡时重新加载页面并重抓，而不是直接失败。"""

        page = mock.MagicMock()
        attempts: list = []

        def flaky_capture() -> None:
            attempts.append(1)
            if len(attempts) < 3:
                raise PersistentOcclusionError("仍被遮挡")
            page.screenshot(path="ok")

        prepared: list = []

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            capture_with_recapture(
                flaky_capture,
                page,
                5.0,
                prepare=lambda p: prepared.append(1),
                reloads=2,
            )

        self.assertEqual(len(attempts), 3)
        self.assertEqual(page.reload.call_count, 2)
        self.assertEqual(len(prepared), 2)
        self.assertIn("重新加载页面后重抓", stdout.getvalue())

    def test_capture_with_recapture_raises_after_reloads_exhausted(self) -> None:
        page = mock.MagicMock()

        def always_occluded() -> None:
            raise PersistentOcclusionError("仍被遮挡")

        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(PersistentOcclusionError):
                capture_with_recapture(
                    always_occluded, page, 5.0, prepare=None, reloads=2
                )

        self.assertEqual(page.reload.call_count, 2)

    def test_capture_with_recapture_does_not_reload_on_login_expired(self) -> None:
        """登录过期不属于遮挡：不重抓，直接向上抛出。"""

        page = mock.MagicMock()

        def login_expired() -> None:
            raise LoginRequiredError("登录态可能已过期")

        with self.assertRaises(LoginRequiredError):
            capture_with_recapture(login_expired, page, 5.0, reloads=2)

        page.reload.assert_not_called()

    def test_capture_area_retries_with_reload_and_persists_screenshot(self) -> None:
        """capture_area 遇持续遮挡时重载页面并最终产出截图。"""

        page = self._page_without_login()
        occluder = {"tag": "DIV", "id": "sticky-ad", "className": ""}
        # 第一轮：遮挡持续（首次复测 3 次都返回遮挡）→ 重载；
        # 第二轮：首次复测发现并隐藏遮挡，复测通过，截图前复测也通过。
        page.evaluate.side_effect = [
            occluder, occluder, occluder,  # 第一轮 clear_occlusions
            occluder, None,                # 第二轮首次复测
            None,                          # 第二轮截图前复测
        ]
        element = mock.MagicMock()
        page.locator.return_value.first = element
        element.count.return_value = 1
        element.is_visible.return_value = True

        with contextlib.redirect_stdout(io.StringIO()):
            capture_area(page, [".k_chart"], Path("/tmp/fake-area.png"), 5.0)

        self.assertEqual(page.reload.call_count, 1)
        element.screenshot.assert_called_once()

    def test_capture_area_catches_overlay_appearing_before_screenshot(self) -> None:
        """浮层在检查后、截图前的等待窗口内弹出时，截图前复测能抓住并重抓。"""

        page = self._page_without_login()
        late_ad = {"tag": "DIV", "id": "app-promo", "className": "popup"}
        page.evaluate.side_effect = [
            None,      # 第一轮首次复测通过
            late_ad,   # 截图前复测发现弹窗（隐藏后仍在）
            late_ad,
            late_ad,   # 三轮均被遮挡 → 触发重抓
            None,      # 第二轮首次复测通过
            None,      # 第二轮截图前复测通过
        ]
        element = mock.MagicMock()
        page.locator.return_value.first = element
        element.count.return_value = 1
        element.is_visible.return_value = True

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            capture_area(page, [".k_chart"], Path("/tmp/fake-area.png"), 5.0)

        self.assertEqual(page.reload.call_count, 1)
        element.screenshot.assert_called_once()
        self.assertIn("重新加载页面后重抓", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
