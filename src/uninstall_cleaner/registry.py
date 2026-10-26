"""注册表读取与环境变量/命令行路径解析。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import winreg
from dataclasses import dataclass

from .models import Hive, InstalledApp

CREATE_NO_WINDOW = 0x08000000

HIVE_MAP = {
    Hive.HKLM: winreg.HKEY_LOCAL_MACHINE,
    Hive.HKCU: winreg.HKEY_CURRENT_USER,
    Hive.HKCR: winreg.HKEY_CLASSES_ROOT,
}

UNINSTALL_KEYS = [
    (Hive.HKLM, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (Hive.HKLM, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (Hive.HKCU, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]

RUN_KEYS = [
    (Hive.HKLM, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
    (Hive.HKLM, r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"),
    (Hive.HKLM, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
    (Hive.HKLM, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\RunOnce"),
    (Hive.HKCU, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
    (Hive.HKCU, r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"),
]

SERVICES_KEY = r"SYSTEM\CurrentControlSet\Services"
SOFTWARE_ROOTS = [
    (Hive.HKLM, r"SOFTWARE"),
    (Hive.HKCU, r"SOFTWARE"),
    (Hive.HKLM, r"SOFTWARE\WOW6432Node"),
]

EXE_EXTENSIONS = (".exe", ".com", ".bat", ".cmd", ".scr")


def read_values(hive: Hive, path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with winreg.OpenKey(HIVE_MAP[hive], path, 0, winreg.KEY_READ) as key:
            index = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                values[name] = value if isinstance(value, str) else str(value)
                index += 1
    except OSError:
        return {}
    return values


def list_subkeys(hive: Hive, path: str) -> list[str]:
    names: list[str] = []
    try:
        with winreg.OpenKey(HIVE_MAP[hive], path, 0, winreg.KEY_READ) as key:
            index = 0
            while True:
                try:
                    names.append(winreg.EnumKey(key, index))
                except OSError:
                    break
                index += 1
    except OSError:
        return []
    return names


def key_exists(hive: Hive, path: str) -> bool:
    try:
        with winreg.OpenKey(HIVE_MAP[hive], path, 0, winreg.KEY_READ):
            return True
    except OSError:
        return False


def full_key(hive: Hive, path: str) -> str:
    return f"{hive.value}\\{path}" if path else hive.value


def split_command(command: str) -> tuple[str, str]:
    """拆分命令为 (可执行文件路径, 参数)，支持带引号与无引号路径。"""
    cmd = os.path.expandvars((command or "").strip())
    if not cmd:
        return "", ""
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        if end > 0:
            return cmd[1:end].strip(), cmd[end + 1 :].strip()
        return cmd[1:].strip(), ""
    match = re.match(r"(?i)^(.+?\.(?:exe|com|bat|cmd|scr))(?=\s|$)", cmd)
    if match:
        return match.group(1).strip(), cmd[match.end() :].strip()
    first, _, rest = cmd.partition(" ")
    return first.strip(), rest.strip()


def is_msi(command: str) -> bool:
    return "msiexec" in (command or "").lower()


def resolve_command(command: str) -> tuple[str, bool | None]:
    """返回 (候选路径, 是否存在)；None 表示无法校验。"""
    target, _ = split_command(command)
    target = re.sub(r",\s*-?\d+$", "", target).strip().strip('"')
    if not target or is_msi(target):
        return target, None
    if "://" in target:
        return target, None
    if target.startswith("\\\\?\\"):
        target = target[4:]
    if target.startswith("\\??\\"):
        target = target[4:]
    if target.lower().startswith("\\systemroot\\"):
        target = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), target[12:])
    elif target.lower().startswith("system32\\"):
        target = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), target)
    if not os.path.isabs(target):
        if os.sep not in target and "/" not in target:
            found = shutil.which(target)
            if found:
                return found, True
            for base in ("System32", "SysWOW64"):
                candidate = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), base, target)
                if os.path.exists(candidate):
                    return candidate, True
            return target, False
        return target, None
    if os.path.exists(target):
        return target, True
    lowered = target.lower()
    if not lowered.endswith(EXE_EXTENSIONS) and not os.path.splitext(target)[1]:
        return target, None
    return target, False


def installed_apps() -> list[InstalledApp]:
    apps: list[InstalledApp] = []
    for hive, base in UNINSTALL_KEYS:
        for sub in list_subkeys(hive, base):
            path = f"{base}\\{sub}"
            values = read_values(hive, path)
            display_name = (values.get("DisplayName") or "").strip()
            if not display_name:
                continue
            if (values.get("SystemComponent") or "").strip() == "1":
                continue
            if (values.get("ParentKeyName") or "").strip():
                continue
            release_type = (values.get("ReleaseType") or "").strip().lower()
            if release_type in ("update", "hotfix", "security update", "servicepack"):
                continue
            if display_name.upper().startswith("KB") and "update" in release_type:
                continue
            apps.append(
                InstalledApp(
                    display_name=display_name,
                    publisher=(values.get("Publisher") or "").strip(),
                    install_location=(values.get("InstallLocation") or "").strip().strip('"'),
                    uninstall_string=(values.get("UninstallString") or "").strip(),
                    display_icon=(values.get("DisplayIcon") or "").strip(),
                    key_path=path,
                    hive=hive,
                )
            )
    return apps


@dataclass
class ServiceEntry:
    name: str
    display_name: str
    image_path: str
    service_type: int


def services() -> list[ServiceEntry]:
    result: list[ServiceEntry] = []
    for name in list_subkeys(Hive.HKLM, SERVICES_KEY):
        path = f"{SERVICES_KEY}\\{name}"
        values = read_values(Hive.HKLM, path)
        image = (values.get("ImagePath") or "").strip()
        if not image:
            continue
        try:
            service_type = int(values.get("Type") or "0")
        except ValueError:
            service_type = 0
        result.append(
            ServiceEntry(
                name=name,
                display_name=(values.get("DisplayName") or name).strip(),
                image_path=image,
                service_type=service_type,
            )
        )
    return result


def export_key(hive: Hive, path: str, dest: str) -> tuple[bool, str]:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        proc = subprocess.run(
            ["reg", "export", full_key(hive, path), dest, "/y"],
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
        return False, message or f"reg export 返回码 {proc.returncode}"
    return True, dest


def delete_key(hive: Hive, path: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["reg", "delete", full_key(hive, path), "/f"],
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
        return False, message or f"reg delete 返回码 {proc.returncode}"
    return True, ""


def delete_value(hive: Hive, path: str, name: str) -> tuple[bool, str]:
    try:
        with winreg.OpenKey(HIVE_MAP[hive], path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except OSError as exc:
        return False, str(exc)
    return True, ""


def delete_service(name: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["sc", "delete", name],
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
        return False, message or f"sc delete 返回码 {proc.returncode}"
    return True, ""
