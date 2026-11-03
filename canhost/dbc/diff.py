"""Semantic and textual differences between two states of one DBC.

The semantic list is what the "review before saving" dialog shows: which
messages and signals were added, removed or changed and which fields moved.
"""

from __future__ import annotations

import difflib
from typing import Any

_MESSAGE_FIELDS = ("name", "id_text", "dlc", "senders", "cycle_ms", "comment", "attrs", "signal_groups")
_SIGNAL_FIELDS = ("start", "length", "byte_order", "signed", "value_type", "factor", "offset", "minimum",
                  "maximum", "unit", "receivers", "mux", "comment", "choices", "initial_raw", "attrs")
FIELD_LABELS = {
    "name": "名称", "id_text": "ID", "dlc": "DLC", "senders": "发送节点", "cycle_ms": "周期 ms",
    "comment": "注释", "start": "起始位", "length": "长度", "byte_order": "字节序", "signed": "有符号",
    "value_type": "数值类型", "factor": "因子", "offset": "偏移", "minimum": "最小值", "maximum": "最大值",
    "unit": "单位", "receivers": "接收节点", "mux": "多路复用", "choices": "值表", "initial_raw": "初始原始值",
    "attrs": "属性", "signal_groups": "信号组",
}


def _enum_labels(*catalogs: dict[str, Any]) -> dict[str, list[str]]:
    """ENUM attribute names to their option labels; later catalogs win."""
    labels: dict[str, list[str]] = {}
    for catalog in catalogs:
        for definition in catalog.get("attr_defs", []):
            if definition["type"] == "ENUM":
                labels[definition["name"]] = definition["choices"]
    return labels


def _show_attr(name: str, value: Any, labels: dict[str, list[str]]) -> str:
    """ENUM values are stored as an index; show the label next to it."""
    options = labels.get(name)
    if options and isinstance(value, int) and 0 <= value < len(options):
        return f"{options[value]}({value})"
    return _show("", value)


def _show(field: str, value: Any, labels: dict[str, list[str]] | None = None) -> str:
    if value is None:
        return "—"
    if field == "choices":
        return ", ".join(f"{c['value']}={c['label']}" for c in value) or "—"
    if field == "attrs":
        return ", ".join(f"{name}={_show_attr(name, item, labels or {})}"
                         for name, item in sorted(value.items())) or "—"
    if field == "signal_groups":
        return ", ".join(f"{g['name']}(×{g['repetitions']}): {' '.join(g['signals'])}" for g in value) or "—"
    if field == "mux":
        text = value["role"]
        if value.get("value") is not None:
            text += f" {value['value']}"
        if value.get("ext"):
            text += f" 扩展:{value['ext']['signal']}"
        return text
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, str):
        return value if value else "—"
    return f"{value:g}" if isinstance(value, float) else str(value)


def _changes(old: dict[str, Any], new: dict[str, Any], fields: tuple[str, ...],
             labels: dict[str, list[str]] | None = None) -> list[dict[str, str]]:
    return [
        {"field": FIELD_LABELS.get(name, name), "old": _show(name, old.get(name), labels),
         "new": _show(name, new.get(name), labels)}
        for name in fields if old.get(name) != new.get(name)
    ]


def semantic_diff(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    labels = _enum_labels(old, new)
    database_changes = _changes(old, new, ("attrs",), labels)
    if database_changes:
        records.append({"scope": "database", "op": "change", "target": "数据库属性", "changes": database_changes})
    old_nodes = {n["name"]: n for n in old["nodes"]}
    new_nodes = {n["name"]: n for n in new["nodes"]}
    for name in new_nodes.keys() - old_nodes.keys():
        records.append({"scope": "node", "op": "add", "target": name, "changes": []})
    for name in old_nodes.keys() - new_nodes.keys():
        records.append({"scope": "node", "op": "delete", "target": name, "changes": []})
    for name in new_nodes.keys() & old_nodes.keys():
        changes = _changes(old_nodes[name], new_nodes[name], ("comment", "attrs"), labels)
        if changes:
            records.append({"scope": "node", "op": "change", "target": name, "changes": changes})

    old_messages = {m["key"]: m for m in old["messages"]}
    new_messages = {m["key"]: m for m in new["messages"]}
    # A message whose ID moved keeps its name: report one change, not add + delete.
    moved: dict[str, str] = {}
    by_new_name = {m["name"]: k for k, m in new_messages.items() if k not in old_messages}
    for key, message in old_messages.items():
        if key not in new_messages and message["name"] in by_new_name:
            moved[key] = by_new_name[message["name"]]
    matched_new = set(moved.values())

    for key, message in old_messages.items():
        if key not in new_messages and key not in moved:
            records.append({"scope": "message", "op": "delete", "target": message["name"],
                            "detail": f"{len(message['signals'])} 个信号", "changes": []})
    for key, message in new_messages.items():
        if key not in old_messages and key not in matched_new:
            records.append({"scope": "message", "op": "add", "target": message["name"],
                            "detail": f"{len(message['signals'])} 个信号", "changes": []})

    pairs = [(key, key) for key in old_messages if key in new_messages] + list(moved.items())
    for old_key, new_key in pairs:
        before, after = old_messages[old_key], new_messages[new_key]
        changes = _changes(before, after, _MESSAGE_FIELDS, labels)
        if changes:
            records.append({"scope": "message", "op": "change", "target": after["name"], "changes": changes})
        old_signals = {s["name"]: s for s in before["signals"]}
        new_signals = {s["name"]: s for s in after["signals"]}
        for name in old_signals.keys() - new_signals.keys():
            records.append({"scope": "signal", "op": "delete", "message": after["name"], "target": name, "changes": []})
        for name in new_signals.keys() - old_signals.keys():
            records.append({"scope": "signal", "op": "add", "message": after["name"], "target": name, "changes": []})
        for name in new_signals.keys() & old_signals.keys():
            changes = _changes(old_signals[name], new_signals[name], _SIGNAL_FIELDS, labels)
            if changes:
                records.append({"scope": "signal", "op": "change", "message": after["name"], "target": name,
                                "changes": changes})
    old_tables = {t["name"]: t for t in old["value_tables"]}
    new_tables = {t["name"]: t for t in new["value_tables"]}
    for name in sorted(old_tables.keys() | new_tables.keys()):
        if name not in old_tables:
            records.append({"scope": "table", "op": "add", "target": name, "changes": []})
        elif name not in new_tables:
            records.append({"scope": "table", "op": "delete", "target": name, "changes": []})
        elif old_tables[name] != new_tables[name]:
            records.append({"scope": "table", "op": "change", "target": name, "changes": _changes(
                old_tables[name], new_tables[name], ("choices",))})
    old_attrs = {(a["scope"], a["name"]): a for a in old["attr_defs"]}
    new_attrs = {(a["scope"], a["name"]): a for a in new["attr_defs"]}
    for key in sorted(old_attrs.keys() | new_attrs.keys()):
        if key not in old_attrs:
            records.append({"scope": "attribute", "op": "add", "target": key[1], "changes": []})
        elif key not in new_attrs:
            records.append({"scope": "attribute", "op": "delete", "target": key[1], "changes": []})
        elif old_attrs[key] != new_attrs[key]:
            changes = []
            before_def = f"{old_attrs[key]['type']} {old_attrs[key]['args']}"
            after_def = f"{new_attrs[key]['type']} {new_attrs[key]['args']}"
            if before_def != after_def:  # 只改默认值时不重复列出相同的定义
                changes.append({"field": "定义", "old": before_def, "new": after_def})
            if old_attrs[key].get("default") != new_attrs[key].get("default"):
                changes.append({"field": "默认值", "old": _show("", old_attrs[key].get("default")),
                                "new": _show("", new_attrs[key].get("default"))})
            records.append({"scope": "attribute", "op": "change", "target": key[1], "changes": changes})
    return records


def count_ops(records: list[dict[str, Any]]) -> dict[str, int]:
    return {op: sum(1 for r in records if r["op"] == op) for op in ("add", "delete", "change")}


def text_diff(old: str, new: str, limit: int = 600,
              labels: tuple[str, str] = ("磁盘上的文件", "当前内容")) -> dict[str, Any]:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), labels[0], labels[1],
                                      lineterm="", n=2))
    truncated = len(lines) > limit
    return {"lines": lines[:limit], "truncated": truncated, "total": len(lines)}
