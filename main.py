"""源码兼容入口：桌面快捷方式直接运行本文件。"""

from __future__ import annotations

import sys

from uninstall_cleaner.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
