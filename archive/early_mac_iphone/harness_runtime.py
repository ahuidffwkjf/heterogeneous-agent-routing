"""Local Harness runtime for discovering and composing internal executors.

The global Controller sees a Harness endpoint as one opaque execution unit.
This module represents the private layer inside that Harness: it discovers
local Agents/Agent Teams and creates a task-specific internal plan. The plan
is intentionally not returned by the public Controller API.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from router import ExecutionUnit, Task


@dataclass(frozen=True)
class InternalAgent:
    agent_id: str
    capabilities: frozenset[str]
    tools: frozenset[str]
    state: str = "idle"
    quality_score: float = 0.8

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InternalAgent":
        return cls(
            agent_id=str(data["agent_id"]),
            capabilities=frozenset(data.get("capabilities", [])),
            tools=frozenset(data.get("tools", [])),
            state=str(data.get("state", "idle")),
            quality_score=float(data.get("quality_score", 0.8)),
        )


@dataclass(frozen=True)
class InternalTeamPlan:
    execution_id: str
    mode: str  # single_agent | agent_team
    member_ids: tuple[str, ...]
    missing_capabilities: tuple[str, ...] = ()
    missing_tools: tuple[str, ...] = ()


class HarnessCannotServe(RuntimeError):
    pass


class HarnessRuntime:
    """Discovers and composes internal executors behind one Harness API."""

    def __init__(self, harness: ExecutionUnit) -> None:
        self.harness = harness
        self._agents: dict[str, InternalAgent] = {}
        self.discover()

    def discover(self) -> list[InternalAgent]:
        raw_agents = self.harness.metadata.get("internal_agents", [])
        if not isinstance(raw_agents, list):
            raw_agents = []
        discovered = [InternalAgent.from_dict(item) for item in raw_agents]
        if not discovered:
            # A Harness can expose itself as one default local Agent. This
            # keeps legacy single-agent endpoints compatible.
            discovered = [
                InternalAgent(
                    agent_id=f"{self.harness.unit_id}/default",
                    capabilities=frozenset(self.harness.capabilities),
                    tools=frozenset(self.harness.tools),
                    quality_score=self.harness.quality_score,
                )
            ]
        self._agents = {agent.agent_id: agent for agent in discovered}
        return list(self._agents.values())

    def compose(self, task: Task) -> InternalTeamPlan:
        available = [
            agent for agent in self._agents.values()
            if agent.state not in {"offline", "failed"}
        ]
        required_capabilities = set(task.required_capabilities)
        required_tools = set(task.required_tools)

        direct = [
            agent for agent in available
            if required_capabilities.issubset(agent.capabilities)
            and required_tools.issubset(agent.tools)
        ]
        if direct:
            selected = max(direct, key=lambda agent: agent.quality_score)
            return InternalTeamPlan(
                execution_id=uuid.uuid4().hex[:12],
                mode="single_agent",
                member_ids=(selected.agent_id,),
            )

        # Greedy local team formation: add the highest-quality Agent that
        # contributes a missing capability or tool until requirements close.
        selected: list[InternalAgent] = []
        covered_capabilities: set[str] = set()
        covered_tools: set[str] = set()
        remaining = list(available)
        while remaining and (
            not required_capabilities.issubset(covered_capabilities)
            or not required_tools.issubset(covered_tools)
        ):
            candidate = max(
                remaining,
                key=lambda agent: (
                    len((agent.capabilities - covered_capabilities) & required_capabilities)
                    + len((agent.tools - covered_tools) & required_tools),
                    agent.quality_score,
                ),
            )
            contribution = (
                (candidate.capabilities - covered_capabilities) & required_capabilities
            ) or ((candidate.tools - covered_tools) & required_tools)
            if not contribution:
                break
            selected.append(candidate)
            covered_capabilities.update(candidate.capabilities)
            covered_tools.update(candidate.tools)
            remaining.remove(candidate)

        missing_capabilities = tuple(sorted(required_capabilities - covered_capabilities))
        missing_tools = tuple(sorted(required_tools - covered_tools))
        if missing_capabilities or missing_tools:
            raise HarnessCannotServe(
                f"Harness {self.harness.unit_id} cannot compose a team for task "
                f"{task.task_id}: missing capabilities={missing_capabilities}, "
                f"tools={missing_tools}"
            )
        return InternalTeamPlan(
            execution_id=uuid.uuid4().hex[:12],
            mode="agent_team" if len(selected) > 1 else "single_agent",
            member_ids=tuple(agent.agent_id for agent in selected),
        )

    def prepare(self, task: Task) -> InternalTeamPlan:
        self.discover()
        return self.compose(task)

    def public_summary(self) -> dict[str, Any]:
        """Return aggregate information only; never expose member identities."""
        return {
            "harness_id": self.harness.unit_id,
            "harness_type": self.harness.metadata.get("harness_type", "unknown"),
            "internal_executor_count": len(self._agents),
            "internal_capabilities": sorted(
                set().union(*(agent.capabilities for agent in self._agents.values()))
            ),
        }
