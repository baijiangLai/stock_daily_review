"""Atomic persistence for workflow snapshots."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Optional

from review_workflow.domain.models import WorkflowState


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCREENSHOT_ROOT = PROJECT_ROOT / "screenshots"


def default_state_path(review_date: str) -> Path:
    year, month, day = review_date.split("-")
    return SCREENSHOT_ROOT / year / month / day / "workflow_state.json"


class WorkflowStateStore:
    def __init__(self, path: Path):
        self.path = path

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> Optional[WorkflowState]:
        if not self.path.is_file():
            return None
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return WorkflowState.from_dict(payload)

    def save(self, state: WorkflowState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def reset(self, state: WorkflowState) -> None:
        if self.path.exists():
            archive_suffix = state.created_at.replace(":", "").replace("-", "")
            archived = self.path.with_name(f"workflow_state_{archive_suffix}.json")
            counter = 1
            while archived.exists():
                archived = self.path.with_name(
                    f"workflow_state_{archive_suffix}_{counter}.json"
                )
                counter += 1
            self.path.rename(archived)
        self.save(state)
