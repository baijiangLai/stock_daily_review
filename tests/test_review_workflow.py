import json
import socket
import tempfile
import threading
import unittest
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from review_workflow.application.agent import WorkflowAgent
from review_workflow.application.ports import AnalysisResult, CaptureResult, RuntimeInfo
from review_workflow.application.service import WorkflowService
from review_workflow.domain.config import WorkflowConfig
from review_workflow.domain.document import render_review_document
from review_workflow.domain.models import (
    TASK_ANALYSIS_FAILED,
    TASK_ANALYZED,
    TASK_CAPTURED,
    StockTask,
    WorkflowState,
)
from review_workflow.infrastructure.json_state_store import WorkflowStateStore
from review_workflow.infrastructure.legacy_gateway import LegacyPortfolioGateway
from review_workflow.interfaces.http_api import ApiError, ReviewApiServer, config_from_payload


class FakeGateway:
    def parse_review_date(self, value: str) -> date:
        return date.fromisoformat(value)

    def load_holdings(self, config: WorkflowConfig) -> List[Dict[str, str]]:
        if config.holdings:
            return [dict(item) for item in config.holdings]
        holdings: List[Dict[str, str]] = []
        for line in config.portfolio_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            name, cost, shares, plan = [item.strip() for item in line.split(",")]
            holdings.append(
                {"name": name, "cost": cost, "shares": shares, "plan": plan}
            )
        return holdings

    def capture(
        self,
        holding: Mapping[str, str],
        config: WorkflowConfig,
        seen_codes: Iterable[str],
    ) -> CaptureResult:
        stock_dir = config.state_file.parent / f"{holding['name']}_{len(seen_codes)}"
        stock_dir.mkdir(parents=True, exist_ok=True)
        return CaptureResult(
            stock_dir=stock_dir,
            resolved_stock={"name": holding["name"], "code": f"{len(seen_codes)}"},
        )

    def analyze(
        self,
        holding: Mapping[str, str],
        stock_dir: Path,
        config: WorkflowConfig,
    ) -> AnalysisResult:
        review_path = stock_dir / "fake当日复盘.md"
        review_path.write_text(f"# {holding['name']} 复盘", encoding="utf-8")
        return AnalysisResult(
            stock={"name": holding["name"], "code": stock_dir.name.rsplit("_", 1)[-1]},
            individual_images=4,
            board_images=9,
            review_path=review_path,
        )

    def summarize(
        self,
        review_date: str,
        reviews: Sequence[Mapping[str, Any]],
        config: WorkflowConfig,
    ) -> str:
        return f"组合摘要：{len(reviews)} 只个股。"

    def runtime_info(self, config: WorkflowConfig) -> RuntimeInfo:
        date_root = config.state_file.parent
        return RuntimeInfo(
            provider="fake",
            model="fake-model",
            search_provider="none",
            date_root=date_root,
            output_path=config.output_file or date_root / "final.md",
        )


def _can_bind_local_port() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False


class ReviewWorkflowTest(unittest.TestCase):
    def test_default_document_stays_in_review_date_directory(self) -> None:
        config = WorkflowConfig(review_date="2026-09-10")

        local_runtime = LegacyPortfolioGateway().runtime_info(config)
        model_runtime = LegacyPortfolioGateway().runtime_info(
            WorkflowConfig(review_date="2026-09-10", provider="gemini")
        )

        expected = (
            Path(__file__).resolve().parents[1]
            / "screenshots"
            / "2026"
            / "09"
            / "10"
            / "20260910_持股个股复盘.md"
        )
        self.assertEqual(local_runtime.output_path, expected)
        self.assertEqual(model_runtime.output_path, expected)

    def test_api_accepts_local_provider(self) -> None:
        config = config_from_payload(
            {
                "date": "2026-09-10",
                "provider": "local",
                "holdings": [
                    {
                        "name": "示例股票A",
                        "cost": 10,
                        "shares": 100,
                        "plan": "1年之内",
                    }
                ],
            }
        )

        self.assertEqual(config.provider, "local")

    def test_document_renderer_preserves_reviews_and_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            review_path = Path(directory) / "fake当日复盘.md"
            review_path.write_text("# 测试个股复盘\n\n包含技术面与仓位计划。", encoding="utf-8")
            document = render_review_document(
                "2026-09-10",
                [{"name": "示例股票B", "cost": "20.00", "shares": "200", "plan": "6个月"}],
                [
                    {
                        "holding": {"name": "示例股票B"},
                        "stock": {"name": "示例股票B", "code": "000002"},
                        "review_path": str(review_path),
                    }
                ],
                "组合摘要：主线集中，注意回踩承接。",
                [{"stock": "示例股票A", "stage": "AI 复盘", "error": "API 429"}],
                {
                    "workflow_status": "partial_completed",
                    "provider": "gemini",
                    "model": "gemini-3.7-flash",
                    "search_provider": "model",
                    "generated_at": "2026-09-10 10:00:00",
                    "artifact_root": directory,
                    "state_path": str(Path(directory) / "workflow_state.json"),
                },
            )

            self.assertIn("2026.9.10 持股个股复盘", document)
            self.assertIn("成功生成个股复盘：1 / 1", document)
            self.assertIn("示例股票A | AI 复盘 | API 429", document)
            self.assertIn("包含技术面与仓位计划", document)
            self.assertIn("审计材料", document)

    def test_api_payload_accepts_frontend_holding_form(self) -> None:
        config = config_from_payload(
            {
                "date": "2026-09-10",
                "holdings": [
                    {
                        "name": "示例股票B",
                        "cost": "20.00",
                        "shares": "200",
                        "plan": "6个月内",
                    }
                ],
            }
        )

        self.assertEqual(
            config.holdings,
            [
                {
                    "name": "示例股票B",
                    "cost": "20.00",
                    "shares": "200",
                    "plan": "6个月内",
                }
            ],
        )

        with self.assertRaises(ApiError):
            config_from_payload(
                {
                    "date": "2026-09-10",
                    "holdings": [
                        {
                            "name": "示例股票B",
                            "cost": 20.00,
                            "shares": 200,
                            "plan": "永久持有",
                        }
                    ],
                }
            )

    def test_state_store_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "workflow_state.json"
            store = WorkflowStateStore(path)
            state = WorkflowState(
                run_id="review-test",
                review_date="2026-09-10",
                stocks=[
                    StockTask(
                        holding={
                            "name": "示例股票A",
                            "cost": "10.00",
                            "shares": "100",
                            "plan": "1年",
                        },
                        status=TASK_CAPTURED,
                        stage="analyze",
                    )
                ],
            )
            store.save(state)
            loaded = store.load()

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.run_id, "review-test")
            self.assertEqual(loaded.stocks[0].status, TASK_CAPTURED)

    def test_agent_advances_atomic_steps_and_persists_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            portfolio_path = root / "my_stock.txt"
            portfolio_path.write_text(
                "示例股票A,10.00,100,1年\n示例股票B,20.00,200,6个月\n",
                encoding="utf-8",
            )
            state_path = root / "workflow_state.json"
            config = WorkflowConfig(
                review_date="2026-09-10",
                portfolio_file=portfolio_path,
                template_file=root / "template.md",
                output_file=root / "final.md",
                state_file=state_path,
            )
            agent = WorkflowAgent(
                config,
                WorkflowStateStore(state_path),
                FakeGateway(),
            )
            agent.start()

            for _ in range(7):
                state = agent.step()
                if state.status == "running":
                    continue
                break

            self.assertEqual(agent.state.status if agent.state else None, "completed")
            self.assertTrue(state_path.is_file())
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [item["status"] for item in persisted["stocks"]],
                ["analyzed", "analyzed"],
            )
            self.assertTrue((root / "final.md").is_file())

    def test_resume_resets_analysis_failure_without_recapture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "workflow_state.json"
            store = WorkflowStateStore(state_path)
            persisted_output = Path(directory) / "persisted.md"
            store.save(
                WorkflowState(
                    run_id="review-test",
                    review_date="2026-09-10",
                    status="partial_completed",
                    stage="done",
                    config=WorkflowConfig(
                        review_date="2026-09-10",
                        output_file=persisted_output,
                    ).to_dict(),
                    stocks=[
                        StockTask(
                            holding={
                                "name": "示例股票A",
                                "cost": "10.00",
                                "shares": "100",
                                "plan": "1年",
                            },
                            status=TASK_ANALYSIS_FAILED,
                            stage="analyze",
                            stock_dir=str(Path(directory) / "示例股票A_000157"),
                        )
                    ],
                )
            )
            config = WorkflowConfig(
                review_date="2026-09-10",
                state_file=state_path,
                provider="zhipu",
                search_provider="model",
            )
            agent = WorkflowAgent(config, store, FakeGateway())
            resumed = agent.resume(retry_failed=True)

            self.assertEqual(resumed.status, "running")
            self.assertEqual(resumed.stage, "capture")
            self.assertEqual(resumed.current_stock_index, 0)
            self.assertEqual(resumed.stocks[0].status, TASK_CAPTURED)
            self.assertEqual(resumed.stocks[0].stage, "analyze")
            self.assertIsNone(resumed.stocks[0].error)
            self.assertEqual(resumed.config["output_file"], str(persisted_output))
            self.assertEqual(resumed.config["provider"], "zhipu")
            self.assertEqual(resumed.config["search_provider"], "model")

    @unittest.skipIf(not _can_bind_local_port(), "当前沙箱禁止本地端口监听")
    def test_http_api_create_step_status_and_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            portfolio_path = root / "my_stock.txt"
            portfolio_path.write_text(
                "示例股票A,10.00,100,1年\n示例股票B,20.00,200,6个月\n",
                encoding="utf-8",
            )
            state_path = root / "workflow_state.json"

            def factory(config: WorkflowConfig) -> WorkflowAgent:
                config.portfolio_file = portfolio_path
                config.state_file = state_path
                config.output_file = root / "final.md"
                return WorkflowAgent(
                    config,
                    WorkflowStateStore(state_path),
                    FakeGateway(),
                )

            service = WorkflowService(factory)
            server = ReviewApiServer(("127.0.0.1", 0), service)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                health = self._request(f"{base_url}/api/health")
                self.assertEqual(health["ok"], True)

                created = self._request(
                    f"{base_url}/api/review-runs",
                    method="POST",
                    payload={
                        "date": "2026-09-10",
                        "provider": "auto",
                        "holdings": [
                            {
                                "name": "示例股票A",
                                "cost": 10.00,
                                "shares": "100",
                                "plan": "1年之内",
                            },
                            {
                                "name": "示例股票B",
                                "cost": "20.00",
                                "shares": 200,
                                "plan": "6个月内",
                            },
                        ],
                    },
                )
                self.assertEqual(created["review_date"], "2026-09-10")
                self.assertEqual(
                    [item["holding"]["name"] for item in created["stocks"]],
                    ["示例股票A", "示例股票B"],
                )

                final = {}
                for _ in range(20):
                    final = self._request(
                        f"{base_url}/api/review-runs/2026-09-10/steps", method="POST"
                    )
                    if final.get("status") != "running":
                        break
                self.assertEqual(final.get("status"), "completed")

                status = self._request(f"{base_url}/api/review-runs/2026-09-10")
                self.assertEqual(status["progress"]["analyzed"], 2)
                self.assertFalse(status["active"])

                document = self._request(
                    f"{base_url}/api/review-runs/2026-09-10/document"
                )
                self.assertIn("持股个股复盘", document)
                self.assertIn("示例股票A 复盘", document)

                schema = self._request(f"{base_url}/api/openapi")
                self.assertIn("/api/review-runs", schema["paths"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def _request(
        self,
        url: str,
        method: str = "GET",
        payload: Any = None,
    ) -> Any:
        data = None
        headers = {}
        if payload is not None or method == "POST":
            data = json.dumps(payload or {}).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=3) as response:
            body = response.read().decode("utf-8")
            content_type = response.headers.get("Content-Type", "")
            if "json" in content_type:
                return json.loads(body)
            return body


if __name__ == "__main__":
    unittest.main()
