"""Serializable state models shared by the agent, CLI and frontend."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


TASK_PENDING = "pending"
TASK_CAPTURING = "capturing"
TASK_CAPTURED = "captured"
TASK_ANALYZING = "analyzing"
TASK_ANALYZED = "analyzed"
TASK_CAPTURE_FAILED = "capture_failed"
TASK_ANALYSIS_FAILED = "analysis_failed"

WORKFLOW_RUNNING = "running"
WORKFLOW_COMPLETED = "completed"
WORKFLOW_PARTIAL = "partial_completed"
WORKFLOW_FAILED = "failed"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class StockTask:
    """One holding's position in the review workflow."""

    holding: Dict[str, Any]
    status: str = TASK_PENDING
    stage: str = "capture"
    stock_dir: Optional[str] = None
    resolved_stock: Optional[Dict[str, Any]] = None
    review_path: Optional[str] = None
    individual_images: int = 0
    board_images: int = 0
    capture_attempts: int = 0
    analysis_attempts: int = 0
    error: Optional[str] = None
    updated_at: str = field(default_factory=_now)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "StockTask":
        return cls(**value)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def touch(self) -> None:
        self.updated_at = _now()


@dataclass
class WorkflowState:
    """Complete, JSON-serializable workflow snapshot."""

    run_id: str
    review_date: str
    status: str = WORKFLOW_RUNNING
    stage: str = "capture"
    current_stock_index: int = 0
    config: Dict[str, Any] = field(default_factory=dict)
    stocks: List[StockTask] = field(default_factory=list)
    portfolio_summary: str = ""
    output_path: Optional[str] = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    finished_at: Optional[str] = None
    events: List[Dict[str, str]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "WorkflowState":
        stocks = [StockTask.from_dict(item) for item in value.get("stocks", [])]
        return cls(
            run_id=value["run_id"],
            review_date=value["review_date"],
            status=value.get("status", WORKFLOW_RUNNING),
            stage=value.get("stage", "capture"),
            current_stock_index=int(value.get("current_stock_index", 0)),
            config=dict(value.get("config", {})),
            stocks=stocks,
            portfolio_summary=value.get("portfolio_summary", ""),
            output_path=value.get("output_path"),
            created_at=value.get("created_at", _now()),
            updated_at=value.get("updated_at", _now()),
            finished_at=value.get("finished_at"),
            events=list(value.get("events", [])),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "review_date": self.review_date,
            "status": self.status,
            "stage": self.stage,
            "current_stock_index": self.current_stock_index,
            "config": self.config,
            "stocks": [task.to_dict() for task in self.stocks],
            "portfolio_summary": self.portfolio_summary,
            "output_path": self.output_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "events": self.events,
        }

    def touch(self) -> None:
        self.updated_at = _now()


@dataclass
class WorkflowProgress:
    total: int
    analyzed: int
    failed: int
    remaining: int

    def to_dict(self) -> Dict[str, int]:
        return asdict(self)


def workflow_progress(state: WorkflowState) -> WorkflowProgress:
    analyzed = sum(task.status == TASK_ANALYZED for task in state.stocks)
    failed = sum(
        task.status in {TASK_CAPTURE_FAILED, TASK_ANALYSIS_FAILED}
        for task in state.stocks
    )
    return WorkflowProgress(
        total=len(state.stocks),
        analyzed=analyzed,
        failed=failed,
        remaining=max(len(state.stocks) - analyzed - failed, 0),
    )
