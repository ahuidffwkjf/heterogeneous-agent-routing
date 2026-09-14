import tempfile
import unittest
from pathlib import Path

from dsh_agent import DSHAgent, build_execution_prompt
from local_runtime import adapter_command, controller_command


class LocalRuntimeTests(unittest.TestCase):
    def test_mock_harness_runs_without_dsh_or_ecs(self):
        with tempfile.TemporaryDirectory() as workspace:
            agent = DSHAgent(
                controller_url="http://127.0.0.1:8081",
                registry_token="test-token",
                unit_id="local-test",
                profile="headless",
                capabilities=["python"],
                tools=["python"],
                plugins=["test"],
                workspace=workspace,
                execution_mode="mock",
            )
            result = agent.execute({"description": "运行 Python 单元测试"})
            registration = agent.registration_payload()

        self.assertTrue(result["success"])
        self.assertEqual(result["execution_mode"], "mock")
        self.assertEqual(registration["metadata"]["isolation"], "local_process")
        self.assertNotIn("sandbox", registration["metadata"])

    def test_execution_prompt_keeps_harness_private(self):
        prompt = build_execution_prompt(
            {"description": "生成报告", "inferred_requirements": {}},
            unit_id="docs",
            capabilities=["document_generation"],
            plugins=["document"],
        )
        self.assertIn("自治的黑箱 Harness", prompt)
        self.assertIn("不要返回思维链", prompt)
        self.assertIn("失败时必须明确报告", prompt)

    def test_local_commands_do_not_require_cube_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            controller = controller_command(
                python="python3",
                host="127.0.0.1",
                port=8081,
                registry=Path("execution_units_local.json"),
                state_dir=state_dir,
                token="token",
            )
            adapter = adapter_command(
                {
                    "unit_id": "local-code",
                    "capabilities": ["python"],
                    "tools": ["dsh", "python"],
                    "metadata": {"plugins": ["python"]},
                },
                python="python3",
                controller_url="http://127.0.0.1:8081",
                token="token",
                engine="mock",
                workspace=state_dir,
                poll_interval=0.1,
            )

        self.assertIn("mock", controller)
        self.assertNotIn("cubesandbox", controller)
        self.assertIn("mock", adapter)
        self.assertNotIn("--cube-api-url", adapter)


if __name__ == "__main__":
    unittest.main()
