#!/usr/bin/env python3
"""
盤中主流程和盤後掃描把吞掉的例外記下來,給 health.json。

單一股票或加分功能失敗只計數,不讓整輪中斷。超過門檻時:
盤中只讓健康燈變紅(迴圈還要繼續);盤後掃描由呼叫端決定要不要讓 job 失敗。
"""

import sys

FLAG_WARN = 8
FLAG_ERROR = 3
_box = {"warn": 0, "error": 0, "samples": []}


def reset():
    _box["warn"] = 0
    _box["error"] = 0
    _box["samples"] = []


def _note(kind, msg):
    _box[kind] += 1
    if len(_box["samples"]) < 8:
        _box["samples"].append(str(msg)[:180])


def warn(msg):
    _note("warn", msg)
    print(f"[警告] {msg}", file=sys.stderr)


def error(msg):
    _note("error", msg)
    print(f"[錯誤] {msg}", file=sys.stderr)


def snapshot():
    return {"warn": _box["warn"], "error": _box["error"], "samples": list(_box["samples"])}


def over_threshold(snap=None):
    s = snap if snap is not None else snapshot()
    return int(s.get("warn") or 0) >= FLAG_WARN or int(s.get("error") or 0) >= FLAG_ERROR
