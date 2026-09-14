#!/usr/bin/env python3
"""由 ``CHANGELOG.md`` 生成 GitHub Release 说明和打包进程序的更新说明 JSON。

Release 正文、软件内更新的“版本说明”和更新完成弹窗共用同一份条目，避免同一
版本在三处各写一遍后互相不一致。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canhost import release_notes as notes_source  # noqa: E402  （脚本按仓库根注入路径）


def _app_links(version: str) -> str:
    return (
        "## 下载\n\n"
        f"- Windows 安装版：`BITFSAE_CAN_Host_v{version}_setup.exe`"
        "（每用户安装，可从桌面/开始菜单启动）\n"
        f"- Windows 便携版：`BITFSAE_CAN_Host_v{version}.zip`"
        "（保留完整目录结构，不能只复制 exe）\n"
        f"- Apple Silicon macOS：`BITFSAE_CAN_Host_macOS_arm64_v{version}.dmg`\n\n"
        "同目录的 `.sha256` 用于校验下载完整性；已安装的 Windows 版本可直接使用"
        "软件内更新，macOS 请用 DMG 覆盖安装。\n"
        "\n## 安装与依赖\n\n"
        "- Windows 10/11：安装 PEAK PCAN 驱动与 PCAN-Basic；安装器与便携包都用于"
        "实体 PCAN-USB 联调。\n"
        "- Apple Silicon macOS 12+：连接实体 PCAN-USB 前安装 MacCAN `libPCBUSB` 0.13+"
        "（含 arm64），并先用 MacCAN Monitor 验证适配器收发。\n"
        "- macOS 包为团队内部 ad-hoc 签名，未做 Apple 公证；首次启动如被 Gatekeeper"
        "拦截，请在“系统设置 → 隐私与安全性”中确认打开。\n"
        "- 源码运行或 macOS 过渡版本保留的调试模拟不经过驱动、适配器和实体总线，"
        "不能作为车辆或台架验收结果。\n"
    )


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
    parser = argparse.ArgumentParser(description="生成上位机 Release 说明")
    parser.add_argument("--version", required=True, help="版本号，可带 v 前缀")
    parser.add_argument("--changelog", default=str(ROOT / "CHANGELOG.md"))
    parser.add_argument("--output", required=True, help="GitHub Release 说明 Markdown")
    parser.add_argument("--json-output", required=True, help="更新弹窗使用的条目 JSON")
    args = parser.parse_args(argv)

    version = notes_source.base_version(args.version)
    if not version:
        raise SystemExit(f"无法识别的版本号：{args.version}")
    changelog = Path(args.changelog)
    text = changelog.read_text(encoding="utf-8") if changelog.exists() else ""
    section = next(
        (item for item in notes_source.release_sections(text)
         if str(item["version"]) == version),
        None,
    )
    notes = [str(note) for note in (section or {}).get("notes") or []]
    date = notes_source.release_date_for_version(text, version) if text else ""
    heading = f"# BITFSAE CAN Host {version}" + (f" · {date}" if date else "")
    if notes:
        body = "\n".join(f"- {note}" for note in notes)
    else:
        # 宁愿在 Release 里说清楚，也不要发布一份没有内容的说明。
        print(f"warning: CHANGELOG.md 中没有 {version} 的条目", file=sys.stderr)
        body = "- 本版本没有登记面向用户的变更条目，请以提交记录为准。"
    markdown = f"{heading}\n\n## 本次更新\n\n{body}\n\n{_app_links(version)}"

    Path(args.output).write_text(markdown, encoding="utf-8")
    Path(args.json_output).write_text(
        json.dumps(notes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.output}（{len(notes)} 条更新说明）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
