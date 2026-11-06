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

DRIVE_FIXED = 3

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400

SHELL_ERROR_HINTS = {
    0x20: "文件被占用",
    0x21: "文件被锁定",
    0x78: "拒绝访问",
    0x79: "路径层级过深",
    0x7C: "路径无效或过长",
    0x81: "文件名过长",
}


def recycle_supported(path: str) -> bool:
    """固定磁盘且未关闭回收站时才可回收。"""
    if registry.recycle_disabled():
        return False
    drive, _ = os.path.splitdrive(os.path.abspath(path))
    if not drive:
        return False
    root = drive if drive.endswith("\\") else drive + "\\"
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_FIXED
    except OSError:
        return False


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


def _dir_entry_count(path: str) -> int:
    if not os.path.isdir(path):
        return 0
    try:
        with os.scandir(path) as entries:
            return sum(1 for _ in entries)
    except OSError:
        return 0


def recycle_path(path: str, permanent: bool = False) -> tuple[bool, str]:
    if not os.path.exists(path):
        return False, "路径已不存在"
    before = _dir_entry_count(path)
    operation = SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = FO_DELETE
    operation.pFrom = path + "\0\0"
    operation.pTo = None
    flags = FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    if not permanent:
        flags |= FOF_ALLOWUNDO
    operation.fFlags = flags
    operation.fAnyOperationsAborted = False
    operation.hNameMappings = None
    operation.lpszProgressTitle = None
    shell32 = ctypes.windll.shell32
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int
    code = shell32.SHFileOperationW(ctypes.byref(operation))
    if operation.fAnyOperationsAborted:
        if not os.path.exists(path):
            return False, "部分删除：操作被取消，原路径已不存在"
        after = _dir_entry_count(path)
        if before and after < before:
            return False, "部分删除：操作被取消，目录内容已减少"
        return False, "操作被取消"
    if code != 0:
        if not os.path.exists(path):
            return False, "部分删除：原路径已不存在，请检查回收站"
        after = _dir_entry_count(path)
        if before and after < before:
            return False, "部分删除：目录内容已减少，请检查回收站"
        hint = SHELL_ERROR_HINTS.get(code)
        suffix = f"（{hint}）" if hint else ""
        return False, f"Shell 错误码 0x{code:04X}{suffix}"
    if os.path.exists(path):
        return False, "未完全删除：路径仍存在"
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
    permanent: int = 0
    manifest_error: str = ""

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
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    candidate = base / stamp
    index = 2
    while candidate.exists():
        candidate = base / f"{stamp}_{index}"
        index += 1
    return candidate


class Remover:
    def __init__(self) -> None:
        self.last_backup_dir = ""

    @staticmethod
    def recycle_supported(path: str) -> bool:
        return recycle_supported(path)

    def validate(self, findings: list[Finding]) -> list[tuple[Finding, str]]:
        problems: list[tuple[Finding, str]] = []
        for finding in findings:
            reason = ""
            if finding.protected:
                reason = finding.protected_reason or "受保护项"
            elif finding.action == ActionKind.RECYCLE_FILE:
                reason = paths.protected_reason(finding.location)
                if not reason and not os.path.exists(finding.location):
                    reason = "路径已不存在"
            if reason:
                problems.append((finding, f"{finding.location}：{reason}"))
        return problems

    def remove(
        self,
        findings: list[Finding],
        backup_dir: str = "",
        progress=None,
        allow_permanent: bool = False,
    ) -> RemoveSummary:
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
        try:
            for index, finding in enumerate(findings, 1):
                backup_file = ""
                finding.status = ""
                try:
                    backup_file, backup_ok, backup_message = self._backup(finding, target, index)
                    if not backup_ok and finding.action != ActionKind.RECYCLE_FILE:
                        result = RemoveResult(finding, False, f"备份失败，已跳过：{backup_message}")
                    else:
                        ok, message = self._delete(finding, allow_permanent)
                        result = RemoveResult(finding, ok, message, backup_file)
                except Exception as exc:  # noqa: BLE001
                    result = RemoveResult(finding, False, f"执行异常：{exc}", backup_file)
                if result.ok:
                    finding.status = finding.status or "已删除"
                    if (
                        finding.action == ActionKind.RECYCLE_FILE
                        and not recycle_supported(finding.location)
                    ):
                        summary.permanent += 1
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
        finally:
            if target is not None:
                manifest_path = target / "manifest.json"
                temp_path = target / "manifest.json.tmp"
                try:
                    temp_path.write_text(
                        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    temp_path.replace(manifest_path)
                except OSError as exc:
                    summary.manifest_error = str(exc)
        return summary

    def _backup(self, finding: Finding, backup_dir: Path | None, index: int) -> tuple[str, bool, str]:
        prefix = f"{index:03d}"
        if finding.action == ActionKind.RECYCLE_FILE:
            return "", True, ""
        if backup_dir is None:
            return "", False, "未指定备份目录"
        if finding.action == ActionKind.DELETE_REG_KEY:
            if finding.hive is None or not finding.reg_path:
                return "", True, "无注册表路径，跳过备份"
            dest = backup_dir / f"{prefix}_{_safe_name(finding.name)}.reg"
            ok, message = registry.export_key(finding.hive, finding.reg_path, str(dest))
            return (str(dest) if ok else ""), ok, message
        if finding.action == ActionKind.DELETE_REG_VALUE:
            if finding.hive is None or not finding.reg_path:
                return "", True, "无注册表路径，跳过备份"
            dest = backup_dir / f"{prefix}_{_safe_name(finding.name)}.reg"
            ok, message = registry.export_value(
                finding.hive, finding.reg_path, finding.value_name, str(dest)
            )
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

    def _delete(self, finding: Finding, allow_permanent: bool = False) -> tuple[bool, str]:
        if finding.protected:
            return False, f"受保护：{finding.protected_reason}"
        if finding.action == ActionKind.RECYCLE_FILE:
            recyclable = recycle_supported(finding.location)
            if not recyclable and not allow_permanent:
                return False, "该位置不支持回收站，未删除"
            ok, message = recycle_path(finding.location, permanent=not recyclable)
            if not ok and message.startswith(("部分删除", "未完全删除")):
                finding.status = "部分删除"
            return ok, message
        if finding.action == ActionKind.DELETE_REG_KEY:
            if finding.hive is None:
                return False, "缺少注册表配置单元"
            return registry.delete_key(finding.hive, finding.reg_path)
        if finding.action == ActionKind.DELETE_REG_VALUE:
            if finding.hive is None:
                return False, "缺少注册表配置单元"
            return registry.delete_value(finding.hive, finding.reg_path, finding.value_name)
        if finding.action == ActionKind.DELETE_SERVICE:
            ok, message = registry.delete_service(finding.service_name)
            if ok and message == registry.SERVICE_PENDING:
                finding.status = "待重启生效"
            return ok, message
        if finding.action == ActionKind.DELETE_TASK:
            try:
                subprocess.run(
                    ["schtasks", "/end", "/tn", finding.task_path],
                    capture_output=True,
                    timeout=30,
                    creationflags=CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.SubprocessError):
                pass
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
