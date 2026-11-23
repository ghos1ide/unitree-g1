import socket
import threading
import time

from pc_a_simulator.demo_runtime import DemoRuntime
from pc_a_simulator.callback_server import create_app as callback_app
from pc_a_simulator.scenarios import SAMPLES


def test_real_http_demo_and_cancel(tmp_path):
    runtime = DemoRuntime(tmp_path, step_seconds=0.01)
    runtime.start()
    gateway_port = int(runtime.gateway_url.rsplit(":", 1)[1])
    callback_port = int(runtime.receiver_url.rsplit(":", 1)[1])
    try:
        for kind, parameters in SAMPLES:
            received = []
            result = runtime.run_task(kind, parameters, threading.Event(),
                lambda event, data: received.append(data) if event == "event" else None)
            assert result["status"] == "SUCCEEDED"
            assert result["progress"] == parameters.get("count", 1)
            assert received[0]["status"] == "ACCEPTED"
            assert received[-1]["status"] == "SUCCEEDED"
            assert result["callbackDelivery"]["pending"] == 0
        stop = threading.Event()

        def cancel_after_first_operation(event, data):
            if event == "state" and data["progress"] >= 1:
                stop.set()

        result = runtime.run_task("NWS_CYCLE", dict(count=100),
                                  stop, cancel_after_first_operation)
        assert result["status"] == "CANCELLED"
        assert 1 <= result["progress"] < 100
    finally:
        runtime.close()
    for port in (gateway_port, callback_port):
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0


def test_occupied_port_selects_another(tmp_path):
    runtime = DemoRuntime(tmp_path)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        try:
            url = runtime._serve(callback_app(str(tmp_path / "callbacks.db")), port)
            assert int(url.rsplit(":", 1)[1]) != port
        finally:
            runtime.close()


def test_desktop_buttons_and_shutdown(tmp_path):
    import tkinter as tk
    from pc_a_simulator.simple_demo import DemoWindow

    root = tk.Tk()
    root.withdraw()
    window = DemoWindow(root, DemoRuntime(tmp_path, step_seconds=0.03))

    def pump_until(predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate():
            root.update()
            if time.monotonic() > deadline:
                raise AssertionError("Desktop operation timed out")
            time.sleep(0.01)

    try:
        pump_until(lambda: window.ready)
        window.all_button.invoke()
        pump_until(lambda: not window.busy)
        rows = [window.events.item(row)["values"] for row in window.events.get_children()]
        assert sum(row[2] == "成功" for row in rows) == len(SAMPLES)
        assert "全部状态回调已送达" in window.status.get()
        window.count.set("100")
        window.run_button.invoke()
        pump_until(lambda: window.active_id is not None)
        window.cancel_button.invoke()
        pump_until(lambda: not window.busy)
        assert window.status.get() in {"任务已取消", "已停止后续任务"}
        window.count.set("2")
        for task in ("前起落架 WOW", "起落架连续收放"):
            window.kind.set(task)
            window.show_parameters()
            window.run_button.invoke()
            pump_until(lambda: not window.busy)
            assert "全部状态回调已送达" in window.status.get()
        window.kind.set("前起落架 WOW")
        window.show_parameters()
        window.fault.set("WOW 信号未到位")
        window.run_button.invoke()
        pump_until(lambda: not window.busy)
        assert "错误码 2004" in window.status.get()
        assert window.progress["value"] == 0
        window.fault.set("正常")
        window.run_button.invoke()
        pump_until(lambda: not window.busy)
        assert "全部状态回调已送达" in window.status.get()
    finally:
        window.close()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                root.update()
                if not root.winfo_exists():
                    break
            except tk.TclError:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("Desktop did not shut down")
    assert not window.runtime.servers
