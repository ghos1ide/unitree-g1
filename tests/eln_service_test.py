import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import socket
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


spec = importlib.util.spec_from_file_location("eln_service", Path(__file__).resolve().parents[1] / "scripts/eln_service.py")
eln = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eln)


class ElnTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.fail_callbacks = 0
        owner = self

        class Callback(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.events.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                data = b'{"code":1}' if owner.fail_callbacks else b'{"code":0}'
                owner.fail_callbacks = max(0, owner.fail_callbacks - 1)
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.receiver = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
        self.receiver_thread = threading.Thread(target=self.receiver.serve_forever)
        self.receiver_thread.start()
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name) / "tasks.sqlite3")
        self.callback = f"http://127.0.0.1:{self.receiver.server_port}/callback"
        self.service = eln.Service(self.database, eln.SimulationExecutor(0.02), self.callback)

    def tearDown(self):
        self.service.close()
        self.receiver.shutdown()
        self.receiver.server_close()
        self.receiver_thread.join()
        self.temp.cleanup()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Timed out")

    def wait_done(self, task_id):
        self.wait_for(lambda: self.service.query(task_id)["status"] in eln.FINAL and self.service.active is None)
        return self.service.query(task_id)

    def initialize(self):
        self.service.submit({"taskId": "init"}, initialize=True)
        self.assertEqual(self.wait_done("init")["status"], "SUCCEEDED")

    def task(self, task_id="pedal", **params):
        return dict(taskId=task_id, taskType="PEDAL_ROTATE", callbackUrl=self.callback,
                    parameters=params or dict(direction="LEFT", count=3))

    def assert_code(self, code, call):
        with self.assertRaises(eln.ApiError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_initialization_gate_validation_and_duplicate(self):
        self.assert_code(2002, lambda: self.service.submit(self.task()))
        self.initialize()
        for count in (True, 0, -1, 1.5, 10001):
            self.assert_code(1001, lambda: self.service.submit(self.task(count=count, direction="LEFT")))
        self.service.submit(self.task())
        state = self.wait_done("pedal")
        self.assertEqual((state["status"], state["progress"]), ("SUCCEEDED", 3))
        self.assert_code(1003, lambda: self.service.submit(self.task()))
        self.wait_for(lambda: any(e["taskId"] == "pedal" and e["status"] == "SUCCEEDED" for e in self.events))
        events = [e for e in self.events if e["taskId"] == "pedal"]
        self.assertEqual([e["status"] for e in events], ["ACCEPTED", "RUNNING", "RUNNING", "RUNNING", "SUCCEEDED"])
        self.assertEqual([e["progress"] for e in events], [0, 0, 1, 2, 3])
        self.assertTrue(all(e["errorCode"] is None and e["timestamp"].endswith("+00:00") for e in events))

    def test_busy_cancel_and_terminal_immutability(self):
        self.initialize()
        self.service.submit(self.task(count=100, direction="RIGHT"))
        self.assert_code(2001, lambda: self.service.submit(self.task("other")))
        self.wait_for(lambda: self.service.query("pedal")["progress"] >= 1)
        self.service.cancel_task("pedal", {"reason": "test"})
        state = self.wait_done("pedal")
        self.assertEqual(state["status"], "CANCELLED")
        self.assertLess(state["progress"], 100)
        self.assertEqual(self.service.cancel_task("pedal", {}), state)
        self.assert_code(2004, lambda: self.service.query("missing"))

    def test_single_actions(self):
        self.initialize()
        for kind, params in [("MLG_WOW", {"wowState": "ON"}), ("MLG_WOW", {"wowState": "OFF"}),
                             ("GEAR_OPERATION", {"action": "RETRACT"}), ("GEAR_OPERATION", {"action": "EXTEND"})]:
            task_id = kind + next(iter(params.values()))
            self.service.submit(dict(taskId=task_id, taskType=kind, parameters=params))
            self.assertEqual(self.wait_done(task_id)["progress"], 1)

    def test_failure_resets_readiness(self):
        self.initialize()
        def fail(*args):
            raise eln.ApiError(2002, "interlock open")
        self.service.executor.step = fail
        self.service.submit(self.task())
        state = self.wait_done("pedal")
        self.assertEqual((state["status"], state["errorCode"], state["progress"]), ("FAILED", "2002", 0))
        self.assert_code(2002, lambda: self.service.submit(self.task("next")))

    def test_restart_preserves_ids_and_fails_interrupted_task(self):
        self.initialize()
        self.service.close()
        request = self.task("interrupted")
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO tasks VALUES (?,?,?)", ("interrupted", json.dumps(request), json.dumps(dict(status="RUNNING", progress=2))))
        self.service = eln.Service(self.database, eln.SimulationExecutor(), self.callback)
        state = self.service.query("interrupted")
        self.assertEqual((state["status"], state["progress"], state["errorCode"]), ("FAILED", 2, "9000"))
        self.assert_code(1003, lambda: self.service.submit(request))
        self.assert_code(2002, lambda: self.service.submit(self.task("next")))

    def test_retry_keeps_identical_event_and_order(self):
        self.fail_callbacks = 1
        self.initialize()
        self.wait_for(lambda: any(e["status"] == "SUCCEEDED" for e in self.events))
        self.assertEqual(self.events[0], self.events[1])
        self.assertEqual([e["status"] for e in self.events], ["ACCEPTED", "ACCEPTED", "RUNNING", "SUCCEEDED"])

    def test_disabled_and_callback_allowlist(self):
        self.service.executor = eln.DisabledExecutor()
        self.assert_code(2002, lambda: self.service.submit({"taskId": "init"}, True))
        self.assert_code(1001, lambda: self.service.submit(dict(self.task(), callbackUrl="http://unconfigured/")))

    def test_database_has_single_owner(self):
        with self.assertRaisesRegex(RuntimeError, "owns this database"):
            eln.Service(self.database, eln.SimulationExecutor(), self.callback)

    def test_concurrent_duplicate_executes_once(self):
        self.initialize()
        barrier = threading.Barrier(5)
        results = []
        def submit():
            barrier.wait()
            try:
                results.append(self.service.submit(self.task())["code"])
            except eln.ApiError as error:
                results.append(error.code)
        threads = [threading.Thread(target=submit) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [0, 1003, 1003, 1003, 1003])
        self.assertEqual(self.wait_done("pedal")["progress"], 3)

    def test_cancel_initialization_does_not_enable_actions(self):
        self.service.executor.delay = 1
        self.service.submit({"taskId": "init"}, True)
        self.service.cancel_task("init", {})
        self.assertEqual(self.wait_done("init")["status"], "CANCELLED")
        self.assert_code(2002, lambda: self.service.submit(self.task()))

    def test_retry_exhaustion_is_bounded(self):
        self.fail_callbacks = 100
        self.initialize()
        def exhausted():
            with self.service.lock, self.service.db:
                self.service.db.execute("UPDATE callbacks SET due=0")
                row = self.service.db.execute("SELECT attempts,delivered FROM callbacks ORDER BY id LIMIT 1").fetchone()
                return row == (4, 0)
        self.wait_for(exhausted)
        accepted = [event for event in self.events if event["status"] == "ACCEPTED"]
        self.assertEqual(len(accepted), 4)
        self.assertTrue(all(event == accepted[0] for event in accepted))

    def test_http_auth_routes_and_errors(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = eln.uvicorn.Server(eln.uvicorn.Config(eln.create_app(self.service, "test-token"), log_level="error"))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]))
        thread.start()
        self.wait_for(lambda: server.started)
        def post(path, body, authorized=True):
            headers = {"Content-Type": "application/json"}
            if authorized:
                headers["Authorization"] = "Bearer test-token"
            request = Request(f"http://127.0.0.1:{port}" + path,
                              json.dumps(body).encode(), headers, method="POST")
            try:
                response = urlopen(request, timeout=2)
            except HTTPError as error:
                response = error
            with response:
                return response.status, json.load(response)
        try:
            self.assertEqual(post("/api/v1/robot/initialize", {}, False)[1]["code"], 1002)
            status, body = post("/api/v1/robot/initialize", {"taskId": "init"})
            self.assertEqual((status, body["taskId"], body["status"]), (200, "init", "ACCEPTED"))
            self.wait_done("init")
            self.assertEqual(post("/api/v1/tasks/init/query", {})[1]["progress"], 1)
            self.assertEqual(post("/api/v1/tasks/init/cancel", {})[1]["status"], "SUCCEEDED")
            self.assertEqual(post("/api/v1/tasks", self.task())[1]["code"], 0)
            self.wait_done("pedal")
            self.assertEqual(post("/api/v1/tasks", self.task())[1]["code"], 1003)
            self.assertEqual(post("/api/v1/tasks", [])[1]["code"], 1001)
            self.assertEqual(post("/api/v1/tasks", dict(self.task(), taskType="UNKNOWN"))[1]["code"], 1001)
            self.assertEqual(post("/api/v1/tasks", dict(self.task(), taskId=123))[1]["code"], 1001)
            with urlopen(f"http://127.0.0.1:{port}/openapi.json", timeout=2) as response:
                schema = json.load(response)
                self.assertEqual(len(schema["paths"]), 4)
                self.assertIn("HTTPBearer", schema["components"]["securitySchemes"])
        finally:
            server.should_exit = True
            thread.join()
            listener.close()


if __name__ == "__main__":
    unittest.main()
