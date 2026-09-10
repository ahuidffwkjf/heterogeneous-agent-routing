"""Run one DeepSeek Harness instance as a polling execution unit.

This adapter is intended to run inside one MicroVM.  It keeps the global
Controller reachable through outbound HTTP only, so the Controller does not
need to open a callback port inside the VM.  The actual work is delegated to
the official DSH headless profile:

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
import shlex
import subprocess
import threading
import time
from http.client import HTTPResponse
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


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
        return {
            "unit_id": self.unit_id,
            "unit_type": "harness",
            "platforms": ["linux"],
            "capabilities": self.capabilities,
            "tools": self.tools,
            "state": self.state,
            "load": self.load,
            "success_rate": 0.8,
            "quality_score": 0.8,
            "avg_latency_ms": 1000,
            "cost_score": 0.2,
            "metadata": {
                "transport": "poll",
                "heartbeat_required": True,
                "harness_type": "deepseek_harness",
                "runtime": "dsh",
                "dsh_profile": self.profile,
                "plugins": self.plugins,
                "scope": "harness",
                "sandbox": {
                    "type": "microvm",
                    "profile": f"dsh-{self.profile}",
                    "runtime_version": os.environ.get("DSH_RUNTIME_VERSION", "dsh-0.1"),
                },
                "hardware": {
                    "platform": "linux",
                    "architecture": "x86_64",
                    "gpu": bool(os.environ.get("CUDA_VISIBLE_DEVICES")),
                },
            },
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
        print(f"Registered DSH Harness {self.unit_id} with profile {self.profile}")

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
        if self.execution_mode == "cube":
            return self._execute_in_cube_sandbox(task, description)
        command = [self.dsh_bin, "--profile", "headless", description]
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
        description: str,
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
                [self.dsh_bin, "--profile", self.profile, description]
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
                task = self.poll(background=self.background_mode)
                if task is None:
                    self._stop.wait(self.poll_interval)
                    continue
                self.state = "busy"
                self.load = 1.0
                self.heartbeat()
                outcome = self.execute(task)
                if self.background_mode:
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
    parser.add_argument("--dsh-bin", default="dsh")
    parser.add_argument(
        "--execution-mode",
        choices=("host", "cube"),
        default=os.environ.get("DSH_EXECUTION_MODE", "host"),
        help="Run DSH on the adapter host or inside the leased CubeSandbox VM",
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
