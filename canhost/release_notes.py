"""版本说明的唯一解析实现：从 ``CHANGELOG.md`` 和 Release 正文取出面向用户的条目。

三处调用都走这里，避免同一份说明出现多种解释：

- ``scripts/release_notes.py`` 生成 GitHub Release 说明和打包进程序的 ``release_info.py``；
- ``canhost.updater`` 从 Release 正文里取出更新弹窗要显示的条目；
- ``canhost.app`` 在源码运行时回退读取仓库 ``CHANGELOG.md``。

本模块不导入包内其他模块，只做纯文本处理。
"""
from __future__ import annotations

import re
from pathlib import Path

# ``## v0.9.5 — 2026-09-13``；``## 未发布`` 这类无版本号的小节不会被匹配。
RELEASE_HEADING = re.compile(
    r"^##\s+v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z][0-9A-Za-z.\-]*)?)"
    r"\s*(?:[—–-]\s*(\d{4}-\d{2}-\d{2}))?\s*$",
    re.MULTILINE,
)
# 任意二级或三级标题，用来切分 Release 正文里的小节。
ANY_HEADING = re.compile(r"^#{2,3}\s+\S.*$", re.MULTILINE)
# 更新弹窗只显示变更条目所在的这一节。
UPDATE_SECTION = re.compile(
    r"^#{2,3}\s*(?:本次更新|更新内容|变更内容|更新说明|What'?s changed|Changes)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
BULLET = re.compile(r"^-\s+(.+?)\s*$")

# 仓库根的 ``CHANGELOG.md``：冻结包里不存在，源码运行和测试用它兜底。
CHANGELOG_PATH = Path(__file__).resolve().parents[1] / "CHANGELOG.md"


def _version_key(version: str) -> tuple[int, int, int, tuple]:
    """按 ``X.Y.Z[-pre]`` 排序；与 ``canhost.updater.version_key`` 语义一致。

    这里单独实现是为了不引入反向依赖：``updater`` 会导入本模块。
    """
    text = version.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z][0-9A-Za-z.\-]*))?", text)
    if not match:
        return (0, 0, 0, ())
    major, minor, patch = (int(match.group(index)) for index in (1, 2, 3))
    label = match.group(4)
    if label is None:
        return (major, minor, patch, (1,))
    parts: list[tuple[int, int | str]] = []
    for part in re.split(r"(\d+)", label):
        if not part:
            continue
        parts.append((0, int(part)) if part.isdigit() else (1, part.lower()))
    return (major, minor, patch, (0, tuple(parts)))


def section_notes(body: str) -> list[str]:
    """取一节里没有缩进的条目文字，忽略标题和正文段落。"""
    notes: list[str] = []
    for line in str(body or "").splitlines():
        match = BULLET.match(line)
        if match is None:
            continue
        value = match.group(1).strip()
        if value:
            notes.append(value)
    return notes


def release_sections(text: str) -> list[dict[str, object]]:
    """按出现顺序列出 ``CHANGELOG.md`` 中所有带版本号的小节。"""
    matches = list(RELEASE_HEADING.finditer(str(text or "")))
    sections: list[dict[str, object]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end].strip()
        sections.append({
            "version": match.group(1),
            "date": match.group(2) or "",
            "body": body,
            "notes": section_notes(body),
        })
    return sections


def newest_release(text: str) -> dict[str, object] | None:
    """返回版本号最高的一节；文件里没有版本小节时返回 ``None``。"""
    sections = release_sections(text)
    if not sections:
        return None
    return max(sections, key=lambda section: _version_key(str(section["version"])))


def notes_for_version(text: str, version: str) -> list[str]:
    """取指定版本的条目；``v0.9.5-rc1`` 这类预发布标签回落到 ``0.9.5`` 一节。"""
    wanted = [version.strip().lstrip("vV"), version.strip().lstrip("vV").split("-")[0]]
    for section in release_sections(text):
        if str(section["version"]) in wanted:
            return [str(note) for note in section["notes"]]
    return []


def release_date_for_version(text: str, version: str) -> str:
    """取指定版本的日期；找不到时返回空字符串。"""
    wanted = [version.strip().lstrip("vV"), version.strip().lstrip("vV").split("-")[0]]
    for section in release_sections(text):
        if str(section["version"]) in wanted:
            return str(section["date"])
    return ""


def release_notes_from_body(body: str, limit: int = 12) -> list[str]:
    """从 Release 正文取出更新弹窗要显示的条目。

    优先取“本次更新”小节，其次取第一节，最后退回整篇的顶层条目。旧版发布只有
    安装说明段落、没有任何条目时返回空列表，由调用方回退显示原始正文。
    """
    text = str(body or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    hint = UPDATE_SECTION.search(text)
    if hint is not None:
        rest = text[hint.end():]
        following = ANY_HEADING.search(rest)
        notes = section_notes(rest[:following.start()] if following else rest)
        if notes:
            return notes[:limit]
    first = ANY_HEADING.search(text)
    if first is not None:
        rest = text[first.end():]
        following = ANY_HEADING.search(rest)
        notes = section_notes(rest[:following.start()] if following else rest)
        if notes:
            return notes[:limit]
    return section_notes(text)[:limit]


def base_version(version: str) -> str:
    """去掉 ``v`` 前缀和 ``-rc1`` 这类预发布后缀。"""
    return str(version or "").strip().lstrip("vV").split("-")[0]


def version_sort_key(version: str) -> tuple[int, int, int, tuple]:
    """``vX.Y.Z`` 的排序键；无法解析的版本排在所有可解析版本之后。"""
    return _version_key(version)


def changelog_text() -> str:
    """读取仓库 ``CHANGELOG.md``；冻结包或文件缺失时返回空字符串。"""
    try:
        return CHANGELOG_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def embedded_release() -> dict[str, object]:
    """随包发布的当前版本说明（构建脚本生成 ``canhost/release_info.py``）。"""
    try:
        from .release_info import RELEASE_DATE, RELEASE_NOTES, RELEASE_VERSION
    except Exception:  # 源码运行或未生成时没有这个模块
        return {"version": "", "date": "", "notes": []}
    return {
        "version": str(RELEASE_VERSION),
        "date": str(RELEASE_DATE),
        "notes": [str(note) for note in RELEASE_NOTES if str(note).strip()],
    }


def embedded_history() -> list[dict[str, object]]:
    """随包发布的版本历史（新到旧），供离线浏览版本说明。"""
    try:
        from .release_info import RELEASE_HISTORY
    except Exception:
        return []
    entries: list[dict[str, object]] = []
    for item in RELEASE_HISTORY or []:
        if not isinstance(item, dict):
            continue
        version = str(item.get("version") or "")
        if not version:
            continue
        entries.append({
            "version": version,
            "date": str(item.get("date") or ""),
            "notes": [str(note) for note in (item.get("notes") or []) if str(note).strip()],
        })
    return entries


def notes_for_release(version: str, cached: list[str] | None = None) -> tuple[list[str], str]:
    """某个已安装版本的用户可见条目及其来源。

    依次尝试：更新检查时缓存的 Release 正文条目、随包说明、仓库
    ``CHANGELOG.md``。安装包升级和软件内更新都会命中其中之一，不会出现
    “没有更新说明”的空弹窗。
    """
    wanted = base_version(version)
    if not wanted:
        return [], ""
    if cached:
        notes = [str(note) for note in cached if str(note).strip()]
        if notes:
            return notes, "cached"
    embedded = embedded_release()
    if base_version(str(embedded["version"])) == wanted and embedded["notes"]:
        return [str(note) for note in embedded["notes"]], "embedded"
    text = changelog_text()
    if text:
        notes = notes_for_version(text, wanted)
        if notes:
            return notes, "changelog"
    return [], ""


def release_date_for(version: str) -> str:
    """发布说明里记录的日期，未知时返回空字符串。"""
    embedded = embedded_release()
    if base_version(str(embedded["version"])) == base_version(version) and embedded["date"]:
        return str(embedded["date"])
    text = changelog_text()
    return release_date_for_version(text, version) if text else ""


def history(limit: int = 40) -> list[dict[str, object]]:
    """离线版本历史，新到旧：优先随包数据，源码运行时用仓库 ``CHANGELOG.md``。

    打包版本不再依赖仓库文件，因此“查看历史版本”在任何安装方式下都能打开。
    """
    entries = embedded_history()
    if not entries:
        text = changelog_text()
        entries = [
            {
                "version": str(section["version"]),
                "date": str(section["date"]),
                "notes": [str(note) for note in section["notes"]],
            }
            for section in release_sections(text)
            if section["notes"]
        ] if text else []
    ordered = sorted(entries, key=lambda item: _version_key(str(item["version"])), reverse=True)
    return ordered[: max(1, int(limit))]
