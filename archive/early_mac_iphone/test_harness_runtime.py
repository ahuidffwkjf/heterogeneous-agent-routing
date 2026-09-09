import unittest

from harness_runtime import HarnessRuntime, HarnessCannotServe
from router import ExecutionUnit, Task


class HarnessRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.harness = ExecutionUnit(
            unit_id="dsh_mac_harness_01",
            unit_type="harness",
            platforms={"macos"},
            capabilities={"local_file_access", "document_generation"},
            tools={"python"},
            metadata={
                "harness_type": "dsh",
                "internal_agents": [
                    {
                        "agent_id": "file_agent",
                        "capabilities": ["local_file_access"],
                        "tools": ["python"],
                    },
                    {
                        "agent_id": "writer_agent",
                        "capabilities": ["document_generation"],
                        "tools": ["python"],
                    },
                ],
            },
        )
        self.runtime = HarnessRuntime(self.harness)

    def test_harness_composes_private_team(self):
        plan = self.runtime.prepare(
            Task(
                task_id="report-1",
                required_capabilities={"local_file_access", "document_generation"},
                required_tools={"python"},
            )
        )
        self.assertEqual(plan.mode, "agent_team")
        self.assertEqual(len(plan.member_ids), 2)
        public = self.runtime.public_summary()
        self.assertEqual(public["internal_executor_count"], 2)
        self.assertNotIn("file_agent", public)
        self.assertNotIn("writer_agent", public)

    def test_harness_rejects_unavailable_internal_capability(self):
        with self.assertRaises(HarnessCannotServe):
            self.runtime.prepare(
                Task(task_id="gpu-1", required_capabilities={"gpu"})
            )


if __name__ == "__main__":
    unittest.main()
