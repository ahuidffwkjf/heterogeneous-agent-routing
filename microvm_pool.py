"""MicroVM pool with CubeSandbox-inspired lifecycle semantics.

The default backend is still a local mock so the project can be tested on a
Mac or on an ECS instance without KVM.  The pool deliberately models the
control-plane contracts needed by a real backend such as CubeSandbox or
Firecracker:

* placement is node-aware and resource-aware;
* nodes can be isolated or drained without accepting new VMs;
* snapshots live in a shared store and can be restored on another node;
* snapshot/runtime compatibility is checked explicitly;
* task VMs are destroyed after completion and the warm pool is replenished.

Replace ``MockMicroVMBackend`` with a CubeSandbox/Firecracker adapter when
the ECS has a real KVM-backed execution environment.
"""

from __future__ import annotations

import json
import inspect
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol


DEFAULT_NODE_ID = "node-ecs-01"
DEFAULT_RUNTIME_VERSION = "dsh-0.1"


@dataclass
class MicroVMNode:
    node_id: str
    state: str = "ready"  # ready | isolated | draining | offline
    total_vcpus: int = 8
    total_memory_mb: int = 16384
    used_vcpus: int = 0
    used_memory_mb: int = 0
    supported_runtime_versions: set[str] = field(
        default_factory=lambda: {DEFAULT_RUNTIME_VERSION}
    )
    labels: dict[str, str] = field(default_factory=dict)
    last_heartbeat_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MicroVMNode":
        return cls(
            node_id=str(payload["node_id"]),
            state=str(payload.get("state", "ready")),
            total_vcpus=max(1, int(payload.get("total_vcpus", 8))),
            total_memory_mb=max(128, int(payload.get("total_memory_mb", 16384))),
            used_vcpus=max(0, int(payload.get("used_vcpus", 0))),
            used_memory_mb=max(0, int(payload.get("used_memory_mb", 0))),
            supported_runtime_versions=set(
                payload.get("supported_runtime_versions", [DEFAULT_RUNTIME_VERSION])
            ),
            labels=dict(payload.get("labels", {})),
            last_heartbeat_at=float(payload.get("last_heartbeat_at", time.time())),
        )

    def can_host(self, runtime_version: str, vcpus: int, memory_mb: int) -> bool:
        return (
            self.state == "ready"
            and runtime_version in self.supported_runtime_versions
            and self.used_vcpus + vcpus <= self.total_vcpus
            and self.used_memory_mb + memory_mb <= self.total_memory_mb
        )

    def reserve(self, vcpus: int, memory_mb: int) -> None:
        if self.used_vcpus + vcpus > self.total_vcpus:
            raise RuntimeError(f"node_cpu_exhausted:{self.node_id}")
        if self.used_memory_mb + memory_mb > self.total_memory_mb:
            raise RuntimeError(f"node_memory_exhausted:{self.node_id}")
        self.used_vcpus += vcpus
        self.used_memory_mb += memory_mb

    def release(self, vcpus: int, memory_mb: int) -> None:
        self.used_vcpus = max(0, self.used_vcpus - vcpus)
        self.used_memory_mb = max(0, self.used_memory_mb - memory_mb)

    def utilization(self) -> float:
        cpu = self.used_vcpus / max(1, self.total_vcpus)
        memory = self.used_memory_mb / max(1, self.total_memory_mb)
        return max(cpu, memory)


@dataclass
class MicroVM:
    vm_id: str
    profile: str
    node_id: str = DEFAULT_NODE_ID
    runtime_version: str = DEFAULT_RUNTIME_VERSION
    state: str = "ready"  # starting | ready | busy | paused | resetting | destroyed
    job_id: str | None = None
    created_at: float = 0.0
    last_used_at: float | None = None
    vcpus: int = 1
    memory_mb: int = 512
    snapshot_id: str | None = None
    network_id: str | None = None


@dataclass
class MicroVMSnapshot:
    snapshot_id: str
    profile: str
    runtime_version: str
    source_vm_id: str
    source_node_id: str
    created_at: float
    filesystem_version: str = "fs-v1"
    memory_state: str = "mock-memory-state"
    filesystem_state: str = "mock-filesystem-state"
    network_state: str = "mock-network-state"


class SharedSnapshotStore:
    """Small shared-object-store abstraction.

    With ``root=None`` this is an in-process store for tests.  Passing a
    directory persists snapshots as JSON objects, which is enough to model
    the shared S3/MinIO control-plane contract before wiring a real SDK.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root else None
        if self.root:
            self.root.mkdir(parents=True, exist_ok=True)
        self._snapshots: dict[str, MicroVMSnapshot] = {}
        self._lock = threading.RLock()

    def put(self, snapshot: MicroVMSnapshot) -> MicroVMSnapshot:
        with self._lock:
            self._snapshots[snapshot.snapshot_id] = snapshot
            if self.root:
                path = self.root / f"{snapshot.snapshot_id}.json"
                path.write_text(
                    json.dumps(asdict(snapshot), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            return snapshot

    def get(self, snapshot_id: str) -> MicroVMSnapshot:
        with self._lock:
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot:
                return snapshot
            if self.root:
                path = self.root / f"{snapshot_id}.json"
                if path.exists():
                    snapshot = MicroVMSnapshot(**json.loads(path.read_text(encoding="utf-8")))
                    self._snapshots[snapshot_id] = snapshot
                    return snapshot
            raise KeyError(snapshot_id)

    def list(self) -> list[MicroVMSnapshot]:
        with self._lock:
            if self.root:
                for path in self.root.glob("*.json"):
                    snapshot_id = path.stem
                    if snapshot_id not in self._snapshots:
                        self._snapshots[snapshot_id] = MicroVMSnapshot(
                            **json.loads(path.read_text(encoding="utf-8"))
                        )
            return list(self._snapshots.values())


class MicroVMBackend(Protocol):
    def create(
        self,
        profile: str,
        *,
        node_id: str,
        runtime_version: str,
        vcpus: int,
        memory_mb: int,
    ) -> MicroVM:
        """Create and boot one clean MicroVM on a node."""

    def destroy(self, vm: MicroVM) -> None:
        """Destroy a task VM after one task completes."""


class MockMicroVMBackend:
    """Local backend for testing pool scheduling without Linux/KVM."""

    def create(
        self,
        profile: str,
        *,
        node_id: str = DEFAULT_NODE_ID,
        runtime_version: str = DEFAULT_RUNTIME_VERSION,
        vcpus: int = 1,
        memory_mb: int = 512,
    ) -> MicroVM:
        return MicroVM(
            vm_id=f"mock-vm-{uuid.uuid4().hex[:10]}",
            profile=profile,
            node_id=node_id,
            runtime_version=runtime_version,
            state="ready",
            created_at=time.time(),
            vcpus=vcpus,
            memory_mb=memory_mb,
            network_id=f"net-{uuid.uuid4().hex[:8]}",
        )

    def destroy(self, vm: MicroVM) -> None:
        vm.state = "destroyed"


class CubeSandboxBackend:
    """Real MicroVM backend backed by the CubeSandbox Python SDK.

    ``profile`` is mapped to a READY CubeSandbox template ID.  The backend
    keeps the live SDK objects in memory because task VMs are intentionally
    destroyed after completion.  The pool remains responsible for capacity,
    placement metadata, and replenishment; CubeSandbox owns the actual VM
    lifecycle.

    The SDK is imported lazily so local tests do not need CubeSandbox or its
    dependencies installed.
    """

    def __init__(
        self,
        *,
        api_url: str,
        api_key: str = "e2b_000000",
        proxy_node_ip: str | None = None,
        proxy_port_http: int = 80,
        template_map: dict[str, str] | None = None,
        default_template_id: str | None = None,
        sandbox_env_vars: dict[str, str] | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.proxy_node_ip = proxy_node_ip
        self.proxy_port_http = int(proxy_port_http)
        self.template_map = dict(template_map or {})
        self.default_template_id = default_template_id
        # These values are injected at Sandbox creation time and are never
        # copied into task payloads or registry metadata.  In production this
        # is where Controller supplies DSH credentials and the inner-harness
        # execution policy to the outer CubeSandbox VM.
        self.sandbox_env_vars = {
            str(key): str(value)
            for key, value in (sandbox_env_vars or {}).items()
            if value is not None
        }
        self._sandboxes: dict[str, Any] = {}
        self._lock = threading.RLock()

    def _template_for(self, profile: str) -> str:
        template_id = self.template_map.get(profile) or self.default_template_id
        if not template_id:
            raise RuntimeError(f"cube_template_missing:{profile}")
        return str(template_id)

    def _config(self, template_id: str) -> Any:
        try:
            from cubesandbox import Config
        except ImportError as exc:
            raise RuntimeError(
                "cubesandbox_sdk_not_installed: pip install cubesandbox"
            ) from exc
        candidate_kwargs = {
            "api_url": self.api_url,
            "api_key": self.api_key,
            "template_id": template_id,
            "proxy_port_http": self.proxy_port_http,
        }
        if self.proxy_node_ip:
            candidate_kwargs["proxy_node_ip"] = self.proxy_node_ip
        parameters = inspect.signature(Config).parameters
        accepts_arbitrary_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        kwargs = (
            candidate_kwargs
            if accepts_arbitrary_kwargs
            else {
                key: value
                for key, value in candidate_kwargs.items()
                if key in parameters
            }
        )
        return Config(**kwargs)

    def create(
        self,
        profile: str,
        *,
        node_id: str = DEFAULT_NODE_ID,
        runtime_version: str = DEFAULT_RUNTIME_VERSION,
        vcpus: int = 1,
        memory_mb: int = 512,
    ) -> MicroVM:
        template_id = self._template_for(profile)
        try:
            from cubesandbox import Sandbox
        except ImportError as exc:
            raise RuntimeError(
                "cubesandbox_sdk_not_installed: pip install cubesandbox"
            ) from exc

        create_kwargs: dict[str, Any] = {
            "template": template_id,
            "config": self._config(template_id),
        }
        if self.sandbox_env_vars:
            create_kwargs["env_vars"] = dict(self.sandbox_env_vars)
        sandbox = Sandbox.create(**create_kwargs)
        vm_id = str(sandbox.sandbox_id)
        with self._lock:
            self._sandboxes[vm_id] = sandbox
        return MicroVM(
            vm_id=vm_id,
            profile=profile,
            node_id=node_id,
            runtime_version=runtime_version,
            state="ready",
            created_at=time.time(),
            vcpus=vcpus,
            memory_mb=memory_mb,
            network_id=f"cube-network-{vm_id}",
        )

    def destroy(self, vm: MicroVM) -> None:
        with self._lock:
            sandbox = self._sandboxes.pop(vm.vm_id, None)
        if sandbox is None:
            return
        try:
            sandbox.kill()
        finally:
            vm.state = "destroyed"


class MicroVMPoolManager:
    """Warm pool with shared snapshots and node-aware placement."""

    def __init__(
        self,
        backend: MicroVMBackend | None = None,
        *,
        min_ready: int = 3,
        max_total: int = 8,
        nodes: list[MicroVMNode | dict[str, Any]] | None = None,
        snapshot_store: SharedSnapshotStore | None = None,
        default_runtime_version: str = DEFAULT_RUNTIME_VERSION,
        default_vcpus: int = 1,
        default_memory_mb: int = 512,
    ) -> None:
        if min_ready < 0 or max_total < max(1, min_ready):
            raise ValueError("require 0 <= min_ready <= max_total")
        self.backend = backend or MockMicroVMBackend()
        self.min_ready = min_ready
        self.max_total = max_total
        self.default_runtime_version = default_runtime_version
        self.default_vcpus = max(1, int(default_vcpus))
        self.default_memory_mb = max(128, int(default_memory_mb))
        self.snapshot_store = snapshot_store or SharedSnapshotStore()
        self._lock = threading.RLock()
        self._vms: dict[str, MicroVM] = {}
        self._nodes: dict[str, MicroVMNode] = {}
        for raw_node in nodes or [MicroVMNode(node_id=DEFAULT_NODE_ID)]:
            self.register_node(raw_node)

    def register_node(self, node: MicroVMNode | dict[str, Any]) -> MicroVMNode:
        value = node if isinstance(node, MicroVMNode) else MicroVMNode.from_dict(node)
        with self._lock:
            self._nodes[value.node_id] = value
            return value

    def list_nodes(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._node_payload(node) for node in self._nodes.values()]

    def isolate_node(self, node_id: str, *, drain: bool = False) -> dict[str, Any]:
        with self._lock:
            node = self._nodes[node_id]
            node.state = "draining" if drain else "isolated"
            return self._node_payload(node)

    def restore_node(self, node_id: str) -> dict[str, Any]:
        with self._lock:
            node = self._nodes[node_id]
            node.state = "ready"
            node.last_heartbeat_at = time.time()
            return self._node_payload(node)

    def ensure_pool(self, profile: str) -> list[MicroVM]:
        with self._lock:
            self._replenish_locked(profile)
            return self._profile_vms_locked(profile)

    def reserve(
        self,
        profile: str,
        job_id: str,
        *,
        preferred_node_id: str | None = None,
        runtime_version: str | None = None,
        vcpus: int | None = None,
        memory_mb: int | None = None,
    ) -> MicroVM:
        with self._lock:
            self._replenish_locked(profile)
            runtime = runtime_version or self.default_runtime_version
            requested_vcpus = vcpus or self.default_vcpus
            requested_memory = memory_mb or self.default_memory_mb
            ready = [
                vm
                for vm in self._profile_vms_locked(profile)
                if vm.state == "ready"
                and vm.runtime_version == runtime
                and self._node_can_host_locked(
                    vm.node_id, vm.runtime_version, vm.vcpus, vm.memory_mb
                )
            ]
            if preferred_node_id:
                preferred = [vm for vm in ready if vm.node_id == preferred_node_id]
                ready = preferred or ready
            if ready:
                ready.sort(key=lambda vm: self._nodes[vm.node_id].utilization())
                vm = ready[0]
            else:
                total = len(self._profile_vms_locked(profile))
                if total >= self.max_total:
                    raise RuntimeError(f"microvm_pool_exhausted:{profile}")
                vm = self._create_locked(
                    profile,
                    runtime_version=runtime,
                    vcpus=requested_vcpus,
                    memory_mb=requested_memory,
                    preferred_node_id=preferred_node_id,
                )
            vm.state = "busy"
            vm.job_id = job_id
            vm.last_used_at = time.time()
            self._replenish_locked(profile)
            return vm

    def pause(self, vm_id: str) -> MicroVMSnapshot:
        """Persist VM state to the shared store and release node resources."""
        with self._lock:
            vm = self._vms[vm_id]
            if vm.state == "destroyed":
                raise RuntimeError(f"microvm_destroyed:{vm_id}")
            snapshot = MicroVMSnapshot(
                snapshot_id=f"snap-{uuid.uuid4().hex[:12]}",
                profile=vm.profile,
                runtime_version=vm.runtime_version,
                source_vm_id=vm.vm_id,
                source_node_id=vm.node_id,
                created_at=time.time(),
            )
            self.snapshot_store.put(snapshot)
            vm.snapshot_id = snapshot.snapshot_id
            self._release_vm_resources_locked(vm)
            vm.state = "paused"
            return snapshot

    def resume(
        self,
        vm_id: str,
        *,
        target_node_id: str | None = None,
    ) -> MicroVM:
        """Resume a paused VM on the same or a different compatible node."""
        with self._lock:
            vm = self._vms[vm_id]
            if not vm.snapshot_id:
                raise RuntimeError(f"microvm_has_no_snapshot:{vm_id}")
            snapshot = self.snapshot_store.get(vm.snapshot_id)
            node = self._select_node_locked(
                runtime_version=snapshot.runtime_version,
                vcpus=vm.vcpus,
                memory_mb=vm.memory_mb,
                preferred_node_id=target_node_id,
            )
            if node is None:
                raise RuntimeError(
                    f"no_compatible_node_for_runtime:{snapshot.runtime_version}"
                )
            node.reserve(vm.vcpus, vm.memory_mb)
            vm.node_id = node.node_id
            vm.state = "busy" if vm.job_id else "ready"
            vm.last_used_at = time.time()
            return vm

    def create_from_snapshot(
        self,
        snapshot_id: str,
        *,
        target_node_id: str | None = None,
        job_id: str | None = None,
    ) -> MicroVM:
        """Create a new VM from a snapshot on any compatible node."""
        with self._lock:
            snapshot = self.snapshot_store.get(snapshot_id)
            node = self._select_node_locked(
                runtime_version=snapshot.runtime_version,
                vcpus=self.default_vcpus,
                memory_mb=self.default_memory_mb,
                preferred_node_id=target_node_id,
            )
            if node is None:
                raise RuntimeError(
                    f"no_compatible_node_for_runtime:{snapshot.runtime_version}"
                )
            vm = self._create_locked(
                snapshot.profile,
                node_id=node.node_id,
                runtime_version=snapshot.runtime_version,
                vcpus=self.default_vcpus,
                memory_mb=self.default_memory_mb,
            )
            vm.snapshot_id = snapshot_id
            vm.job_id = job_id
            vm.state = "busy" if job_id else "ready"
            return vm

    def migrate(self, vm_id: str, target_node_id: str) -> MicroVM:
        """Pause and resume a VM across nodes through the shared snapshot store."""
        self.pause(vm_id)
        return self.resume(vm_id, target_node_id=target_node_id)

    def destroy_and_replenish(self, vm_id: str) -> None:
        """Destroy a task VM and immediately create a clean replacement."""
        with self._lock:
            vm = self._vms.get(vm_id)
            if vm is None:
                return
            profile = vm.profile
            self._release_vm_resources_locked(vm)
            self.backend.destroy(vm)
            vm.state = "destroyed"
            vm.job_id = None
            self._vms.pop(vm_id, None)
            self._replenish_locked(profile)

    def release(self, vm_id: str, *, healthy: bool = False) -> None:
        """Compatibility alias: task VMs are never reused."""
        self.destroy_and_replenish(vm_id)

    def discard(self, vm_id: str) -> None:
        self.destroy_and_replenish(vm_id)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            profiles: dict[str, list[dict[str, Any]]] = {}
            for profile in sorted({vm.profile for vm in self._vms.values()}):
                profiles[profile] = [
                    asdict(vm) for vm in self._profile_vms_locked(profile)
                ]
            return {
                "min_ready": self.min_ready,
                "max_total": self.max_total,
                "default_runtime_version": self.default_runtime_version,
                "nodes": self.list_nodes(),
                "profiles": profiles,
                "snapshots": [
                    asdict(snapshot) for snapshot in self.snapshot_store.list()
                ],
            }

    def _profile_vms_locked(self, profile: str) -> list[MicroVM]:
        return [vm for vm in self._vms.values() if vm.profile == profile]

    def _create_locked(
        self,
        profile: str,
        *,
        node_id: str | None = None,
        runtime_version: str | None = None,
        vcpus: int | None = None,
        memory_mb: int | None = None,
        preferred_node_id: str | None = None,
    ) -> MicroVM:
        runtime = runtime_version or self.default_runtime_version
        requested_vcpus = vcpus or self.default_vcpus
        requested_memory = memory_mb or self.default_memory_mb
        selected_node = self._select_node_locked(
            runtime_version=runtime,
            vcpus=requested_vcpus,
            memory_mb=requested_memory,
            preferred_node_id=node_id or preferred_node_id,
        )
        if selected_node is None:
            raise RuntimeError(f"no_capacity_for_runtime:{runtime}")
        selected_node.reserve(requested_vcpus, requested_memory)
        try:
            vm = self.backend.create(
                profile,
                node_id=selected_node.node_id,
                runtime_version=runtime,
                vcpus=requested_vcpus,
                memory_mb=requested_memory,
            )
        except Exception:
            # Reserve happens before the external backend call.  If
            # CubeSandbox (or another real backend) rejects creation, release
            # the accounting reservation or later retries will see phantom
            # resource usage and the node can become permanently unavailable.
            selected_node.release(requested_vcpus, requested_memory)
            raise
        vm.node_id = selected_node.node_id
        vm.runtime_version = runtime
        vm.vcpus = requested_vcpus
        vm.memory_mb = requested_memory
        self._vms[vm.vm_id] = vm
        return vm

    def _replenish_locked(self, profile: str) -> None:
        vms = self._profile_vms_locked(profile)
        ready_count = sum(vm.state == "ready" for vm in vms)
        total = len(vms)
        while ready_count < self.min_ready and total < self.max_total:
            try:
                self._create_locked(profile)
            except RuntimeError:
                # An isolated or full node should not crash the Controller;
                # the pool reports the shortage and retries after node repair.
                break
            ready_count += 1
            total += 1

    def _select_node_locked(
        self,
        *,
        runtime_version: str,
        vcpus: int,
        memory_mb: int,
        preferred_node_id: str | None = None,
    ) -> MicroVMNode | None:
        candidates = [
            node
            for node in self._nodes.values()
            if node.can_host(runtime_version, vcpus, memory_mb)
        ]
        if preferred_node_id:
            preferred = [node for node in candidates if node.node_id == preferred_node_id]
            if preferred:
                return preferred[0]
        candidates.sort(key=lambda node: (node.utilization(), node.node_id))
        return candidates[0] if candidates else None

    def _node_can_host_locked(
        self,
        node_id: str,
        runtime_version: str,
        vcpus: int,
        memory_mb: int,
    ) -> bool:
        node = self._nodes.get(node_id)
        return bool(node and node.can_host(runtime_version, vcpus, memory_mb))

    def _release_vm_resources_locked(self, vm: MicroVM) -> None:
        node = self._nodes.get(vm.node_id)
        if node and vm.state not in {"paused", "destroyed"}:
            node.release(vm.vcpus, vm.memory_mb)

    @staticmethod
    def _node_payload(node: MicroVMNode) -> dict[str, Any]:
        payload = asdict(node)
        payload["supported_runtime_versions"] = sorted(node.supported_runtime_versions)
        payload["utilization"] = round(node.utilization(), 6)
        return payload
