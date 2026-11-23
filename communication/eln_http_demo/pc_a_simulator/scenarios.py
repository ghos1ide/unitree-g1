NAMES = {"ROBOT_INITIALIZE": "机器人初始化", "NWS_CYCLE": "前轮转弯循环",
         "NLG_WOW_OPERATION": "前起落架 WOW", "GEAR_OPERATION": "起落架连续收放"}
STATES = {"ACCEPTED": "已接收", "RUNNING": "执行中", "SUCCEEDED": "成功",
          "FAILED": "失败", "CANCELLED": "已取消"}
FAULTS = {"正常": "NONE", "安全联锁触发": "SAFETY_INTERLOCK", "WOW 信号未到位": "WOW_TIMEOUT",
          "上位锁未确认": "UPLOCK_TIMEOUT", "下位锁未确认": "DOWNLOCK_TIMEOUT", "执行机构故障": "ACTUATOR_FAULT"}
SAMPLES = [
    ("ROBOT_INITIALIZE", {}),
    ("NWS_CYCLE", {"count": 3}),
    ("NLG_WOW_OPERATION", {"wowSignal": "AIR"}),
    ("NLG_WOW_OPERATION", {"wowSignal": "GROUND"}),
    ("GEAR_OPERATION", {"count": 2, "preAction": "NONE", "cycleMode": "FULL_CYCLE"}),
    ("GEAR_OPERATION", {"count": 2, "preAction": "NONE", "cycleMode": "FINAL_RETRACT_ONLY"}),
    ("GEAR_OPERATION", {"count": 2, "preAction": "EXTEND_DOWNLOCK", "cycleMode": "FULL_CYCLE"}),
]
