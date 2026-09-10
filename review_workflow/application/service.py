"""Frontend- and CLI-facing use cases."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict

from review_workflow.domain.config import WorkflowConfig
from review_workflow.domain.models import WorkflowState, workflow_progress

from .agent import WorkflowAgent


AgentFactory = Callable[[WorkflowConfig], WorkflowAgent]


class WorkflowService:
    def __init__(self, agent_factory: AgentFactory):
        self.agent_factory = agent_factory

    def start(self, config: WorkflowConfig, force: bool = False) -> Dict[str, Any]:
        agent = self.agent_factory(config)
        state = agent.start(force=force)
        return self._payload(agent, state)

    def run(self, config: WorkflowConfig) -> Dict[str, Any]:
        agent = self.agent_factory(config)
        state = agent.run()
        return self._payload(agent, state)

    def resume(
        self,
        config: WorkflowConfig,
        retry_failed: bool = True,
        execute: bool = False,
    ) -> Dict[str, Any]:
        agent = self.agent_factory(config)
        state = agent.resume(retry_failed=retry_failed)
        if execute:
            state = agent.run()
        return self._payload(agent, state)

    def step(self, config: WorkflowConfig) -> Dict[str, Any]:
        agent = self.agent_factory(config)
        state = agent.step()
        return self._payload(agent, state)

    def status(self, config: WorkflowConfig) -> Dict[str, Any]:
        agent = self.agent_factory(config)
        agent.load()
        return agent.status()

    def render(self, config: WorkflowConfig) -> Dict[str, Any]:
        agent = self.agent_factory(config)
        output_path = agent.render_only()
        payload = agent.status()
        payload["output_path"] = str(output_path)
        return payload

    def document(self, config: WorkflowConfig) -> str:
        agent = self.agent_factory(config)
        state = agent.load()
        if not state.output_path:
            raise FileNotFoundError("最终复盘文档尚未生成")
        path = Path(state.output_path)
        if not path.is_file():
            raise FileNotFoundError(f"最终复盘文档不存在：{path}")
        return path.read_text(encoding="utf-8")

    def _payload(self, agent: WorkflowAgent, state: WorkflowState) -> Dict[str, Any]:
        payload = state.to_dict()
        payload["progress"] = workflow_progress(state).to_dict()
        payload["state_path"] = str(getattr(agent.store, "path", ""))
        return payload
