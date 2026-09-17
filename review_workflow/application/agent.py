"""Step-based application service for the review workflow."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from review_workflow.domain.config import WorkflowConfig
from review_workflow.domain.document import render_review_document
from review_workflow.domain.models import (
    TASK_ANALYSIS_FAILED,
    TASK_ANALYZED,
    TASK_CAPTURE_FAILED,
    TASK_CAPTURED,
    WORKFLOW_COMPLETED,
    WORKFLOW_FAILED,
    WORKFLOW_PARTIAL,
    WORKFLOW_RUNNING,
    StockTask,
    WorkflowState,
    workflow_progress,
)

from .errors import WorkflowConflictError, WorkflowNotFoundError
from .ports import ReviewGateway, StateStore


class WorkflowAgent:
    """Advance a review workflow one auditable step at a time."""

    def __init__(
        self,
        config: WorkflowConfig,
        store: StateStore,
        gateway: ReviewGateway,
    ):
        self.config = config
        self.store = store
        self.gateway = gateway
        self.state: Optional[WorkflowState] = None

    def start(self, force: bool = False) -> WorkflowState:
        if self.store.exists() and not force:
            raise WorkflowConflictError(
                f"工作流状态已存在：{getattr(self.store, 'path', '')}。"
                "使用 resume 继续，或使用 force 重新开始。"
            )

        self.gateway.parse_review_date(self.config.review_date)
        holdings = self.gateway.load_holdings(self.config)
        run_id = (
            f"review-{self.config.review_date.replace('-', '')}-"
            f"{datetime.now().strftime('%H%M%S')}"
        )
        self.state = WorkflowState(
            run_id=run_id,
            review_date=self.config.review_date,
            config=self.config.to_dict(),
            stocks=[StockTask(holding=holding) for holding in holdings],
        )
        self._record("工作流初始化完成")
        self._save_initial(force=force)
        return self.state

    def resume(self, retry_failed: bool = True) -> WorkflowState:
        state = self._load_required()
        self.state = state
        self.config = self._merged_resume_config()
        state.config = self.config.to_dict()

        if retry_failed:
            for task in state.stocks:
                self._reset_failed_task(task)

        state.current_stock_index = 0
        has_incomplete = any(task.status != TASK_ANALYZED for task in state.stocks)
        state.stage = "capture" if has_incomplete else "summary"
        state.finished_at = None
        state.status = WORKFLOW_RUNNING
        self._record("工作流恢复")
        self.store.save(state)
        return state

    def load(self) -> WorkflowState:
        state = self._load_required()
        self.state = state
        return state

    def run(self) -> WorkflowState:
        if self.state is None:
            if self.store.exists():
                self.resume(retry_failed=True)
            else:
                self.start()
        while self.state is not None and self.state.status == WORKFLOW_RUNNING:
            self.step()
        assert self.state is not None
        return self.state

    def step(self) -> WorkflowState:
        if self.state is None:
            self.load()
            self.config = self._merged_resume_config()
        assert self.state is not None
        state = self.state
        if state.status != WORKFLOW_RUNNING:
            return state

        try:
            if state.stage in {"capture", "analyze"}:
                self._step_one_stock()
            elif state.stage == "summary":
                self._step_summary()
            elif state.stage == "render":
                self._step_render()
            else:
                raise RuntimeError(f"未知工作流阶段：{state.stage}")
        except Exception as exc:
            state.status = WORKFLOW_FAILED
            state.finished_at = _now()
            self._record(f"工作流异常终止：{exc}", level="error")
            self.store.save(state)
            raise
        return state

    def status(self) -> Dict[str, Any]:
        if self.state is None:
            self.load()
        assert self.state is not None
        payload = self.state.to_dict()
        payload["progress"] = workflow_progress(self.state).to_dict()
        payload["state_path"] = str(getattr(self.store, "path", ""))
        return payload

    def render_only(self) -> Path:
        if self.state is None:
            self.load()
            self.config = self._merged_resume_config()
        output_path = self._write_document()
        self.store.save(self._required_state())
        return output_path

    def _step_one_stock(self) -> None:
        state = self._required_state()
        if state.current_stock_index >= len(state.stocks):
            state.stage = "summary"
            state.touch()
            self.store.save(state)
            return

        task = state.stocks[state.current_stock_index]
        if task.stage == "capture":
            self._execute_capture(task)
            if task.status != TASK_CAPTURE_FAILED:
                state.touch()
                self.store.save(state)
                return
        elif task.stage == "analyze":
            self._execute_analysis(task)
        elif task.status not in {
            TASK_CAPTURE_FAILED,
            TASK_ANALYSIS_FAILED,
            TASK_ANALYZED,
        }:
            raise RuntimeError(f"未知个股阶段：{task.stage}")

        state.current_stock_index += 1
        state.touch()
        self.store.save(state)

    def _execute_capture(self, task: StockTask) -> None:
        state = self._required_state()
        task.status = "capturing"
        task.capture_attempts += 1
        task.error = None
        task.touch()
        state.touch()
        self.store.save(state)

        try:
            result = self.gateway.capture(
                task.holding,
                self.config,
                self._seen_codes_before(task),
            )
            task.stock_dir = str(result.stock_dir)
            task.resolved_stock = result.resolved_stock
            task.status = TASK_CAPTURED
            task.stage = "analyze"
            task.error = None
            self._record(f"{task.holding['name']} 截图阶段完成")
        except Exception as exc:
            task.status = TASK_CAPTURE_FAILED
            task.stage = "capture"
            task.error = str(exc)
            self._record(f"{task.holding['name']} 截图阶段失败：{exc}", level="error")
        task.touch()
        state.touch()
        self.store.save(state)

    def _execute_analysis(self, task: StockTask) -> None:
        state = self._required_state()
        if not task.stock_dir:
            task.status = TASK_ANALYSIS_FAILED
            task.error = "缺少个股截图目录，无法执行 AI 复盘"
            self._record(task.error, level="error")
            return

        task.status = "analyzing"
        task.analysis_attempts += 1
        task.error = None
        task.touch()
        state.touch()
        self.store.save(state)

        try:
            result = self.gateway.analyze(
                task.holding,
                Path(task.stock_dir),
                self.config,
            )
            task.resolved_stock = result.stock
            task.individual_images = result.individual_images
            task.board_images = result.board_images
            task.review_path = str(result.review_path)
            task.status = TASK_ANALYZED
            task.stage = "done"
            task.error = None
            self._record(f"{task.holding['name']} 个股复盘生成完成")
        except Exception as exc:
            task.status = TASK_ANALYSIS_FAILED
            task.stage = "analyze"
            task.error = str(exc)
            self._record(f"{task.holding['name']} 复盘生成失败：{exc}", level="error")
        task.touch()
        state.touch()
        self.store.save(state)

    def _step_summary(self) -> None:
        state = self._required_state()
        successful = self._successful_tasks()
        if not successful or self.config.no_portfolio_summary:
            state.portfolio_summary = ""
            state.stage = "render"
            state.touch()
            self.store.save(state)
            return

        try:
            reviews: List[Dict[str, Any]] = []
            for task in successful:
                assert task.review_path is not None
                reviews.append(
                    {
                        "holding": task.holding,
                        "stock": task.resolved_stock or {},
                        "review_path": task.review_path,
                        "content": Path(task.review_path).read_text(encoding="utf-8"),
                    }
                )
            state.portfolio_summary = self.gateway.summarize(
                state.review_date,
                reviews,
                self.config,
            )
            self._record("组合级复盘摘要生成完成")
        except Exception as exc:
            state.portfolio_summary = (
                "组合级摘要自动生成失败，请在下方个股复盘中人工汇总。\n\n"
                f"失败原因：{exc}"
            )
            self._record(f"组合级摘要生成失败：{exc}", level="error")

        state.stage = "render"
        state.touch()
        self.store.save(state)

    def _step_render(self) -> None:
        state = self._required_state()
        successful = self._successful_tasks()
        failed = self._failed_tasks()
        if successful and not failed:
            state.status = WORKFLOW_COMPLETED
        elif successful:
            state.status = WORKFLOW_PARTIAL
        else:
            state.status = WORKFLOW_FAILED

        output_path = self._write_document()
        state.stage = "done"
        state.output_path = str(output_path)
        state.finished_at = _now()
        self._record(f"最终复盘文档已输出：{output_path}")
        self.store.save(state)

    def _write_document(self) -> Path:
        state = self._required_state()
        stock_reviews: List[Dict[str, Any]] = []
        for task in self._successful_tasks():
            stock_reviews.append(
                {
                    "holding": task.holding,
                    "stock": task.resolved_stock or {},
                    "review_path": task.review_path,
                }
            )

        failures = [
            {
                "stock": task.holding.get("name", ""),
                "stage": "截图" if task.status == TASK_CAPTURE_FAILED else "AI 复盘",
                "error": task.error or "待核实",
            }
            for task in self._failed_tasks()
        ]
        runtime = self.gateway.runtime_info(self.config)
        metadata = {
            "workflow_status": state.status,
            "provider": runtime.provider,
            "model": runtime.model,
            "search_provider": runtime.search_provider,
            "generated_at": _now(),
            "artifact_root": str(runtime.date_root),
            "state_path": str(getattr(self.store, "path", "")),
        }
        content = render_review_document(
            state.review_date,
            [task.holding for task in state.stocks],
            stock_reviews,
            state.portfolio_summary,
            failures,
            metadata,
        )
        output_path = runtime.output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
        state.output_path = str(output_path)
        return output_path

    def _reset_failed_task(self, task: StockTask) -> None:
        if task.status == TASK_CAPTURE_FAILED:
            task.stage = "capture"
            task.status = "pending"
            task.error = None
        elif task.status == TASK_ANALYSIS_FAILED:
            task.stage = "analyze"
            task.status = TASK_CAPTURED
            task.error = None
        else:
            return
        task.touch()

    def _merged_resume_config(self) -> WorkflowConfig:
        state = self._required_state()
        persisted = dict(state.config or {})
        current = self.config.to_dict()
        defaults = WorkflowConfig(review_date=self.config.review_date).to_dict()
        for key, value in current.items():
            if key == "state_file" or value != defaults[key]:
                persisted[key] = value
        persisted["review_date"] = state.review_date
        persisted["state_file"] = str(getattr(self.store, "path"))
        return WorkflowConfig.from_dict(persisted)

    def _successful_tasks(self) -> List[StockTask]:
        return [
            task
            for task in self._required_state().stocks
            if task.status == TASK_ANALYZED
        ]

    def _failed_tasks(self) -> List[StockTask]:
        return [
            task
            for task in self._required_state().stocks
            if task.status in {TASK_CAPTURE_FAILED, TASK_ANALYSIS_FAILED}
        ]

    def _seen_codes_before(self, current: StockTask) -> Iterable[str]:
        codes: List[str] = []
        for task in self._required_state().stocks:
            if task is current:
                break
            stock = task.resolved_stock or {}
            code = str(stock.get("code"))
            if code:
                codes.append(code)
        return codes

    def _record(self, message: str, level: str = "info") -> None:
        state = self._required_state()
        state.events.append({"at": _now(), "level": level, "message": message})
        if len(state.events) > 500:
            del state.events[: len(state.events) - 500]
        state.touch()

    def _save_initial(self, force: bool) -> None:
        reset = getattr(self.store, "reset", None)
        assert self.state is not None
        if force and callable(reset):
            reset(self.state)
        else:
            self.store.save(self.state)

    def _load_required(self) -> WorkflowState:
        state = self.store.load()
        if state is None:
            raise WorkflowNotFoundError(
                f"未找到工作流状态：{getattr(self.store, 'path', '')}"
            )
        return state

    def _required_state(self) -> WorkflowState:
        if self.state is None:
            raise RuntimeError("工作流尚未初始化")
        return self.state


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
