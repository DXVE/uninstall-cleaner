"""数据模型：扫描选项、残留项与风险/类别枚举。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Category(str, Enum):
    INSTALL_DIR = "安装目录"
    USER_DATA = "用户数据"
    REGISTRY = "注册表"
    UNINSTALL_ENTRY = "卸载登记项"
    STARTUP = "启动项"
    SERVICE = "服务"
    TASK = "计划任务"
    SHORTCUT = "快捷方式"


class Risk(str, Enum):
    HIGH = "高"
    MEDIUM = "中"
    LOW = "低"


RISK_ORDER = {Risk.HIGH: 0, Risk.MEDIUM: 1, Risk.LOW: 2}


class ActionKind(str, Enum):
    RECYCLE_FILE = "移入回收站"
    DELETE_REG_KEY = "删除注册表项"
    DELETE_REG_VALUE = "删除注册表值"
    DELETE_SERVICE = "删除服务"
    DELETE_TASK = "删除计划任务"


class Hive(str, Enum):
    HKLM = "HKEY_LOCAL_MACHINE"
    HKCU = "HKEY_CURRENT_USER"
    HKCR = "HKEY_CLASSES_ROOT"


@dataclass
class InstalledApp:
    display_name: str
    publisher: str = ""
    install_location: str = ""
    uninstall_string: str = ""
    display_icon: str = ""
    key_path: str = ""
    hive: Hive = Hive.HKLM


@dataclass
class ScanOptions:
    keywords: list[str] = field(default_factory=list)
    scan_dirs: bool = True
    scan_registry: bool = True
    scan_startup: bool = True
    detect_orphan: bool = True
    detect_unmatched: bool = True
    scan_drive_roots: bool = False

    @property
    def keyword_text(self) -> str:
        return ",".join(self.keywords)


@dataclass
class Finding:
    category: Category
    name: str
    location: str
    risk: Risk
    action: ActionKind
    reasons: list[str] = field(default_factory=list)
    size: int = 0
    mtime: float = 0.0
    protected: bool = False
    protected_reason: str = ""
    keywords: list[str] = field(default_factory=list)
    hive: Hive | None = None
    reg_path: str = ""
    value_name: str = ""
    service_name: str = ""
    task_path: str = ""
    target: str = ""
    status: str = ""

    @property
    def key(self) -> str:
        return "|".join(
            [
                self.category.value,
                self.action.value,
                self.hive.value if self.hive else "",
                self.reg_path.lower(),
                self.value_name.lower(),
                self.service_name.lower(),
                self.task_path.lower(),
                self.location.lower(),
            ]
        )

    @property
    def deletable(self) -> bool:
        return not self.protected and self.status != "已删除"


def human_size(size: int) -> str:
    if size <= 0:
        return "-"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"
