import tempfile
import time
import unittest
from pathlib import Path

from controller import PhaseOneController
from router import NoEligibleUnit


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.controller = PhaseOneController(
            registry_path=Path(__file__).with_name("test_execution_units.json"),
            database_path=Path(self.temp_dir.name) / "registry.db",
            heartbeat_timeout=0.2,
            monitor_interval=0.05,
            max_retries=1,
        )

    def tearDown(self):
        self.controller.close()
        self.temp_dir.cleanup()

    def test_dynamic_registration_and_failover(self):
        self.controller.register_unit(
            {
                "unit_id": "iphone_agent_02",
                "unit_type": "single_agent",
                "platforms": ["ios"],
                "capabilities": ["mobile", "camera"],
                "tools": ["ios_app"],
                "state": "idle",
                "metadata": {"transport": "poll"},
                "heartbeat_required": True,
                "registration_mode": "trusted",
            }
        )
        self.assertEqual(self.controller.registry.get("iphone_agent_02").unit_id, "iphone_agent_02")

        job = self.controller.submit(
            {
                "task_id": "failover-test",
                "description": "移动端拍照任务",
                "required_capabilities": ["mobile"],
                "max_retries": 1,
            }
        )
        original_unit = job.selected_unit
        original_lease = job.lease_id
        self.controller._handle_failure(
            job.job_id,
            {"success": False, "failure_type": "agent_lost", "error": "test"},
            lease_id=original_lease,
        )
        job.next_retry_at = time.time() - 1
        self.controller._retry_job(job.job_id)

        self.assertNotEqual(job.selected_unit, original_unit)
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.attempt, 2)

    def test_new_unit_is_background_only_until_probe_succeeds(self):
        unit = self.controller.register_unit(
            {
                "unit_id": "new_dsh_harness_01",
                "unit_type": "harness",
                "platforms": ["linux"],
                "capabilities": ["new_capability"],
                "tools": ["dsh"],
                "state": "idle",
                "metadata": {"transport": "poll"},
                "heartbeat_required": True,
            }
        )
        self.assertEqual(unit.state, "testing")
        self.assertEqual(unit.metadata["routing_scope"], "background")
        heartbeat_unit = self.controller.heartbeat(
            {"unit_id": "new_dsh_harness_01", "state": "idle", "load": 0.0}
        )
        self.assertEqual(heartbeat_unit.state, "testing")
        with self.assertRaises(NoEligibleUnit):
            self.controller.submit(
                {
                    "task_id": "must-wait-for-probe",
                    "description": "new capability task",
                    "required_capabilities": ["new_capability"],
                }
            )

        probe = self.controller.next_background_probe("new_dsh_harness_01")
        self.assertIsNotNone(probe)
        result = self.controller.complete_background_probe(
            probe["probe_id"],
            {"success": True, "latency_ms": 12.0, "executor": "new_dsh_harness_01"},
            lease_id=probe["lease_id"],
            reported_unit_id="new_dsh_harness_01",
        )
        self.assertEqual(result["unit"]["state"], "idle")
        self.assertEqual(result["unit"]["metadata"]["routing_scope"], "foreground")

        job = self.controller.submit(
            {
                "task_id": "can-run-after-probe",
                "description": "new capability task",
                "required_capabilities": ["new_capability"],
            }
        )
        self.assertEqual(job.selected_unit, "new_dsh_harness_01")

    def test_harness_discovered_agent_is_background_tested_before_routing(self):
        discovery = self.controller.discover_agents(
            {
                "harness_id": "dsh_test_mobile_01",
                "agents": [
                    {
                        "agent_id": "vision-agent-01",
                        "platforms": ["ios"],
                        "capabilities": ["image_inference"],
                        "tools": ["dsh", "ios_app"],
                        "hardware": {"camera": True},
                        "probe_task": "验证新发现视觉 Agent 能完成一项图像推理任务",
                    }
                ],
            }
        )
        self.assertEqual(discovery["accepted"][0]["state"], "testing")
        self.assertIsNotNone(discovery["accepted"][0]["probe_id"])

        with self.assertRaises(NoEligibleUnit):
            self.controller.submit(
                {
                    "task_id": "must-wait-for-discovered-agent",
                    "description": "图像推理",
                    "required_capabilities": ["image_inference"],
                }
            )

        probe = self.controller.next_background_probe("dsh_test_mobile_01")
        self.assertEqual(probe["kind"], "agent_discovery")
        self.assertEqual(probe["candidate_id"], "vision-agent-01")
        result = self.controller.complete_background_probe(
            probe["probe_id"],
            {
                "success": True,
                "latency_ms": 18.0,
                "executor": "dsh_test_mobile_01",
            },
            lease_id=probe["lease_id"],
            reported_unit_id="dsh_test_mobile_01",
        )
        self.assertEqual(result["candidate"]["state"], "eligible")
        self.assertIn(
            "image_inference",
            self.controller.registry.get("dsh_test_mobile_01").capabilities,
        )

        job = self.controller.submit(
            {
                "task_id": "can-run-after-discovery",
                "description": "图像推理",
                "required_capabilities": ["image_inference"],
            }
        )
        self.assertEqual(job.selected_unit, "dsh_test_mobile_01")

    def test_job_is_persisted_and_recovered_after_controller_restart(self):
        job = self.controller.submit(
            {
                "task_id": "restart-test",
                "description": "移动端测试",
                "required_capabilities": ["mobile"],
                "max_retries": 1,
            }
        )
        database_path = Path(self.temp_dir.name) / "registry.db"
        self.controller.close()
        self.controller = PhaseOneController(
            registry_path=Path(__file__).with_name("test_execution_units.json"),
            database_path=database_path,
            heartbeat_timeout=0.2,
            monitor_interval=0.05,
            max_retries=1,
        )
        recovered = self.controller.get_job(job.job_id)
        self.assertEqual(recovered.status, "retry_wait")
        self.assertEqual(recovered.failure_history[-1]["failure_type"], "controller_restart")

    def test_task_cannot_force_a_harness(self):
        with self.assertRaises(ValueError):
            self.controller.submit(
                {
                    "task_id": "forbidden-routing",
                    "description": "生成报告",
                    "harness_id": "iphone_agent_01",
                }
            )


if __name__ == "__main__":
    unittest.main()
