"""Local JSON REST API for frontend integration."""

from __future__ import annotations

import json
import re
import threading
from datetime import date
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

from review_workflow.application.errors import WorkflowConflictError, WorkflowNotFoundError
from review_workflow.application.service import WorkflowService
from review_workflow.domain.config import WorkflowConfig
from review_workflow.infrastructure.factory import create_workflow_agent

from .openapi import openapi_schema


DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_BODY_BYTES = 1024 * 1024
HOLDING_PLAN_OPTIONS = {
    "3个月内",
    "6个月内",
    "1年之内",
    "3年之内",
    "5年之内",
}


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def config_from_payload(payload: Dict[str, Any]) -> WorkflowConfig:
    """Map a public API request to config without accepting arbitrary paths."""

    review_date = payload.get("date") or payload.get("review_date")
    if not isinstance(review_date, str) or not DATE_PATTERN.fullmatch(review_date):
        raise ApiError(422, "date 必须为 YYYY-MM-DD")
    try:
        date.fromisoformat(review_date)
    except ValueError as exc:
        raise ApiError(422, f"date 无效：{exc}") from exc

    provider = payload.get("provider", "auto")
    if provider not in {"auto", "gemini", "zhipu"}:
        raise ApiError(422, "provider 只支持 auto、gemini、zhipu")

    search_provider = payload.get("search_provider", "auto")
    if search_provider not in {"auto", "zhipu", "model", "none"}:
        raise ApiError(422, "search_provider 只支持 auto、zhipu、model、none")

    try:
        timeout = float(payload.get("timeout", 180.0))
        device_scale_factor = float(payload.get("device_scale_factor", 2.0))
    except (TypeError, ValueError) as exc:
        raise ApiError(422, "timeout 和 device_scale_factor 必须是数字") from exc
    if not 5 <= timeout <= 3600:
        raise ApiError(422, "timeout 必须在 5 到 3600 秒之间")
    if not 0.5 <= device_scale_factor <= 4:
        raise ApiError(422, "device_scale_factor 必须在 0.5 到 4 之间")

    model = payload.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ApiError(422, "model 必须是非空字符串")

    board = _string_list(payload.get("board", []), "board")
    peer_stock = _string_list(payload.get("peer_stock", []), "peer_stock")
    holdings = _holdings(payload.get("holdings"))
    boolean_fields = {
        "headed": payload.get("headed", False),
        "skip_capture": payload.get("skip_capture", False),
        "recapture": payload.get("recapture", False),
        "no_web_search": payload.get("no_web_search", False),
        "no_peer_capture": payload.get("no_peer_capture", False),
        "no_portfolio_summary": payload.get("no_portfolio_summary", False),
    }
    for name, value in boolean_fields.items():
        if not isinstance(value, bool):
            raise ApiError(422, f"{name} 必须是布尔值")

    return WorkflowConfig(
        review_date=review_date,
        provider=provider,
        model=model,
        search_provider=search_provider,
        timeout=timeout,
        device_scale_factor=device_scale_factor,
        **boolean_fields,
        board=board,
        peer_stock=peer_stock,
        holdings=holdings,
    )


def _string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ApiError(422, f"{name} 必须是字符串数组")
    return list(value)


def _holdings(value: Any) -> list[dict[str, str]]:
    """Validate and normalize the frontend holding form payload."""

    if value is None:
        return []
    if not isinstance(value, list) or not value:
        raise ApiError(422, "holdings 必须是非空数组；若使用服务端默认持仓则可省略")

    result: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ApiError(422, f"holdings[{index - 1}] 必须是对象")

        name_value = item.get("name")
        if not isinstance(name_value, str) or not 1 <= len(name_value.strip()) <= 40:
            raise ApiError(422, f"holdings[{index - 1}].name 必须是 1-40 个字符")
        name = name_value.strip()
        normalized_name = name.casefold()
        if normalized_name in seen_names:
            raise ApiError(422, f"持仓重复：{name}")
        seen_names.add(normalized_name)

        cost_value = item.get("cost")
        if isinstance(cost_value, bool) or cost_value is None:
            raise ApiError(422, f"holdings[{index - 1}].cost 必须是大于 0 的数字")
        try:
            cost = Decimal(str(cost_value).strip())
        except InvalidOperation as exc:
            raise ApiError(
                422,
                f"holdings[{index - 1}].cost 必须是大于 0 的数字",
            ) from exc
        if not cost.is_finite() or cost <= 0:
            raise ApiError(422, f"holdings[{index - 1}].cost 必须是大于 0 的数字")

        shares_value = item.get("shares")
        if isinstance(shares_value, bool) or shares_value is None:
            raise ApiError(422, f"holdings[{index - 1}].shares 必须是正整数")
        try:
            shares = int(str(shares_value).strip())
        except ValueError as exc:
            raise ApiError(422, f"holdings[{index - 1}].shares 必须是正整数") from exc
        if shares <= 0:
            raise ApiError(422, f"holdings[{index - 1}].shares 必须是正整数")

        plan = item.get("plan")
        if plan not in HOLDING_PLAN_OPTIONS:
            raise ApiError(
                422,
                f"holdings[{index - 1}].plan 必须是："
                + "、".join(sorted(HOLDING_PLAN_OPTIONS)),
            )

        result.append(
            {
                "name": name,
                "cost": str(cost_value).strip(),
                "shares": str(shares),
                "plan": str(plan),
            }
        )
    return result


class ReviewApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        service: WorkflowService,
    ):
        super().__init__(address, ReviewApiHandler)
        self.service = service
        self._locks: Dict[str, threading.Lock] = {}
        self._lock_guard = threading.Lock()
        self._active_runs: set[str] = set()

    def lock_for(self, review_date: str) -> threading.Lock:
        with self._lock_guard:
            return self._locks.setdefault(review_date, threading.Lock())

    def claim(self, review_date: str) -> None:
        with self._lock_guard:
            if review_date in self._active_runs:
                raise ApiError(409, f"{review_date} 的工作流正在执行中")
            self._active_runs.add(review_date)

    def release(self, review_date: str) -> None:
        with self._lock_guard:
            self._active_runs.discard(review_date)

    def is_active(self, review_date: str) -> bool:
        with self._lock_guard:
            return review_date in self._active_runs

    def start_background(
        self,
        review_date: str,
        config: WorkflowConfig,
    ) -> None:
        self.claim(review_date)

        def target() -> None:
            try:
                self.service.resume(config, retry_failed=True, execute=True)
            except Exception as exc:
                print(f"[review-api] {review_date} 后台执行失败：{exc}")
            finally:
                self.release(review_date)

        threading.Thread(target=target, daemon=True, name=f"review-{review_date}").start()


class ReviewApiHandler(BaseHTTPRequestHandler):
    server: ReviewApiServer

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._add_common_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/api/health":
                self._send_json(200, {"ok": True, "service": "review-workflow"})
                return
            if path == "/api/openapi":
                self._send_json(200, openapi_schema())
                return

            match = re.fullmatch(r"/api/review-runs/(\d{4}-\d{2}-\d{2})", path)
            if match:
                review_date = match.group(1)
                config = WorkflowConfig(review_date=review_date)
                payload = self.server.service.status(config)
                payload["active"] = self.server.is_active(review_date)
                self._send_json(200, payload)
                return

            match = re.fullmatch(
                r"/api/review-runs/(\d{4}-\d{2}-\d{2})/document", path
            )
            if match:
                config = WorkflowConfig(review_date=match.group(1))
                document = self.server.service.document(config)
                body = document.encode("utf-8")
                self.send_response(200)
                self._add_common_headers()
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            raise ApiError(404, "接口不存在")
        except ApiError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except WorkflowConflictError as exc:
            self._send_json(409, {"error": str(exc)})
        except WorkflowNotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
        except FileNotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path.rstrip("/")
            payload = self._read_json()

            if path == "/api/review-runs":
                config = config_from_payload(payload)
                force = _boolean(payload.get("force", False), "force")
                execute = _boolean(payload.get("execute", False), "execute")
                if self.server.is_active(config.review_date):
                    raise ApiError(409, f"{config.review_date} 的工作流正在执行中")
                result = self.server.service.start(config, force=force)
                if execute:
                    self.server.start_background(config.review_date, config)
                    result["active"] = True
                self._send_json(201 if not execute else 202, result)
                return

            match = re.fullmatch(r"/api/review-runs/(\d{4}-\d{2}-\d{2})/steps", path)
            if match:
                review_date = match.group(1)
                config = self._date_config(payload, review_date)
                result = self._synchronous_action(review_date, lambda: self.server.service.step(config))
                self._send_json(200, result)
                return

            match = re.fullmatch(r"/api/review-runs/(\d{4}-\d{2}-\d{2})/resume", path)
            if match:
                review_date = match.group(1)
                config = self._date_config(payload, review_date)
                retry_failed = _boolean(payload.get("retry_failed", True), "retry_failed")
                execute = _boolean(payload.get("execute", False), "execute")
                result = self._synchronous_action(
                    review_date,
                    lambda: self.server.service.resume(
                        config,
                        retry_failed=retry_failed,
                        execute=False,
                    ),
                )
                if execute:
                    self.server.start_background(review_date, config)
                    result["active"] = True
                self._send_json(200 if not execute else 202, result)
                return

            match = re.fullmatch(r"/api/review-runs/(\d{4}-\d{2}-\d{2})/render", path)
            if match:
                review_date = match.group(1)
                config = self._date_config(payload, review_date)
                result = self._synchronous_action(
                    review_date,
                    lambda: self.server.service.render(config),
                )
                self._send_json(200, result)
                return

            raise ApiError(404, "接口不存在")
        except ApiError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except WorkflowConflictError as exc:
            self._send_json(409, {"error": str(exc)})
        except WorkflowNotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
        except FileNotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def _date_config(self, payload: Dict[str, Any], review_date: str) -> WorkflowConfig:
        payload = dict(payload)
        if "holdings" in payload:
            raise ApiError(422, "holdings 只能在创建工作流时提交；恢复时使用已保存持仓")
        payload["date"] = review_date
        return config_from_payload(payload)

    def _synchronous_action(
        self,
        review_date: str,
        action: Callable[[], Dict[str, Any]],
    ) -> Dict[str, Any]:
        lock = self.server.lock_for(review_date)
        if not lock.acquire(timeout=0.1):
            raise ApiError(409, f"{review_date} 的操作正在执行中")
        try:
            self.server.claim(review_date)
        except Exception:
            lock.release()
            raise
        try:
            return action()
        finally:
            self.server.release(review_date)
            lock.release()

    def _read_json(self) -> Dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header or "0")
        except ValueError as exc:
            raise ApiError(400, "Content-Length 无效") from exc
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "请求体过大")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "请求体必须是合法 JSON") from exc
        if not isinstance(value, dict):
            raise ApiError(422, "请求体必须是 JSON 对象")
        return value

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self._add_common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _add_common_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[review-api] {self.address_string()} {format % args}")


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ApiError(422, f"{name} 必须是布尔值")
    return value


def run_api(
    host: str = "127.0.0.1",
    port: int = 8787,
    service: Optional[WorkflowService] = None,
) -> None:
    workflow_service = service or WorkflowService(create_workflow_agent)
    server = ReviewApiServer((host, port), workflow_service)
    print(f"Review workflow API listening on http://{host}:{port}")
    print("OpenAPI contract: http://%s:%d/api/openapi" % (host, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run_api()
