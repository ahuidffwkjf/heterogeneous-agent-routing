"""Controller for the DSH + MicroVM experiment.

The Controller owns task state and the execution-unit registry. The Router
only makes a routing decision from the latest registry snapshot. Agents can
join at runtime by POSTing /registry/register and stay available by sending
heartbeats to /registry/heartbeat. Newly registered units enter a separate
background canary lane until their probe evidence promotes them to the
foreground routing pool.

Dynamic registry endpoints require the X-Registry-Token header. Never expose
an unauthenticated registration endpoint on a LAN.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from microvm_pool import (
    CubeSandboxBackend,
    MicroVMNode,
    MicroVMPoolManager,
    SharedSnapshotStore,
)
from registry import UnitRegistry
from router import ExecutionUnit, NoEligibleUnit, Router, Task, TaskParser


DEFAULT_REGISTRY = Path(__file__).with_name("execution_units_dsh.json")
DEFAULT_DATABASE = Path(__file__).with_name("registry.db")
FORBIDDEN_ROUTING_FIELDS = {
    "harness_id",
    "selected_harness",
    "selected_unit",
    "agent_id",
    "agent_team_id",
}


def load_execution_units(registry_path: str | Path) -> list[ExecutionUnit]:
    path = Path(registry_path).expanduser().resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_units = data.get("units", data) if isinstance(data, dict) else data
    if not isinstance(raw_units, list):
        raise ValueError("execution unit registry must contain a list or a 'units' list")
    return [ExecutionUnit.from_dict(item) for item in raw_units]


def serialize_unit(unit: ExecutionUnit) -> dict[str, Any]:
    result = asdict(unit)
    result["platforms"] = sorted(unit.platforms)
    result["capabilities"] = sorted(unit.capabilities)
    result["tools"] = sorted(unit.tools)
    # The global registry exposes a Harness summary. Internal Agent and Team
    # members remain private to the Harness runtime.
    metadata = dict(result.get("metadata", {}))
    metadata.pop("internal_agents", None)
    # Agent discovery stays inside the parent Harness. The global Router sees
    # only aggregate capabilities after a discovery probe succeeds.
    candidates = metadata.pop("discovery_candidates", None)
    if isinstance(candidates, dict):
        metadata["discovered_agent_count"] = len(candidates)
        states = {str(candidate.get("state", "unknown")) for candidate in candidates.values()}
        metadata["discovered_agent_states"] = {
            state: sum(
                1
                for candidate in candidates.values()
                if candidate.get("state") == state
            )
            for state in states
        }
    result["metadata"] = metadata
    result["harness_id"] = unit.unit_id
    result["scope"] = "harness"
    return result


@dataclass
class Job:
    job_id: str
    task: dict[str, Any]
    selected_unit: str
    selected_unit_type: str
    status: str = "queued"
    result: dict[str, Any] | None = None
    created_at: float = 0.0
    completed_at: float | None = None
    attempt: int = 1
    max_retries: int = 2
    retryable: bool = True
    lease_id: str = ""
    next_retry_at: float | None = None
    failure_history: list[dict[str, Any]] = field(default_factory=list)
    microvm_id: str | None = None
    microvm_profile: str | None = None
    microvm_node_id: str | None = None
    microvm_runtime_version: str | None = None


@dataclass
class BackgroundProbe:
    """A canary task used to onboard a newly registered Harness.

    Probes are intentionally separate from foreground Jobs.  A probe can
    update a unit's trust state, but it never becomes a user-visible task and
    never enters the foreground Router queue.
    """

    probe_id: str
    unit_id: str
    task_id: str
    description: str
    lease_id: str
    attempt: int = 1
    status: str = "queued"  # queued | running | completed | failed
    result: dict[str, Any] | None = None
    created_at: float = 0.0
    started_at: float | None = None
    completed_at: float | None = None
    kind: str = "onboarding"  # onboarding | agent_discovery
    candidate_id: str | None = None
    microvm_id: str | None = None
    microvm_profile: str | None = None
    microvm_node_id: str | None = None
    microvm_runtime_version: str | None = None


class PhaseOneController:
    def __init__(
        self,
        registry_path: str | Path = DEFAULT_REGISTRY,
        database_path: str | Path = DEFAULT_DATABASE,
        heartbeat_timeout: float = 15.0,
        monitor_interval: float = 2.0,
        max_retries: int = 2,
        registry_token: str | None = None,
        microvm_pool_size: int = 3,
        microvm_max_total: int = 8,
        microvm_snapshot_dir: str | Path | None = None,
        microvm_nodes: list[MicroVMNode | dict[str, Any]] | None = None,
        microvm_runtime_version: str = "dsh-0.1",
        probe_successes_required: int = 1,
        probe_max_attempts: int = 3,
        microvm_backend: Any | None = None,
    ) -> None:
        self.registry = UnitRegistry(database_path)
        self.registry_token = registry_token or secrets.token_urlsafe(24)
        units = load_execution_units(registry_path)

        # Seed the configured DSH Harnesses into SQLite. New remote Harnesses
        # must register with a token and heartbeat by calling the API.
        for unit in units:
            heartbeat_required = bool(unit.metadata.get("heartbeat_required", False))
            self.registry.register(unit, heartbeat_required=heartbeat_required)

        self.registry_path = str(registry_path)
        self.router = Router(self.registry.list_units())
        self.jobs: dict[str, Job] = {}
        for payload in self.registry.list_jobs():
            # Keep jobs written by the pre-MicroVM version readable.
            payload.setdefault("microvm_id", None)
            payload.setdefault("microvm_profile", None)
            payload.setdefault("microvm_node_id", None)
            payload.setdefault("microvm_runtime_version", None)
            self.jobs[payload["job_id"]] = Job(**payload)
        self.poll_queues: dict[str, queue.Queue[str]] = {}
        self.background_queues: dict[str, queue.Queue[str]] = {}
        self.background_probes: dict[str, BackgroundProbe] = {}
        snapshot_dir = microvm_snapshot_dir
        if snapshot_dir is None:
            snapshot_dir = Path(database_path).expanduser().resolve().parent / "microvm_snapshots"
        self.microvm_pool = MicroVMPoolManager(
            backend=microvm_backend,
            min_ready=max(0, int(microvm_pool_size)),
            max_total=max(max(1, int(microvm_max_total)), int(microvm_pool_size)),
            nodes=microvm_nodes,
            snapshot_store=SharedSnapshotStore(snapshot_dir),
            default_runtime_version=microvm_runtime_version,
        )
        self.lock = threading.RLock()
        self.heartbeat_timeout = heartbeat_timeout
        self.monitor_interval = monitor_interval
        self.default_max_retries = max(0, int(max_retries))
        self.probe_successes_required = max(1, int(probe_successes_required))
        self.probe_max_attempts = max(
            self.probe_successes_required,
            int(probe_max_attempts),
        )
        self._stop_event = threading.Event()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="controller-health-monitor",
            daemon=True,
        )
        self._refresh_router()
        self._ensure_microvm_pools()
        self._ensure_background_probes()
        self._recover_jobs_after_restart()
        self._monitor_thread.start()

    def _refresh_router(self) -> None:
        units = self.registry.list_units()
        with self.lock:
            self.router.units = {unit.unit_id: unit for unit in units}
            for unit in units:
                transport = unit.metadata.get("transport", "http" if unit.endpoint else "poll")
                if transport == "poll":
                    self.poll_queues.setdefault(unit.unit_id, queue.Queue())
                    self.background_queues.setdefault(unit.unit_id, queue.Queue())

    @staticmethod
    def _microvm_profile(unit: ExecutionUnit) -> str | None:
        sandbox = unit.metadata.get("sandbox", {})
        if isinstance(sandbox, dict) and sandbox.get("type") == "microvm":
            return str(sandbox.get("profile", unit.unit_id))
        if unit.metadata.get("sandbox_type") == "microvm":
            return str(unit.metadata.get("sandbox_profile", unit.unit_id))
        return None

    def _ensure_microvm_pools(self) -> None:
        for unit in self.router.units.values():
            # The checked-in DSH registry starts units as offline until their
            # adapters register and heartbeat.  Do not spend real
            # CubeSandbox capacity warming a Harness that is not connected.
            if unit.state == "offline":
                continue
            profile = self._microvm_profile(unit)
            if profile:
                self.microvm_pool.ensure_pool(profile)

    def _save_job_locked(self, job: Job) -> None:
        self.registry.save_job(asdict(job))

    def _recover_jobs_after_restart(self) -> None:
        now = time.time()
        with self.lock:
            for job in self.jobs.values():
                if job.status in ("queued", "running"):
                    job.status = "retry_wait"
                    job.attempt += 1
                    job.next_retry_at = now
                    job.lease_id = uuid.uuid4().hex
                    job.failure_history.append(
                        {
                            "attempt": job.attempt - 1,
                            "unit_id": job.selected_unit,
                            "failure_type": "controller_restart",
                            "error": "controller restarted before task completion",
                            "timestamp": now,
                        }
                    )
                self._save_job_locked(job)

    def register_unit(self, payload: dict[str, Any]) -> ExecutionUnit:
        unit_payload = payload.get("unit", payload)
        if not isinstance(unit_payload, dict) or not unit_payload.get("unit_id"):
            raise ValueError("registration requires a unit_id")
        unit_id = str(unit_payload["unit_id"])
        try:
            existing = self.registry.get(unit_id)
        except KeyError:
            existing = None
        unit = ExecutionUnit.from_dict(unit_payload)
        if existing:
            # A Harness may reconnect with only its static registration
            # fields. Preserve discovery state owned by Controller so a
            # reconnect cannot erase candidates waiting for background tests.
            for key in (
                "discovery_candidates",
                "discovered_capabilities",
                "discovered_tools",
            ):
                if key in existing.metadata and key not in unit.metadata:
                    unit.metadata[key] = existing.metadata[key]
        registration_mode = str(payload.get("registration_mode", "background"))
        is_new = existing is None
        should_onboard = registration_mode not in {"trusted", "eligible"}
        if is_new and should_onboard:
            unit.state = "testing"
            unit.metadata = {
                **unit.metadata,
                "routing_scope": "background",
                "onboarding_status": "testing",
                "probe_count": 0,
                "probe_successes": 0,
                "probe_failures": 0,
            }
        elif existing and should_onboard and (
            existing.state == "testing"
            or existing.metadata.get("onboarding_status") == "probe_failed"
        ):
            # Re-registration must not accidentally promote a unit merely
            # because its adapter reports an idle process state.
            unit.state = "testing"
            retrying_after_failure = (
                existing.metadata.get("onboarding_status") == "probe_failed"
            )
            unit.metadata = {
                **unit.metadata,
                "routing_scope": "background",
                "onboarding_status": "testing",
                "probe_count": (
                    0
                    if retrying_after_failure
                    else existing.metadata.get("probe_count", 0)
                ),
                "probe_successes": (
                    0
                    if retrying_after_failure
                    else existing.metadata.get("probe_successes", 0)
                ),
                "probe_failures": (
                    0
                    if retrying_after_failure
                    else existing.metadata.get("probe_failures", 0)
                ),
            }
        heartbeat_required = bool(payload.get("heartbeat_required", True))
        self.registry.register(unit, heartbeat_required=heartbeat_required)
        self._refresh_router()
        profile = self._microvm_profile(unit)
        if profile:
            self.microvm_pool.ensure_pool(profile)
        if unit.state == "testing":
            self._ensure_background_probe(unit, payload=payload)
        return self.router.units[unit.unit_id]

    def discover_agents(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Accept Agent discoveries from a Harness without exposing internals.

        Discovered Agents are candidates of the parent Harness, not global
        Router units.  Each candidate is tested through the parent Harness's
        background poll lane.  Only after a successful probe are its
        capabilities folded into the parent's foreground routing summary.
        """
        harness_id = str(payload.get("harness_id") or payload.get("unit_id") or "")
        if not harness_id:
            raise ValueError("discovery requires harness_id")
        parent = self.registry.get(harness_id)
        if parent.state in {"offline", "degraded"}:
            raise ValueError(f"harness_not_available:{harness_id}")
        raw_agents = payload.get("agents", payload.get("discovered_agents", []))
        if not isinstance(raw_agents, list) or not raw_agents:
            raise ValueError("discovery requires a non-empty agents list")

        with self.lock:
            metadata = dict(parent.metadata)
            candidates = dict(metadata.get("discovery_candidates", {}))
            accepted: list[dict[str, Any]] = []
            for raw in raw_agents:
                if not isinstance(raw, dict):
                    raise ValueError("each discovered agent must be an object")
                candidate_id = str(raw.get("agent_id") or raw.get("id") or "")
                if not candidate_id:
                    raise ValueError("discovered agent requires agent_id")
                capabilities = sorted({str(value) for value in raw.get("capabilities", [])})
                tools = sorted({str(value) for value in raw.get("tools", [])})
                platforms = sorted({str(value) for value in raw.get("platforms", [])})
                previous = dict(candidates.get(candidate_id, {}))
                already_eligible = previous.get("state") == "eligible"
                unchanged = (
                    already_eligible
                    and set(previous.get("capabilities", [])) == set(capabilities)
                    and set(previous.get("tools", [])) == set(tools)
                    and set(previous.get("platforms", [])) == set(platforms)
                )
                if unchanged:
                    accepted.append(
                        {
                            "agent_id": candidate_id,
                            "state": "eligible",
                            "probe_id": None,
                        }
                    )
                    continue
                candidate = {
                    **previous,
                    "state": "testing",
                    "capabilities": capabilities,
                    "tools": tools,
                    "platforms": platforms,
                    "hardware": dict(raw.get("hardware", {})),
                    "discovered_by": harness_id,
                    "probe_task": str(raw.get("probe_task", "")).strip(),
                    "probe_count": int(previous.get("probe_count", 0)),
                    "probe_successes": int(previous.get("probe_successes", 0)),
                    "probe_failures": int(previous.get("probe_failures", 0)),
                }
                candidates[candidate_id] = candidate
                description = candidate["probe_task"] or (
                    f"在当前 Harness 内验证新发现 Agent {candidate_id} 的能力："
                    f"{', '.join(capabilities) or '基础任务执行'}。"
                    "请返回 success、latency_ms 和 failure_type。"
                )
                probe = self._ensure_background_probe(
                    parent,
                    payload={"probe_task": description},
                    kind="agent_discovery",
                    candidate_id=candidate_id,
                )
                accepted.append(
                    {
                        "agent_id": candidate_id,
                        "state": "testing",
                        "probe_id": probe.probe_id if probe else None,
                    }
                )
            metadata["discovery_candidates"] = candidates
            parent.metadata = metadata
            self.registry.register(parent, heartbeat_required=True)
            self._refresh_router()
            return {
                "harness_id": harness_id,
                "accepted": accepted,
                "routing_effect": "capabilities_enter_foreground_after_probe_success",
            }

    def heartbeat(self, payload: dict[str, Any]) -> ExecutionUnit:
        unit_id = str(payload.get("unit_id", ""))
        if not unit_id:
            raise ValueError("heartbeat requires unit_id")
        current = self.registry.get(unit_id)
        reported_state = payload.get("state")
        if current.state == "testing":
            reported_state = "testing"
        elif (
            current.state == "degraded"
            and current.metadata.get("routing_scope") == "background"
        ):
            reported_state = "degraded"
        unit = self.registry.heartbeat(
            unit_id,
            state=reported_state,
            load=payload.get("load"),
            metadata=payload.get("metadata"),
        )
        self._refresh_router()
        return unit

    def _ensure_background_probes(self) -> None:
        for unit in self.registry.list_units():
            if unit.state == "testing":
                self._ensure_background_probe(unit)

    def _ensure_background_probe(
        self,
        unit: ExecutionUnit,
        *,
        payload: dict[str, Any] | None = None,
        attempt: int = 1,
        kind: str = "onboarding",
        candidate_id: str | None = None,
    ) -> BackgroundProbe | None:
        with self.lock:
            active = [
                probe
                for probe in self.background_probes.values()
                if probe.unit_id == unit.unit_id
                and probe.status in {"queued", "running"}
                and (
                    (candidate_id is None and probe.kind == kind)
                    or probe.candidate_id == candidate_id
                )
            ]
            if active:
                return active[0]
            if attempt > self.probe_max_attempts:
                return None
            metadata = dict(unit.metadata)
            description = str(
                (payload or {}).get("probe_task")
                or metadata.get("probe_task")
                or (
                    "Run a short canary task using the declared Harness plugins, "
                    "report success, latency, and any tool failure."
                )
            )
            now = time.time()
            probe = BackgroundProbe(
                probe_id=uuid.uuid4().hex[:12],
                unit_id=unit.unit_id,
                task_id=f"probe-{unit.unit_id}-{uuid.uuid4().hex[:8]}",
                description=description,
                lease_id=uuid.uuid4().hex,
                attempt=attempt,
                created_at=now,
                kind=kind,
                candidate_id=candidate_id,
            )
            self.background_probes[probe.probe_id] = probe
            self.background_queues.setdefault(unit.unit_id, queue.Queue()).put(
                probe.probe_id
            )
            return probe

    def next_background_probe(self, unit_id: str) -> dict[str, Any] | None:
        self._refresh_router()
        with self.lock:
            unit = self.router.units.get(unit_id)
            if unit is None:
                raise ValueError("unknown execution unit")
            probe_queue = self.background_queues.setdefault(unit_id, queue.Queue())
            # A foreground Harness may still have discovery probes. Peek at
            # the queue so those probes can run without exposing onboarding
            # probes for an already eligible unit.
            with probe_queue.mutex:
                queued_probe_id = probe_queue.queue[0] if probe_queue.queue else None
            if queued_probe_id is None:
                return None
            queued_probe = self.background_probes.get(queued_probe_id)
            if unit.state != "testing" and (
                queued_probe is None or queued_probe.kind != "agent_discovery"
            ):
                return None
            try:
                probe_id = probe_queue.get_nowait()
            except queue.Empty:
                return None
            probe = self.background_probes[probe_id]
            if probe.status != "queued":
                return None

            # Foreground jobs obtain their MicroVM in _dispatch. Background
            # Canary tasks must follow the same lifecycle; otherwise a Cube
            # backed Adapter receives no microvm_id and fails closed with
            # missing_microvm_lease.
            profile = self._microvm_profile(unit)
            vm = None
            if profile:
                try:
                    vm = self.microvm_pool.reserve(
                        profile,
                        f"probe-{probe.probe_id}",
                    )
                except Exception:
                    # Keep the probe queued so a later poll can retry after
                    # the pool is replenished or capacity becomes available.
                    probe_queue.put(probe_id)
                    return None

            probe.status = "running"
            probe.started_at = time.time()
            if vm is not None:
                probe.microvm_id = vm.vm_id
                probe.microvm_profile = vm.profile
                probe.microvm_node_id = vm.node_id
                probe.microvm_runtime_version = vm.runtime_version
            return {
                "probe_id": probe.probe_id,
                "lease_id": probe.lease_id,
                "attempt": probe.attempt,
                "task_id": probe.task_id,
                "description": probe.description,
                "background": True,
                "unit_id": unit_id,
                "kind": probe.kind,
                "candidate_id": probe.candidate_id,
                "microvm_id": probe.microvm_id,
                "microvm_profile": probe.microvm_profile,
                "microvm_node_id": probe.microvm_node_id,
                "microvm_runtime_version": probe.microvm_runtime_version,
            }

    def _release_background_probe_vm(
        self,
        probe: BackgroundProbe,
        outcome: dict[str, Any],
    ) -> None:
        if not probe.microvm_id:
            return
        outcome.setdefault(
            "sandbox",
            {
                "vm_id": probe.microvm_id,
                "profile": probe.microvm_profile,
                "node_id": probe.microvm_node_id,
                "runtime_version": probe.microvm_runtime_version,
                "lifecycle": (
                    "destroyed_after_background_success"
                    if outcome.get("success")
                    else "destroyed_after_background_failure"
                ),
            },
        )
        self.microvm_pool.destroy_and_replenish(probe.microvm_id)
        probe.microvm_id = None
        probe.microvm_profile = None
        probe.microvm_node_id = None
        probe.microvm_runtime_version = None

    def complete_background_probe(
        self,
        probe_id: str,
        outcome: dict[str, Any],
        *,
        lease_id: str | None = None,
        reported_unit_id: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            if probe_id not in self.background_probes:
                raise KeyError(probe_id)
            probe = self.background_probes[probe_id]
            if lease_id and lease_id != probe.lease_id:
                raise ValueError("stale_probe_lease")
            if reported_unit_id and reported_unit_id != probe.unit_id:
                raise ValueError("probe_unit_mismatch")
            if probe.status in {"completed", "failed"}:
                return asdict(probe)
            outcome = dict(outcome)
            probe.completed_at = time.time()
            success = bool(outcome.get("success"))
            probe.status = "completed" if success else "failed"
            self._release_background_probe_vm(probe, outcome)
            probe.result = dict(outcome)

            unit = self.registry.get(probe.unit_id)
            if probe.kind == "agent_discovery":
                metadata = dict(unit.metadata)
                candidates = dict(metadata.get("discovery_candidates", {}))
                candidate = dict(candidates.get(probe.candidate_id or "", {}))
                if not candidate:
                    raise KeyError(f"discovered_agent_not_found:{probe.candidate_id}")
                count = int(candidate.get("probe_count", 0)) + 1
                successes = int(candidate.get("probe_successes", 0)) + int(success)
                failures = int(candidate.get("probe_failures", 0)) + int(not success)
                history = list(candidate.get("probe_history", []))
                history.append(
                    {
                        "probe_id": probe.probe_id,
                        "attempt": probe.attempt,
                        "success": success,
                        "latency_ms": outcome.get("latency_ms"),
                        "failure_type": outcome.get("failure_type"),
                        "timestamp": probe.completed_at,
                    }
                )
                candidate.update(
                    {
                        "probe_count": count,
                        "probe_successes": successes,
                        "probe_failures": failures,
                        "probe_history": history[-20:],
                        "confidence": round(successes / max(1, count), 4),
                    }
                )
                if success:
                    candidate["state"] = "eligible"
                    eligible_capabilities = set(
                        metadata.get("discovered_capabilities", [])
                    )
                    eligible_tools = set(metadata.get("discovered_tools", []))
                    eligible_capabilities.update(candidate.get("capabilities", []))
                    eligible_tools.update(candidate.get("tools", []))
                    for item in candidates.values():
                        if item.get("state") == "eligible":
                            eligible_capabilities.update(item.get("capabilities", []))
                            eligible_tools.update(item.get("tools", []))
                    unit.capabilities.update(candidate.get("capabilities", []))
                    unit.tools.update(candidate.get("tools", []))
                    metadata["discovered_capabilities"] = sorted(eligible_capabilities)
                    metadata["discovered_tools"] = sorted(eligible_tools)
                elif probe.attempt < self.probe_max_attempts:
                    candidate["state"] = "testing"
                else:
                    candidate["state"] = "rejected"
                candidates[probe.candidate_id or ""] = candidate
                metadata["discovery_candidates"] = candidates
                unit.metadata = metadata
                self.registry.register(unit, heartbeat_required=True)
                self._refresh_router()
                if candidate["state"] == "testing":
                    self._ensure_background_probe(
                        unit,
                        payload={"probe_task": probe.description},
                        attempt=probe.attempt + 1,
                        kind="agent_discovery",
                        candidate_id=probe.candidate_id,
                    )
                return {
                    "probe": asdict(probe),
                    "unit": serialize_unit(unit),
                    "candidate": {
                        "agent_id": probe.candidate_id,
                        "state": candidate["state"],
                        "probe_count": candidate["probe_count"],
                        "confidence": candidate["confidence"],
                    },
                }

            metadata = dict(unit.metadata)
            count = int(metadata.get("probe_count", 0)) + 1
            successes = int(metadata.get("probe_successes", 0)) + int(success)
            failures = int(metadata.get("probe_failures", 0)) + int(not success)
            history = list(metadata.get("probe_history", []))
            history.append(
                {
                    "probe_id": probe.probe_id,
                    "attempt": probe.attempt,
                    "success": success,
                    "latency_ms": outcome.get("latency_ms"),
                    "failure_type": outcome.get("failure_type"),
                    "timestamp": probe.completed_at,
                }
            )
            metadata.update(
                {
                    "probe_count": count,
                    "probe_successes": successes,
                    "probe_failures": failures,
                    "probe_history": history[-20:],
                    "confidence": round(successes / max(1, count), 4),
                }
            )
            if successes >= self.probe_successes_required:
                unit.state = "idle"
                metadata["routing_scope"] = "foreground"
                metadata["onboarding_status"] = "eligible"
            elif probe.attempt < self.probe_max_attempts:
                unit.state = "testing"
                metadata["routing_scope"] = "background"
                metadata["onboarding_status"] = "testing"
            else:
                unit.state = "degraded"
                metadata["routing_scope"] = "background"
                metadata["onboarding_status"] = "probe_failed"
            unit.metadata = metadata
            self.registry.register(unit, heartbeat_required=True)
            self._refresh_router()
            if unit.state == "testing":
                self._ensure_background_probe(unit, attempt=probe.attempt + 1)
            return {
                "probe": asdict(probe),
                "unit": serialize_unit(unit),
            }

    def submit(self, task_data: dict[str, Any]) -> Job:
        forced_fields = sorted(FORBIDDEN_ROUTING_FIELDS.intersection(task_data))
        if forced_fields:
            raise ValueError(
                "任务请求不能指定 Harness 或 Agent；请只提供任务描述和任务需求。"
                f" forbidden_fields={forced_fields}"
            )
        self._refresh_router()
        task = Task.from_dict(task_data)
        decision = self.router.route(task)
        inferred = TaskParser.infer(task.description)
        stored_task = dict(task_data)
        stored_task["inferred_requirements"] = {
            "required_capabilities": sorted(inferred["required_capabilities"]),
            "required_tools": sorted(inferred["required_tools"]),
            "allowed_platforms": sorted(inferred["allowed_platforms"]),
            "requires_gpu": inferred["requires_gpu"],
            "requires_mobile": inferred["requires_mobile"],
            "reasons": inferred["reasons"],
        }
        job = Job(
            job_id=uuid.uuid4().hex[:12],
            task=stored_task,
            selected_unit=decision.selected_unit,
            selected_unit_type=decision.selected_unit_type,
            created_at=time.time(),
            max_retries=max(0, int(task_data.get("max_retries", self.default_max_retries))),
            retryable=bool(task_data.get("retryable", True)),
            lease_id=uuid.uuid4().hex,
        )
        with self.lock:
            self.jobs[job.job_id] = job
            self._save_job_locked(job)
        self._dispatch(job.job_id)
        return job

    def _dispatch(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs[job_id]
            unit = self.router.units.get(job.selected_unit)
            if unit is None:
                self._fail_locked(job, "selected_unit_missing", "selected unit is no longer registered")
                return
            profile = self._microvm_profile(unit)
            if profile and not job.microvm_id:
                try:
                    vm = self.microvm_pool.reserve(profile, job.job_id)
                except Exception as exc:
                    self._handle_failure(
                        job.job_id,
                        {
                            "success": False,
                            "failure_type": "microvm_pool_unavailable",
                            "error": str(exc),
                            "executor": unit.unit_id,
                        },
                        lease_id=job.lease_id,
                    )
                    return
                job.microvm_id = vm.vm_id
                job.microvm_profile = profile
                job.microvm_node_id = vm.node_id
                job.microvm_runtime_version = vm.runtime_version
            transport = unit.metadata.get("transport", "http" if unit.endpoint else "poll")
            if transport == "poll":
                job.status = "queued"
                self.poll_queues.setdefault(unit.unit_id, queue.Queue()).put(job.job_id)
                self._save_job_locked(job)
                return
            if unit.endpoint:
                job.status = "running"
                self._save_job_locked(job)
                threading.Thread(
                    target=self._execute_on_endpoint,
                    args=(job.job_id, unit.unit_id, job.lease_id),
                    daemon=True,
                ).start()
                return
            self._fail_locked(job, "missing_agent_endpoint", "execution unit has no endpoint")

    def _execute_on_endpoint(self, job_id: str, unit_id: str, lease_id: str) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            unit = self.router.units.get(unit_id)
            if job is None or unit is None or not unit.endpoint:
                return
            payload = dict(job.task)
            payload["lease_id"] = lease_id
            payload["attempt"] = job.attempt
            if job.microvm_id:
                payload["microvm_id"] = job.microvm_id
            endpoint = unit.endpoint
        try:
            request = Request(
                f"{endpoint.rstrip('/')}/execute",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=60) as response:
                outcome = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            self._handle_failure(
                job_id,
                {
                    "success": False,
                    "quality": 0.0,
                    "latency_ms": 0.0,
                    "cost": 0.0,
                    "failure_type": "agent_unavailable",
                    "executor": unit_id,
                    "error": str(exc),
                },
                lease_id=lease_id,
            )
            return
        self.complete(job_id, outcome, lease_id=lease_id, reported_unit_id=unit_id)

    def next_poll_job(self, unit_id: str) -> dict[str, Any] | None:
        self._refresh_router()
        with self.lock:
            if unit_id not in self.poll_queues:
                raise ValueError("unknown execution unit")
            try:
                job_id = self.poll_queues[unit_id].get_nowait()
            except queue.Empty:
                return None
            job = self.jobs[job_id]
            if job.status in ("completed", "failed"):
                return None
            job.status = "running"
            job.selected_unit = unit_id
            self._save_job_locked(job)
            return {
                "job_id": job.job_id,
                "lease_id": job.lease_id,
                "attempt": job.attempt,
                "microvm_id": job.microvm_id,
                "task_id": job.task.get("task_id", job.job_id),
                "description": job.task.get("description", ""),
                "required_capabilities": job.task.get("inferred_requirements", {}).get(
                    "required_capabilities", []
                ),
            }

    def next_mobile_job(self, unit_id: str) -> dict[str, Any] | None:
        return self.next_poll_job(unit_id)

    def complete(
        self,
        job_id: str,
        outcome: dict[str, Any],
        *,
        lease_id: str | None = None,
        reported_unit_id: str | None = None,
    ) -> Job:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            job = self.jobs[job_id]
            if lease_id and lease_id != job.lease_id:
                raise ValueError("stale_result_lease")
            if job.status in ("completed", "failed"):
                return job
            if reported_unit_id and reported_unit_id != job.selected_unit:
                raise ValueError("result_unit_mismatch")
            if outcome.get("success"):
                if job.microvm_id:
                    outcome.setdefault(
                        "sandbox",
                        {
                            "vm_id": job.microvm_id,
                            "profile": job.microvm_profile,
                            "node_id": job.microvm_node_id,
                            "runtime_version": job.microvm_runtime_version,
                            "lifecycle": "destroyed_after_success",
                        },
                    )
                    self.microvm_pool.destroy_and_replenish(job.microvm_id)
                    job.microvm_id = None
                    job.microvm_profile = None
                    job.microvm_node_id = None
                    job.microvm_runtime_version = None
                job.status = "completed"
                job.result = outcome
                job.completed_at = time.time()
                job.next_retry_at = None
                self._save_job_locked(job)
                return job
        self._handle_failure(job_id, outcome, lease_id=lease_id)
        return self.get_job(job_id)

    def _handle_failure(
        self,
        job_id: str,
        outcome: dict[str, Any],
        *,
        lease_id: str | None = None,
    ) -> None:
        with self.lock:
            job = self.jobs[job_id]
            if lease_id and lease_id != job.lease_id:
                return
            failure = {
                "attempt": job.attempt,
                "unit_id": job.selected_unit,
                "failure_type": outcome.get("failure_type", "execution_failed"),
                "error": outcome.get("error"),
                "timestamp": time.time(),
            }
            job.failure_history.append(failure)
            job.result = outcome
            if job.microvm_id:
                outcome.setdefault(
                    "sandbox",
                    {
                        "vm_id": job.microvm_id,
                        "profile": job.microvm_profile,
                        "node_id": job.microvm_node_id,
                        "runtime_version": job.microvm_runtime_version,
                        "lifecycle": "destroyed_after_failure",
                    },
                )
                self.microvm_pool.destroy_and_replenish(job.microvm_id)
                job.microvm_id = None
                job.microvm_profile = None
                job.microvm_node_id = None
                job.microvm_runtime_version = None
            if job.retryable and job.attempt <= job.max_retries:
                job.status = "retry_wait"
                job.attempt += 1
                job.next_retry_at = time.time() + min(30.0, 2.0 ** min(job.attempt - 1, 4))
                job.lease_id = uuid.uuid4().hex
                self._save_job_locked(job)
            else:
                self._fail_locked(
                    job,
                    failure["failure_type"],
                    outcome.get("error"),
                    outcome=outcome,
                )

    def _fail_locked(
        self,
        job: Job,
        failure_type: str,
        error: str | None,
        *,
        outcome: dict[str, Any] | None = None,
    ) -> None:
        if job.microvm_id:
            sandbox = {
                "vm_id": job.microvm_id,
                "profile": job.microvm_profile,
                "node_id": job.microvm_node_id,
                "runtime_version": job.microvm_runtime_version,
                "lifecycle": "destroyed_after_controller_failure",
            }
            self.microvm_pool.destroy_and_replenish(job.microvm_id)
            job.microvm_id = None
            job.microvm_profile = None
            job.microvm_node_id = None
            job.microvm_runtime_version = None
        else:
            sandbox = None
        job.status = "failed"
        job.completed_at = time.time()
        result = dict(outcome or {})
        result.update(
            {
            "success": False,
            "quality": result.get("quality", 0.0),
            "latency_ms": result.get("latency_ms", 0.0),
            "cost": result.get("cost", 0.0),
            "failure_type": failure_type,
            "error": result.get("error") or error,
            "executor": result.get("executor", job.selected_unit),
            }
        )
        job.result = result
        if sandbox:
            job.result["sandbox"] = sandbox
        self._save_job_locked(job)

    def _retry_job(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job.status != "retry_wait":
                return
            failed_unit = job.selected_unit
        self._refresh_router()
        task = Task.from_dict(job.task)
        # Temporarily exclude every unit already failed for this job so a
        # retry does not immediately return to the same broken executor.
        failed_units = {entry["unit_id"] for entry in job.failure_history}
        original_states = {}
        with self.lock:
            for unit_id in failed_units:
                unit = self.router.units.get(unit_id)
                if unit:
                    original_states[unit_id] = unit.state
                    unit.state = "offline"
        try:
            decision = self.router.route(task)
        except NoEligibleUnit as exc:
            with self.lock:
                for unit_id, state in original_states.items():
                    if unit_id in self.router.units:
                        self.router.units[unit_id].state = state
                job = self.jobs[job_id]
                job.next_retry_at = time.time() + 5.0
                job.result = {
                    "success": False,
                    "failure_type": "no_replacement_agent",
                    "error": str(exc),
                    "rejected_units": exc.rejected,
                }
                self._save_job_locked(job)
            return
        with self.lock:
            for unit_id, state in original_states.items():
                if unit_id in self.router.units:
                    self.router.units[unit_id].state = state
            job = self.jobs[job_id]
            job.selected_unit = decision.selected_unit
            job.selected_unit_type = decision.selected_unit_type
            job.status = "queued"
            job.next_retry_at = None
            self._save_job_locked(job)
        self._dispatch(job_id)

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(self.monitor_interval):
            try:
                stale_ids = self.registry.mark_stale(self.heartbeat_timeout)
                if stale_ids:
                    self._refresh_router()
                    with self.lock:
                        active_jobs = [
                            job.job_id
                            for job in self.jobs.values()
                            if job.status in ("queued", "running")
                            and job.selected_unit in stale_ids
                        ]
                    for job_id in active_jobs:
                        with self.lock:
                            unit_id = self.jobs[job_id].selected_unit
                            lease_id = self.jobs[job_id].lease_id
                        self._handle_failure(
                            job_id,
                            {
                                "success": False,
                                "failure_type": "agent_lost",
                                "error": "heartbeat timeout",
                                "executor": unit_id,
                            },
                            lease_id=lease_id,
                        )

                now = time.time()
                with self.lock:
                    retry_ids = [
                        job.job_id
                        for job in self.jobs.values()
                        if job.status == "retry_wait"
                        and job.next_retry_at is not None
                        and job.next_retry_at <= now
                    ]
                for job_id in retry_ids:
                    self._retry_job(job_id)
            except Exception as exc:
                print(f"[Controller monitor] {exc}")

    def get_job(self, job_id: str) -> Job:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return self.jobs[job_id]

    def list_jobs(self) -> list[Job]:
        with self.lock:
            return list(self.jobs.values())

    # CubeSandbox-inspired lifecycle and operations-plane methods.  These
    # methods intentionally stay outside Router: Router selects a Harness,
    # while the operations plane manages nodes and sandbox state.
    def register_microvm_node(self, payload: dict[str, Any]) -> dict[str, Any]:
        node_payload = payload.get("node", payload)
        if not isinstance(node_payload, dict) or not node_payload.get("node_id"):
            raise ValueError("node registration requires a node_id")
        return self.microvm_pool._node_payload(
            self.microvm_pool.register_node(node_payload)
        )

    def isolate_microvm_node(self, node_id: str, *, drain: bool = False) -> dict[str, Any]:
        return self.microvm_pool.isolate_node(node_id, drain=drain)

    def restore_microvm_node(self, node_id: str) -> dict[str, Any]:
        return self.microvm_pool.restore_node(node_id)

    def pause_microvm(self, vm_id: str) -> dict[str, Any]:
        return asdict(self.microvm_pool.pause(vm_id))

    def resume_microvm(self, vm_id: str, target_node_id: str | None = None) -> dict[str, Any]:
        return asdict(
            self.microvm_pool.resume(vm_id, target_node_id=target_node_id)
        )

    def create_microvm_from_snapshot(
        self,
        snapshot_id: str,
        target_node_id: str | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        return asdict(
            self.microvm_pool.create_from_snapshot(
                snapshot_id,
                target_node_id=target_node_id,
                job_id=job_id,
            )
        )

    def close(self) -> None:
        self._stop_event.set()
        self.registry.close()


# Avoid opening the default SQLite database merely by importing this module.
# The HTTP service creates the Controller in ``main``; tests and embedding code
# can construct an isolated Controller with their own database path.
CONTROLLER: PhaseOneController | None = None


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _require_registry_token(self) -> bool:
        supplied = self.headers.get("X-Registry-Token", "")
        if not supplied or not secrets.compare_digest(supplied, CONTROLLER.registry_token):
            self._json({"error": "invalid_registry_token"}, status=401)
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json({"status": "ok", "service": "phase1-controller"})
            return
        if parsed.path in ("/units", "/registry/units"):
            if parsed.path == "/registry/units" and not self._require_registry_token():
                return
            self._json({"units": [serialize_unit(unit) for unit in CONTROLLER.registry.list_units()]})
            return
        if parsed.path == "/microvms":
            if not self._require_registry_token():
                return
            self._json(CONTROLLER.microvm_pool.snapshot())
            return
        if parsed.path == "/ops/nodes":
            if not self._require_registry_token():
                return
            self._json({"nodes": CONTROLLER.microvm_pool.list_nodes()})
            return
        if parsed.path == "/tasks":
            task_id = parse_qs(parsed.query).get("task_id", [None])[0]
            jobs = CONTROLLER.list_jobs()
            if task_id:
                jobs = [job for job in jobs if job.task.get("task_id") == task_id]
            self._json({"jobs": [asdict(job) for job in jobs]})
            return
        if parsed.path in ("/mobile/tasks/next", "/tasks/next"):
            unit_id = parse_qs(parsed.query).get("unit_id", [""])[0]
            try:
                task = CONTROLLER.next_poll_job(unit_id)
            except ValueError as exc:
                self._json({"error": str(exc)}, status=400)
                return
            if task is None:
                self.send_response(204)
                self.end_headers()
                return
            self._json(task)
            return
        if parsed.path == "/background/tasks/next":
            unit_id = parse_qs(parsed.query).get("unit_id", [""])[0]
            try:
                probe = CONTROLLER.next_background_probe(unit_id)
            except ValueError as exc:
                self._json({"error": str(exc)}, status=400)
                return
            if probe is None:
                self.send_response(204)
                self.end_headers()
                return
            self._json(probe)
            return
        if parsed.path.startswith("/tasks/"):
            try:
                job = CONTROLLER.get_job(parsed.path.rsplit("/", 1)[-1])
            except KeyError:
                self._json({"error": "job_not_found"}, status=404)
                return
            self._json(asdict(job))
            return
        self._json({"error": "not_found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/ops/nodes/register":
                if not self._require_registry_token():
                    return
                node = CONTROLLER.register_microvm_node(self._read_json())
                self._json({"registered": True, "node": node}, status=201)
                return
            if parsed.path.startswith("/ops/nodes/"):
                if not self._require_registry_token():
                    return
                parts = parsed.path.strip("/").split("/")
                if len(parts) != 4:
                    self._json({"error": "invalid_node_operation"}, status=400)
                    return
                node_id, action = parts[2], parts[3]
                if action == "isolate":
                    node = CONTROLLER.isolate_microvm_node(node_id)
                elif action == "drain":
                    node = CONTROLLER.isolate_microvm_node(node_id, drain=True)
                elif action == "restore":
                    node = CONTROLLER.restore_microvm_node(node_id)
                else:
                    self._json({"error": "unknown_node_operation"}, status=404)
                    return
                self._json({"node": node})
                return
            if parsed.path.startswith("/microvms/"):
                if not self._require_registry_token():
                    return
                parts = parsed.path.strip("/").split("/")
                if len(parts) == 3 and parts[2] == "pause":
                    self._json(
                        {"snapshot": CONTROLLER.pause_microvm(parts[1])},
                        status=201,
                    )
                    return
                if len(parts) == 3 and parts[2] == "resume":
                    payload = self._read_json()
                    self._json(
                        {
                            "vm": CONTROLLER.resume_microvm(
                                parts[1], payload.get("target_node_id")
                            )
                        }
                    )
                    return
                if parsed.path == "/microvms/from-snapshot":
                    payload = self._read_json()
                    self._json(
                        {
                            "vm": CONTROLLER.create_microvm_from_snapshot(
                                str(payload["snapshot_id"]),
                                payload.get("target_node_id"),
                                payload.get("job_id"),
                            )
                        },
                        status=201,
                    )
                    return
            if parsed.path == "/tasks":
                job = CONTROLLER.submit(self._read_json())
                self._json(asdict(job), status=201)
                return
            if parsed.path == "/registry/register":
                if not self._require_registry_token():
                    return
                unit = CONTROLLER.register_unit(self._read_json())
                self._json({"registered": True, "unit": serialize_unit(unit)}, status=201)
                return
            if parsed.path in ("/harness/discover", "/registry/discover"):
                if not self._require_registry_token():
                    return
                result = CONTROLLER.discover_agents(self._read_json())
                self._json(result, status=202)
                return
            if parsed.path == "/registry/heartbeat":
                if not self._require_registry_token():
                    return
                unit = CONTROLLER.heartbeat(self._read_json())
                self._json({"ok": True, "unit": serialize_unit(unit)})
                return
            if (
                parsed.path.startswith("/background/tasks/")
                and parsed.path.endswith("/result")
            ):
                parts = parsed.path.strip("/").split("/")
                payload = self._read_json()
                result = CONTROLLER.complete_background_probe(
                    parts[2],
                    payload,
                    lease_id=payload.get("lease_id"),
                    reported_unit_id=payload.get("executor"),
                )
                self._json(result)
                return
            if (
                parsed.path.startswith("/mobile/tasks/") or parsed.path.startswith("/tasks/")
            ) and parsed.path.endswith("/result"):
                parts = parsed.path.strip("/").split("/")
                payload = self._read_json()
                # /tasks/<job_id>/result has the job ID at index 1;
                # /mobile/tasks/<job_id>/result has it at index 2.
                job_id = parts[1] if parts[0] == "tasks" else parts[2]
                job = CONTROLLER.complete(
                    job_id,
                    payload,
                    lease_id=payload.get("lease_id"),
                    reported_unit_id=payload.get("executor"),
                )
                self._json(asdict(job))
                return
        except KeyError:
            self._json({"error": "job_not_found_or_unknown_unit"}, status=404)
            return
        except ValueError as exc:
            self._json({"error": str(exc)}, status=409 if "stale" in str(exc) else 400)
            return
        except Exception as exc:
            self._json({"error": str(exc)}, status=400)
            return
        self._json({"error": "not_found"}, status=404)

    def log_message(self, *_args: Any) -> None:
        return


def main() -> None:
    global CONTROLLER
    parser = argparse.ArgumentParser(description="DSH Harness + MicroVM controller")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--heartbeat-timeout", type=float, default=15.0)
    parser.add_argument("--monitor-interval", type=float, default=2.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--registry-token", default=None)
    parser.add_argument("--microvm-pool-size", type=int, default=3)
    parser.add_argument("--microvm-max-total", type=int, default=8)
    parser.add_argument("--microvm-snapshot-dir", default=None)
    parser.add_argument("--microvm-runtime-version", default="dsh-0.1")
    parser.add_argument("--microvm-nodes", default=None)
    parser.add_argument(
        "--microvm-backend",
        choices=("mock", "cubesandbox"),
        default=os.environ.get("MICROVM_BACKEND", "mock"),
        help="MicroVM implementation: mock for tests or cubesandbox for ECS",
    )
    parser.add_argument(
        "--cube-api-url",
        default=os.environ.get("CUBE_API_URL", "http://127.0.0.1:3000"),
    )
    parser.add_argument(
        "--cube-api-key",
        default=os.environ.get("CUBE_API_KEY", "e2b_000000"),
    )
    parser.add_argument(
        "--cube-proxy-node-ip",
        default=os.environ.get("CUBE_PROXY_NODE_IP", "127.0.0.1"),
    )
    parser.add_argument(
        "--cube-proxy-port-http",
        type=int,
        default=int(os.environ.get("CUBE_PROXY_PORT_HTTP", "80")),
    )
    parser.add_argument(
        "--cube-template-id",
        default=os.environ.get("CUBE_TEMPLATE_ID"),
        help="Default READY CubeSandbox template used by all profiles",
    )
    parser.add_argument(
        "--cube-template-map",
        default=None,
        help="JSON file mapping MicroVM profiles to READY template IDs",
    )
    parser.add_argument(
        "--dsh-api-key-file",
        default=os.environ.get("DEEPSEEK_API_KEY_FILE"),
        help="Protected file containing DEEPSEEK_API_KEY; injected only at Sandbox creation",
    )
    parser.add_argument(
        "--dsh-permission-mode",
        default=os.environ.get("DSH_PERMISSION_MODE", "danger-full-access"),
        choices=("read-only", "workspace-write", "danger-full-access"),
        help="Inner DSH file policy; outer CubeSandbox remains the isolation boundary",
    )
    parser.add_argument("--probe-successes-required", type=int, default=1)
    parser.add_argument("--probe-max-attempts", type=int, default=3)
    args = parser.parse_args()
    if CONTROLLER is not None:
        CONTROLLER.close()
    microvm_nodes = None
    if args.microvm_nodes:
        node_config = json.loads(Path(args.microvm_nodes).read_text(encoding="utf-8"))
        microvm_nodes = node_config.get("nodes", node_config)
    microvm_backend = None
    if args.microvm_backend == "cubesandbox":
        template_map: dict[str, str] = {}
        if args.cube_template_map:
            template_config = json.loads(
                Path(args.cube_template_map).read_text(encoding="utf-8")
            )
            template_map = template_config.get("profiles", template_config)
        sandbox_env_vars = {
            "DSH_PERMISSION_MODE": args.dsh_permission_mode,
        }
        if args.dsh_api_key_file:
            secret_path = Path(args.dsh_api_key_file).expanduser()
            if not secret_path.is_file():
                raise SystemExit(f"DEEPSEEK_API_KEY file not found: {secret_path}")
            try:
                secret_mode = secret_path.stat().st_mode & 0o777
            except OSError as exc:
                raise SystemExit(f"cannot stat DEEPSEEK_API_KEY file: {exc}") from exc
            if secret_mode & 0o077:
                raise SystemExit(
                    f"DEEPSEEK_API_KEY file must not be group/world readable: {secret_path}"
                )
            api_key = secret_path.read_text(encoding="utf-8").strip()
            if not api_key:
                raise SystemExit(f"DEEPSEEK_API_KEY file is empty: {secret_path}")
            sandbox_env_vars["DEEPSEEK_API_KEY"] = api_key
        elif os.environ.get("DEEPSEEK_API_KEY"):
            sandbox_env_vars["DEEPSEEK_API_KEY"] = os.environ["DEEPSEEK_API_KEY"]
        microvm_backend = CubeSandboxBackend(
            api_url=args.cube_api_url,
            api_key=args.cube_api_key,
            proxy_node_ip=args.cube_proxy_node_ip,
            proxy_port_http=args.cube_proxy_port_http,
            template_map=template_map,
            default_template_id=args.cube_template_id,
            sandbox_env_vars=sandbox_env_vars,
        )
    CONTROLLER = PhaseOneController(
        args.registry,
        args.database,
        args.heartbeat_timeout,
        args.monitor_interval,
        args.max_retries,
        args.registry_token,
        args.microvm_pool_size,
        args.microvm_max_total,
        args.microvm_snapshot_dir,
        microvm_nodes,
        args.microvm_runtime_version,
        args.probe_successes_required,
        args.probe_max_attempts,
        microvm_backend,
    )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"DSH controller listening on http://{args.host}:{args.port}")
    print(f"Registry token: {CONTROLLER.registry_token}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        CONTROLLER.close()


if __name__ == "__main__":
    main()
