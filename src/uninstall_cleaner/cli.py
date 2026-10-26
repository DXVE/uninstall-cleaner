"""命令行自检模式：无界面执行扫描并输出结果。"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .models import Finding, ScanOptions, human_size
from .scanner import Scanner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uninstall-cleaner", description="卸载残留检测（命令行模式）")
    parser.add_argument("--version", "-V", action="version", version=f"%(prog)s v{__version__}")
    parser.add_argument("--cli", action="store_true", help="启用命令行模式")
    parser.add_argument("--keyword", "-k", default="", help="关键词，多个用逗号分隔")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--no-unmatched", action="store_true", help="关闭已安装程序对比")
    parser.add_argument("--no-orphan", action="store_true", help="关闭孤儿卸载项检测")
    parser.add_argument("--no-startup", action="store_true", help="关闭启动项/服务/任务/快捷方式检测")
    parser.add_argument("--drive-roots", action="store_true", help="额外扫描磁盘根目录一级目录")
    parser.add_argument("--limit", type=int, default=0, help="最多输出条数，0 表示不限制")
    return parser


def _to_dict(finding: Finding) -> dict:
    return {
        "category": finding.category.value,
        "name": finding.name,
        "location": finding.location,
        "risk": finding.risk.value,
        "action": finding.action.value,
        "size": finding.size,
        "size_text": human_size(finding.size),
        "protected": finding.protected,
        "protected_reason": finding.protected_reason,
        "keywords": finding.keywords,
        "reasons": finding.reasons,
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    keywords = [item.strip() for item in args.keyword.replace("，", ",").split(",") if item.strip()]
    options = ScanOptions(
        keywords=keywords,
        detect_orphan=not args.no_orphan,
        detect_unmatched=not args.no_unmatched,
        scan_startup=not args.no_startup,
        scan_drive_roots=args.drive_roots,
    )
    scanner = Scanner(options, log=lambda message: print(message, file=sys.stderr))
    findings = scanner.scan()
    if args.limit > 0:
        findings = findings[: args.limit]
    if args.json:
        print(json.dumps([_to_dict(f) for f in findings], ensure_ascii=False, indent=2))
        return 0
    for finding in findings:
        flag = " [受保护]" if finding.protected else ""
        print(
            f"[{finding.risk.value}] {finding.category.value} | {finding.name} | "
            f"{finding.location} | {human_size(finding.size)}{flag}"
        )
        for reason in finding.reasons:
            print(f"    - {reason}")
    print(f"共 {len(findings)} 项", file=sys.stderr)
    return 0
