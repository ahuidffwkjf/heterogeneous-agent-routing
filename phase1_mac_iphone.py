"""Phase 1 controller with dynamic registration and fault recovery.

The Controller owns task state and the execution-unit registry. The Router
only makes a routing decision from the latest registry snapshot. Agents can
join at runtime by POSTing /registry/register and stay available by sending
heartbeats to /registry/heartbeat.

Dynamic registry endpoints require the X-Registry-Token header. Never expose
an unauthenticated registration endpoint on a LAN.
"""

from __future__ import annotations

import argparse
import json
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

from microvm_pool import MicroVMNode, MicroVMPoolManager, SharedSnapshotStore
from registry import UnitRegistry
from router import ExecutionUnit, NoEligibleUnit, Router, Task, TaskParser


DEFAULT_REGISTRY = Path(__file__).with_name("execution_units.json")
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


class PhaseOneController:
    def __init__(
        self,
        registry_path: str | Path = DEFAULT_REGISTRY,
        mac_agent_url: str | None = None,
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
    ) -> None:
        self.registry = UnitRegistry(database_path)
        self.registry_token = registry_token or secrets.token_urlsafe(24)
        units = load_execution_units(registry_path)
        if mac_agent_url and "mac_agent_01" in {unit.unit_id for unit in units}:
            for unit in units:
                if unit.unit_id == "mac_agent_01":
                    unit.endpoint = mac_agent_url.rstrip("/")

        # Seed the local config into SQLite. New remote units must register
        # with a token and heartbeat by calling the API.
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
        snapshot_dir = microvm_snapshot_dir
        if snapshot_dir is None:
            snapshot_dir = Path(database_path).expanduser().resolve().parent / "microvm_snapshots"
        self.microvm_pool = MicroVMPoolManager(
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
        self._stop_event = threading.Event()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="controller-health-monitor",
            daemon=True,
        )
        self._refresh_router()
        self._ensure_microvm_pools()
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
        unit = ExecutionUnit.from_dict(unit_payload)
        heartbeat_required = bool(payload.get("heartbeat_required", True))
        self.registry.register(unit, heartbeat_required=heartbeat_required)
        self._refresh_router()
        profile = self._microvm_profile(unit)
        if profile:
            self.microvm_pool.ensure_pool(profile)
        return self.router.units[unit.unit_id]

    def heartbeat(self, payload: dict[str, Any]) -> ExecutionUnit:
        unit_id = str(payload.get("unit_id", ""))
        if not unit_id:
            raise ValueError("heartbeat requires unit_id")
        unit = self.registry.heartbeat(
            unit_id,
            state=payload.get("state"),
            load=payload.get("load"),
            metadata=payload.get("metadata"),
        )
        self._refresh_router()
        return unit

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
                self._fail_locked(job, failure["failure_type"], outcome.get("error"))

    def _fail_locked(self, job: Job, failure_type: str, error: str | None) -> None:
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
        job.result = {
            "success": False,
            "quality": 0.0,
            "latency_ms": 0.0,
            "cost": 0.0,
            "failure_type": failure_type,
            "error": error,
            "executor": job.selected_unit,
        }
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
            if parsed.path == "/registry/heartbeat":
                if not self._require_registry_token():
                    return
                unit = CONTROLLER.heartbeat(self._read_json())
                self._json({"ok": True, "unit": serialize_unit(unit)})
                return
            if (
                parsed.path.startswith("/mobile/tasks/") or parsed.path.startswith("/tasks/")
            ) and parsed.path.endswith("/result"):
                parts = parsed.path.strip("/").split("/")
                payload = self._read_json()
                job = CONTROLLER.complete(
                    parts[2],
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
    parser = argparse.ArgumentParser(description="Phase 1 Mac + iPhone controller")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--mac-agent-url", default=None)
    parser.add_argument("--heartbeat-timeout", type=float, default=15.0)
    parser.add_argument("--monitor-interval", type=float, default=2.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--registry-token", default=None)
    parser.add_argument("--microvm-pool-size", type=int, default=3)
    parser.add_argument("--microvm-max-total", type=int, default=8)
    parser.add_argument("--microvm-snapshot-dir", default=None)
    parser.add_argument("--microvm-runtime-version", default="dsh-0.1")
    parser.add_argument("--microvm-nodes", default=None)
    args = parser.parse_args()
    if CONTROLLER is not None:
        CONTROLLER.close()
    microvm_nodes = None
    if args.microvm_nodes:
        node_config = json.loads(Path(args.microvm_nodes).read_text(encoding="utf-8"))
        microvm_nodes = node_config.get("nodes", node_config)
    CONTROLLER = PhaseOneController(
        args.registry,
        args.mac_agent_url,
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
    )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Phase 1 controller listening on http://{args.host}:{args.port}")
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
