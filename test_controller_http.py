import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import controller as controller_module
from controller import Handler, PhaseOneController


class ControllerHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        controller_module.CONTROLLER = PhaseOneController(
            registry_path=Path(__file__).with_name("test_execution_units.json"),
            database_path=Path(self.temp_dir.name) / "registry.db",
            microvm_pool_size=0,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        controller_module.CONTROLLER.close()
        controller_module.CONTROLLER = None
        self.temp_dir.cleanup()

    def test_poll_job_result_uses_job_id_from_normal_task_path(self):
        request = Request(
            self.base + "/tasks",
            data=json.dumps(
                {
                    "task_id": "http-result-test",
                    "description": "mobile test",
                    "required_capabilities": ["mobile"],
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            created = json.load(response)

        with urlopen(
            self.base
            + "/tasks/next?unit_id=dsh_test_mobile_01"
        ) as response:
            task = json.load(response)

        result_request = Request(
            self.base + f"/tasks/{task['job_id']}/result",
            data=json.dumps(
                {
                    "success": True,
                    "quality": 1.0,
                    "executor": "dsh_test_mobile_01",
                    "lease_id": task["lease_id"],
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(result_request) as response:
            completed = json.load(response)

        self.assertEqual(task["job_id"], created["job_id"])
        self.assertEqual(completed["status"], "completed")

    def test_harness_discovery_enters_background_probe_lane(self):
        token = controller_module.CONTROLLER.registry_token
        request = Request(
            self.base + "/harness/discover",
            data=json.dumps(
                {
                    "harness_id": "dsh_test_mobile_01",
                    "agents": [
                        {
                            "agent_id": "http-discovered-agent",
                            "platforms": ["ios"],
                            "capabilities": ["image_inference"],
                            "tools": ["python"],
                        }
                    ],
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Registry-Token": token,
            },
            method="POST",
        )
        with urlopen(request) as response:
            discovery = json.load(response)

        with urlopen(
            self.base + "/background/tasks/next?unit_id=dsh_test_mobile_01"
        ) as response:
            probe = json.load(response)

        self.assertEqual(response.status, 200)
        self.assertEqual(discovery["accepted"][0]["state"], "testing")
        self.assertEqual(probe["kind"], "agent_discovery")
        self.assertEqual(probe["candidate_id"], "http-discovered-agent")


if __name__ == "__main__":
    unittest.main()
