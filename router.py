"""Global router for heterogeneous Harness endpoints.

The global router treats each registered Harness as one execution unit. A
Harness may contain one Agent, multiple Agents, or an Agent Team; its internal
discovery and coordination remain private to the Harness runtime.

Run a demo:
    python router.py

Run the HTTP service:
    python router.py --serve --port 8080
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable


DEFAULT_OBJECTIVE_WEIGHTS = {
    "success": 0.35,
    "quality": 0.30,
    "latency": 0.15,
    "cost": 0.10,
    "load": 0.10,
}


class TaskParser:
    """Small rule-based parser used before the learned World Model exists.

    The user only needs to provide a natural-language description. Explicit
    structured requirements are still accepted and are merged with inferred
    requirements.
    """

    @staticmethod
    def infer(description: str) -> dict[str, Any]:
        text = description.lower()
        capabilities: set[str] = set()
        tools: set[str] = set()
        platforms: set[str] = set()
        reasons: list[str] = []
        requires_gpu = False
        requires_mobile = False

        def has(*keywords: str) -> bool:
            return any(keyword in text for keyword in keywords)

        if has("手机", "iphone", "ios", "移动端", "移动设备"):
            capabilities.add("mobile")
            requires_mobile = True
            platforms.add("ios")
            reasons.append("检测到移动端/iPhone需求")
        if has("摄像头", "拍照", "拍摄", "照片", "摄像", "扫描", "camera", "传感器", "sensor", "二维码"):
            capabilities.add("camera" if has("摄像头", "拍照", "拍摄", "照片", "摄像", "camera") else "sensor")
            requires_mobile = True
            capabilities.add("mobile")
            platforms.add("ios")
            reasons.append("检测到摄像头或传感器需求")
        if has("macos", "mac", "xcode"):
            platforms.add("macos")
            reasons.append("检测到 macOS/Xcode 需求")
            if has("xcode"):
                tools.add("xcode")
                capabilities.add("code_build")
        if has("linux"):
            platforms.add("linux")
            reasons.append("检测到 Linux 需求")
        if has("gpu", "cuda", "模型训练", "深度学习"):
            capabilities.add("gpu")
            requires_gpu = True
            if has("cuda"):
                tools.add("cuda")
            reasons.append("检测到 GPU/CUDA 计算需求")
        if has("并行", "矩阵", "批量计算"):
            capabilities.add("parallel_compute")
            reasons.append("检测到并行计算需求")
        if has("本地文件", "桌面文件", "文件夹", "磁盘", "终端", "命令行", "脚本", "批处理", "编译"):
            capabilities.add("local_file_access")
            reasons.append("检测到本地文件或本机执行需求")
            if has("脚本", "终端", "命令行", "编译"):
                capabilities.add("local_compute")
        if has("录音", "定位", "震动", "触摸"):
            capabilities.add("mobile")
            requires_mobile = True
            platforms.add("ios")
            reasons.append("检测到手机设备能力需求")
        if has("图像识别", "图片分类", "图像推理", "图像", "图片", "ocr", "识别"):
            capabilities.add("image_inference")
            reasons.append("检测到图像处理需求")
        if has("文档", "报告", "总结", "写作"):
            capabilities.add("document_generation")
            reasons.append("检测到文档生成需求")
        if has("python"):
            capabilities.add("python")
            tools.add("python")
            reasons.append("检测到 Python 工具需求")

        return {
            "required_capabilities": capabilities,
            "required_tools": tools,
            "allowed_platforms": platforms,
            "requires_gpu": requires_gpu,
            "requires_mobile": requires_mobile,
            "reasons": reasons,
        }


class NoEligibleUnit(RuntimeError):
    """Raised when no execution unit satisfies the task's hard constraints."""

    def __init__(self, task_id: str, rejected: dict[str, list[str]]):
        self.task_id = task_id
        self.rejected = rejected
        super().__init__(f"No eligible execution unit for task {task_id!r}")


@dataclass
class Task:
    task_id: str
    description: str = ""
    required_capabilities: set[str] = field(default_factory=set)
    preferred_capabilities: set[str] = field(default_factory=set)
    required_tools: set[str] = field(default_factory=set)
    allowed_platforms: set[str] = field(default_factory=set)
    preferred_platforms: set[str] = field(default_factory=set)
    preferred_unit_types: set[str] = field(default_factory=set)
    requires_gpu: bool = False
    requires_mobile: bool = False
    objective_weights: dict[str, float] = field(default_factory=dict)
    inference_reasons: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        inferred = TaskParser.infer(str(data.get("description", "")))
        return cls(
            task_id=str(data.get("task_id", "anonymous-task")),
            description=str(data.get("description", "")),
            required_capabilities=set(data.get("required_capabilities", [])) | inferred["required_capabilities"],
            preferred_capabilities=set(data.get("preferred_capabilities", [])),
            required_tools=set(data.get("required_tools", [])) | inferred["required_tools"],
            allowed_platforms=set(data.get("allowed_platforms", [])) | inferred["allowed_platforms"],
            preferred_platforms=set(data.get("preferred_platforms", [])),
            preferred_unit_types=set(data.get("preferred_unit_types", [])),
            requires_gpu=bool(data.get("requires_gpu", False)) or inferred["requires_gpu"],
            requires_mobile=bool(data.get("requires_mobile", False)) or inferred["requires_mobile"],
            objective_weights=dict(data.get("objective_weights", {})),
            inference_reasons=inferred["reasons"],
        )


@dataclass
class ExecutionUnit:
    unit_id: str
    unit_type: str  # compatibility field; global units are Harness endpoints
    platforms: set[str] = field(default_factory=set)
    capabilities: set[str] = field(default_factory=set)
    tools: set[str] = field(default_factory=set)
    state: str = "idle"  # idle | busy | degraded | offline
    load: float = 0.0  # 0.0 to 1.0
    success_rate: float = 0.8
    quality_score: float = 0.8
    avg_latency_ms: float = 1000.0
    cost_score: float = 0.5  # larger means more expensive
    endpoint: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionUnit":
        return cls(
            unit_id=str(data["unit_id"]),
            unit_type=str(data.get("unit_type", "single_agent")),
            platforms=set(data.get("platforms", [])),
            capabilities=set(data.get("capabilities", [])),
            tools=set(data.get("tools", [])),
            state=str(data.get("state", "idle")),
            load=float(data.get("load", 0.0)),
            success_rate=float(data.get("success_rate", 0.8)),
            quality_score=float(data.get("quality_score", 0.8)),
            avg_latency_ms=float(data.get("avg_latency_ms", 1000.0)),
            cost_score=float(data.get("cost_score", 0.5)),
            endpoint=data.get("endpoint"),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class RouteDecision:
    task_id: str
    selected_unit: str
    selected_unit_type: str
    score: float
    ranked_candidates: list[dict[str, Any]]
    rejected_units: dict[str, list[str]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Router:
    """Constraint-first router for global Harness endpoints."""

    def __init__(self, units: Iterable[ExecutionUnit] = ()):
        self.units: dict[str, ExecutionUnit] = {unit.unit_id: unit for unit in units}

    def register(self, unit: ExecutionUnit) -> None:
        self.units[unit.unit_id] = unit

    def update(self, unit_id: str, **changes: Any) -> None:
        if unit_id not in self.units:
            raise KeyError(f"Unknown execution unit: {unit_id}")
        for key, value in changes.items():
            if not hasattr(self.units[unit_id], key):
                raise ValueError(f"Unknown execution unit field: {key}")
            setattr(self.units[unit_id], key, value)

    def route(self, task: Task) -> RouteDecision:
        eligible: list[tuple[ExecutionUnit, float, dict[str, Any]]] = []
        rejected: dict[str, list[str]] = {}

        for unit in self.units.values():
            reasons = self._hard_constraint_failures(task, unit)
            if reasons:
                rejected[unit.unit_id] = reasons
                continue
            score, breakdown = self._score(task, unit)
            eligible.append((unit, score, breakdown))

        if not eligible:
            raise NoEligibleUnit(task.task_id, rejected)

        eligible.sort(key=lambda item: item[1], reverse=True)
        selected, selected_score, _ = eligible[0]
        ranked = [
            {
                "unit_id": unit.unit_id,
                "unit_type": unit.unit_type,
                "score": round(score, 6),
                "breakdown": breakdown,
            }
            for unit, score, breakdown in eligible
        ]
        return RouteDecision(
            task_id=task.task_id,
            selected_unit=selected.unit_id,
            selected_unit_type=selected.unit_type,
            score=round(selected_score, 6),
            ranked_candidates=ranked,
            rejected_units=rejected,
        )

    @staticmethod
    def _hard_constraint_failures(task: Task, unit: ExecutionUnit) -> list[str]:
        failures: list[str] = []
        if unit.state == "offline":
            failures.append("offline")
        if not task.required_capabilities.issubset(unit.capabilities):
            missing = sorted(task.required_capabilities - unit.capabilities)
            failures.append(f"missing_capabilities:{','.join(missing)}")
        if not task.required_tools.issubset(unit.tools):
            missing = sorted(task.required_tools - unit.tools)
            failures.append(f"missing_tools:{','.join(missing)}")
        if task.allowed_platforms and not task.allowed_platforms.intersection(unit.platforms):
            failures.append("platform_incompatible")
        if task.requires_gpu and "gpu" not in unit.capabilities:
            failures.append("gpu_required")
        if task.requires_mobile and "mobile" not in unit.capabilities:
            failures.append("mobile_required")
        return failures

    @staticmethod
    def _score(task: Task, unit: ExecutionUnit) -> tuple[float, dict[str, float]]:
        weights = {**DEFAULT_OBJECTIVE_WEIGHTS, **task.objective_weights}
        preferred_capabilities = (
            len(task.preferred_capabilities & unit.capabilities)
            / max(1, len(task.preferred_capabilities))
        )
        preferred_platforms = (
            1.0
            if task.preferred_platforms.intersection(unit.platforms)
            else 0.0
        )
        preferred_type = (
            1.0 if unit.unit_type in task.preferred_unit_types else 0.0
        )
        # Bounded normalization: better latency/cost/load means a larger score.
        latency = 1.0 / (1.0 + max(0.0, unit.avg_latency_ms) / 1000.0)
        cost = 1.0 - min(1.0, max(0.0, unit.cost_score))
        load = 1.0 - min(1.0, max(0.0, unit.load))
        capability_bonus = 0.5 * preferred_capabilities + 0.3 * preferred_platforms + 0.2 * preferred_type
        breakdown = {
            "success": unit.success_rate,
            "quality": unit.quality_score,
            "latency": latency,
            "cost": cost,
            "load": load,
            "preference_bonus": capability_bonus,
        }
        score = (
            weights["success"] * breakdown["success"]
            + weights["quality"] * breakdown["quality"]
            + weights["latency"] * breakdown["latency"]
            + weights["cost"] * breakdown["cost"]
            + weights["load"] * breakdown["load"]
            + 0.05 * capability_bonus
        )
        return score, {key: round(value, 6) for key, value in breakdown.items()}


def demo_router() -> Router:
    return Router(
        [
            ExecutionUnit(
                unit_id="dsh_harness_file_demo",
                unit_type="harness",
                platforms={"linux"},
                capabilities={"code_build", "document_generation"},
                tools={"xcode", "python"},
                success_rate=0.92,
                quality_score=0.86,
                avg_latency_ms=1300,
                cost_score=0.35,
            ),
            ExecutionUnit(
                unit_id="dsh_harness_code_demo",
                unit_type="harness",
                platforms={"linux"},
                capabilities={"code_build", "document_generation", "python"},
                tools={"python", "docker"},
                load=0.25,
                success_rate=0.89,
                quality_score=0.82,
                avg_latency_ms=900,
                cost_score=0.25,
            ),
            ExecutionUnit(
                unit_id="dsh_harness_gpu_demo",
                unit_type="harness",
                platforms={"linux"},
                capabilities={"gpu", "parallel_compute", "image_inference", "python"},
                tools={"cuda", "python"},
                load=0.45,
                success_rate=0.96,
                quality_score=0.94,
                avg_latency_ms=650,
                cost_score=0.70,
            ),
        ]
    )


class RouterHandler(BaseHTTPRequestHandler):
    router = demo_router()

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/units":
            self._send_json({"units": [asdict(unit) for unit in self.router.units.values()]})
            return
        if self.path == "/health":
            self._send_json({"status": "ok"})
            return
        self._send_json({"error": "not_found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/route":
            self._send_json({"error": "not_found"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            task = Task.from_dict(json.loads(self.rfile.read(length)))
            self._send_json(self.router.route(task).to_dict())
        except NoEligibleUnit as exc:
            self._send_json(
                {"error": str(exc), "task_id": exc.task_id, "rejected_units": exc.rejected},
                status=422,
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send_json({"error": f"invalid_request: {exc}"}, status=400)

    def log_message(self, *_args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal heterogeneous Agent router")
    parser.add_argument("--serve", action="store_true", help="start the HTTP service")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if args.serve:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), RouterHandler)
        print(f"Router listening on http://127.0.0.1:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return

    router = demo_router()
    task = Task.from_dict(
        {
            "task_id": "demo-gpu-task",
            "description": "Run parallel image inference",
            "required_capabilities": ["image_inference"],
            "requires_gpu": True,
            "objective_weights": {"latency": 0.25, "quality": 0.35},
        }
    )
    print(json.dumps(router.route(task).to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
