"""Standalone Mac Agent for the first Mac + iPhone experiment.

Run:
    python3 mac_agent.py --host 127.0.0.1 --port 9001

The Controller calls POST /execute. This prototype performs safe local
actions and returns a structured result; it does not execute arbitrary code.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from harness_runtime import HarnessRuntime
from router import ExecutionUnit, Task

ALLOWED_DATA_ROOT = (Path(__file__).resolve().parent / "data").resolve()
UNIT_ID = "mac_agent_01"


def registration_payload() -> dict[str, Any]:
    return {
        "unit_id": UNIT_ID,
        "unit_type": "single_agent",
        "platforms": ["macos"],
        "capabilities": [
            "local_compute",
            "local_file_access",
            "document_generation",
            "image_inference",
            "code_build",
        ],
        "tools": ["python"],
        "state": "idle",
        "load": 0.0,
        "success_rate": 0.95,
        "quality_score": 0.88,
        "avg_latency_ms": 500,
        "cost_score": 0.25,
        "endpoint": "http://127.0.0.1:9001",
        "metadata": {
            "transport": "http",
            "heartbeat_required": True,
            "harness_type": "mac_local",
            "scope": "harness",
            "internal_agents": [
                {
                    "agent_id": "mac_file_agent",
                    "capabilities": ["local_file_access", "local_compute"],
                    "tools": ["python"],
                    "quality_score": 0.86
                },
                {
                    "agent_id": "mac_report_agent",
                    "capabilities": ["document_generation"],
                    "tools": ["python"],
                    "quality_score": 0.90
                },
                {
                    "agent_id": "mac_code_agent",
                    "capabilities": ["code_build", "local_compute"],
                    "tools": ["python", "xcode"],
                    "quality_score": 0.88
                }
            ],
            "hardware": {
                "platform": "macos",
                "architecture": platform.machine(),
                "cpu_cores": os.cpu_count() or 1,
                "gpu": False,
            },
        },
    }


MAC_HARNESS = HarnessRuntime(ExecutionUnit.from_dict(registration_payload()))


def heartbeat_loop(controller_url: str, registry_token: str, stop_event: threading.Event) -> None:
    headers = {
        "Content-Type": "application/json",
        "X-Registry-Token": registry_token,
    }
    register_request = Request(
        f"{controller_url.rstrip('/')}/registry/register",
        data=json.dumps(registration_payload(), ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(register_request, timeout=5):
            pass
        print(f"Registered {UNIT_ID} with controller")
    except Exception as exc:
        print(f"[Mac Agent] registration failed: {exc}")

    while not stop_event.wait(5.0):
        heartbeat = Request(
            f"{controller_url.rstrip('/')}/registry/heartbeat",
            data=json.dumps({"unit_id": UNIT_ID, "state": "idle", "load": 0.0}).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(heartbeat, timeout=5):
                pass
        except Exception as exc:
            print(f"[Mac Agent] heartbeat failed: {exc}")


def execute_task(task: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    description = str(task.get("description", ""))
    try:
        internal_plan = MAC_HARNESS.prepare(Task.from_dict(task))
        print(
            f"[Mac Harness] internal plan={internal_plan.mode} "
            f"members={len(internal_plan.member_ids)}"
        )
    except Exception as exc:
        return {
            "success": False,
            "quality": 0.0,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "cost": 0.0,
            "failure_type": "harness_cannot_compose_team",
            "executor": UNIT_ID,
            "error": str(exc),
        }
    inferred = task.get("inferred_requirements", {})
    inferred_capabilities = inferred.get("required_capabilities", []) if isinstance(inferred, dict) else []
    is_file_task = (
        "local_file_access" in task.get("required_capabilities", [])
        or "local_file_access" in inferred_capabilities
        or any(word in description for word in ("本地文件", "文件夹", "桌面文件", "读取文件"))
    )

    if is_file_task:
        input_path = task.get("input_path") or task.get("file_path")
        if not input_path:
            return {
                "success": False,
                "quality": 0.0,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "cost": 0.0,
                "failure_type": "missing_input_path",
                "executor": UNIT_ID,
                "error": "请在任务中提供 data/ 目录下的 input_path",
            }
        candidate = Path(str(input_path)).expanduser()
        if not candidate.is_absolute():
            candidate = Path(__file__).resolve().parent / candidate
        path = candidate.resolve()
        if ALLOWED_DATA_ROOT not in path.parents or not path.is_file():
            return {
                "success": False,
                "quality": 0.0,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "cost": 0.0,
                "failure_type": "input_file_not_allowed",
                "executor": UNIT_ID,
                "error": "只允许读取项目 data/ 目录中的文件",
            }
        if path.stat().st_size > 5 * 1024 * 1024:
            return {
                "success": False,
                "quality": 0.0,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "cost": 0.0,
                "failure_type": "input_file_too_large",
                "executor": UNIT_ID,
                "error": "演示版只读取不超过 5MB 的文本文件",
            }
        content = path.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        return {
            "success": True,
            "quality": 0.90,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "cost": 0.0,
            "failure_type": None,
            "executor": UNIT_ID,
            "run_id": uuid.uuid4().hex[:12],
            "output": {
                "input_path": str(path),
                "file_size_bytes": path.stat().st_size,
                "line_count": len(lines),
                "character_count": len(content),
                "report": (
                    f"文件《{path.name}》共 {len(lines)} 行、{len(content)} 个字符。"
                    f"\n内容摘录：{content[:500]}"
                ),
            },
        }

    if any(word in description for word in ("文档", "报告", "总结", "写作")) or "document_generation" in task.get("required_capabilities", []):
        output = (
            "Mac Agent 已完成文档任务。\n\n"
            f"任务描述：{description}\n"
            "执行方式：Mac 本地 Agent Harness\n"
            f"系统：{platform.platform()}"
        )
        quality = 0.90
    elif any(word in description for word in ("系统信息", "system info", "mac信息")):
        output = {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        }
        quality = 0.95
    else:
        output = f"Mac Agent 已接收任务：{description}"
        quality = 0.80

    return {
        "success": True,
        "quality": quality,
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "cost": 0.0,
        "failure_type": None,
        "executor": "mac_agent_01",
        "run_id": uuid.uuid4().hex[:12],
        "output": output,
    }


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json({"status": "ok", "service": "mac-agent", "unit_id": UNIT_ID})
            return
        self._json({"error": "not_found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/execute":
            self._json({"error": "not_found"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            task = json.loads(self.rfile.read(length))
            if not isinstance(task, dict):
                raise ValueError("task must be a JSON object")
            self._json(execute_task(task))
        except Exception as exc:
            self._json({"success": False, "failure_type": "invalid_request", "error": str(exc)}, status=400)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[Mac Agent] {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone Mac Agent")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--controller-url", default=None)
    parser.add_argument("--registry-token", default=None)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Mac Agent listening on http://{args.host}:{args.port}")
    heartbeat_stop = threading.Event()
    heartbeat_thread = None
    if args.controller_url and args.registry_token:
        heartbeat_thread = threading.Thread(
            target=heartbeat_loop,
            args=(args.controller_url, args.registry_token, heartbeat_stop),
            daemon=True,
        )
        heartbeat_thread.start()
    elif args.controller_url or args.registry_token:
        print("[Mac Agent] both --controller-url and --registry-token are required for registration")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        heartbeat_stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
