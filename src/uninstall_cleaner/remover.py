"""删除执行：回收站删除、注册表/服务/任务备份与删除。"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, registry
from .models import ActionKind, Finding, Hive

CREATE_NO_WINDOW = 0x08000000

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def recycle_path(path: str) -> tuple[bool, str]:
    if not os.path.exists(path):
        return False, "路径已不存在"
    operation = SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = FO_DELETE
    operation.pFrom = path + "\0\0"
    operation.pTo = None
    operation.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    operation.fAnyOperationsAborted = False
    operation.hNameMappings = None
    operation.lpszProgressTitle = None
    shell32 = ctypes.windll.shell32
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int
    code = shell32.SHFileOperationW(ctypes.byref(operation))
    if code != 0:
        return False, f"Shell 错误码 0x{code:04X}"
    if operation.fAnyOperationsAborted:
        return False, "操作被取消"
    return True, ""


def open_path(path: str) -> None:
    os.startfile(path)  # noqa: S606


@dataclass
class RemoveResult:
    finding: Finding
    ok: bool
    message: str
    backup_file: str = ""


@dataclass
class RemoveSummary:
    backup_dir: str
    results: list[RemoveResult] = field(default_factory=list)

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)


def _safe_name(text: str, limit: int = 80) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text or "").strip(" .")
    return (cleaned or "item")[:limit]


def plan_backup_dir(parent: str) -> Path:
    """在指定父目录下生成不重名的时间戳备份目录路径，不创建目录。"""
    base = Path(parent)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = base / stamp
    index = 2
    while candidate.exists():
        candidate = base / f"{stamp}_{index}"
        index += 1
    return candidate


class Remover:
    def __init__(self) -> None:
        self.last_backup_dir = ""

    def validate(self, findings: list[Finding]) -> list[str]:
        problems: list[str] = []
        for finding in findings:
            if finding.protected:
                problems.append(f"{finding.location}：{finding.protected_reason or '受保护项'}")
                continue
            if finding.action == ActionKind.RECYCLE_FILE:
                guard = paths.protected_reason(finding.location)
                if guard:
                    problems.append(f"{finding.location}：{guard}")
                elif not os.path.exists(finding.location):
                    problems.append(f"{finding.location}：路径已不存在")
        return problems

    def remove(self, findings: list[Finding], backup_dir: str = "", progress=None) -> RemoveSummary:
        target = Path(backup_dir) if backup_dir else None
        if target is not None:
            target.mkdir(parents=True, exist_ok=True)
            self.last_backup_dir = str(target)
        summary = RemoveSummary(backup_dir=str(target) if target is not None else "")
        total = len(findings)
        manifest = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "backup_dir": str(target) if target is not None else "",
            "items": [],
        }
        for index, finding in enumerate(findings, 1):
            backup_file, backup_ok, backup_message = self._backup(finding, target, index)
            if not backup_ok and finding.action != ActionKind.RECYCLE_FILE:
                result = RemoveResult(finding, False, f"备份失败，已跳过：{backup_message}")
            else:
                ok, message = self._delete(finding)
                result = RemoveResult(finding, ok, message, backup_file)
            if result.ok:
                finding.status = "已删除"
            summary.results.append(result)
            manifest["items"].append(
                {
                    "category": finding.category.value,
                    "name": finding.name,
                    "location": finding.location,
                    "action": finding.action.value,
                    "reasons": finding.reasons,
                    "size": finding.size,
                    "backup": backup_file,
                    "ok": result.ok,
                    "message": result.message,
                }
            )
            if progress:
                progress(index, total, result)
        if target is not None:
            manifest_path = target / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary

    def _backup(self, finding: Finding, backup_dir: Path | None, index: int) -> tuple[str, bool, str]:
        prefix = f"{index:03d}"
        if finding.action == ActionKind.RECYCLE_FILE:
            return "", True, ""
        if backup_dir is None:
            return "", False, "未指定备份目录"
        if finding.action in (ActionKind.DELETE_REG_KEY, ActionKind.DELETE_REG_VALUE):
            if finding.hive is None or not finding.reg_path:
                return "", True, "无注册表路径，跳过备份"
            dest = backup_dir / f"{prefix}_{_safe_name(finding.name)}.reg"
            ok, message = registry.export_key(finding.hive, finding.reg_path, str(dest))
            return (str(dest) if ok else ""), ok, message
        if finding.action == ActionKind.DELETE_SERVICE:
            dest = backup_dir / f"{prefix}_service_{_safe_name(finding.service_name)}.reg"
            ok, message = registry.export_key(
                finding.hive or Hive.HKLM,
                f"{registry.SERVICES_KEY}\\{finding.service_name}",
                str(dest),
            )
            return (str(dest) if ok else ""), ok, message
        if finding.action == ActionKind.DELETE_TASK:
            dest = backup_dir / f"{prefix}_task_{_safe_name(finding.name)}.xml"
            ok, message = self._export_task(finding.task_path, dest)
            return (str(dest) if ok else ""), ok, message
        return "", True, ""

    @staticmethod
    def _export_task(task_path: str, dest: Path) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                ["schtasks", "/query", "/tn", task_path, "/xml"],
                capture_output=True,
                timeout=60,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        if proc.returncode != 0:
            return False, "schtasks 导出失败"
        dest.write_bytes(proc.stdout)
        return True, ""

    def _delete(self, finding: Finding) -> tuple[bool, str]:
        if finding.protected:
            return False, f"受保护：{finding.protected_reason}"
        if finding.action == ActionKind.RECYCLE_FILE:
            return recycle_path(finding.location)
        if finding.action == ActionKind.DELETE_REG_KEY:
            if finding.hive is None:
                return False, "缺少注册表配置单元"
            return registry.delete_key(finding.hive, finding.reg_path)
        if finding.action == ActionKind.DELETE_REG_VALUE:
            if finding.hive is None:
                return False, "缺少注册表配置单元"
            return registry.delete_value(finding.hive, finding.reg_path, finding.value_name)
        if finding.action == ActionKind.DELETE_SERVICE:
            return registry.delete_service(finding.service_name)
        if finding.action == ActionKind.DELETE_TASK:
            try:
                proc = subprocess.run(
                    ["schtasks", "/delete", "/tn", finding.task_path, "/f"],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=60,
                    creationflags=CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return False, str(exc)
            if proc.returncode != 0:
                message = (proc.stderr or proc.stdout or "").strip()
                return False, message or f"schtasks 返回码 {proc.returncode}"
            return True, ""
        return False, "未知的删除类型"
