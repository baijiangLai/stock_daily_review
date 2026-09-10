"""Ports required by the application layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence

from review_workflow.domain.config import WorkflowConfig
from review_workflow.domain.models import WorkflowState


@dataclass(frozen=True)
class CaptureResult:
    stock_dir: Path
    resolved_stock: Dict[str, Any]


@dataclass(frozen=True)
class AnalysisResult:
    stock: Dict[str, Any]
    individual_images: int
    board_images: int
    review_path: Path


@dataclass(frozen=True)
class RuntimeInfo:
    provider: str
    model: str
    search_provider: str
    date_root: Path
    output_path: Path


class StateStore(Protocol):
    def exists(self) -> bool: ...

    def load(self) -> Optional[WorkflowState]: ...

    def save(self, state: WorkflowState) -> None: ...


class ReviewGateway(Protocol):
    def parse_review_date(self, value: str) -> date: ...

    def load_holdings(self, config: WorkflowConfig) -> List[Dict[str, str]]: ...

    def capture(
        self,
        holding: Mapping[str, str],
        config: WorkflowConfig,
        seen_codes: Iterable[str],
    ) -> CaptureResult: ...

    def analyze(
        self,
        holding: Mapping[str, str],
        stock_dir: Path,
        config: WorkflowConfig,
    ) -> AnalysisResult: ...

    def summarize(
        self,
        review_date: str,
        reviews: Sequence[Mapping[str, Any]],
        config: WorkflowConfig,
    ) -> str: ...

    def runtime_info(self, config: WorkflowConfig) -> RuntimeInfo: ...
