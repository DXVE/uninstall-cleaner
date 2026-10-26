"""扫描引擎：目录、注册表、卸载登记、启动项与关键词匹配。"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from . import paths, registry
from .models import (
    ActionKind,
    Category,
    Finding,
    Hive,
    InstalledApp,
    Risk,
    RISK_ORDER,
    ScanOptions,
)

CREATE_NO_WINDOW = 0x08000000

GUID_RE = re.compile(
    r"^\{?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}?$",
    re.IGNORECASE,
)


def normalize_name(text: str) -> str:
    cleaned = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


@dataclass
class _AppIndex:
    apps: list[InstalledApp]
    names: list[tuple[str, InstalledApp]]
    publishers: list[tuple[str, InstalledApp]]
    locations: list[tuple[str, InstalledApp]]


class Scanner:
    def __init__(self, options: ScanOptions, progress=None, log=None):
        self.options = options
        self.keywords = [k.strip().lower() for k in options.keywords if k.strip()]
        self._progress_cb = progress or (lambda phase, detail, count: None)
        self._log_cb = log or (lambda message: None)
        self._cancel = threading.Event()
        self._findings: dict[str, Finding] = {}
        self._reported_reg_keys: list[str] = []
        self._index = _AppIndex([], [], [], [])

    def cancel(self) -> None:
        self._cancel.set()

    def _progress(self, phase: str, detail: str, count: int) -> None:
        if self._cancel.is_set():
            return
        self._progress_cb(phase, detail, count)

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def scan(self) -> list[Finding]:
        self._progress("准备", "读取已安装程序清单", 0)
        self._index = self._build_index(registry.installed_apps())
        self._log_cb(f"已读取 {len(self._index.apps)} 个已安装程序")

        if self.options.detect_orphan:
            self._progress("卸载登记项", "检查卸载信息指向的路径", len(self._findings))
            self._scan_uninstall_entries()
        if self.options.scan_dirs:
            self._progress("目录", "扫描安装目录与用户数据目录", len(self._findings))
            self._scan_directories()
        if self.options.scan_registry:
            self._progress("注册表", "扫描软件注册表键", len(self._findings))
            self._scan_registry()
        if self.options.scan_startup:
            self._progress("启动项", "检查启动命令", len(self._findings))
            self._scan_run_keys()
            self._progress("服务", "检查服务映像路径", len(self._findings))
            self._scan_services()
            self._progress("计划任务", "检查计划任务命令", len(self._findings))
            self._scan_tasks()
            self._progress("快捷方式", "检查快捷方式目标", len(self._findings))
            self._scan_shortcuts()

        self._compute_sizes()
        result = list(self._findings.values())
        result.sort(key=lambda f: (RISK_ORDER[f.risk], f.category.value, f.name.lower()))
        self._progress("完成", f"共发现 {len(result)} 项", len(result))
        return result

    # ---------- 基础判定 ----------

    def _build_index(self, apps: list[InstalledApp]) -> _AppIndex:
        names: list[tuple[str, InstalledApp]] = []
        publishers: list[tuple[str, InstalledApp]] = []
        locations: list[tuple[str, InstalledApp]] = []
        for app in apps:
            name = normalize_name(app.display_name)
            if len(name) >= 3:
                names.append((name, app))
            publisher = normalize_name(app.publisher)
            if len(publisher) >= 3:
                publishers.append((publisher, app))
            if app.install_location:
                locations.append((paths.norm(app.install_location).rstrip("\\/"), app))
        return _AppIndex(apps, names, publishers, locations)

    def _match_app(self, name: str, path: str = "") -> InstalledApp | None:
        candidate = normalize_name(name)
        if path:
            normalized_path = paths.norm(path).rstrip("\\/")
            for location, app in self._index.locations:
                if normalized_path == location or normalized_path.startswith(location + os.sep):
                    return app
        if len(candidate) >= 3:
            for app_name, app in self._index.names:
                if candidate == app_name:
                    return app
                if len(candidate) >= 4 and (candidate in app_name or app_name in candidate):
                    return app
            for publisher, app in self._index.publishers:
                if candidate == publisher or (len(candidate) >= 4 and candidate in publisher):
                    return app
        return None

    def _keyword_hits(self, *texts: str) -> list[str]:
        if not self.keywords:
            return []
        blob = "\n".join(t for t in texts if t).lower()
        return [kw for kw in self.keywords if kw in blob]

    def _accept(self, finding: Finding, *texts: str) -> bool:
        hits = self._keyword_hits(*texts)
        if self.keywords and not hits:
            return False
        finding.keywords = hits
        self._add(finding)
        return True

    def _add(self, finding: Finding) -> None:
        key = finding.key
        existing = self._findings.get(key)
        if existing is None:
            self._findings[key] = finding
            return
        for reason in finding.reasons:
            if reason not in existing.reasons:
                existing.reasons.append(reason)
        for keyword in finding.keywords:
            if keyword not in existing.keywords:
                existing.keywords.append(keyword)
        if RISK_ORDER[finding.risk] < RISK_ORDER[existing.risk]:
            existing.risk = finding.risk
        if finding.protected:
            existing.protected = True
            existing.protected_reason = existing.protected_reason or finding.protected_reason

    # ---------- 卸载登记项 ----------

    def _scan_uninstall_entries(self) -> None:
        for app in self._index.apps:
            if self.cancelled:
                return
            reasons: list[str] = []
            risk = Risk.LOW
            install_location = os.path.expandvars(app.install_location.strip().strip('"'))
            if install_location and not os.path.isdir(install_location):
                reasons.append(f"安装目录不存在：{install_location}")
                risk = Risk.MEDIUM
            if app.uninstall_string:
                target, exists = registry.resolve_command(app.uninstall_string)
                if exists is False:
                    reasons.append(f"卸载程序不存在：{target}")
                    risk = Risk.HIGH
                elif exists is None and not registry.is_msi(app.uninstall_string):
                    reasons.append(f"卸载命令无法校验：{app.uninstall_string}")
                    risk = Risk.MEDIUM
            if app.display_icon:
                icon_target, icon_exists = registry.resolve_command(app.display_icon)
                if icon_exists is False:
                    reasons.append(f"图标路径不存在：{icon_target}")
            if not reasons:
                continue
            finding = Finding(
                category=Category.UNINSTALL_ENTRY,
                name=app.display_name,
                location=registry.full_key(app.hive, app.key_path),
                risk=risk,
                action=ActionKind.DELETE_REG_KEY,
                reasons=reasons,
                hive=app.hive,
                reg_path=app.key_path,
            )
            self._accept(finding, app.display_name, app.publisher, app.key_path, install_location)
            self._progress("卸载登记项", app.display_name, len(self._findings))

    # ---------- 目录 ----------

    def _scan_directories(self) -> None:
        for root, category in paths.scan_roots():
            if self.cancelled:
                return
            self._scan_dir_root(root, category)
        if self.options.scan_drive_roots:
            for drive in paths.drive_roots():
                if self.cancelled:
                    return
                self._scan_dir_root(drive, Category.INSTALL_DIR, drive_root=True)
        for path in list(self._findings):
            finding = self._findings[path]
            if finding.category in (Category.INSTALL_DIR, Category.USER_DATA):
                finding.mtime = paths.path_mtime(finding.location)

    def _scan_dir_root(self, root: str, category: Category, drive_root: bool = False) -> None:
        try:
            entries = list(os.scandir(root))
        except (OSError, PermissionError):
            return
        for entry in entries:
            if self.cancelled:
                return
            if paths.is_reparse(entry):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if drive_root and paths.skip_root_child(entry.path):
                continue
            self._consider_dir(entry.path, category, depth=1, expand_container=True)

    def _consider_dir(self, path: str, category: Category, depth: int, expand_container: bool) -> None:
        name = os.path.basename(path.rstrip("\\/"))
        hits = self._keyword_hits(name, path)
        app = self._match_app(name, path)
        guard = paths.protected_reason(path)
        if expand_container and not app and not hits and not guard and self._is_container(path):
            for child, child_depth in paths.iter_child_dirs(path, max_depth=1):
                self._consider_dir(child, category, child_depth, expand_container=False)
            return
        reasons: list[str] = []
        risk: Risk
        protected = bool(guard)
        if hits:
            if app:
                risk = Risk.LOW
                reasons.append(f"关键词命中（{'、'.join(hits)}），但存在对应已安装程序：{app.display_name}")
            else:
                risk = Risk.MEDIUM
                reasons.append(f"关键词命中：{'、'.join(hits)}")
        elif self.options.detect_unmatched and app is None and not guard:
            if not paths.has_executable(path):
                return
            risk = Risk.LOW
            reasons.append("未在已安装程序清单中找到对应项")
        else:
            return
        if guard:
            protected = True
            reasons.append(f"受保护：{guard}")
        finding = Finding(
            category=category,
            name=name,
            location=path,
            risk=risk,
            action=ActionKind.RECYCLE_FILE,
            reasons=reasons,
            protected=protected,
            protected_reason=guard,
            keywords=hits,
        )
        if self.keywords and not hits:
            return
        self._add(finding)
        self._progress("目录", path, len(self._findings))

    def _is_container(self, path: str) -> bool:
        files = 0
        dirs = 0
        try:
            entries = list(os.scandir(path))
        except (OSError, PermissionError):
            return False
        for entry in entries:
            if paths.is_reparse(entry):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    dirs += 1
                elif entry.is_file(follow_symlinks=False):
                    files += 1
            except OSError:
                continue
        return dirs > 0 and files == 0

    # ---------- 注册表 ----------

    def _scan_registry(self) -> None:
        max_depth = 3 if self.keywords else 1
        for hive, base in registry.SOFTWARE_ROOTS:
            for name in registry.list_subkeys(hive, base):
                if self.cancelled:
                    return
                key = name.lower()
                if key in paths.SYSTEM_REG_KEYS:
                    continue
                self._walk_reg_key(hive, base, name, depth=1, max_depth=max_depth)

    def _walk_reg_key(self, hive: Hive, base: str, name: str, depth: int, max_depth: int) -> None:
        if self.cancelled:
            return
        sub_path = f"{base}\\{name}" if base else name
        full = registry.full_key(hive, sub_path)
        full_lower = full.lower()
        if any(full_lower == parent or full_lower.startswith(parent + "\\") for parent in self._reported_reg_keys):
            return
        hits = self._keyword_hits(name, full)
        app = self._match_app(name)
        guard = name.lower() in paths.SYSTEM_REG_KEYS or bool(GUID_RE.match(name))
        reasons: list[str] = []
        risk: Risk
        include = False
        if hits:
            include = True
            if app:
                risk = Risk.LOW
                reasons.append(f"关键词命中（{'、'.join(hits)}），但存在对应已安装程序：{app.display_name}")
            else:
                risk = Risk.MEDIUM
                reasons.append(f"关键词命中：{'、'.join(hits)}")
        elif not self.keywords and depth <= 1 and self.options.detect_unmatched and app is None and not guard:
            include = True
            risk = Risk.LOW
            reasons.append("未在已安装程序清单中找到对应软件")
        if include:
            if guard:
                reasons.append("受保护：系统或厂商共用注册表键")
            finding = Finding(
                category=Category.REGISTRY,
                name=name,
                location=full,
                risk=risk,
                action=ActionKind.DELETE_REG_KEY,
                reasons=reasons,
                protected=guard,
                protected_reason="系统或厂商共用注册表键" if guard else "",
                keywords=hits,
                hive=hive,
                reg_path=sub_path,
            )
            self._add(finding)
            self._reported_reg_keys.append(full.lower())
            self._progress("注册表", full, len(self._findings))
        if depth < max_depth and not guard:
            for child in registry.list_subkeys(hive, sub_path):
                if child.lower() in paths.SYSTEM_REG_KEYS:
                    continue
                self._walk_reg_key(hive, sub_path, child, depth + 1, max_depth)

    # ---------- 启动项 / 服务 / 任务 / 快捷方式 ----------

    def _scan_run_keys(self) -> None:
        for hive, path in registry.RUN_KEYS:
            if self.cancelled:
                return
            values = registry.read_values(hive, path)
            for name, command in values.items():
                target, exists = registry.resolve_command(command)
                if exists is not False:
                    continue
                finding = Finding(
                    category=Category.STARTUP,
                    name=name,
                    location=registry.full_key(hive, path),
                    risk=Risk.HIGH,
                    action=ActionKind.DELETE_REG_VALUE,
                    reasons=[f"启动命令指向的文件不存在：{target}"],
                    hive=hive,
                    reg_path=path,
                    value_name=name,
                    target=target,
                )
                self._accept(finding, name, command, target)
                self._progress("启动项", name, len(self._findings))

    def _scan_services(self) -> None:
        for service in registry.services():
            if self.cancelled:
                return
            target, exists = registry.resolve_command(service.image_path)
            if exists is not False:
                continue
            if target and target.lower().startswith(os.environ.get("SystemRoot", r"C:\Windows").lower()):
                continue
            finding = Finding(
                category=Category.SERVICE,
                name=service.display_name,
                location=f"{registry.SERVICES_KEY}\\{service.name}",
                risk=Risk.HIGH,
                action=ActionKind.DELETE_SERVICE,
                reasons=[f"服务映像文件不存在：{target or service.image_path}"],
                service_name=service.name,
                target=target,
            )
            self._accept(finding, service.display_name, service.name, service.image_path)
            self._progress("服务", service.name, len(self._findings))

    def _run_powershell(self, script: str) -> str:
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._log_cb(f"PowerShell 调用失败：{exc}")
            return ""
        if proc.returncode != 0 and not proc.stdout.strip():
            detail = (proc.stderr or "").replace("\n", " ").strip()
            self._log_cb(f"PowerShell 返回码 {proc.returncode}：{detail[:200]}")
        return proc.stdout.strip()

    def _scan_tasks(self) -> None:
        script = (
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
            "$ProgressPreference='SilentlyContinue';"
            "$ErrorActionPreference='SilentlyContinue';"
            "Get-ScheduledTask | ForEach-Object {"
            "$t=$_;$a=@($t.Actions)[0];"
            "if($a){[PSCustomObject]@{TaskPath=$t.TaskPath;TaskName=$t.TaskName;"
            "Execute=[string]$a.Execute;Arguments=[string]$a.Arguments}}}"
            "|ConvertTo-Json -Compress"
        )
        raw = self._run_powershell(script)
        for item in self._parse_json_list(raw):
            if self.cancelled:
                return
            execute = (item.get("Execute") or "").strip()
            if not execute or "%" in os.path.expandvars(execute):
                continue
            command = execute
            if item.get("Arguments"):
                command = f'"{execute}" {item["Arguments"]}'
            target, exists = registry.resolve_command(command)
            if exists is not False:
                continue
            task_path = f"{item.get('TaskPath') or ''}{item.get('TaskName') or ''}"
            finding = Finding(
                category=Category.TASK,
                name=item.get("TaskName") or task_path,
                location=task_path,
                risk=Risk.HIGH,
                action=ActionKind.DELETE_TASK,
                reasons=[f"计划任务命令不存在：{target}"],
                task_path=task_path,
                target=target,
            )
            self._accept(finding, task_path, execute, target)
            self._progress("计划任务", task_path, len(self._findings))

    def _scan_shortcuts(self) -> None:
        roots = [
            paths.env_path("ProgramData") + r"\Microsoft\Windows\Start Menu\Programs",
            paths.env_path("APPDATA") + r"\Microsoft\Windows\Start Menu\Programs",
            paths.env_path("PUBLIC") + r"\Desktop",
            paths.env_path("USERPROFILE") + r"\Desktop",
        ]
        roots = [root for root in roots if os.path.isdir(root)]
        if not roots:
            return
        roots_expr = ",".join("'" + root.replace("'", "''") + "'" for root in roots)
        script = (
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
            "$ProgressPreference='SilentlyContinue';"
            "$ErrorActionPreference='SilentlyContinue';"
            f"$roots=@({roots_expr});"
            "$sh=New-Object -ComObject WScript.Shell;"
            "Get-ChildItem -LiteralPath $roots -Filter *.lnk -Recurse -ErrorAction SilentlyContinue"
            " | ForEach-Object {"
            "$s=$sh.CreateShortcut($_.FullName);"
            "if($s.TargetPath){[PSCustomObject]@{Path=$_.FullName;Target=$s.TargetPath;"
            "Arguments=[string]$s.Arguments}}}"
            "|ConvertTo-Json -Compress"
        )
        raw = self._run_powershell(script)
        for item in self._parse_json_list(raw):
            if self.cancelled:
                return
            target = os.path.expandvars((item.get("Target") or "").strip())
            if not target or "://" in target:
                continue
            if os.path.exists(target):
                continue
            lnk = item.get("Path") or ""
            finding = Finding(
                category=Category.SHORTCUT,
                name=os.path.splitext(os.path.basename(lnk))[0],
                location=lnk,
                risk=Risk.HIGH,
                action=ActionKind.RECYCLE_FILE,
                reasons=[f"快捷方式目标不存在：{target}"],
                target=target,
            )
            guard = paths.protected_reason(lnk)
            if guard:
                finding.protected = True
                finding.protected_reason = guard
                finding.risk = Risk.LOW
                finding.reasons.append(f"受保护：{guard}")
            self._accept(finding, lnk, target)
            self._progress("快捷方式", lnk, len(self._findings))

    @staticmethod
    def _parse_json_list(raw: str) -> list[dict]:
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    # ---------- 体积统计 ----------

    def _compute_sizes(self) -> None:
        targets = [
            finding
            for finding in self._findings.values()
            if finding.action == ActionKind.RECYCLE_FILE and os.path.isdir(finding.location)
        ]
        if not targets:
            return
        self._progress("统计", f"统计 {len(targets)} 个目录的体积", len(self._findings))
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {}
            for finding in targets:
                limit = 100_000 if finding.risk == Risk.LOW else 300_000
                futures[executor.submit(paths.dir_size, finding.location, limit)] = finding
            for future in as_completed(futures):
                if self.cancelled:
                    executor.shutdown(wait=False, cancel_futures=True)
                    return
                finding = futures[future]
                try:
                    size, count, truncated = future.result()
                except Exception as exc:  # noqa: BLE001
                    self._log_cb(f"统计失败：{finding.location}：{exc}")
                    continue
                finding.size = size
                finding.mtime = paths.path_mtime(finding.location)
                if truncated:
                    finding.reasons.append(f"体积统计达到上限（已统计 {count} 个文件）")
                self._progress("统计", finding.location, len(self._findings))
