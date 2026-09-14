#!/usr/bin/env python3
"""把当前版本与面向用户的更新说明写进 `canhost/release_info.py`。

发布链路里只调用这一个脚本落盘版本信息；它同时更新
`build_windows.ps1` / `build_macos.sh` 默认使用的 `canhost/__init__.py`，避免标签、
程序版本和更新说明出现不一致。

更新说明的来源与 `scripts/release_notes.py` 完全一致：`CHANGELOG.md` 里版本号最高的
那一节。没有条目的版本会写入空列表，弹窗改为提示查看 Release 页面。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canhost import release_notes  # noqa: E402  （脚本按仓库根注入路径）

# 打包进程序的版本历史条数：足够在离线状态下回看最近若干次更新。
HISTORY_LIMIT = 40


def _version_pattern() -> re.Pattern[str]:
    return re.compile(r'^__version__ = "[^"]+"', re.MULTILINE)


def _configure_stdout() -> None:
    """控制台编码装不下中文时（英文 Windows、CI）改用 UTF-8，装得下就保持原样。

    Windows 的 cp936 控制台能正常显示中文，不需要改；cp1252 会直接抛
    UnicodeEncodeError，必须换成 UTF-8 并替换无法编码的字符。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        encoding = getattr(stream, "encoding", None) or "utf-8"
        try:
            "中文输出".encode(encoding)
        except (UnicodeEncodeError, LookupError):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # pragma: no cover - 环境不支持时保持原样
                pass


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    parser = argparse.ArgumentParser(description="写入上位机版本与更新说明")
    parser.add_argument("version", help="版本号，可带 v 前缀，如 v0.9.6")
    parser.add_argument("--date", default="", help="发布日期 YYYY-MM-DD；留空取 CHANGELOG 或今天")
    parser.add_argument("--changelog", default=str(ROOT / "CHANGELOG.md"))
    parser.add_argument("--package-root", default=str(ROOT),
                        help="仓库根目录；测试用它写入临时副本，不碰工作区文件")
    args = parser.parse_args(argv)
    root = Path(args.package_root).resolve()

    version = release_notes.base_version(args.version)
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"无法识别的版本号：{args.version}")
    changelog = Path(args.changelog)
    text = changelog.read_text(encoding="utf-8") if changelog.exists() else ""
    notes = release_notes.notes_for_version(text, version) if text else []
    date = args.date or (release_notes.release_date_for_version(text, version) if text else "")
    if not date:
        from datetime import date as today
        date = today.today().isoformat()

    init_file = root / "canhost" / "__init__.py"
    init_text = init_file.read_text(encoding="utf-8")
    if _version_pattern().search(init_text) is None:
        raise SystemExit(f"{init_file} 中没有 __version__ 定义")
    init_text = _version_pattern().sub(f'__version__ = "{version}"', init_text, count=1)
    init_text = re.sub(
        r'^__version_date__ = "[^"]+"',
        f'__version_date__ = "{date}"',
        init_text,
        count=1,
        flags=re.MULTILINE,
    )
    init_file.write_text(init_text, encoding="utf-8")

    release_info = root / "canhost" / "release_info.py"
    history = [
        {
            "version": str(section["version"]),
            "date": str(section["date"]),
            "notes": [str(note) for note in section["notes"]],
        }
        for section in release_notes.release_sections(text)
        if section["notes"]
    ][:HISTORY_LIMIT]
    release_info.write_text(
        '"""由 scripts/set_version.py 生成的当前版本发布说明。"""\n\n'
        f"RELEASE_VERSION = {version!r}\n"
        f"RELEASE_DATE = {date!r}\n"
        f"RELEASE_NOTES = {json.dumps(notes, ensure_ascii=False, indent=2)}\n\n"
        "# 离线浏览用的版本历史，新到旧；只包含有面向使用者条目的版本。\n"
        f"RELEASE_HISTORY = {json.dumps(history, ensure_ascii=False, indent=2)}\n",
        encoding="utf-8",
    )
    print(f"set {init_file.relative_to(root)} to {version}（{date}）")
    print(f"set {release_info.relative_to(root)} embedded release notes ({len(notes)} items)")
    if not notes:
        print(f"warning: CHANGELOG.md 中没有 {version} 的条目，弹窗将提示查看 Release 页面")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
