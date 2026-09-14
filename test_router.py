import unittest

from router import ExecutionUnit, NoEligibleUnit, Router, Task, TaskParser


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.router = Router(
            [
                ExecutionUnit(
                    unit_id="mac",
                    unit_type="single_agent",
                    platforms={"macos"},
                    capabilities={"code_build"},
                    tools={"xcode"},
                    success_rate=0.9,
                    quality_score=0.8,
                    avg_latency_ms=1000,
                    cost_score=0.3,
                ),
                ExecutionUnit(
                    unit_id="gpu_team",
                    unit_type="agent_team",
                    platforms={"linux"},
                    capabilities={"gpu", "image_inference"},
                    tools={"cuda"},
                    success_rate=0.96,
                    quality_score=0.95,
                    avg_latency_ms=500,
                    cost_score=0.6,
                ),
            ]
        )

    def test_hard_constraints_select_gpu_team(self):
        decision = self.router.route(
            Task(
                task_id="image-1",
                required_capabilities={"image_inference"},
                requires_gpu=True,
            )
        )
        self.assertEqual(decision.selected_unit, "gpu_team")
        self.assertEqual(decision.selected_unit_type, "agent_team")

    def test_platform_constraint_rejects_incompatible_units(self):
        decision = self.router.route(
            Task(
                task_id="mac-1",
                required_capabilities={"code_build"},
                allowed_platforms={"macos"},
            )
        )
        self.assertEqual(decision.selected_unit, "mac")
        self.assertIn("gpu_team", decision.rejected_units)

    def test_no_eligible_unit_exposes_reasons(self):
        with self.assertRaises(NoEligibleUnit) as context:
            self.router.route(
                Task(task_id="impossible", required_capabilities={"quantum_compute"})
            )
        self.assertIn("mac", context.exception.rejected)
        self.assertIn("gpu_team", context.exception.rejected)

    def test_parser_does_not_turn_camera_into_ios_constraint(self):
        inferred = TaskParser.infer("调用摄像头拍照并保存")
        self.assertIn("camera", inferred["required_capabilities"])
        self.assertFalse(inferred["requires_mobile"])
        self.assertEqual(inferred["allowed_platforms"], set())

    def test_parser_respects_negated_gpu_requirement(self):
        inferred = TaskParser.infer("用 Python 做图像推理，不需要 GPU")
        self.assertFalse(inferred["requires_gpu"])
        self.assertNotIn("gpu", inferred["required_capabilities"])
        self.assertNotIn("gpu", inferred["preferred_capabilities"])

    def test_parser_extracts_risk_privacy_and_objective_preferences(self):
        inferred = TaskParser.infer("紧急处理不能上传的医疗数据，要求结果准确")
        self.assertEqual(inferred["privacy_level"], "high")
        self.assertEqual(inferred["risk_level"], "high")
        self.assertIn("data_locality", inferred["required_capabilities"])
        self.assertGreaterEqual(inferred["objective_weights"]["latency"], 0.35)
        self.assertGreaterEqual(inferred["objective_weights"]["quality"], 0.4)

    def test_policy_prompt_describes_black_box_boundary(self):
        prompt = TaskParser.policy_prompt("生成报告")
        self.assertIn("Harness 是黑箱", prompt)
        self.assertIn("否定表达", prompt)
        self.assertIn("用户任务：生成报告", prompt)


if __name__ == "__main__":
    unittest.main()
