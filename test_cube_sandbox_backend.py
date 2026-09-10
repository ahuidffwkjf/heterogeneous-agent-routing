import sys
import types
import unittest
from unittest.mock import patch

from microvm_pool import CubeSandboxBackend, MicroVMPoolManager


class FakeConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeSandbox:
    created = []
    killed = []

    def __init__(self, sandbox_id):
        self.sandbox_id = sandbox_id

    @classmethod
    def create(cls, *, template, config, env_vars=None):
        sandbox = cls(f"cube-{len(cls.created) + 1}")
        sandbox.template = template
        sandbox.config = config
        sandbox.env_vars = dict(env_vars or {})
        cls.created.append(sandbox)
        return sandbox

    def kill(self):
        self.killed.append(self.sandbox_id)


class CubeSandboxBackendTests(unittest.TestCase):
    def test_backend_creation_failure_rolls_back_node_capacity(self):
        class FailingBackend:
            def create(self, *args, **kwargs):
                raise RuntimeError("backend_create_failed")

            def destroy(self, vm):
                return None

        pool = MicroVMPoolManager(backend=FailingBackend(), min_ready=0, max_total=1)
        with self.assertRaisesRegex(RuntimeError, "backend_create_failed"):
            pool.reserve("dsh-headless", "job-failed")
        node = pool.list_nodes()[0]
        self.assertEqual(node["used_vcpus"], 0)
        self.assertEqual(node["used_memory_mb"], 0)

    def test_pool_uses_cube_sdk_and_destroys_task_vm(self):
        fake_module = types.ModuleType("cubesandbox")
        fake_module.Config = FakeConfig
        fake_module.Sandbox = FakeSandbox
        FakeSandbox.created = []
        FakeSandbox.killed = []

        backend = CubeSandboxBackend(
            api_url="http://127.0.0.1:3000",
            proxy_node_ip="127.0.0.1",
            default_template_id="tpl-test",
        )
        with patch.dict(sys.modules, {"cubesandbox": fake_module}):
            pool = MicroVMPoolManager(backend=backend, min_ready=1, max_total=2)
            vm = pool.reserve("dsh-headless", "job-1")

            self.assertEqual(vm.vm_id, "cube-1")
            self.assertEqual(FakeSandbox.created[0].template, "tpl-test")
            self.assertEqual(
                FakeSandbox.created[0].config.kwargs["proxy_node_ip"], "127.0.0.1"
            )

            pool.destroy_and_replenish(vm.vm_id)

        self.assertEqual(FakeSandbox.killed, ["cube-1"])
        snapshot = pool.snapshot()["profiles"]["dsh-headless"]
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["state"], "ready")

    def test_profile_template_map_overrides_default(self):
        fake_module = types.ModuleType("cubesandbox")
        fake_module.Config = FakeConfig
        fake_module.Sandbox = FakeSandbox
        FakeSandbox.created = []
        FakeSandbox.killed = []
        backend = CubeSandboxBackend(
            api_url="http://127.0.0.1:3000",
            template_map={"dsh-code": "tpl-code"},
            default_template_id="tpl-default",
        )
        with patch.dict(sys.modules, {"cubesandbox": fake_module}):
            pool = MicroVMPoolManager(backend=backend, min_ready=1, max_total=2)
            pool.ensure_pool("dsh-code")
        self.assertEqual(FakeSandbox.created[0].template, "tpl-code")

    def test_backend_injects_sandbox_environment_without_task_payload(self):
        fake_module = types.ModuleType("cubesandbox")
        fake_module.Config = FakeConfig
        fake_module.Sandbox = FakeSandbox
        FakeSandbox.created = []
        FakeSandbox.killed = []
        backend = CubeSandboxBackend(
            api_url="http://127.0.0.1:3000",
            default_template_id="tpl-dsh",
            sandbox_env_vars={
                "DEEPSEEK_API_KEY": "secret-test-key",
                "DSH_PERMISSION_MODE": "danger-full-access",
            },
        )
        with patch.dict(sys.modules, {"cubesandbox": fake_module}):
            pool = MicroVMPoolManager(backend=backend, min_ready=1, max_total=1)
            pool.reserve("dsh-headless", "job-credentials")

        self.assertEqual(
            FakeSandbox.created[0].env_vars,
            {
                "DEEPSEEK_API_KEY": "secret-test-key",
                "DSH_PERMISSION_MODE": "danger-full-access",
            },
        )


if __name__ == "__main__":
    unittest.main()
