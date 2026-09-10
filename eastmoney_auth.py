#!/usr/bin/env python3
"""Manage the reusable Eastmoney browser login state."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.sync_api import Browser, sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_AUTH_STATE = PROJECT_ROOT / ".auth" / "eastmoney-state.json"
LOGIN_URL = "https://quote.eastmoney.com/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class AuthStateError(RuntimeError):
    """Raised when the Eastmoney login state cannot be loaded or saved."""


def resolve_auth_state(path: Optional[str]) -> Path:
    """Resolve a login-state path without exposing its contents."""
    return Path(path).expanduser() if path else DEFAULT_AUTH_STATE


def load_auth_state(path: Optional[str]) -> Optional[Dict[str, Any]]:
    """Load a Playwright storage state, or return None when it does not exist."""
    state_path = resolve_auth_state(path)
    if not state_path.is_file():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuthStateError(f"读取东方财富登录状态失败（{state_path}）：{exc}") from exc
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        raise AuthStateError(f"东方财富登录状态文件格式无效：{state_path}")
    return state


def create_eastmoney_context(
    browser: Browser,
    auth_state: Optional[str],
    *,
    device_scale_factor: float,
) -> Any:
    """Create a context that reuses the saved Eastmoney login cookies."""
    state = load_auth_state(auth_state)
    return browser.new_context(
        viewport={"width": 1440, "height": 1200},
        device_scale_factor=device_scale_factor,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        user_agent=USER_AGENT,
        storage_state=state,
    )


def _write_state_safely(state_path: Path, state: Dict[str, Any]) -> None:
    """Write login data atomically and restrict it to the current user."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{state_path.name}.", suffix=".tmp", dir=str(state_path.parent)
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, state_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def login_and_save(auth_state: Optional[str], timeout: float) -> Path:
    """Open a visible browser and save the manually completed Eastmoney login."""
    state_path = resolve_auth_state(auth_state)
    timeout_seconds = max(timeout, 5.0)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=False)
        try:
            context = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                user_agent=USER_AGENT,
                storage_state=load_auth_state(auth_state),
            )
            page = context.new_page()
            page.set_default_timeout(timeout_seconds * 1000)
            page.goto(LOGIN_URL, wait_until="domcontentloaded")

            print("已打开东方财富行情页。请在浏览器中完成登录，并确认页面右上角显示账号信息。")
            print("登录状态只保存在本机，用于后续截图；不会保存或输出你的密码。")
            confirmed = input("完成后输入 yes 保存登录状态，直接回车或输入其他内容取消：").strip().lower()
            if confirmed not in {"y", "yes"}:
                raise AuthStateError("已取消保存东方财富登录状态")

            state = context.storage_state()
            _write_state_safely(state_path, state)
            return state_path
        finally:
            browser.close()
