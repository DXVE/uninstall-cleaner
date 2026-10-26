"""程序入口：DPI 适配、命令行与界面模式分派。"""

from __future__ import annotations

import ctypes
import sys


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    enable_dpi_awareness()
    if "--cli" in args or "--version" in args or "-V" in args:
        from uninstall_cleaner.cli import main as cli_main

        return cli_main(args)
    from uninstall_cleaner.ui import main as ui_main

    return ui_main()


if __name__ == "__main__":
    sys.exit(main())
