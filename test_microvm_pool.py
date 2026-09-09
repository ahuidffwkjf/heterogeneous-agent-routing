import tempfile
import unittest
from pathlib import Path

from microvm_pool import MicroVMNode, MicroVMPoolManager
from phase1_mac_iphone import PhaseOneController


class MicroVMPoolTests(unittest.TestCase):
    def test_pool_replenishes_after_reservation_and_discard(self):
        pool = MicroVMPoolManager(min_ready=3, max_total=5)
        initial = pool.ensure_pool("dsh-linux")
        self.assertEqual(len(initial), 3)

        vm = pool.reserve("dsh-linux", "job-1")
        snapshot = pool.snapshot()["profiles"]["dsh-linux"]
        self.assertEqual(sum(item["state"] == "ready" for item in snapshot), 3)
        self.assertEqual(sum(item["state"] == "busy" for item in snapshot), 1)

        pool.discard(vm.vm_id)
        snapshot = pool.snapshot()["profiles"]["dsh-linux"]
        self.assertEqual(len(snapshot), 3)
        self.assertTrue(all(item["state"] == "ready" for item in snapshot))

        vm = pool.reserve("dsh-linux", "job-2")
        pool.destroy_and_replenish(vm.vm_id)
        snapshot = pool.snapshot()["profiles"]["dsh-linux"]
        self.assertEqual(len(snapshot), 3)
        self.assertTrue(all(item["state"] == "ready" for item in snapshot))

    def test_controller_assigns_and_releases_microvm(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = PhaseOneController(
                database_path=Path(directory) / "registry.db",
                microvm_pool_size=3,
            )
            self.addCleanup(controller.close)
            controller.register_unit(
                {
                    "unit_id": "linux_microvm_01",
                    "unit_type": "single_agent",
                    "platforms": ["linux"],
                    "capabilities": ["microvm_test"],
                    "tools": [],
                    "state": "idle",
                    "metadata": {
                        "transport": "poll",
                        "sandbox": {"type": "microvm", "profile": "dsh-linux"},
                    },
                    "heartbeat_required": True,
                }
            )
            job = controller.submit(
                {
                    "task_id": "microvm-task",
                    "description": "MicroVM test",
                    "required_capabilities": ["microvm_test"],
                }
            )
            self.assertEqual(job.selected_unit, "linux_microvm_01")
            self.assertIsNotNone(job.microvm_id)
            self.assertEqual(job.status, "queued")

            controller.complete(
                job.job_id,
                {
                    "success": True,
                    "quality": 1.0,
                    "executor": "linux_microvm_01",
                },
                lease_id=job.lease_id,
                reported_unit_id="linux_microvm_01",
            )
            self.assertEqual(job.status, "completed")
            self.assertIsNone(job.microvm_id)
            vms = controller.microvm_pool.snapshot()["profiles"]["dsh-linux"]
            self.assertTrue(all(item["state"] == "ready" for item in vms))

    def test_snapshot_resumes_on_another_node(self):
        pool = MicroVMPoolManager(
            min_ready=1,
            max_total=4,
            nodes=[
                MicroVMNode("node-a", total_vcpus=2, total_memory_mb=2048),
                MicroVMNode("node-b", total_vcpus=8, total_memory_mb=8192),
            ],
        )
        vm = pool.reserve("dsh-linux", "job-cross-node")
        source_node = vm.node_id
        snapshot = pool.pause(vm.vm_id)
        restored = pool.resume(vm.vm_id, target_node_id="node-b")

        self.assertEqual(snapshot.source_node_id, source_node)
        self.assertEqual(restored.node_id, "node-b")
        self.assertEqual(restored.snapshot_id, snapshot.snapshot_id)

    def test_isolated_node_is_not_used_for_replenishment(self):
        pool = MicroVMPoolManager(
            min_ready=2,
            max_total=4,
            nodes=[
                MicroVMNode("node-a", total_vcpus=4, total_memory_mb=4096),
                MicroVMNode("node-b", total_vcpus=4, total_memory_mb=4096),
            ],
        )
        pool.isolate_node("node-a")
        vms = pool.ensure_pool("dsh-linux")

        self.assertEqual(len(vms), 2)
        self.assertTrue(all(vm.node_id == "node-b" for vm in vms))


if __name__ == "__main__":
    unittest.main()
