"""Run one local or remote DeepSeek Harness as a polling execution unit.

The default mode runs on the user's own computer and needs no ECS.  The actual
work can be delegated to the official DSH headless profile:

    dsh --profile headless "task text"

The adapter does not expose DSH's internal agents or teams to the global
Registry. It reports only the aggregate Harness capability summary. A newly
registered unit automatically consumes background canary probes first and
switches to the foreground task queue after promotion.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import platform
import shlex
import subprocess
import sys
import threading
import time
from http.client import HTTPResponse
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def local_platforms() -> list[str]:
    """Return normalized platform labels without exposing host details."""
    if sys.platform == "darwin":
        return ["local", "macos"]
    if sys.platform.startswith("linux"):
        return ["local", "linux"]
    if sys.platform.startswith("win"):
        return ["local", "windows"]
    return ["local", sys.platform]


def build_execution_prompt(
    task: dict[str, Any],
    *,
    unit_id: str,
    capabilities: list[str],
    plugins: list[str],
) -> str:
    """Build the black-box Harness contract sent to DSH.

    The Controller deliberately asks only for task-level output and failure
    evidence.  Internal Agent identities, messages, reasoning traces, local
    model parameters, and private data must remain inside the Harness.
    """
    description = str(task.get("description", "")).strip()
    inferred = task.get("inferred_requirements", {})
    return "\n".join(
        [
            "你是一个自治的黑箱 Harness。请完成以下任务。",
            f"任务：{description}",
            f"当前 Harness：{unit_id}",
            f"声明能力：{', '.join(capabilities) or '未声明'}",
            f"已装插件：{', '.join(plugins) or '未声明'}",
            f"路由推断：{json.dumps(inferred, ensure_ascii=False)}",
            "执行规则：",
            "1. 只使用当前 Harness 实际具备的能力，不虚构工具、设备、文件或执行结果。",
            "2. Harness 内部可自行选择 Agent、组建 Agent Team 并安排分工，无需公开内部结构。",
            "3. 不要返回思维链、内部消息、私有数据、模型参数或完整执行轨迹。",
            "4. 成功时返回最终产物和必要的可验证摘要。",
            "5. 失败时必须明确报告 failure_type、失败阶段、直接原因以及是否适合重试。",
            "6. 若任务与声明能力不匹配，立即失败并说明 missing_capability，不要假装完成。",
        ]
    )


def request_json(
    method: str,
    url: str,
    *,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, Any] | None]:
    body = None
    headers = {"X-Registry-Token": token}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, decode_json_response(response)
    except HTTPError as exc:
        if exc.code == 204:
            return 204, None
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        message = f"http_error:{exc.code}"
        if detail:
            message = f"{message}:{detail}"
        raise RuntimeError(message) from exc


def decode_json_response(response: HTTPResponse) -> dict[str, Any] | None:
    raw = response.read()
    if not raw:
        return None
    value = json.loads(raw.decode("utf-8"))
    return value if isinstance(value, dict) else None


class DSHAgent:
    def __init__(
        self,
        *,
        controller_url: str,
        registry_token: str,
        unit_id: str,
        profile: str,
        capabilities: list[str],
        tools: list[str],
        plugins: list[str],
        workspace: str,
        platforms: list[str] | None = None,
        dsh_bin: str = "dsh",
        execution_mode: str = "host",
        cube_api_url: str = "http://127.0.0.1:3000",
        cube_api_key: str = "e2b_000000",
        cube_proxy_node_ip: str | None = None,
        cube_proxy_port_http: int = 80,
        poll_interval: float = 2.0,
        timeout: float = 900.0,
    ) -> None:
        self.controller_url = controller_url.rstrip("/")
        self.registry_token = registry_token
        self.unit_id = unit_id
        self.profile = profile
        self.capabilities = capabilities
        self.tools = tools
        self.plugins = plugins
        self.workspace = workspace
        self.platforms = platforms or local_platforms()
        self.dsh_bin = dsh_bin
        self.execution_mode = execution_mode
        self.cube_api_url = cube_api_url.rstrip("/")
        self.cube_api_key = cube_api_key
        self.cube_proxy_node_ip = cube_proxy_node_ip
        self.cube_proxy_port_http = int(cube_proxy_port_http)
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.state = "idle"
        self.load = 0.0
        self.background_mode = False
        self._stop = threading.Event()

    def registration_payload(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "transport": "poll",
            "heartbeat_required": True,
            "harness_type": "deepseek_harness" if self.execution_mode != "mock" else "mock_harness",
            "runtime": "dsh" if self.execution_mode != "mock" else "mock",
            "dsh_profile": self.profile,
            "plugins": self.plugins,
            "scope": "harness",
            "isolation": "cube_microvm" if self.execution_mode == "cube" else "local_process",
            "hardware": {
                "platform": self.platforms[-1],
                "architecture": platform.machine() or "unknown",
                "cpu_cores": os.cpu_count() or 1,
                "gpu": bool(os.environ.get("CUDA_VISIBLE_DEVICES")),
            },
        }
        if self.execution_mode == "cube":
            metadata["sandbox"] = {
                "type": "microvm",
                "profile": f"dsh-{self.profile}",
                "runtime_version": os.environ.get("DSH_RUNTIME_VERSION", "dsh-0.1"),
            }
        return {
            "unit_id": self.unit_id,
            "unit_type": "harness",
            "platforms": self.platforms,
            "capabilities": self.capabilities,
            "tools": self.tools,
            "state": self.state,
            "load": self.load,
            "success_rate": 0.8,
            "quality_score": 0.8,
            "avg_latency_ms": 1000,
            "cost_score": 0.2,
            "metadata": metadata,
            # Unknown runtime Harnesses are onboarded through the background
            # probe lane. Pre-seeded units keep their configured foreground
            # status when they reconnect.
            "registration_mode": "background",
        }

    def register(self) -> None:
        status, body = request_json(
            "POST",
            f"{self.controller_url}/registry/register",
            token=self.registry_token,
            payload=self.registration_payload(),
        )
        if status != 201:
            raise RuntimeError(f"registration_failed:{status}")
        self._update_mode(body)
        print(
            f"Registered Harness {self.unit_id} "
            f"with profile {self.profile} ({self.execution_mode})"
        )

    def _update_mode(self, body: dict[str, Any] | None) -> None:
        unit = (body or {}).get("unit", {})
        metadata = unit.get("metadata", {}) if isinstance(unit, dict) else {}
        self.background_mode = (
            unit.get("state") == "testing"
            or metadata.get("routing_scope") == "background"
        )

    def heartbeat(self) -> None:
        _status, body = request_json(
            "POST",
            f"{self.controller_url}/registry/heartbeat",
            token=self.registry_token,
            payload={
                "unit_id": self.unit_id,
                "state": self.state,
                "load": self.load,
            },
            timeout=5.0,
        )
        self._update_mode(body)

    def poll(self, *, background: bool = False) -> dict[str, Any] | None:
        query = urlencode({"unit_id": self.unit_id})
        path = "/background/tasks/next" if background else "/tasks/next"
        status, task = request_json(
            "GET",
            f"{self.controller_url}{path}?{query}",
            token=self.registry_token,
            timeout=10.0,
        )
        if status == 204:
            return None
        return task

    def execute(self, task: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        description = str(task.get("description", "")).strip()
        if not description:
            return {
                "success": False,
                "failure_type": "empty_task_description",
                "executor": self.unit_id,
            }
        prompt = build_execution_prompt(
            task,
            unit_id=self.unit_id,
            capabilities=self.capabilities,
            plugins=self.plugins,
        )
        if self.execution_mode == "mock":
            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            return {
                "success": True,
                "quality": 0.75,
                "latency_ms": latency_ms,
                "cost": 0.0,
                "failure_type": None,
                "executor": self.unit_id,
                "output": f"[local mock] 已完成：{description}",
                "execution_mode": "mock",
            }
        if self.execution_mode == "cube":
            return self._execute_in_cube_sandbox(task, prompt)
        command = [self.dsh_bin, "--profile", self.profile, prompt]
        try:
            completed = subprocess.run(
                command,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            return {
                "success": False,
                "failure_type": "dsh_not_installed",
                "error": str(exc),
                "executor": self.unit_id,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "success": False,
                "failure_type": "dsh_timeout",
                "error": str(exc),
                "executor": self.unit_id,
            }
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        output = (completed.stdout or completed.stderr).strip()
        return {
            "success": completed.returncode == 0,
            "quality": 0.8 if completed.returncode == 0 else 0.0,
            "latency_ms": latency_ms,
            "cost": 0.0,
            "failure_type": None if completed.returncode == 0 else "dsh_execution_failed",
            "executor": self.unit_id,
            "dsh_profile": self.profile,
            "output": output,
            "returncode": completed.returncode,
        }

    def _execute_in_cube_sandbox(
        self,
        task: dict[str, Any],
        prompt: str,
    ) -> dict[str, Any]:
        """Run DSH inside the task MicroVM selected by the Controller.

        The Controller leases the sandbox and passes its ID with the poll
        payload.  This adapter only connects to that lease; it never creates
        or destroys the sandbox itself.  That keeps lifecycle ownership in
        the Controller and makes retries safe.
        """
        started = time.perf_counter()
        vm_id = str(task.get("microvm_id", ""))
        if not vm_id:
            return {
                "success": False,
                "failure_type": "missing_microvm_lease",
                "executor": self.unit_id,
            }
        try:
            from cubesandbox import Config, Sandbox

            candidate_config_kwargs: dict[str, Any] = {
                "api_url": self.cube_api_url,
                "api_key": self.cube_api_key,
                "proxy_port_http": self.cube_proxy_port_http,
            }
            if self.cube_proxy_node_ip:
                candidate_config_kwargs["proxy_node_ip"] = self.cube_proxy_node_ip
            parameters = inspect.signature(Config).parameters
            accepts_arbitrary_kwargs = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            config_kwargs = (
                candidate_config_kwargs
                if accepts_arbitrary_kwargs
                else {
                    key: value
                    for key, value in candidate_config_kwargs.items()
                    if key in parameters
                }
            )
            sandbox = Sandbox.connect(vm_id, config=Config(**config_kwargs))
            command = shlex.join(
                [self.dsh_bin, "--profile", self.profile, prompt]
            )
            result = sandbox.commands.run(command)
            returncode = int(getattr(result, "exit_code", 0) or 0)
            stdout = str(getattr(result, "stdout", "") or "")
            stderr = str(getattr(result, "stderr", "") or "")
            return {
                "success": returncode == 0,
                "quality": 0.8 if returncode == 0 else 0.0,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "cost": 0.0,
                "failure_type": None if returncode == 0 else "dsh_execution_failed",
                "executor": self.unit_id,
                "dsh_profile": self.profile,
                "output": (stdout or stderr).strip(),
                "returncode": returncode,
                "execution_mode": "cube",
                "microvm_id": vm_id,
            }
        except ImportError as exc:
            return {
                "success": False,
                "failure_type": "cubesandbox_sdk_not_installed",
                "error": str(exc),
                "executor": self.unit_id,
                "execution_mode": "cube",
            }
        except Exception as exc:
            return {
                "success": False,
                "failure_type": "cube_sandbox_execution_failed",
                "error": str(exc),
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "executor": self.unit_id,
                "execution_mode": "cube",
                "microvm_id": vm_id,
            }

    def complete(self, task: dict[str, Any], outcome: dict[str, Any]) -> None:
        job_id = str(task["job_id"])
        outcome["executor"] = self.unit_id
        outcome["lease_id"] = task.get("lease_id")
        request_json(
            "POST",
            f"{self.controller_url}/tasks/{job_id}/result",
            token=self.registry_token,
            payload=outcome,
            timeout=15.0,
        )

    def complete_probe(self, task: dict[str, Any], outcome: dict[str, Any]) -> None:
        probe_id = str(task["probe_id"])
        outcome["executor"] = self.unit_id
        outcome["lease_id"] = task.get("lease_id")
        request_json(
            "POST",
            f"{self.controller_url}/background/tasks/{probe_id}/result",
            token=self.registry_token,
            payload=outcome,
            timeout=15.0,
        )

    def run(self) -> None:
        self.register()
        while not self._stop.is_set():
            try:
                self.state = "idle"
                self.load = 0.0
                self.heartbeat()
                # A foreground Harness may discover a new internal Agent at
                # runtime. Keep polling the background lane even after the
                # parent Harness is eligible, then fall back to user work.
                task = self.poll(background=True)
                task_is_background = task is not None
                if task is None and not self.background_mode:
                    task = self.poll(background=False)
                elif task is None:
                    task = None
                if task is None:
                    self._stop.wait(self.poll_interval)
                    continue
                self.state = "busy"
                self.load = 1.0
                self.heartbeat()
                outcome = self.execute(task)
                if task_is_background:
                    self.complete_probe(task, outcome)
                else:
                    self.complete(task, outcome)
            except (
                HTTPError,
                URLError,
                TimeoutError,
                OSError,
                ValueError,
                RuntimeError,
            ) as exc:
                print(f"[DSH Agent] loop error: {exc}")
                self._stop.wait(self.poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="DeepSeek Harness polling adapter")
    parser.add_argument("--controller-url", required=True)
    parser.add_argument("--registry-token", required=True)
    parser.add_argument("--unit-id", required=True)
    parser.add_argument("--profile", default="headless")
    parser.add_argument("--capability", action="append", default=[])
    parser.add_argument("--tool", action="append", default=[])
    parser.add_argument("--plugin", action="append", default=[])
    parser.add_argument("--workspace", default="/workspace")
    parser.add_argument(
        "--platform",
        action="append",
        default=[],
        help="Override auto-detected local platform labels",
    )
    parser.add_argument("--dsh-bin", default="dsh")
    parser.add_argument(
        "--execution-mode",
        choices=("host", "mock", "cube"),
        default=os.environ.get("DSH_EXECUTION_MODE", "host"),
        help="Run DSH locally, run a zero-dependency mock, or use optional CubeSandbox",
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
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args()
    agent = DSHAgent(
        controller_url=args.controller_url,
        registry_token=args.registry_token,
        unit_id=args.unit_id,
        profile=args.profile,
        capabilities=args.capability,
        tools=args.tool,
        plugins=args.plugin,
        workspace=args.workspace,
        platforms=args.platform or None,
        dsh_bin=args.dsh_bin,
        execution_mode=args.execution_mode,
        cube_api_url=args.cube_api_url,
        cube_api_key=args.cube_api_key,
        cube_proxy_node_ip=args.cube_proxy_node_ip,
        cube_proxy_port_http=args.cube_proxy_port_http,
        poll_interval=args.poll_interval,
        timeout=args.timeout,
    )
    try:
        agent.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
