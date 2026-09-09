import json
import threading
import unittest
from urllib.request import Request, urlopen

from phase1_mac_iphone import CONTROLLER, Handler
from http.server import ThreadingHTTPServer


class PhaseOneControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_mobile_task_is_queued_and_completed(self):
        task = {
            "task_id": "iphone-test",
            "description": "mobile test",
            "required_capabilities": ["mobile"],
        }
        request = Request(
            self.base + "/tasks",
            data=json.dumps(task).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            created = json.load(response)
        self.assertEqual(created["selected_unit"], "iphone_agent_01")
        self.assertEqual(created["status"], "queued")

        with urlopen(self.base + "/mobile/tasks/next?unit_id=iphone_agent_01") as response:
            mobile_task = json.load(response)
        self.assertEqual(mobile_task["job_id"], created["job_id"])

        result_request = Request(
            self.base + f"/mobile/tasks/{created['job_id']}/result",
            data=json.dumps({"success": True, "quality": 0.8}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(result_request) as response:
            completed = json.load(response)
        self.assertEqual(completed["status"], "completed")


if __name__ == "__main__":
    unittest.main()
