"""Chinese desktop controls for a self-contained mock HTTP demonstration."""
import argparse
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .demo_runtime import DemoRuntime
from .scenarios import FAULTS, NAMES, SAMPLES, STATES


class DemoWindow:
    def __init__(self, root, runtime=None):
        self.root = root
        self.runtime = runtime or DemoRuntime()
        self.messages = queue.Queue()
        self.stop = threading.Event()
        self.ready = False
        self.busy = False
        self.closing = False
        self.active_id = None
        self.worker = None
        self.total = 1
        root.title("人形机器人 · ELN V1.5 通信仿真")
        root.geometry("1080x800")
        root.minsize(960, 720)
        root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 7))
        style.configure("Treeview", font=("Microsoft YaHei UI", 10), rowheight=29)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10))
        body = ttk.Frame(root, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="ELN V1.5 通信仿真", font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        self.mode = tk.StringVar(value=self.runtime.mode_label)
        ttk.Label(body, textvariable=self.mode, foreground="#52616b").pack(anchor="w", pady=(4, 14))

        toolbar = ttk.Frame(body)
        toolbar.pack(fill="x")
        self.all_button = ttk.Button(toolbar, text="运行全部示例", command=self.run_all)
        self.all_button.pack(side="left")
        self.init_button = ttk.Button(toolbar, text="机器人初始化", command=lambda: self.begin([SAMPLES[0]]))
        self.init_button.pack(side="left", padx=8)
        self.cancel_button = ttk.Button(toolbar, text="取消当前任务", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="right")

        ttk.Separator(body).pack(fill="x", pady=16)
        form = ttk.Frame(body)
        form.pack(fill="x")
        ttk.Label(form, text="任务类型").grid(row=0, column=0, sticky="w")
        self.kind = tk.StringVar(value="前轮转弯循环")
        self.kind_box = ttk.Combobox(form, textvariable=self.kind, values=list(NAMES.values())[1:],
                                     state="readonly", width=18)
        self.kind_box.grid(row=1, column=0, sticky="w", pady=6)
        self.kind_box.bind("<<ComboboxSelected>>", lambda event: self.show_parameters())
        self.parameter_frame = ttk.Frame(form)
        self.parameter_frame.grid(row=2, column=0, columnspan=3, sticky="w")
        self.option = tk.StringVar(value="空中 AIR")
        self.pre_action = tk.StringVar(value="无预动作")
        self.cycle_mode = tk.StringVar(value="完整收放循环")
        self.count = tk.StringVar(value="3")
        self.run_button = ttk.Button(form, text="执行所选任务", command=self.run_selected)
        self.run_button.grid(row=1, column=2, sticky="e")
        form.columnconfigure(2, weight=1)
        self.show_parameters()

        fault_form = ttk.Frame(body)
        fault_form.pack(fill="x", pady=(10, 0))
        ttk.Label(fault_form, text="仿真场景").pack(side="left")
        self.fault = tk.StringVar(value="正常")
        self.fault_box = ttk.Combobox(fault_form, textvariable=self.fault, values=list(FAULTS), width=20, state="readonly")
        self.fault_box.pack(side="left", padx=10)
        ttk.Label(fault_form, text="第几次匹配阶段触发").pack(side="left")
        self.occurrence = tk.StringVar(value="1")
        self.occurrence_box = ttk.Spinbox(fault_form, from_=1, to=1000, textvariable=self.occurrence, width=6)
        self.occurrence_box.pack(side="left", padx=10)

        self.status = tk.StringVar(value="正在启动模拟服务…")
        ttk.Label(body, textvariable=self.status, font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", pady=(16, 5))
        self.progress = ttk.Progressbar(body, maximum=1)
        self.progress.pack(fill="x")
        self.task_text = tk.StringVar(value="尚无任务")
        ttk.Label(body, textvariable=self.task_text, foreground="#52616b").pack(anchor="w", pady=(5, 14))
        self.phase = tk.StringVar(value="等待任务")
        ttk.Label(body, textvariable=self.phase, wraplength=900).pack(anchor="w", pady=(0, 10))
        ttk.Label(body, text="ELN 已收到的状态回调").pack(anchor="w", pady=(0, 6))
        table = ttk.Frame(body)
        table.pack(fill="both", expand=True)
        self.events = ttk.Treeview(table, columns=("time", "task", "status", "progress", "message", "id"), show="headings")
        for name, title, width in [("time", "时间", 105), ("task", "任务", 150),
                                    ("status", "状态", 80), ("progress", "完成次数", 85),
                                    ("message", "动作阶段 / 确认结果", 340), ("id", "任务号", 185)]:
            self.events.heading(name, text=title)
            self.events.column(name, width=width, minwidth=75, stretch=True)
        self.events.tag_configure("SUCCEEDED", foreground="#157347")
        self.events.tag_configure("FAILED", foreground="#b42318")
        self.events.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table, command=self.events.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.events.configure(yscrollcommand=scrollbar.set)
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.events.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.events.configure(xscrollcommand=horizontal.set)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.addresses = tk.StringVar(value="服务启动中")
        ttk.Label(body, textvariable=self.addresses, foreground="#52616b").pack(anchor="w", pady=(12, 0))
        self.set_controls(False)
        self.startup = threading.Thread(target=self.start_runtime, daemon=True)
        self.startup.start()
        root.after(50, self.drain)

    def show_parameters(self):
        for child in self.parameter_frame.winfo_children():
            child.destroy()
        self.parameter_boxes = []
        self.count_box = None
        if self.kind.get() != "前起落架 WOW":
            ttk.Label(self.parameter_frame, text="完整操作次数").grid(row=0, column=0, sticky="w")
            self.count_box = ttk.Spinbox(self.parameter_frame, from_=1, to=1000, textvariable=self.count, width=7)
            self.count_box.grid(row=1, column=0, pady=6, sticky="w")
        choices = []
        if self.kind.get() == "前起落架 WOW":
            choices = [("目标 WOW 信号", self.option, ["空中 AIR", "地面 GROUND"])]
        elif self.kind.get() == "起落架连续收放":
            choices = [("预动作", self.pre_action, ["无预动作", "先放下并确认下位锁"]),
                       ("循环模式", self.cycle_mode, ["完整收放循环", "最后一次仅收起"])]
        for column, (label, variable, values) in enumerate(choices, start=1):
            ttk.Label(self.parameter_frame, text=label).grid(row=0, column=column, sticky="w", padx=(20, 0))
            box = ttk.Combobox(self.parameter_frame, textvariable=variable, values=values, state="readonly", width=23)
            box.grid(row=1, column=column, padx=(20, 0), pady=6)
            self.parameter_boxes.append(box)

    def set_controls(self, enabled):
        for widget in (self.all_button, self.init_button, self.run_button):
            widget.configure(state="normal" if enabled else "disabled")
        for widget in (self.kind_box, self.fault_box, *self.parameter_boxes):
            widget.configure(state="readonly" if enabled else "disabled")
        if self.count_box:
            self.count_box.configure(state="normal" if enabled else "disabled")
        self.occurrence_box.configure(state="normal" if enabled else "disabled")
        self.cancel_button.configure(state="normal" if self.busy and not self.stop.is_set() else "disabled")

    def start_runtime(self):
        try:
            self.runtime.start()
            self.messages.put(("ready", None))
        except Exception as exc:
            self.messages.put(("error", str(exc)))

    def run_all(self):
        self.fault.set("正常")
        self.begin(SAMPLES)

    def run_selected(self):
        name = self.kind.get()
        kind = next(key for key, value in NAMES.items() if value == name)
        if kind in {"NWS_CYCLE", "GEAR_OPERATION"}:
            try:
                count = int(self.count.get())
                if not 1 <= count <= 1000:
                    raise ValueError
            except ValueError:
                messagebox.showerror("次数不正确", "请输入 1 到 1000 之间的整数。", parent=self.root)
                return
            parameters = dict(count=count)
            if kind == "GEAR_OPERATION":
                parameters.update(preAction="NONE" if self.pre_action.get() == "无预动作" else "EXTEND_DOWNLOCK",
                                  cycleMode="FULL_CYCLE" if self.cycle_mode.get() == "完整收放循环" else "FINAL_RETRACT_ONLY")
        else:
            parameters = dict(wowSignal="AIR" if self.option.get() == "空中 AIR" else "GROUND")
        self.begin([(kind, parameters)])

    def begin(self, tasks):
        if not self.ready or self.busy or self.closing:
            return
        try:
            occurrence = int(self.occurrence.get())
            if not 1 <= occurrence <= 1000:
                raise ValueError
        except ValueError:
            messagebox.showerror("触发次数不正确", "请输入 1 到 1000 之间的整数。", parent=self.root)
            return
        self.busy = True
        self.stop.clear()
        self.set_controls(False)
        self.status.set("正在准备任务…")
        self.worker = threading.Thread(target=self.run_tasks,
            args=(list(tasks), FAULTS[self.fault.get()], occurrence), daemon=True)
        self.worker.start()

    def run_tasks(self, tasks, fault="NONE", occurrence=1):
        try:
            auto_init = tasks[0][0] != "ROBOT_INITIALIZE" and not self.runtime.health()["initialized"]
            if auto_init:
                tasks.insert(0, SAMPLES[0])
            for index, (kind, parameters) in enumerate(tasks):
                if self.stop.is_set():
                    break
                self.runtime.configure_simulation("NONE" if auto_init and index == 0 else fault, occurrence)
                state = self.runtime.run_task(kind, parameters, self.stop,
                    lambda event, data: self.messages.put((event, data)))
                if state["status"] != "SUCCEEDED":
                    self.messages.put(("done", "任务已取消" if state["status"] == "CANCELLED"
                                       else f"任务失败 · 错误码 {state['errorCode']} · 已完成 {state['progress']} 次"))
                    return
            self.messages.put(("done", "已停止后续任务" if self.stop.is_set() else "完成，全部状态回调已送达"))
        except Exception as exc:
            self.messages.put(("error", str(exc)))

    def cancel(self):
        self.stop.set()
        self.cancel_button.configure(state="disabled")
        self.status.set("正在请求受控停止…")

    def drain(self):
        while True:
            try:
                event, data = self.messages.get_nowait()
            except queue.Empty:
                break
            if event == "closed":
                self.root.destroy()
                return
            if self.closing:
                continue
            if event == "ready":
                self.ready = True
                self.mode.set(self.runtime.mode_label)
                self.status.set("就绪")
                self.addresses.set(f"机器人网关 {self.runtime.gateway_url}    ELN {self.runtime.receiver_url}")
                self.set_controls(True)
            elif event == "active":
                self.active_id, kind, self.total = data
                self.progress.configure(maximum=self.total, value=0)
                self.task_text.set(f"{NAMES[kind]}    {self.active_id}")
            elif event == "state":
                self.progress["value"] = data["progress"]
                self.status.set(f"{NAMES[data['taskType']]} · {STATES[data['status']]} · {data['progress']}/{self.total} 次")
                self.phase.set(data["message"])
            elif event == "event":
                # Bound the visible log; complete history remains in the session database.
                if len(self.events.get_children()) >= 1000:
                    self.events.delete(self.events.get_children()[0])
                row = self.events.insert("", "end", values=(data["timestamp"][11:19] + " UTC",
                    NAMES[data["taskType"]], STATES[data["status"]], data["progress"], data["message"], data["taskId"]),
                    tags=(data["status"],))
                self.events.see(row)
            elif event in {"done", "error"}:
                self.busy = False
                self.active_id = None
                self.set_controls(self.ready)
                self.status.set(data if event == "done" else "操作失败")
                if event == "error":
                    messagebox.showerror("模拟程序", data, parent=self.root)
        self.root.after(50, self.drain)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.stop.set()
        self.set_controls(False)
        self.status.set("正在停止任务并关闭模拟服务…")

        def shutdown():
            self.startup.join()
            if self.worker:
                self.worker.join()
            self.runtime.close()
            self.messages.put(("closed", None))

        threading.Thread(target=shutdown, daemon=True).start()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--robot", action="store_true")
    args = parser.parse_args()
    runtime = None
    if args.robot:
        from .remote_demo import RemoteDemoRuntime
        runtime = RemoteDemoRuntime()
    root = tk.Tk()
    DemoWindow(root, runtime)
    root.mainloop()


if __name__ == "__main__":
    main()
