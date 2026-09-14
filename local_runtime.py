"""One-command local runtime for the heterogeneous Harness prototype.

This launcher starts one Controller and every Harness declared in
``execution_units_local.json``.  It deliberately uses localhost and local
processes, so neither ECS nor CubeSandbox is required.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
DEFAULT_REGISTRY = ROOT / "execution_units_local.json"
DEFAULT_STATE_DIR = ROOT / ".local_runtime"


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 2.0,
) -> dict[str, Any]:
    headers: dict[str, str] = {}
    body = None
    if token:
        headers["X-Registry-Token"] = token
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def load_units(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    units = payload.get("units", payload)
    if not isinstance(units, list) or not units:
        raise ValueError("local registry must contain a non-empty units list")
    return units


def controller_command(
    *,
    python: str,
    host: str,
    port: int,
    registry: Path,
    state_dir: Path,
    token: str,
) -> list[str]:
    return [
        python,
        "-u",
        str(ROOT / "controller.py"),
        "--host",
        host,
        "--port",
        str(port),
        "--registry",
        str(registry),
        "--database",
        str(state_dir / "registry.db"),
        "--registry-token",
        token,
        "--microvm-backend",
        "mock",
        "--microvm-pool-size",
        "0",
        "--microvm-max-total",
        "1",
        "--heartbeat-timeout",
        "15",
    ]


def adapter_command(
    unit: dict[str, Any],
    *,
    python: str,
    controller_url: str,
    token: str,
    engine: str,
    workspace: Path,
    poll_interval: float,
) -> list[str]:
    command = [
        python,
        "-u",
        str(ROOT / "dsh_agent.py"),
        "--controller-url",
        controller_url,
        "--registry-token",
        token,
        "--unit-id",
        str(unit["unit_id"]),
        "--profile",
        str(unit.get("metadata", {}).get("dsh_profile", "headless")),
        "--execution-mode",
        engine,
        "--workspace",
        str(workspace),
        "--poll-interval",
        str(poll_interval),
    ]
    for capability in unit.get("capabilities", []):
        command.extend(["--capability", str(capability)])
    for tool in unit.get("tools", []):
        command.extend(["--tool", str(tool)])
    for plugin in unit.get("metadata", {}).get("plugins", []):
        command.extend(["--plugin", str(plugin)])
    return command


def wait_until_ready(
    controller_url: str,
    token: str,
    expected_units: int,
    controller: subprocess.Popen[Any],
    timeout: float = 15.0,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if controller.poll() is not None:
            raise RuntimeError(f"Controller exited with code {controller.returncode}")
        try:
            request_json(f"{controller_url}/health")
            units = request_json(
                f"{controller_url}/registry/units", token=token
            ).get("units", [])
            online = [unit for unit in units if unit.get("state") != "offline"]
            if len(online) >= expected_units:
                return
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"local runtime did not become ready: {last_error}")


def run_smoke_test(controller_url: str) -> dict[str, Any]:
    created = request_json(
        f"{controller_url}/tasks",
        method="POST",
        payload={
            "task_id": f"local-smoke-{int(time.time())}",
            "description": "运行 Python 单元测试",
        },
        timeout=5.0,
    )
    job_id = created["job_id"]
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        job = request_json(f"{controller_url}/tasks/{job_id}", timeout=5.0)
        if job.get("status") in {"completed", "failed"}:
            return job
        time.sleep(0.1)
    raise TimeoutError(f"smoke test timed out: {job_id}")


def stop_processes(processes: list[subprocess.Popen[Any]]) -> None:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 3.0
    for process in reversed(processes):
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Controller and heterogeneous Harnesses locally"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument(
        "--engine",
        choices=("mock", "host"),
        default="mock",
        help="mock is zero-dependency; host invokes the locally installed dsh binary",
    )
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="submit one task, print the result, and exit",
    )
    args = parser.parse_args()

    if args.engine == "host" and shutil.which("dsh") is None:
        raise SystemExit(
            "dsh was not found in PATH. Install/configure DSH or use --engine mock."
        )

    registry = args.registry.expanduser().resolve()
    state_dir = args.state_dir.expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    units = load_units(registry)
    token_path = state_dir / "registry_token"
    token = token_path.read_text(encoding="utf-8").strip() if token_path.exists() else ""
    if not token:
        token = secrets.token_urlsafe(32)
        token_path.write_text(token, encoding="utf-8")
        os.chmod(token_path, 0o600)

    python = sys.executable
    controller_url = f"http://{args.host}:{args.port}"
    processes: list[subprocess.Popen[Any]] = []

    def handle_signal(_signum: int, _frame: Any) -> None:
        stop_processes(processes)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        controller = subprocess.Popen(
            controller_command(
                python=python,
                host=args.host,
                port=args.port,
                registry=registry,
                state_dir=state_dir,
                token=token,
            ),
            cwd=ROOT,
        )
        processes.append(controller)

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if controller.poll() is not None:
                raise RuntimeError(f"Controller exited with code {controller.returncode}")
            try:
                request_json(f"{controller_url}/health")
                break
            except (URLError, TimeoutError, OSError):
                time.sleep(0.1)
        else:
            raise RuntimeError("Controller health check timed out")

        for unit in units:
            workspace = state_dir / "workspaces" / str(unit["unit_id"])
            workspace.mkdir(parents=True, exist_ok=True)
            process = subprocess.Popen(
                adapter_command(
                    unit,
                    python=python,
                    controller_url=controller_url,
                    token=token,
                    engine=args.engine,
                    workspace=workspace,
                    poll_interval=args.poll_interval,
                ),
                cwd=ROOT,
            )
            processes.append(process)

        wait_until_ready(controller_url, token, len(units), controller)
        print(f"Local FedHarness runtime ready: {controller_url}")
        print(f"Harnesses: {len(units)} | engine: {args.engine}")

        if args.smoke_test:
            job = run_smoke_test(controller_url)
            print(json.dumps(job, ensure_ascii=False, indent=2))
            if job.get("status") != "completed":
                raise SystemExit(1)
            return

        while controller.poll() is None:
            time.sleep(0.5)
        raise RuntimeError(f"Controller exited with code {controller.returncode}")
    finally:
        stop_processes(processes)


if __name__ == "__main__":
    main()
