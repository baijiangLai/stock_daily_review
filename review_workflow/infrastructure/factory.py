"""Composition root for infrastructure implementations."""

from __future__ import annotations

from review_workflow.application.agent import WorkflowAgent
from review_workflow.domain.config import WorkflowConfig

from .json_state_store import WorkflowStateStore, default_state_path
from .legacy_gateway import LegacyPortfolioGateway


def create_workflow_agent(config: WorkflowConfig) -> WorkflowAgent:
    store = WorkflowStateStore(
        config.state_file or default_state_path(config.review_date)
    )
    return WorkflowAgent(config, store, LegacyPortfolioGateway())
