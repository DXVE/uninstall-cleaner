"""管理员权限判定与 UAC 提权重启。"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_as_admin() -> tuple[bool, str]:
    script = os.path.abspath(sys.argv[0])
    workdir = os.path.dirname(script) or os.getcwd()
    if getattr(sys, "frozen", False):
        executable = script
        params = subprocess.list2cmdline(sys.argv[1:])
    else:
        executable = sys.executable
        params = subprocess.list2cmdline([script, *sys.argv[1:]])
    try:
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, workdir, 1)
    except OSError as exc:
        return False, str(exc)
    if result <= 32:
        return False, f"提权请求失败或已取消（代码 {result}）"
    return True, ""
