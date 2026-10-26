# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件、无控制台窗口，内嵌 README，产物名带版本号。"""

import re
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"

# 版本号唯一来源为包 __init__，与之保持同名规则以免 tag 与产物名不一致
VERSION = re.search(
    r'__version__\s*=\s*"([^"]+)"',
    (SRC / "uninstall_cleaner" / "__init__.py").read_text(encoding="utf-8"),
).group(1)

a = Analysis(
    [str(SRC / "uninstall_cleaner" / "__main__.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[(str(ROOT / "README.md"), ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=f"uninstall-cleaner-v{VERSION}",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
