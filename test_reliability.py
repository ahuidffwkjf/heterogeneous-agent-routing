import tempfile
import time
import unittest
from pathlib import Path

from phase1_mac_iphone import PhaseOneController


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.controller = PhaseOneController(
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
