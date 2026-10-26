"""tkinter 主界面：首页扫描入口、结果工作台与删除流程。"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import Counter
from pathlib import Path
from tkinter import filedialog, font as tkfont
from tkinter import messagebox, ttk

from . import __version__, elevation, paths
from .models import (
    ActionKind,
    Category,
    Finding,
    RISK_ORDER,
    Risk,
    ScanOptions,
    human_size,
)
from .remover import Remover, open_path, plan_backup_dir
from .scanner import Scanner

PROJECT_ROOT = Path(paths.tool_dir())


def resource_path(name: str) -> Path:
    """随包资源路径：打包后优先读取解包目录，源码运行时读取项目目录。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base) / name
        if candidate.exists():
            return candidate
    return PROJECT_ROOT / name

# 调色板：浅灰底、白色卡片、1px 边框
BG = "#f5f6f8"
CARD = "#ffffff"
BORDER = "#e3e6ea"
BORDER_STRONG = "#cbd2da"
TEXT = "#1f2933"
MUTED = "#6b7280"
ACCENT = "#1f2933"
ACCENT_ACTIVE = "#374151"
ACCENT_PRESSED = "#111827"
DANGER = "#b3261e"
DANGER_ACTIVE = "#8f1d17"
OK = "#2e7d32"
OK_TINT = "#e8f5e9"
WARN = "#b26a00"
WARN_TINT = "#fff4e5"
STRIPE = "#fafbfc"
HEAD_BG = "#f7f8fa"
SELECT_BG = "#e8f0fe"
HOVER_BG = "#f0f1f3"
PILL_TINT = "#eef0f2"

RISK_COLORS = {
    Risk.HIGH: "#c62828",
    Risk.MEDIUM: "#b26a00",
    Risk.LOW: "#5f6368",
}
RISK_TINTS = {
    Risk.HIGH: "#fdecea",
    Risk.MEDIUM: "#fff4e5",
    Risk.LOW: "#eef0f2",
}

FONT = ("Microsoft YaHei UI", 9)
FONT_SMALL = ("Microsoft YaHei UI", 8)
FONT_SMALL_BOLD = ("Microsoft YaHei UI", 8, "bold")
FONT_BOLD = ("Microsoft YaHei UI", 9, "bold")
FONT_H1 = ("Microsoft YaHei UI", 18, "bold")
FONT_H2 = ("Microsoft YaHei UI", 11, "bold")
FONT_STAT = ("Segoe UI", 22, "bold")
MONO_FONT = ("Consolas", 9)

# 界面按 96 DPI 设计，启动时换算为当前屏幕的物理像素
SC = 1.0


def s(value: float) -> int:
    return max(1, int(round(value * SC)))


def make_card(master) -> tk.Frame:
    """白底卡片：1px 细边框代替阴影。"""
    return tk.Frame(
        master,
        background=CARD,
        highlightbackground=BORDER,
        highlightcolor=BORDER,
        highlightthickness=s(1),
        borderwidth=0,
    )


class Chip(tk.Checkbutton):
    """无指示器复选标签，选中时填充深色。"""

    def __init__(self, master, text: str, variable: tk.BooleanVar, command=None, **kwargs):
        self._callback = command
        super().__init__(
            master,
            text=text,
            variable=variable,
            command=self._handle,
            indicatoron=False,
            relief="flat",
            borderwidth=0,
            highlightthickness=s(1),
            highlightbackground=BORDER,
            highlightcolor=BORDER,
            font=FONT,
            padx=s(12),
            pady=s(5),
            cursor="hand2",
            **kwargs,
        )
        self._var = variable
        self._sync()

    def _handle(self) -> None:
        self._sync()
        if self._callback is not None:
            self._callback()

    def _sync(self) -> None:
        on = bool(self._var.get())
        self.configure(
            background=ACCENT if on else CARD,
            foreground="#ffffff" if on else TEXT,
            activebackground=ACCENT_ACTIVE if on else HOVER_BG,
            activeforeground="#ffffff" if on else TEXT,
            selectcolor=ACCENT if on else CARD,
        )


class ScopeCard(tk.Frame):
    """可点击的范围选择卡：左侧勾选框，右侧标题与说明。"""

    def __init__(self, master, title: str, desc: str, variable: tk.BooleanVar, command=None, **kwargs):
        super().__init__(
            master,
            background=CARD,
            highlightthickness=s(1),
            highlightbackground=BORDER,
            cursor="hand2",
            **kwargs,
        )
        self._var = variable
        self._callback = command

        body = tk.Frame(self, background=CARD, padx=s(12), pady=s(10))
        body.pack(fill="both", expand=True)
        self._box = tk.Label(
            body,
            text="",
            width=2,
            font=FONT_SMALL_BOLD,
            background=CARD,
            foreground="#ffffff",
            highlightthickness=s(1),
            highlightbackground=BORDER_STRONG,
        )
        self._box.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, s(10)), pady=s(2))
        self._title = tk.Label(
            body, text=title, font=FONT_BOLD, background=CARD, foreground=TEXT, anchor="w"
        )
        self._title.grid(row=0, column=1, sticky="w")
        self._desc = tk.Label(
            body,
            text=desc,
            font=FONT_SMALL,
            background=CARD,
            foreground=MUTED,
            anchor="w",
            justify="left",
            wraplength=s(250),
        )
        self._desc.grid(row=1, column=1, sticky="w", pady=(s(2), 0))
        body.columnconfigure(1, weight=1)

        for widget in (self, body, self._box, self._title, self._desc):
            widget.bind("<Button-1>", self._toggle)
        self._sync()

    def _toggle(self, _event=None) -> str:
        self._var.set(not self._var.get())
        self._sync()
        if self._callback is not None:
            self._callback()
        return "break"

    def _sync(self) -> None:
        on = bool(self._var.get())
        self.configure(highlightbackground=ACCENT if on else BORDER)
        self._box.configure(
            text="✓" if on else "",
            background=ACCENT if on else CARD,
            foreground="#ffffff" if on else CARD,
        )


class Segment(tk.Radiobutton):
    """单组分段控件中的一项，选中时填充深色。"""

    def __init__(self, master, text: str, variable: tk.StringVar, value: str, command=None, **kwargs):
        super().__init__(
            master,
            text=text,
            variable=variable,
            value=value,
            command=command,
            indicatoron=False,
            relief="flat",
            borderwidth=0,
            highlightthickness=s(1),
            highlightbackground=BORDER,
            highlightcolor=BORDER,
            font=FONT,
            padx=s(13),
            pady=s(5),
            cursor="hand2",
            **kwargs,
        )
        self._var = variable
        self._value = value
        self._var.trace_add("write", lambda *_: self._sync())
        self._sync()

    def _sync(self) -> None:
        on = self._var.get() == self._value
        self.configure(
            background=ACCENT if on else CARD,
            foreground="#ffffff" if on else TEXT,
            activebackground=ACCENT_ACTIVE if on else HOVER_BG,
            activeforeground="#ffffff" if on else TEXT,
            selectcolor=ACCENT if on else CARD,
        )


class App:
    def __init__(self, root: tk.Tk):
        global SC
        SC = root.winfo_fpixels("1p") / (96.0 / 72.0)

        self.root = root
        self.findings: list[Finding] = []
        self.checked: set[str] = set()
        self.by_iid: dict[str, Finding] = {}
        self.current: Finding | None = None
        self.scanner: Scanner | None = None
        self.queue: queue.Queue = queue.Queue()
        self.sort_column = "risk"
        self.sort_reverse = False
        self.remover = Remover()
        self.backup_parent = ""
        self._columns: tuple[str, ...] = ()
        self._sash_ready = False
        self._log_open = False
        self._tip: tk.Toplevel | None = None
        self._tip_label: tk.Label | None = None
        self._tip_state: tuple[str, str, str] | None = None
        self._tip_font = tkfont.Font(font=FONT)

        self.keyword_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="关键词")
        self.var_dirs = tk.BooleanVar(value=True)
        self.var_registry = tk.BooleanVar(value=True)
        self.var_startup = tk.BooleanVar(value=True)
        self.var_drive = tk.BooleanVar(value=False)
        self.cat_var = tk.StringVar(value="全部")
        self.risk_var = tk.StringVar(value="全部")
        self.filter_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪：在首页输入关键词或直接开始扫描")

        self._build_style()
        self._build_header()
        self._build_status()
        self._build_log()
        self._build_content()
        self._update_admin_state()
        self._show_home()
        self.root.after(120, self._poll_queue)

    # ---------- 样式 ----------

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        self.root.option_add("*Font", FONT)
        self.root.configure(background=BG)

        style.configure(".", background=BG, foreground=TEXT, font=FONT, focuscolor=BG)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED)

        style.configure(
            "TButton",
            background=CARD,
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=CARD,
            darkcolor=CARD,
            relief="flat",
            padding=(s(12), s(6)),
            font=FONT,
        )
        style.map(
            "TButton",
            background=[("active", HOVER_BG), ("pressed", "#e6e8eb"), ("disabled", "#f3f4f6")],
            foreground=[("disabled", "#9aa3af")],
            bordercolor=[("disabled", BORDER)],
        )
        style.configure(
            "Accent.TButton",
            background=ACCENT,
            foreground="#ffffff",
            bordercolor=ACCENT,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            padding=(s(16), s(9)),
            font=FONT_BOLD,
        )
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_ACTIVE), ("pressed", ACCENT_PRESSED), ("disabled", "#c2c7cf")],
            foreground=[("disabled", "#ffffff")],
            bordercolor=[("disabled", "#c2c7cf")],
        )
        style.configure(
            "Danger.TButton",
            background=DANGER,
            foreground="#ffffff",
            bordercolor=DANGER,
            lightcolor=DANGER,
            darkcolor=DANGER,
            padding=(s(14), s(6)),
            font=FONT_BOLD,
        )
        style.map(
            "Danger.TButton",
            background=[("active", DANGER_ACTIVE), ("pressed", "#7a1712"), ("disabled", "#d9b3b0")],
            foreground=[("disabled", "#ffffff")],
            bordercolor=[("disabled", "#d9b3b0")],
        )
        style.configure(
            "Ghost.TButton",
            background=CARD,
            foreground=TEXT,
            bordercolor=BORDER_STRONG,
            lightcolor=CARD,
            darkcolor=CARD,
            padding=(s(12), s(6)),
        )
        style.configure(
            "Link.TButton",
            background=BG,
            foreground=MUTED,
            borderwidth=0,
            relief="flat",
            padding=(s(6), s(2)),
            font=FONT_SMALL,
        )
        style.map("Link.TButton", background=[("active", HOVER_BG)], foreground=[("active", TEXT)])
        style.configure(
            "CardLink.TButton",
            background=CARD,
            foreground=MUTED,
            borderwidth=0,
            relief="flat",
            padding=(0, s(2)),
            font=FONT_SMALL,
        )
        style.map("CardLink.TButton", background=[("active", CARD)], foreground=[("active", TEXT)])

        style.configure(
            "TEntry",
            fieldbackground=CARD,
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=CARD,
            darkcolor=CARD,
            insertcolor=TEXT,
            padding=(s(8), s(7)),
            relief="flat",
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", "#9aa3af")],
            lightcolor=[("focus", CARD)],
            darkcolor=[("focus", CARD)],
        )
        style.configure("Hero.TEntry", font=("Microsoft YaHei UI", 11), padding=(s(10), s(9)))

        style.configure(
            "Treeview",
            background=CARD,
            fieldbackground=CARD,
            foreground=TEXT,
            bordercolor=BORDER,
            rowheight=s(32),
            borderwidth=0,
            font=FONT,
        )
        style.configure(
            "Treeview.Heading",
            background=HEAD_BG,
            foreground=MUTED,
            font=FONT_SMALL_BOLD,
            relief="flat",
            borderwidth=0,
            padding=(s(6), s(9)),
        )
        style.map(
            "Treeview",
            background=[("selected", SELECT_BG)],
            foreground=[("selected", TEXT)],
            bordercolor=[("focus", BORDER)],
        )
        style.map("Treeview.Heading", background=[("active", "#eef0f3")])

        for scrollbar in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            style.configure(
                scrollbar,
                background="#d5d9df",
                troughcolor=BG,
                bordercolor=BG,
                lightcolor="#d5d9df",
                darkcolor="#d5d9df",
                arrowcolor=MUTED,
                arrowsize=s(12),
                gripcount=0,
                relief="flat",
            )
            style.map(scrollbar, background=[("active", "#bcc3cc")])

        style.configure("TPanedwindow", background=BG)
        style.configure("Sash", sashthickness=s(8), gripcount=0, background=BG)
        style.configure(
            "Thin.Horizontal.TProgressbar",
            troughcolor="#e6e8ec",
            background=ACCENT,
            bordercolor="#e6e8ec",
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            thickness=s(4),
        )
        style.configure("TSeparator", background=BORDER)

    # ---------- 顶部标题栏 ----------

    def _build_header(self) -> None:
        header = tk.Frame(self.root, background=CARD)
        header.pack(side="top", fill="x")
        inner = tk.Frame(header, background=CARD, padx=s(18), pady=s(10))
        inner.pack(fill="x")

        left = tk.Frame(inner, background=CARD)
        left.pack(side="left")
        tk.Label(
            left,
            text="卸载残留检测",
            font=("Microsoft YaHei UI", 12, "bold"),
            background=CARD,
            foreground=TEXT,
        ).pack(anchor="w")
        tk.Label(
            left,
            text="扫描安装目录、注册表、启动项与服务中的卸载残留",
            font=FONT_SMALL,
            background=CARD,
            foreground=MUTED,
        ).pack(anchor="w", pady=(s(2), 0))

        right = tk.Frame(inner, background=CARD)
        right.pack(side="right")
        self.admin_pill = tk.Label(right, text="", font=FONT_SMALL_BOLD, padx=s(10), pady=s(3))
        self.admin_pill.pack(side="right", padx=(s(10), 0))

        help_btn = tk.Menubutton(
            right,
            text="帮助 ▾",
            font=FONT,
            background=CARD,
            foreground=TEXT,
            activebackground=HOVER_BG,
            activeforeground=TEXT,
            relief="flat",
            borderwidth=0,
            padx=s(10),
            pady=s(4),
            cursor="hand2",
        )
        help_menu = tk.Menu(help_btn, tearoff=0)
        help_menu.add_command(label="使用说明", command=self._open_readme)
        help_menu.add_command(label="关于", command=self._about)
        help_btn.configure(menu=help_menu)
        help_btn.pack(side="right")

        tools_btn = tk.Menubutton(
            right,
            text="工具 ▾",
            font=FONT,
            background=CARD,
            foreground=TEXT,
            activebackground=HOVER_BG,
            activeforeground=TEXT,
            relief="flat",
            borderwidth=0,
            padx=s(10),
            pady=s(4),
            cursor="hand2",
        )
        tools_menu = tk.Menu(tools_btn, tearoff=0)
        tools_menu.add_command(label="以管理员身份重启", command=self._elevate)
        tools_menu.add_separator()
        tools_menu.add_command(label="打开备份目录", command=self._open_backups)
        tools_menu.add_command(label="打开回收站", command=self._open_recycle_bin)
        tools_btn.configure(menu=tools_menu)
        tools_btn.pack(side="right")

        tk.Frame(self.root, background=BORDER, height=s(1)).pack(side="top", fill="x")

    # ---------- 页面容器 ----------

    def _build_content(self) -> None:
        self.content = tk.Frame(self.root, background=BG)
        self.content.pack(fill="both", expand=True)
        self.home_frame = tk.Frame(self.content, background=BG)
        self.results_frame = tk.Frame(self.content, background=BG)
        self._build_home()
        self._build_results()

    def _build_home(self) -> None:
        card = self.home_card = make_card(self.home_frame)
        card.place(relx=0.5, rely=0.46, anchor="center", width=s(760))

        inner = tk.Frame(card, background=CARD, padx=s(32), pady=s(28))
        inner.pack(fill="both", expand=True)

        tk.Label(inner, text="清理卸载残留", font=FONT_H1, background=CARD, foreground=TEXT).pack(anchor="w")
        tk.Label(
            inner,
            text="找出已卸载软件留在目录、注册表与启动项中的项目，确认后再清理。",
            font=FONT,
            background=CARD,
            foreground=MUTED,
        ).pack(anchor="w", pady=(s(6), s(20)))

        tk.Label(inner, text="查找方式", font=FONT_BOLD, background=CARD, foreground=TEXT).pack(anchor="w")
        mode_row = tk.Frame(inner, background=CARD)
        mode_row.pack(anchor="w", pady=(s(6), s(4)))
        for index, (label, value) in enumerate((("按关键词查找", "关键词"), ("不在程序列表中", "不在程序列表"))):
            Segment(
                mode_row,
                text=label,
                variable=self.mode_var,
                value=value,
                command=self._on_mode_change,
            ).pack(side="left", padx=(0 if index == 0 else s(6), 0))

        tk.Label(inner, text="关键词", font=FONT_BOLD, background=CARD, foreground=TEXT).pack(
            anchor="w", pady=(s(10), 0)
        )
        self.keyword_entry = ttk.Entry(
            inner,
            textvariable=self.keyword_var,
            style="Hero.TEntry",
            font=("Microsoft YaHei UI", 11),
        )
        self.keyword_entry.pack(fill="x", pady=(s(6), s(6)))
        self.keyword_entry.bind("<Return>", lambda _: self._start_scan())
        self.keyword_hint = tk.Label(
            inner,
            text="",
            font=FONT_SMALL,
            background=CARD,
            foreground=MUTED,
            justify="left",
            wraplength=s(600),
        )
        self.keyword_hint.pack(anchor="w")

        tk.Label(inner, text="检测范围", font=FONT_BOLD, background=CARD, foreground=TEXT).pack(
            anchor="w", pady=(s(18), s(6))
        )
        cards = tk.Frame(inner, background=CARD)
        cards.pack(fill="x")
        cards.columnconfigure(0, weight=1, uniform="scope")
        cards.columnconfigure(1, weight=1, uniform="scope")
        ScopeCard(
            cards, "文件与目录", "安装目录、用户数据目录中的遗留文件夹", self.var_dirs
        ).grid(row=0, column=0, sticky="nsew", padx=(0, s(8)), pady=(0, s(8)))
        ScopeCard(
            cards, "注册表", "软件注册表键与卸载登记信息", self.var_registry
        ).grid(row=0, column=1, sticky="nsew", pady=(0, s(8)))
        ScopeCard(
            cards,
            "启动与后台",
            "启动项、服务、计划任务、桌面与开始菜单快捷方式",
            self.var_startup,
        ).grid(row=1, column=0, columnspan=2, sticky="ew")

        extra = tk.Frame(inner, background=CARD)
        extra.pack(fill="x", pady=(s(10), 0))
        Chip(extra, text="额外扫描各磁盘根目录（较慢）", variable=self.var_drive).pack(side="left")

        self.home_error = tk.Label(inner, text="", font=FONT_SMALL, background=CARD, foreground=DANGER)
        self.home_error.pack(anchor="w")
        self._on_mode_change()

        actions = tk.Frame(inner, background=CARD)
        actions.pack(fill="x", pady=(s(14), 0))
        self.scan_btn = ttk.Button(
            actions, text="开始扫描", style="Accent.TButton", command=self._start_scan
        )
        self.scan_btn.pack(side="left", fill="x", expand=True)
        ttk.Button(
            actions, text="打开备份目录", style="Ghost.TButton", command=self._open_backups
        ).pack(side="left", padx=(s(8), 0))

        self.home_back_btn = ttk.Button(
            inner, text="", style="CardLink.TButton", command=self._show_results
        )

        notes = self.home_notes = tk.Frame(inner, background=CARD)
        notes.pack(fill="x")
        tk.Frame(notes, background=BORDER, height=s(1)).pack(fill="x", pady=(s(18), s(12)))
        for text in (
            "✓  文件与快捷方式移入回收站，可随时还原",
            "✓  注册表、服务与计划任务删除前备份到所选目录",
        ):
            tk.Label(
                notes, text=text, font=FONT_SMALL, background=CARD, foreground=MUTED
            ).pack(anchor="w", pady=s(1))

    def _build_results(self) -> None:
        pad = tk.Frame(self.results_frame, background=BG, padx=s(16), pady=s(14))
        pad.pack(fill="both", expand=True)

        # 概览卡片
        stats = tk.Frame(pad, background=BG)
        stats.pack(fill="x")
        self.stat_values: dict[str, tk.Label] = {}
        specs = (
            ("total", "发现项"),
            ("high", "高风险"),
            ("size", "可清理体积"),
            ("protected", "受保护"),
        )
        for index, (key, caption) in enumerate(specs):
            stats.columnconfigure(index, weight=1, uniform="stat")
            card = make_card(stats)
            card.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else s(8), 0))
            body = tk.Frame(card, background=CARD, padx=s(16), pady=s(10))
            body.pack(fill="both", expand=True)
            value = tk.Label(body, text="0", font=FONT_STAT, background=CARD, foreground=TEXT, anchor="w")
            value.pack(anchor="w")
            tk.Label(
                body, text=caption, font=FONT_SMALL, background=CARD, foreground=MUTED, anchor="w"
            ).pack(anchor="w")
            self.stat_values[key] = value

        actions = tk.Frame(stats, background=BG)
        actions.grid(row=0, column=4, sticky="e", padx=(s(14), 0))
        self.back_btn = ttk.Button(
            actions, text="返回", style="Ghost.TButton", command=self._show_home
        )
        self.back_btn.pack(side="left")

        # 筛选与操作栏
        bar = make_card(pad)
        bar.pack(fill="x", pady=(s(12), s(10)))
        bar_inner = tk.Frame(bar, background=CARD, padx=s(12), pady=s(10))
        bar_inner.pack(fill="x")

        row1 = tk.Frame(bar_inner, background=CARD)
        row1.pack(fill="x")
        tk.Label(row1, text="风险", font=FONT_SMALL, background=CARD, foreground=MUTED).pack(
            side="left", padx=(0, s(6))
        )
        seg = tk.Frame(row1, background=CARD)
        seg.pack(side="left")
        for index, value in enumerate(("全部", "高", "中", "低")):
            segment = Segment(seg, text=value, variable=self.risk_var, value=value, command=self._refresh)
            segment.pack(side="left", padx=(0 if index == 0 else s(4), 0))

        self.delete_btn = ttk.Button(
            row1, text="删除选中项", style="Danger.TButton", command=self._delete_checked, state="disabled"
        )
        self.delete_btn.pack(side="right")
        ttk.Button(
            row1, text="反选", style="Ghost.TButton", command=self._invert_selection
        ).pack(side="right", padx=(0, s(8)))
        ttk.Button(
            row1, text="仅中低风险", style="Ghost.TButton", command=self._select_mid_low
        ).pack(side="right", padx=(0, s(8)))
        ttk.Button(
            row1, text="全选可见", style="Ghost.TButton", command=self._select_all
        ).pack(side="right", padx=(0, s(8)))
        self.search_entry = ttk.Entry(row1, textvariable=self.filter_var, width=24)
        self.search_entry.pack(side="right", padx=(0, s(8)))
        tk.Label(row1, text="搜索", font=FONT_SMALL, background=CARD, foreground=MUTED).pack(
            side="right", padx=(0, s(6))
        )
        self.filter_var.trace_add("write", lambda *_: self._refresh())

        row2 = tk.Frame(bar_inner, background=CARD)
        row2.pack(fill="x", pady=(s(10), 0))
        tk.Label(row2, text="类别", font=FONT_SMALL, background=CARD, foreground=MUTED).pack(
            side="left", padx=(0, s(6))
        )
        self.cat_chips = tk.Frame(row2, background=CARD)
        self.cat_chips.pack(side="left")

        # 结果表与详情
        paned = self.paned = ttk.Panedwindow(pad, orient="horizontal")
        paned.pack(fill="both", expand=True)

        table_card = make_card(paned)
        table_card.rowconfigure(0, weight=1)
        table_card.columnconfigure(0, weight=1)
        paned.add(table_card, weight=3)

        columns = ("sel", "category", "name", "location", "size", "risk", "status")
        self._columns = columns
        self.tree = ttk.Treeview(table_card, columns=columns, show="headings", selectmode="browse")
        headings = {
            "sel": ("选", 42),
            "category": ("类别", 82),
            "name": ("名称", 182),
            "location": ("位置", 240),
            "size": ("大小", 76),
            "risk": ("风险", 64),
            "status": ("状态", 76),
        }
        for column, (text, width_) in headings.items():
            self.tree.heading(column, text=text, anchor="center")
            self.tree.column(
                column,
                width=s(width_),
                minwidth=s(44),
                anchor="w" if column in ("name", "location") else "center",
                stretch=column == "location",
            )
        for column in ("category", "name", "location", "size", "risk"):
            self.tree.heading(column, command=lambda c=column: self._sort_by(c))

        tree_y = ttk.Scrollbar(table_card, orient="vertical", command=self.tree.yview)
        tree_x = ttk.Scrollbar(table_card, orient="horizontal", command=self.tree.xview)
        self.tree_x = tree_x
        self.tree.configure(yscrollcommand=tree_y.set, xscrollcommand=tree_x.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(s(1), 0), pady=(s(1), 0))
        tree_y.grid(row=0, column=1, sticky="ns", padx=(0, s(1)), pady=(s(1), 0))
        tree_x.grid(row=1, column=0, sticky="ew", padx=(s(1), 0), pady=(0, s(1)))
        tree_x.grid_remove()

        self.tree.tag_configure("high", foreground=RISK_COLORS[Risk.HIGH])
        self.tree.tag_configure("medium", foreground=RISK_COLORS[Risk.MEDIUM])
        self.tree.tag_configure("low", foreground=RISK_COLORS[Risk.LOW])
        self.tree.tag_configure("protected", foreground="#9aa3af")
        self.tree.tag_configure("deleted", foreground=OK)
        self.tree.tag_configure("stripe", background=STRIPE)

        # 表格空态与扫描进度覆盖层
        self.empty_box = tk.Frame(self.tree, background=CARD)
        self.empty_title = tk.Label(
            self.empty_box, text="", font=FONT_H2, background=CARD, foreground=TEXT, justify="center"
        )
        self.empty_title.pack()
        self.empty_hint = tk.Label(
            self.empty_box,
            text="",
            font=FONT,
            background=CARD,
            foreground=MUTED,
            justify="center",
            wraplength=s(460),
        )
        self.empty_hint.pack(pady=(s(6), 0))
        self.empty_progress = ttk.Progressbar(
            self.empty_box, mode="indeterminate", length=s(260), style="Thin.Horizontal.TProgressbar"
        )
        self.empty_phase = tk.Label(
            self.empty_box,
            text="",
            font=FONT_SMALL,
            background=CARD,
            foreground=MUTED,
            justify="center",
            wraplength=s(460),
        )

        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.tree.bind("<Double-1>", lambda _: "break")
        self.tree.bind("<space>", self._toggle_current)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Motion>", self._on_tree_hover)
        self.tree.bind("<Leave>", self._hide_tooltip)
        self.tree.bind("<B1-Motion>", self._hide_tooltip, add="+")
        self.tree.bind("<MouseWheel>", self._hide_tooltip, add="+")
        self.root.bind("<FocusOut>", self._hide_tooltip, add="+")
        self.tree.bind("<Configure>", self._update_hscroll)
        self.tree.bind("<ButtonRelease-1>", self._update_hscroll, add="+")
        self.tree.bind("<ButtonPress-1>", self._block_column_drag, add="+")
        self.tree.bind("<Control-a>", lambda _: self._select_all())
        self.tree.bind("<Delete>", lambda _: self._delete_checked())

        self._build_detail(paned)

        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="全选可见项", command=self._select_all)
        self.context_menu.add_command(label="全不选", command=self._unselect_all)
        self.context_menu.add_command(label="仅选中低风险项", command=self._select_mid_low)
        self.context_menu.add_command(label="反选可见项", command=self._invert_selection)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="复制路径", command=self._copy_path)
        self.context_menu.add_command(label="打开位置", command=self._open_location)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="删除选中项…", command=self._delete_checked)

        self.root.bind("<F5>", lambda _: self._start_scan())

    def _build_detail(self, paned: ttk.Panedwindow) -> None:
        card = self.detail_card = make_card(paned)
        paned.add(card, weight=1)
        inner = tk.Frame(card, background=CARD, padx=s(16), pady=s(14))
        inner.pack(fill="both", expand=True)

        self.detail_name = tk.Label(
            inner,
            text="未选择项目",
            font=FONT_H2,
            background=CARD,
            foreground=TEXT,
            anchor="w",
            justify="left",
            wraplength=s(300),
        )
        self.detail_name.pack(fill="x")

        pills = tk.Frame(inner, background=CARD)
        pills.pack(fill="x", pady=(s(8), s(10)))
        self.detail_risk_pill = tk.Label(pills, text="", font=FONT_SMALL_BOLD, padx=s(8), pady=s(2))
        self.detail_cat_pill = tk.Label(
            pills, text="", font=FONT_SMALL_BOLD, padx=s(8), pady=s(2), background=PILL_TINT, foreground="#37474f"
        )

        meta = tk.Frame(inner, background=CARD)
        meta.pack(fill="x")
        meta.columnconfigure(1, weight=1)
        self._meta_values: dict[str, tk.Label] = {}
        for index, label in enumerate(("大小", "处理方式", "状态")):
            tk.Label(
                meta, text=label, font=FONT_SMALL, background=CARD, foreground=MUTED, anchor="w"
            ).grid(row=index, column=0, sticky="nw", pady=s(2))
            value = tk.Label(
                meta,
                text="—",
                font=FONT,
                background=CARD,
                foreground=TEXT,
                anchor="w",
                justify="left",
                wraplength=s(220),
            )
            value.grid(row=index, column=1, sticky="w", padx=(s(12), 0), pady=s(2))
            self._meta_values[label] = value

        tk.Frame(inner, background=BORDER, height=s(1)).pack(fill="x", pady=(s(10), s(8)))

        body = tk.Frame(inner, background=CARD)
        body.pack(fill="both", expand=True)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.detail_text = tk.Text(
            body,
            wrap="word",
            state="disabled",
            relief="flat",
            background=CARD,
            foreground=TEXT,
            font=FONT,
            padx=0,
            pady=0,
            borderwidth=0,
            highlightthickness=0,
            spacing1=1,
            spacing3=2,
            cursor="arrow",
        )
        detail_scroll = ttk.Scrollbar(body, orient="vertical", command=self.detail_text.yview)
        self.detail_text.configure(yscrollcommand=detail_scroll.set)
        self.detail_text.grid(row=0, column=0, sticky="nsew")
        detail_scroll.grid(row=0, column=1, sticky="ns")

        foot = tk.Frame(inner, background=CARD)
        foot.pack(fill="x", pady=(s(12), 0))
        self.open_btn = ttk.Button(
            foot, text="打开位置", style="Ghost.TButton", command=self._open_location, state="disabled"
        )
        self.open_btn.pack(side="left", fill="x", expand=True)
        self.copy_btn = ttk.Button(
            foot, text="复制路径", style="Ghost.TButton", command=self._copy_path, state="disabled"
        )
        self.copy_btn.pack(side="left", fill="x", expand=True, padx=(s(8), 0))

        self.detail_card.bind(
            "<Configure>",
            lambda event: self._resize_detail_wrap(event.width),
        )

    def _resize_detail_wrap(self, width: int) -> None:
        wrap = max(s(160), width - s(50))
        self.detail_name.configure(wraplength=wrap)
        for value in self._meta_values.values():
            value.configure(wraplength=max(s(120), width - s(130)))

    # ---------- 状态栏与日志 ----------

    def _build_status(self) -> None:
        bar = tk.Frame(self.root, background=BG, padx=s(16), pady=s(7))
        bar.pack(side="bottom", fill="x")
        self.status_label = tk.Label(
            bar,
            textvariable=self.status_var,
            font=FONT_SMALL,
            background=BG,
            foreground=MUTED,
            anchor="w",
        )
        self.status_label.pack(side="left", fill="x", expand=True)
        self.log_btn = ttk.Button(
            bar, text="运行日志 ▾", style="Link.TButton", command=self._toggle_log
        )
        self.log_btn.pack(side="right")

    def _build_log(self) -> None:
        self.log_drawer = tk.Frame(self.root, background=CARD)
        tk.Frame(self.log_drawer, background=BORDER, height=s(1)).pack(side="top", fill="x")
        inner = tk.Frame(self.log_drawer, background=CARD, padx=s(16), pady=s(10))
        inner.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            inner,
            height=5,
            wrap="none",
            state="disabled",
            relief="flat",
            font=MONO_FONT,
            background=CARD,
            foreground="#37474f",
            padx=0,
            pady=0,
            borderwidth=0,
            highlightthickness=0,
        )
        log_scroll = ttk.Scrollbar(inner, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")
        inner.rowconfigure(0, weight=1)
        inner.columnconfigure(0, weight=1)

    def _toggle_log(self) -> None:
        self._log_open = not self._log_open
        if self._log_open:
            self.log_drawer.pack(side="bottom", fill="x", after=self.status_label.master)
            self.log_btn.configure(text="运行日志 ▴")
        else:
            self.log_drawer.pack_forget()
            self.log_btn.configure(text="运行日志 ▾")

    # ---------- 视图切换 ----------

    def _show_home(self) -> None:
        self.results_frame.pack_forget()
        self.home_frame.pack(fill="both", expand=True)
        self._refresh_home_back()
        self.scan_btn.configure(state="disabled" if self.scanner is not None else "normal")
        self.keyword_entry.focus_set()

    def _refresh_home_back(self) -> None:
        if self.findings:
            self.home_back_btn.configure(text=f"查看上次扫描结果（{len(self.findings)} 项）")
            self.home_back_btn.pack(anchor="w", pady=(s(8), 0), before=self.home_notes)
        else:
            self.home_back_btn.pack_forget()

    def _show_results(self) -> None:
        self.home_frame.pack_forget()
        self.results_frame.pack(fill="both", expand=True)
        self._refresh()
        if not self._sash_ready:
            self.root.after(40, self._init_sash)

    def _init_sash(self) -> None:
        width = self.paned.winfo_width()
        if width <= 1:
            self.root.after(60, self._init_sash)
            return
        self.paned.sashpos(0, max(s(420), width - s(340)))
        self._sash_ready = True

    # ---------- 扫描 ----------

    def _on_mode_change(self) -> None:
        keyword_mode = self.mode_var.get() == "关键词"
        self.keyword_entry.configure(state="normal" if keyword_mode else "disabled")
        if keyword_mode:
            self.keyword_hint.configure(text="多个关键词用中英文逗号分隔，不区分大小写；至少填写一个。")
        else:
            self.keyword_hint.configure(
                text="不使用关键词，找出不在已安装程序列表中的目录与注册表键；这些项目可能是卸载残留，均为低风险，需要人工确认。"
            )
        self.home_card.configure(highlightbackground=BORDER, highlightcolor=BORDER)
        self.home_error.configure(text="")

    def _scope_selected(self) -> bool:
        return any([self.var_dirs.get(), self.var_registry.get(), self.var_startup.get()])

    def _start_scan(self) -> None:
        if self.scanner is not None:
            return
        if not self._scope_selected():
            self._show_home()
            self.home_card.configure(highlightbackground=DANGER, highlightcolor=DANGER)
            self.home_error.configure(text="请至少选择一项检测范围")
            self.status_var.set("请至少选择一项检测范围")
            return
        self.home_card.configure(highlightbackground=BORDER, highlightcolor=BORDER)
        self.home_error.configure(text="")

        keyword_mode = self.mode_var.get() == "关键词"
        keywords = (
            [item.strip() for item in re.split(r"[,，]", self.keyword_var.get()) if item.strip()]
            if keyword_mode
            else []
        )
        if keyword_mode and not keywords:
            self._show_home()
            self.home_card.configure(highlightbackground=DANGER, highlightcolor=DANGER)
            self.home_error.configure(text="请输入关键词，或切换到“不在程序列表中”")
            self.status_var.set("请输入关键词，或切换到“不在程序列表中”")
            self.keyword_entry.focus_set()
            return
        options = ScanOptions(
            keywords=keywords,
            scan_dirs=self.var_dirs.get(),
            scan_registry=self.var_registry.get(),
            scan_startup=self.var_startup.get(),
            detect_orphan=self.var_registry.get(),
            detect_unmatched=not keyword_mode,
            scan_drive_roots=self.var_drive.get(),
        )
        self.findings = []
        self.checked.clear()
        self.current = None
        self.cat_var.set("全部")
        self.risk_var.set("全部")
        self.filter_var.set("")
        self._build_category_chips()
        self._refresh()
        self._set_detail(None)
        self.scan_btn.configure(state="disabled")
        self.scanner = Scanner(options, progress=self._on_progress, log=self._on_log)
        self._show_results()
        self.status_var.set("正在扫描…")
        self._log("开始扫描" + (f"，关键词：{'、'.join(keywords)}" if keywords else "，未设置关键词"))
        threading.Thread(target=self._scan_worker, args=(self.scanner,), daemon=True).start()

    def _scan_worker(self, scanner: Scanner) -> None:
        try:
            findings = scanner.scan()
            self.queue.put(("scan_done", findings, None))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("scan_done", [], exc))

    def _on_progress(self, phase: str, detail: str, count: int) -> None:
        self.queue.put(("progress", phase, detail, count))

    def _on_log(self, message: str) -> None:
        self.queue.put(("log", message))

    def _poll_queue(self) -> None:
        while True:
            try:
                message = self.queue.get_nowait()
            except queue.Empty:
                break
            kind = message[0]
            if kind == "progress":
                _, phase, detail, count = message
                text = detail if len(detail) <= 90 else detail[:90] + "…"
                self.status_var.set(f"{phase}：{text}（已发现 {count} 项）")
                self._update_scan_phase(f"{phase}：{text}（已发现 {count} 项）")
            elif kind == "log":
                self._log(message[1])
            elif kind == "scan_done":
                self._handle_scan_done(message[1], message[2])
            elif kind == "remove_progress":
                _, index, total, name = message
                self.status_var.set(f"清理中 {index}/{total}：{name}")
            elif kind == "remove_done":
                self._handle_remove_done(message[1], message[2])
        self.root.after(120, self._poll_queue)

    def _handle_scan_done(self, findings: list[Finding], error: Exception | None) -> None:
        self.scanner = None
        self.scan_btn.configure(state="normal")
        if error is not None:
            self.status_var.set("扫描失败")
            self._log(f"扫描失败：{error}")
            messagebox.showerror("扫描失败", str(error))
            self._refresh()
            return
        self.findings = findings
        self.checked = {f.key for f in findings if f.risk == Risk.HIGH and f.deletable}
        self._refresh_home_back()
        self._build_category_chips()
        self._refresh()
        high = sum(1 for f in findings if f.risk == Risk.HIGH)
        if findings:
            self.status_var.set(f"扫描完成：共 {len(findings)} 项，高风险 {high} 项")
            self._log(f"扫描完成：共 {len(findings)} 项，高风险 {high} 项")
        else:
            self.status_var.set("扫描完成：未发现残留项")
            self._log("扫描完成：未发现残留项")

    # ---------- 结果渲染 ----------

    def _refresh(self) -> None:
        self._hide_tooltip()
        selected_key = self.current.key if self.current else None
        visible = self._visible_findings()
        self.tree.delete(*self.tree.get_children())
        self.by_iid.clear()
        for index, finding in enumerate(visible):
            iid = str(index)
            self.by_iid[iid] = finding
            mark = "☑" if finding.key in self.checked else "☐"
            tags = [self._tag_for(finding)]
            if index % 2 == 1:
                tags.append("stripe")
            status = finding.status or ("受保护" if finding.protected else "")
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    mark,
                    finding.category.value,
                    finding.name,
                    finding.location,
                    human_size(finding.size),
                    f"● {finding.risk.value}",
                    status,
                ),
                tags=tags,
            )
            if selected_key and finding.key == selected_key:
                self.tree.selection_set(iid)
        self._update_stats()
        self._update_actions()
        self._update_empty_state(visible)

    def _tag_for(self, finding: Finding) -> str:
        if finding.status == "已删除":
            return "deleted"
        if finding.protected:
            return "protected"
        return {Risk.HIGH: "high", Risk.MEDIUM: "medium", Risk.LOW: "low"}[finding.risk]

    def _visible_findings(self) -> list[Finding]:
        category = self.cat_var.get()
        risk = self.risk_var.get()
        text = self.filter_var.get().strip().lower()
        result = []
        for finding in self.findings:
            if category != "全部" and finding.category.value != category:
                continue
            if risk != "全部" and finding.risk.value != risk:
                continue
            if text:
                haystack = " ".join(
                    [finding.name, finding.location, " ".join(finding.reasons), " ".join(finding.keywords)]
                ).lower()
                if text not in haystack:
                    continue
            result.append(finding)
        result.sort(key=self._sort_key(), reverse=self.sort_reverse)
        return result

    def _sort_key(self):
        if self.sort_column == "size":
            return lambda f: f.size
        if self.sort_column == "risk":
            return lambda f: RISK_ORDER[f.risk]
        if self.sort_column == "category":
            return lambda f: f.category.value
        if self.sort_column == "name":
            return lambda f: f.name.lower()
        return lambda f: f.location.lower()

    def _sort_by(self, column: str) -> None:
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False
        self._refresh()

    def _update_stats(self) -> None:
        high = sum(1 for f in self.findings if f.risk == Risk.HIGH)
        protected = sum(1 for f in self.findings if f.protected)
        reclaimable = sum(f.size for f in self.findings if f.deletable)
        self.stat_values["total"].configure(text=str(len(self.findings)))
        self.stat_values["high"].configure(text=str(high), foreground=RISK_COLORS[Risk.HIGH] if high else TEXT)
        self.stat_values["size"].configure(text=human_size(reclaimable) if reclaimable else "—")
        self.stat_values["protected"].configure(text=str(protected))

    def _checked_count(self) -> int:
        return sum(1 for f in self.findings if f.key in self.checked and f.deletable)

    def _update_actions(self) -> None:
        count = self._checked_count()
        self.delete_btn.configure(
            text=f"删除选中项（{count}）" if count else "删除选中项",
            state="normal" if count and self.scanner is None else "disabled",
        )

    def _update_empty_state(self, visible: list[Finding] | None = None) -> None:
        if visible is None:
            visible = self._visible_findings()
        if self.scanner is not None:
            self._show_scan_overlay()
        elif not self.findings:
            self._show_empty("还没有扫描结果", "返回首页输入关键词后开始扫描")
        elif not visible:
            self._show_empty("没有匹配项", "调整风险、类别或搜索条件后再试")
        else:
            self.empty_progress.stop()
            self.empty_box.place_forget()

    def _show_empty(self, title: str, hint: str) -> None:
        self.empty_progress.stop()
        self.empty_progress.pack_forget()
        self.empty_phase.pack_forget()
        self.empty_title.configure(text=title)
        self.empty_hint.configure(text=hint)
        self.empty_hint.pack(pady=(s(6), 0))
        self.empty_box.place(relx=0.5, rely=0.42, anchor="center")

    def _show_scan_overlay(self) -> None:
        self.empty_title.configure(text="正在扫描…")
        self.empty_hint.configure(text="扫描在后台进行，完成后自动显示结果")
        self.empty_hint.pack(pady=(s(6), 0))
        self.empty_progress.pack(pady=(s(18), s(8)))
        self.empty_progress.start(12)
        if not self.empty_phase.winfo_ismapped():
            self.empty_phase.configure(text="准备中…")
            self.empty_phase.pack()
        self.empty_box.place(relx=0.5, rely=0.42, anchor="center")

    def _update_scan_phase(self, text: str) -> None:
        if self.scanner is not None:
            self.empty_phase.configure(text=text)

    # ---------- 类别筛选 ----------

    def _build_category_chips(self) -> None:
        for child in self.cat_chips.winfo_children():
            child.destroy()
        counts = Counter(f.category.value for f in self.findings)
        options = [("全部", len(self.findings))] + [
            (category.value, counts[category.value]) for category in Category if counts[category.value]
        ]
        labels = {label for label, _ in options}
        if self.cat_var.get() not in labels:
            self.cat_var.set("全部")
        for index, (label, count) in enumerate(options):
            variable = tk.BooleanVar(value=label == self.cat_var.get())
            chip = Chip(
                self.cat_chips,
                text=f"{label} {count}",
                variable=variable,
                command=lambda value=label: self._set_category(value),
            )
            chip.pack(side="left", padx=(0 if index == 0 else s(6), 0))

    def _set_category(self, label: str) -> None:
        self.cat_var.set("全部" if label == self.cat_var.get() else label)
        self._build_category_chips()
        self._refresh()

    # ---------- 勾选 ----------

    def _on_tree_click(self, event: tk.Event) -> str | None:
        self._hide_tooltip()
        if self.tree.identify("region", event.x, event.y) != "cell":
            return None
        if self.tree.identify_column(event.x) != "#1":
            return None
        iid = self.tree.identify_row(event.y)
        if iid:
            self._toggle_finding(self.by_iid.get(iid))
        return "break"

    def _block_column_drag(self, event: tk.Event) -> str | None:
        # 分隔线上按下鼠标时中止后续的列宽拖动
        if self.tree.identify("region", event.x, event.y) == "separator":
            return "break"
        return None

    def _update_hscroll(self, _event=None) -> None:
        # 列宽总和不超过可视区时隐藏横向滚动条
        width = self.tree.winfo_width()
        if width <= 1:
            return
        total = sum(int(self.tree.column(column, "width")) for column in self._columns)
        if total > width:
            self.tree_x.grid()
        else:
            self.tree_x.grid_remove()

    def _on_tree_hover(self, event: tk.Event) -> str | None:
        # 分隔线不显示列宽调整光标；仅对名称与位置列判断截断
        if self.tree.identify("region", event.x, event.y) == "separator":
            self._hide_tooltip()
            return "break"
        column_id = self.tree.identify_column(event.x)
        iid = self.tree.identify_row(event.y)
        if not iid or not column_id.startswith("#"):
            self._hide_tooltip()
            return
        try:
            column = self._columns[int(column_id[1:]) - 1]
        except (ValueError, IndexError):
            self._hide_tooltip()
            return
        if column not in ("name", "location"):
            self._hide_tooltip()
            return
        text = self.tree.set(iid, column)
        if not text:
            self._hide_tooltip()
            return
        width = int(self.tree.column(column, "width"))
        if self._tip_font.measure(text) + s(14) <= width:
            self._hide_tooltip()
            return
        state = (iid, column, text)
        if state == self._tip_state:
            return
        self._tip_state = state
        self._show_tooltip(text, event.x_root, event.y_root)

    def _show_tooltip(self, text: str, x_root: int, y_root: int) -> None:
        if self._tip is None:
            self._tip = tk.Toplevel(self.root)
            self._tip.withdraw()
            self._tip.overrideredirect(True)
            self._tip.attributes("-topmost", True)
            self._tip_label = tk.Label(
                self._tip,
                font=FONT,
                justify="left",
                anchor="w",
                background="#ffffe1",
                foreground=TEXT,
                relief="solid",
                borderwidth=1,
                padx=s(8),
                pady=s(4),
                wraplength=s(560),
            )
            self._tip_label.pack()
        if self._tip_label is None:
            return
        self._tip_label.configure(text=text)
        self._tip.update_idletasks()
        tip_w = self._tip.winfo_reqwidth()
        tip_h = self._tip.winfo_reqheight()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = x_root + s(16)
        y = y_root + s(22)
        if x + tip_w > screen_w:
            x = max(0, x_root - tip_w - s(10))
        if y + tip_h > screen_h:
            y = max(0, y_root - tip_h - s(10))
        self._tip.geometry(f"+{x}+{y}")
        self._tip.deiconify()

    def _hide_tooltip(self, _event=None) -> None:
        self._tip_state = None
        if self._tip is not None:
            self._tip.withdraw()

    def _toggle_current(self, _event=None) -> str:
        selection = self.tree.selection()
        if selection:
            self._toggle_finding(self.by_iid.get(selection[0]))
        return "break"

    def _toggle_finding(self, finding: Finding | None) -> None:
        if finding is None:
            return
        if not finding.deletable:
            self._log(f"{finding.location}：受保护或已删除，不可选择")
            return
        if finding.key in self.checked:
            self.checked.discard(finding.key)
        else:
            self.checked.add(finding.key)
        for iid, item in self.by_iid.items():
            if item is finding:
                values = list(self.tree.item(iid, "values"))
                values[0] = "☑" if finding.key in self.checked else "☐"
                self.tree.item(iid, values=values)
                break
        self._update_actions()

    def _select_all(self) -> None:
        for finding in self._visible_findings():
            if finding.deletable:
                self.checked.add(finding.key)
        self._refresh()

    def _unselect_all(self) -> None:
        for finding in self._visible_findings():
            self.checked.discard(finding.key)
        self._refresh()

    def _select_mid_low(self) -> None:
        for finding in self._visible_findings():
            if not finding.deletable:
                continue
            if finding.risk == Risk.HIGH:
                self.checked.discard(finding.key)
            else:
                self.checked.add(finding.key)
        self._refresh()

    def _invert_selection(self) -> None:
        for finding in self._visible_findings():
            if not finding.deletable:
                continue
            if finding.key in self.checked:
                self.checked.discard(finding.key)
            else:
                self.checked.add(finding.key)
        self._refresh()

    # ---------- 详情 ----------

    def _on_select(self, _event=None) -> None:
        selection = self.tree.selection()
        finding = self.by_iid.get(selection[0]) if selection else None
        self.current = finding
        self._set_detail(finding)

    def _set_detail(self, finding: Finding | None) -> None:
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        for name, config in (
            ("h2", {"font": FONT_BOLD, "foreground": "#37474f", "spacing1": s(10), "spacing3": s(3)}),
            ("value", {"foreground": TEXT}),
            ("mono", {"font": MONO_FONT, "foreground": "#37474f", "lmargin1": s(2)}),
            ("bullet", {"foreground": "#37474f", "lmargin1": s(8), "lmargin2": s(8)}),
            ("muted", {"foreground": MUTED}),
        ):
            self.detail_text.tag_configure(name, **config)

        if finding is None:
            self.detail_name.configure(text="未选择项目")
            self.detail_risk_pill.pack_forget()
            self.detail_cat_pill.pack_forget()
            for value in self._meta_values.values():
                value.configure(text="—")
            self.detail_text.insert(
                "end", "在左侧列表中选择一行，这里会显示该项目的判定依据与处理方式。", "muted"
            )
            self.detail_text.configure(state="disabled")
            self.open_btn.configure(state="disabled")
            self.copy_btn.configure(state="disabled")
            return

        self.detail_name.configure(text=finding.name)
        self.detail_risk_pill.configure(
            text=f"{finding.risk.value}风险",
            background=RISK_TINTS[finding.risk],
            foreground=RISK_COLORS[finding.risk],
        )
        self.detail_risk_pill.pack(side="left")
        self.detail_cat_pill.configure(text=finding.category.value)
        self.detail_cat_pill.pack(side="left", padx=(s(6), 0))

        self._meta_values["大小"].configure(text=human_size(finding.size))
        self._meta_values["处理方式"].configure(text=finding.action.value)
        if finding.protected:
            reason = f"（{finding.protected_reason}）" if finding.protected_reason else ""
            self._meta_values["状态"].configure(text=f"受保护，不可删除{reason}")
        else:
            self._meta_values["状态"].configure(text=finding.status or "可删除")

        self.detail_text.insert("end", "位置\n", "h2")
        self.detail_text.insert("end", f"{finding.location}\n", "mono")
        if finding.reasons:
            self.detail_text.insert("end", "判定依据\n", "h2")
            for reason in finding.reasons:
                self.detail_text.insert("end", f"• {reason}\n", "bullet")
        if finding.keywords:
            self.detail_text.insert("end", "命中关键词\n", "h2")
            self.detail_text.insert("end", f"{'、'.join(finding.keywords)}\n", "value")
        if finding.target:
            self.detail_text.insert("end", "关联目标\n", "h2")
            self.detail_text.insert("end", f"{finding.target}\n", "mono")
        self.detail_text.configure(state="disabled")
        self.detail_text.yview_moveto(0)
        self.open_btn.configure(state="normal")
        self.copy_btn.configure(state="normal")

    def _copy_path(self) -> None:
        if self.current is None:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.current.location)
        self.status_var.set("路径已复制到剪贴板")

    def _open_location(self) -> None:
        if self.current is None:
            return
        finding = self.current
        try:
            if finding.action.value == "移入回收站" and os.path.exists(finding.location):
                if os.path.isdir(finding.location):
                    open_path(finding.location)
                else:
                    subprocess.Popen(["explorer.exe", "/select,", finding.location])
            else:
                self._copy_path()
                self._log("注册表项路径已复制，可在 regedit 中定位")
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc))

    # ---------- 删除 ----------

    def _delete_checked(self) -> None:
        if self.scanner is not None:
            return
        selected = [f for f in self.findings if f.key in self.checked and f.deletable]
        if not selected:
            messagebox.showinfo("提示", "请先勾选要删除的项。")
            return
        problems = self.remover.validate(selected)
        if problems:
            preview = "\n".join(problems[:10])
            if len(problems) > 10:
                preview += f"\n… 另有 {len(problems) - 10} 项"
            messagebox.showwarning("存在不可删除项", preview)
            return
        needs_backup = any(f.action != ActionKind.RECYCLE_FILE for f in selected)
        backup_dir = ""
        if needs_backup:
            parent = self._ask_backup_parent(selected)
            if not parent:
                self.status_var.set("已取消清理")
                return
            self.backup_parent = parent
            backup_dir = str(plan_backup_dir(parent))
        counts = Counter(f.action.value for f in selected)
        total_size = sum(f.size for f in selected)
        backup_note = (
            f"注册表项、服务与计划任务会先备份到：\n{backup_dir}"
            if backup_dir
            else "本次只移入回收站，不生成备份文件。"
        )
        lines = [f"共 {len(selected)} 项，约 {human_size(total_size)}："]
        lines.extend(f"  · {action}：{count} 项" for action, count in counts.items())
        lines.extend(
            [
                "",
                "文件与快捷方式将移入回收站，可从回收站还原。",
                backup_note,
                "",
                "确认执行？",
            ]
        )
        if not messagebox.askyesno("确认删除", "\n".join(lines), icon="warning"):
            return
        self.delete_btn.configure(state="disabled")
        self.scan_btn.configure(state="disabled")
        self.back_btn.configure(state="disabled")
        self.status_var.set("正在清理…")
        threading.Thread(
            target=self._remove_worker, args=(selected, backup_dir), daemon=True
        ).start()

    def _ask_backup_parent(self, selected: list[Finding]) -> str:
        initial = self.backup_parent or os.path.expanduser("~")
        while True:
            parent = filedialog.askdirectory(title="选择备份保存位置", initialdir=initial, mustexist=False)
            if not parent:
                return ""
            parent = os.path.normpath(parent)
            blocked = [
                f.location
                for f in selected
                if f.action == ActionKind.RECYCLE_FILE and paths.is_under(parent, f.location)
            ]
            if blocked:
                messagebox.showwarning(
                    "备份位置不可用", f"备份位置不能位于待删除的目录内：\n{blocked[0]}"
                )
                initial = os.path.dirname(parent) or initial
                continue
            return parent

    def _remove_worker(self, selected: list[Finding], backup_dir: str) -> None:
        try:
            summary = self.remover.remove(
                selected,
                backup_dir,
                progress=lambda index, total, result: self.queue.put(
                    ("remove_progress", index, total, result.finding.name)
                ),
            )
            self.queue.put(("remove_done", summary, None))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("remove_done", None, exc))

    def _handle_remove_done(self, summary, error: Exception | None) -> None:
        self.scan_btn.configure(state="normal")
        self.back_btn.configure(state="normal")
        if error is not None:
            self.status_var.set("清理失败")
            self._log(f"清理失败：{error}")
            messagebox.showerror("清理失败", str(error))
            self._refresh()
            return
        for result in summary.results:
            flag = "成功" if result.ok else "失败"
            self._log(f"{flag}：{result.finding.location}" + (f"（{result.message}）" if not result.ok else ""))
        self.checked = {key for key in self.checked if any(f.key == key and f.deletable for f in self.findings)}
        self._build_category_chips()
        self._refresh()
        self.status_var.set(f"清理完成：成功 {summary.succeeded} 项，失败 {summary.failed} 项")
        if summary.backup_dir:
            message = (
                f"成功 {summary.succeeded} 项，失败 {summary.failed} 项。\n\n"
                f"备份目录：\n{summary.backup_dir}\n\n"
                "文件已移入回收站，可从回收站还原；注册表项可双击备份的 .reg 文件恢复。"
            )
        else:
            message = (
                f"成功 {summary.succeeded} 项，失败 {summary.failed} 项。\n\n"
                "本次只移入回收站，未生成备份文件，可从回收站还原。"
            )
        messagebox.showinfo("清理结果", message)

    # ---------- 辅助 ----------

    def _show_context_menu(self, event: tk.Event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self._on_select()
        self.context_menu.tk_popup(event.x_root, event.y_root)

    def _open_readme(self) -> None:
        target = resource_path("README.md")
        if target.exists():
            open_path(str(target))
        else:
            messagebox.showinfo("提示", "未找到 README.md")

    def _about(self) -> None:
        messagebox.showinfo(
            "关于",
            f"uninstall-cleaner v{__version__}\n\n"
            "扫描安装目录、用户数据目录、注册表与启动项中的卸载残留。\n"
            "文件删除均进入回收站，注册表与服务删除前备份到所选目录。",
        )

    def _open_backups(self) -> None:
        target = self.remover.last_backup_dir
        if target and os.path.isdir(target):
            open_path(target)
            return
        messagebox.showinfo(
            "提示",
            "还没有备份记录。\n\n清理注册表项、服务或计划任务时，程序会先询问备份保存位置。",
        )

    def _open_recycle_bin(self) -> None:
        subprocess.Popen(["explorer.exe", "shell:RecycleBinFolder"])

    def _update_admin_state(self) -> None:
        if elevation.is_admin():
            self.admin_pill.configure(text="管理员权限", foreground=OK, background=OK_TINT)
        else:
            self.admin_pill.configure(text="标准用户（需提权）", foreground=WARN, background=WARN_TINT)

    def _elevate(self) -> None:
        if elevation.is_admin():
            messagebox.showinfo("提示", "当前已是管理员权限。")
            return
        if not messagebox.askyesno("以管理员身份重启", "将通过 UAC 重新启动本工具，当前窗口会关闭。是否继续？"):
            return
        ok, message = elevation.relaunch_as_admin()
        if ok:
            self.root.destroy()
        else:
            messagebox.showerror("提权失败", message)

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.scanner is not None:
            self.scanner.cancel()
        self.root.destroy()


def enable_window(root: tk.Tk) -> None:
    """按屏幕尺寸设定窗口大小并居中。"""
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    width = min(s(1560), max(s(1180), screen_w - s(80)))
    height = min(s(960), max(s(760), screen_h - s(120)))
    left = max(0, (screen_w - width) // 2)
    top = max(0, (screen_h - height) // 3)
    root.geometry(f"{width}x{height}+{left}+{top}")
    root.minsize(min(s(1180), width), min(s(760), height))


def main() -> int:
    root = tk.Tk()
    root.title("uninstall-cleaner")
    SC = root.winfo_fpixels("1p") / (96.0 / 72.0)
    globals()["SC"] = SC
    enable_window(root)
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app._on_close)
    root.mainloop()
    return 0
