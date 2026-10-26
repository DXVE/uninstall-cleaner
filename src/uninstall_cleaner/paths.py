"""扫描根目录、系统白名单与受保护路径判定。"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from .models import Category

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def tool_dir() -> str:
    """本工具所在目录：打包后为 exe 所在目录，源码运行时为项目根目录。"""
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve().parent)
    return str(Path(__file__).resolve().parents[2])

SYSTEM_DIR_NAMES = {
    "microsoft",
    "microsoft shared",
    "microsoft sdks",
    "microsoft devdiv",
    "windows",
    "windowsapps",
    "windows kits",
    "windows nt",
    "windows sidebar",
    "windows powershell",
    "msbuild",
    "application verifier",
    "internet explorer",
    "common files",
    "package cache",
    "packages",
    "usoshared",
    "usoprivate",
    "temp",
    "boost_interprocess",
    "chocolatey",
    "chocolateyhttpcache",
    "intel",
    "nvidia",
    "nvidia corporation",
    "amd",
    "advanced micro devices",
    "realtek",
    "drivers",
    "modifiablewindowsapps",
    "system volume information",
    "$recycle.bin",
}

SYSTEM_REG_KEYS = {
    "microsoft",
    "windows",
    "classes",
    "policies",
    "clients",
    "registeredapplications",
    "odbc",
    "intel",
    "nvidia corporation",
    "amd",
    "realtek",
    "wow6432node",
}


def is_system_name(name: str) -> bool:
    normalized = (name or "").strip().lower()
    if normalized in SYSTEM_DIR_NAMES:
        return True
    return (
        normalized.startswith("windows ")
        or normalized.startswith("microsoft ")
        or normalized.startswith("microsoft.")
    )


def env_path(name: str) -> str:
    return os.path.expandvars(os.environ.get(name, "")).strip()


def norm(path: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(os.path.expandvars(path)))
    except (OSError, ValueError):
        return os.path.normcase(path)


def _root_values() -> list[str]:
    values = [
        env_path("ProgramFiles"),
        env_path("ProgramFiles(x86)"),
        env_path("ProgramW6432"),
        env_path("ProgramData"),
        env_path("LOCALAPPDATA"),
        env_path("APPDATA"),
        env_path("USERPROFILE"),
        env_path("PUBLIC"),
        env_path("SystemRoot"),
        env_path("TEMP"),
    ]
    return [v for v in values if v]


def critical_roots() -> list[str]:
    roots = _root_values()
    roots.append(tool_dir())
    return [norm(p) for p in roots]


def is_under(path: str, parent: str) -> bool:
    p = norm(path)
    base = norm(parent)
    return p == base or p.startswith(base + os.sep)


def protected_reason(path: str) -> str:
    raw = (path or "").strip().strip('"')
    if not raw:
        return "路径为空"
    p = norm(raw)
    drive, tail = os.path.splitdrive(p)
    if tail in ("", os.sep):
        return "磁盘根目录不可删除"

    tool = norm(tool_dir())
    if p == tool or is_under(p, tool):
        return "本工具所在目录不可删除"
    if is_under(tool, p):
        return "该目录包含本工具，禁止删除"

    for root in critical_roots():
        if p == root:
            return "系统或用户根目录不可删除"
        if is_under(root, p):
            return "该目录包含系统或用户目录，禁止删除"

    windir = norm(env_path("SystemRoot"))
    if windir and is_under(p, windir):
        return "Windows 系统目录不可删除"

    name = os.path.basename(p.rstrip("\\/")).lower()
    if name in ("$recycle.bin", "system volume information"):
        return "系统保留目录不可删除"
    if is_system_name(name):
        return "系统或厂商共用目录，默认保护"
    return ""


def scan_roots() -> list[tuple[str, Category]]:
    candidates = [
        (env_path("ProgramFiles"), Category.INSTALL_DIR),
        (env_path("ProgramFiles(x86)"), Category.INSTALL_DIR),
        (env_path("ProgramW6432"), Category.INSTALL_DIR),
        (env_path("ProgramData"), Category.INSTALL_DIR),
        (os.path.join(env_path("LOCALAPPDATA"), "Programs"), Category.INSTALL_DIR),
        (env_path("LOCALAPPDATA"), Category.USER_DATA),
        (env_path("APPDATA"), Category.USER_DATA),
        (os.path.join(env_path("USERPROFILE"), "AppData", "LocalLow"), Category.USER_DATA),
    ]
    seen: set[str] = set()
    result: list[tuple[str, Category]] = []
    for path, category in candidates:
        if not path:
            continue
        key = norm(path)
        if key in seen or not os.path.isdir(path):
            continue
        seen.add(key)
        result.append((os.path.normpath(path), category))
    return result


def drive_roots() -> list[str]:
    roots: list[str] = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        path = f"{letter}:\\"
        if os.path.isdir(path):
            roots.append(path)
    return roots


def skip_root_child(path: str) -> bool:
    name = os.path.basename(path.rstrip("\\/")).lower()
    if is_system_name(name):
        return True
    return protected_reason(path) != ""


def is_reparse(entry: os.DirEntry) -> bool:
    try:
        attrs = entry.stat(follow_symlinks=False).st_file_attributes
    except OSError:
        return True
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


def iter_child_dirs(path: str, max_depth: int = 1) -> list[tuple[str, int]]:
    """返回 (目录路径, 深度)，不跟随目录联接与符号链接。"""
    result: list[tuple[str, int]] = []
    frontier = [(path, 1)]
    while frontier:
        current, depth = frontier.pop()
        try:
            entries = list(os.scandir(current))
        except (OSError, PermissionError):
            continue
        for entry in entries:
            if is_reparse(entry):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            result.append((entry.path, depth))
            if depth < max_depth:
                frontier.append((entry.path, depth + 1))
    return result


def dir_size(path: str, max_files: int = 200_000) -> tuple[int, int, bool]:
    """统计目录体积，返回 (字节数, 文件数, 是否因上限截断)。"""
    total = 0
    count = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except (OSError, PermissionError):
            continue
        for entry in entries:
            if is_reparse(entry):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                    count += 1
                    if count >= max_files:
                        return total, count, True
            except OSError:
                continue
    return total, count, False


def path_mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def has_executable(path: str, max_depth: int = 3, max_entries: int = 5000) -> bool:
    """判断目录内是否存在 .exe，用于区分应用目录与纯数据目录。"""
    stack = [(path, 0)]
    seen = 0
    while stack:
        current, depth = stack.pop()
        try:
            entries = list(os.scandir(current))
        except (OSError, PermissionError):
            continue
        for entry in entries:
            seen += 1
            if seen > max_entries:
                return False
            if is_reparse(entry):
                continue
            try:
                if entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(".exe"):
                    return True
                if depth < max_depth and entry.is_dir(follow_symlinks=False):
                    stack.append((entry.path, depth + 1))
            except OSError:
                continue
    return False
