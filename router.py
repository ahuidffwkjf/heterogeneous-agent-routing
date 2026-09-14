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
import re
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


ROUTING_POLICY_PROMPT = """你是异构 Harness 网络的任务需求分析器。Harness 是黑箱：不要假设能看到其内部 Agent、轨迹、模型或本地数据。

请只根据用户明确表达的任务目标提取路由需求，并遵守以下规则：
1. 区分硬约束与偏好。只有“必须、只能、需要、限定、运行在”等明确表达才形成平台或硬件硬约束；“最好、优先、可以”只能形成偏好。
2. 正确处理否定表达，例如“不需要 GPU”“不要联网”“无需手机”。否定项不得加入需求。
3. 摄像头、传感器和本地文件是能力约束，不自动等同于某一种操作系统；只有用户明确要求 iPhone、Android、macOS 或 Linux 时才限制平台。
4. 图像处理、深度学习和并行任务可以偏好 GPU，但只有明确要求 GPU/CUDA 或规模足够大时才强制 GPU。
5. 同时识别能力、工具、平台、网络、隐私、风险、并行性、时延、成本和质量目标。
6. 不允许用户指定 Harness 或内部 Agent。Router 必须根据注册能力与动态状态选择。
7. 无法确定的信息标为 unknown，不要补造需求；冲突需求必须显式报告。
8. 输出每个判断的简短理由，使路由结果可以审计。

输出字段：required_capabilities、preferred_capabilities、required_tools、allowed_platforms、preferred_platforms、requires_gpu、requires_mobile、needs_network、privacy_level、risk_level、parallelizable、objective_weights、reasons、warnings。
"""


class TaskParser:
    """Explainable local parser used before the learned World Model exists.

    The rules intentionally mirror :data:`ROUTING_POLICY_PROMPT`.  This keeps
    the local baseline deterministic and auditable while providing the same
    schema that a distilled World Model can produce later.
    """

    CAPABILITY_RULES: dict[str, tuple[str, ...]] = {
        "camera": ("摄像头", "拍照", "拍摄", "camera", "扫码", "二维码"),
        "sensor": ("传感器", "定位", "gps", "陀螺仪", "加速度计", "sensor"),
        "audio": ("录音", "音频", "语音", "麦克风", "audio", "microphone"),
        "image_inference": ("图像识别", "图片分类", "图像推理", "视觉分析", "ocr", "目标检测"),
        "document_generation": ("文档", "报告", "总结", "写作", "markdown", "论文", "readme"),
        "local_file_access": ("本地文件", "桌面文件", "文件夹", "磁盘", "读取文件", "写入文件"),
        "local_compute": ("终端", "命令行", "脚本", "批处理", "运行代码", "执行代码", "shell"),
        "code_build": ("编译", "构建", "build", "xcode", "单元测试", "pytest", "unittest"),
        "test_execution": ("运行测试", "执行测试", "test suite", "测试套件", "测试用例"),
        "python": ("python", "pytest", "pip", "pandas", "numpy"),
        "data_processing": ("数据处理", "数据分析", "统计分析", "清洗数据", "csv", "表格"),
        "structured_extraction": ("结构化抽取", "信息抽取", "提取字段", "解析json", "解析 json"),
        "web_research": ("联网搜索", "网页搜索", "浏览网页", "网络检索", "查找资料", "web research"),
        "database": ("数据库", "sqlite", "postgresql", "mysql", "redis", "sql"),
        "parallel_compute": ("并行", "并发", "批量计算", "矩阵", "多线程", "多进程"),
    }

    NEGATION_PREFIXES = (
        "不需要",
        "无需",
        "不要",
        "禁止",
        "不能",
        "不使用",
        "without",
        "no ",
    )

    @classmethod
    def policy_prompt(cls, description: str) -> str:
        """Return the prompt contract used by a future learned parser."""
        return f"{ROUTING_POLICY_PROMPT}\n用户任务：{description.strip()}\n"

    @classmethod
    def _negated(cls, text: str, keyword: str) -> bool:
        start = 0
        while True:
            index = text.find(keyword, start)
            if index < 0:
                return False
            prefix = text[max(0, index - 10):index]
            normalized_prefix = prefix.rstrip()
            if any(
                normalized_prefix.endswith(marker.rstrip())
                for marker in cls.NEGATION_PREFIXES
            ):
                return True
            start = index + len(keyword)

    @classmethod
    def _positive_matches(cls, text: str, keywords: tuple[str, ...]) -> list[str]:
        return [
            keyword
            for keyword in keywords
            if keyword in text and not cls._negated(text, keyword)
        ]

    @classmethod
    def _has_positive(cls, text: str, *keywords: str) -> bool:
        return bool(cls._positive_matches(text, tuple(keywords)))

    @classmethod
    def infer(cls, description: str) -> dict[str, Any]:
        text = re.sub(r"\s+", " ", description.lower()).strip()
        capabilities: set[str] = set()
        preferred_capabilities: set[str] = set()
        tools: set[str] = set()
        platforms: set[str] = set()
        preferred_platforms: set[str] = set()
        reasons: list[str] = []
        warnings: list[str] = []
        requires_gpu = False
        requires_mobile = False
        needs_network = False
        parallelizable = False
        privacy_level = "normal"
        risk_level = "normal"
        objective_weights: dict[str, float] = {}

        def has(*keywords: str) -> bool:
            return cls._has_positive(text, *keywords)

        for capability, keywords in cls.CAPABILITY_RULES.items():
            matches = cls._positive_matches(text, keywords)
            if matches:
                capabilities.add(capability)
                reasons.append(f"检测到 {capability} 能力需求：{matches[0]}")

        if has("手机", "iphone", "ios", "移动端", "移动设备", "android", "安卓"):
            capabilities.add("mobile")
            requires_mobile = True
            reasons.append("检测到明确的移动设备需求")
        if has("iphone", "ios"):
            platforms.add("ios")
            reasons.append("任务明确限定 iOS/iPhone 平台")
        if has("android", "安卓"):
            platforms.add("android")
            reasons.append("任务明确限定 Android 平台")
        if has("macos", "mac", "xcode"):
            platforms.add("macos")
            reasons.append("检测到 macOS/Xcode 需求")
            if has("xcode"):
                tools.add("xcode")
                capabilities.add("code_build")
        if has("linux"):
            platforms.add("linux")
            reasons.append("检测到 Linux 需求")
        gpu_negated = any(
            phrase in text
            for phrase in (
                "不需要gpu",
                "不需要 gpu",
                "无需gpu",
                "无需 gpu",
                "不要gpu",
                "不要 gpu",
                "不使用gpu",
                "不使用 gpu",
                "without gpu",
                "no gpu",
            )
        )
        if not gpu_negated and (
            has("cuda")
            or has(
                "必须gpu",
                "必须 gpu",
                "需要gpu",
                "需要 gpu",
                "使用gpu",
                "使用 gpu",
                "gpu加速",
                "gpu 加速",
            )
        ):
            capabilities.add("gpu")
            requires_gpu = True
            if has("cuda"):
                tools.add("cuda")
            reasons.append("任务明确要求 GPU/CUDA")
        elif not gpu_negated and has("模型训练", "深度学习", "大模型推理", "图像推理", "矩阵", "大规模并行"):
            preferred_capabilities.add("gpu")
            reasons.append("计算密集型任务优先考虑 GPU，但不作为硬约束")

        if "parallel_compute" in capabilities:
            parallelizable = True
        if "python" in capabilities:
            tools.add("python")
        if "local_compute" in capabilities:
            tools.add("shell")
        if "database" in capabilities and has("sqlite"):
            tools.add("sqlite")
        if "database" in capabilities and has("postgresql"):
            tools.add("postgresql")
        if "web_research" in capabilities or has("联网", "访问网络", "下载", "http", "https"):
            needs_network = True
            capabilities.add("network_access")
            reasons.append("任务需要访问外部网络")

        if has("隐私", "敏感数据", "个人信息", "医疗数据", "不能上传", "数据不出本地"):
            privacy_level = "high"
            capabilities.add("data_locality")
            reasons.append("检测到高隐私或数据本地性要求")
        if has("生产环境", "关键任务", "高风险", "金融", "医疗", "安全关键"):
            risk_level = "high"
            objective_weights.update({"success": 0.45, "quality": 0.30, "latency": 0.10})
            reasons.append("高风险任务提高成功率权重")
        if has("紧急", "立即", "尽快", "低延迟", "实时"):
            objective_weights["latency"] = max(0.35, objective_weights.get("latency", 0.0))
            reasons.append("时效性要求提高延迟权重")
        if has("低成本", "省钱", "便宜", "预算有限"):
            objective_weights["cost"] = max(0.30, objective_weights.get("cost", 0.0))
            reasons.append("预算要求提高成本权重")
        if has("高质量", "准确", "精确", "最佳结果"):
            objective_weights["quality"] = max(0.40, objective_weights.get("quality", 0.0))
            reasons.append("质量要求提高质量权重")

        if has("优先mac", "优先 mac", "最好用mac", "最好用 mac"):
            preferred_platforms.add("macos")
        if has("优先linux", "优先 linux", "最好用linux", "最好用 linux"):
            preferred_platforms.add("linux")

        if gpu_negated:
            capabilities.discard("gpu")
            preferred_capabilities.discard("gpu")
            reasons.append("任务明确排除 GPU，不加入 GPU 约束或偏好")

        if not description.strip():
            warnings.append("任务描述为空，无法推断能力需求")

        return {
            "required_capabilities": capabilities,
            "preferred_capabilities": preferred_capabilities,
            "required_tools": tools,
            "allowed_platforms": platforms,
            "preferred_platforms": preferred_platforms,
            "requires_gpu": requires_gpu,
            "requires_mobile": requires_mobile,
            "needs_network": needs_network,
            "privacy_level": privacy_level,
            "risk_level": risk_level,
            "parallelizable": parallelizable,
            "objective_weights": objective_weights,
            "reasons": reasons,
            "warnings": warnings,
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
            preferred_capabilities=set(data.get("preferred_capabilities", [])) | inferred["preferred_capabilities"],
            required_tools=set(data.get("required_tools", [])) | inferred["required_tools"],
            allowed_platforms=set(data.get("allowed_platforms", [])) | inferred["allowed_platforms"],
            preferred_platforms=set(data.get("preferred_platforms", [])) | inferred["preferred_platforms"],
            preferred_unit_types=set(data.get("preferred_unit_types", [])),
            requires_gpu=bool(data.get("requires_gpu", False)) or inferred["requires_gpu"],
            requires_mobile=bool(data.get("requires_mobile", False)) or inferred["requires_mobile"],
            objective_weights={**inferred["objective_weights"], **dict(data.get("objective_weights", {}))},
            inference_reasons=inferred["reasons"],
        )


@dataclass
class ExecutionUnit:
    unit_id: str
    unit_type: str  # compatibility field; global units are Harness endpoints
    platforms: set[str] = field(default_factory=set)
    capabilities: set[str] = field(default_factory=set)
    tools: set[str] = field(default_factory=set)
    state: str = "idle"  # idle | busy | testing | degraded | offline
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
        if unit.state in {"offline", "testing"}:
            failures.append(unit.state)
        if unit.metadata.get("routing_scope") == "background":
            failures.append("background_only")
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
    parser.add_argument("--explain-task", help="print locally inferred routing requirements")
    args = parser.parse_args()

    if args.explain_task:
        inferred = TaskParser.infer(args.explain_task)
        serializable = {
            key: sorted(value) if isinstance(value, set) else value
            for key, value in inferred.items()
        }
        serializable["policy_prompt"] = TaskParser.policy_prompt(args.explain_task)
        print(json.dumps(serializable, ensure_ascii=False, indent=2))
        return

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
