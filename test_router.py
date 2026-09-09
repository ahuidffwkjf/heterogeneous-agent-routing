import unittest

from router import ExecutionUnit, NoEligibleUnit, Router, Task


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


if __name__ == "__main__":
    unittest.main()
