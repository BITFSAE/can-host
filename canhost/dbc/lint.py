"""Consistency checks for the DBC page (the editor's "check database" step).

Errors are the same defects strict cantools/CANdb++ parsing rejects or that make
a signal decode wrongly; warnings and notes explain things that are legal but
usually unintended.  Saving is blocked by errors only.
"""

from __future__ import annotations

import re
from typing import Any

from .catalog import raw_range

ERROR = "error"
WARN = "warn"
NOTE = "note"
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _issue(level: str, code: str, message: str, key: str | None = None, signal: str | None = None,
           node: str | None = None) -> dict[str, Any]:
    return {"level": level, "code": code, "message": message, "message_key": key, "signal": signal,
            "node": node}


def _bit_ranges(bits: list[int]) -> str:
    """``[3, 4, 5, 9]`` -> ``bit 3..5, 9`` (addresses are byte*8+bit)."""
    ordered = sorted(bits)
    spans: list[str] = []
    start = previous = None
    for bit in ordered:
        if start is None:
            start = previous = bit
        elif bit == previous + 1:
            previous = bit
        else:
            spans.append(f"{start}" if start == previous else f"{start}..{previous}")
            start = previous = bit
    if start is not None:
        spans.append(f"{start}" if start == previous else f"{start}..{previous}")
    return "bit " + ", ".join(spans)


def _can_coexist(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Two signals can be present in one frame unless they are selected by
    different values of the same multiplexor."""
    am, bm = a["mux"], b["mux"]
    a_muxed = am["role"] in ("multiplexed", "both")
    b_muxed = bm["role"] in ("multiplexed", "both")
    if not (a_muxed and b_muxed):
        return True
    a_ext, b_ext = am.get("ext"), bm.get("ext")
    if a_ext or b_ext:
        if not (a_ext and b_ext) or a_ext["signal"] != b_ext["signal"]:
            return True
        return any(lo1 <= hi2 and lo2 <= hi1 for lo1, hi1 in a_ext["ranges"] for lo2, hi2 in b_ext["ranges"])
    return am["value"] == bm["value"]


def lint_catalog(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    nodes = {node["name"] for node in catalog["nodes"]}
    messages = catalog["messages"]
    names: dict[str, str] = {}
    ids: dict[str, str] = {}
    with_cycle = sum(1 for m in messages if m["cycle_ms"] is not None and not m["virtual"])
    real_messages = [m for m in messages if not m["virtual"]]
    non_ascii = multiline = 0

    for text in catalog["warnings"]:
        issues.append(_issue(WARN, "doc.unparsed", text))

    for node in catalog["nodes"]:
        if not _IDENTIFIER.fullmatch(node["name"]):
            issues.append(_issue(ERROR, "name.invalid", f"节点名称不合法：{node['name']}", node=node["name"]))
        if not node["sends"] and not node["receives"]:
            issues.append(_issue(NOTE, "node.unused", f"节点 {node['name']} 没有发送或接收任何信号",
                                 node=node["name"]))

    for message in real_messages:
        key, name = message["key"], message["name"]
        if not _IDENTIFIER.fullmatch(name):
            issues.append(_issue(ERROR, "name.invalid", f"报文名称不合法：{name}", key))
        elif len(name) > 32 and message["file_name"] == name:
            issues.append(_issue(NOTE, "name.long", f"报文名称超过 32 个字符，部分旧工具会截断：{name}", key))
        if name in names:
            issues.append(_issue(ERROR, "msg.name.dup", f"报文名称重复：{name}", key))
        names[name] = key
        if key in ids:
            issues.append(_issue(ERROR, "msg.id.dup", f"报文 ID 重复：{message['id_text']}", key))
        ids[key] = name
        if message["dlc"] > 8:
            issues.append(_issue(WARN, "msg.dlc.fd", f"{name} 的 DLC 为 {message['dlc']}，上位机只支持经典 CAN（最多 8 字节）", key))
        for sender in message["senders"]:
            if sender not in nodes:
                issues.append(_issue(WARN, "msg.sender.unknown", f"{name} 的发送节点 {sender} 未在节点列表中定义", key))
        if with_cycle * 2 >= len(real_messages) > 0 and message["cycle_ms"] is None:
            issues.append(_issue(NOTE, "msg.cycle.none", f"{name} 没有设置发送周期 GenMsgCycleTime", key))
        if "\n" in message["comment"] or "\r" in message["comment"]:
            multiline += 1
        if not message["comment"].isascii():
            non_ascii += 1

        signals = message["signals"]
        seen_names: set[str] = set()
        multiplexors = [s for s in signals if s["mux"]["role"] in ("multiplexor", "both")]
        for signal in signals:
            sname = signal["name"]
            if not _IDENTIFIER.fullmatch(sname):
                issues.append(_issue(ERROR, "name.invalid", f"信号名称不合法：{sname}", key, sname))
            elif len(sname) > 32 and signal["file_name"] == sname:
                issues.append(_issue(NOTE, "name.long", f"信号名称超过 32 个字符，部分旧工具会截断：{sname}", key, sname))
            if sname in seen_names:
                issues.append(_issue(ERROR, "sig.name.dup", f"报文 {name} 中信号名称重复：{sname}", key, sname))
            seen_names.add(sname)
            length = signal["length"]
            if not 1 <= length <= 64:
                issues.append(_issue(ERROR, "sig.length", f"{sname} 的长度 {length} 不在 1..64 位范围内", key, sname))
            if signal["value_type"] == "float32" and length != 32:
                issues.append(_issue(ERROR, "sig.float.length", f"{sname} 是 float32，长度必须是 32 位", key, sname))
            if signal["value_type"] == "float64" and length != 64:
                issues.append(_issue(ERROR, "sig.float.length", f"{sname} 是 float64，长度必须是 64 位", key, sname))
            limit = message["dlc"] * 8
            outside = [bit for bit in signal["bits"] if bit >= limit or bit < 0]
            if outside:
                issues.append(_issue(ERROR, "sig.oob", f"{sname} 超出 DLC {message['dlc']}（{_bit_ranges(outside)}）", key, sname))
            if signal["factor"] == 0:
                issues.append(_issue(ERROR, "sig.factor.zero", f"{sname} 的因子为 0，物理值恒为偏移量", key, sname))
            low, high = signal["minimum"], signal["maximum"]
            if low > high:
                issues.append(_issue(ERROR, "sig.minmax", f"{sname} 的最小值 {low:g} 大于最大值 {high:g}", key, sname))
            elif not (low == 0 and high == 0):
                reach = signal["phys_range"]
                span = abs(reach[1] - reach[0]) or 1.0
                if low < reach[0] - span * 1e-9 or high > reach[1] + span * 1e-9:
                    issues.append(_issue(WARN, "sig.range.exceeds",
                                         f"{sname} 的范围 [{low:g}|{high:g}] 超出 {length} 位原始值能表示的 [{reach[0]:g}|{reach[1]:g}]",
                                         key, sname))
            raw_low, raw_high = raw_range(length, signal["signed"], signal["value_type"])
            for choice in signal["choices"]:
                if signal["value_type"] == "int" and not raw_low <= choice["value"] <= raw_high:
                    issues.append(_issue(ERROR, "sig.choice.range",
                                         f"{sname} 的值表取值 {choice['value']} 超出 {length} 位原始值范围", key, sname))
            for receiver in signal["receivers"]:
                if receiver not in nodes:
                    issues.append(_issue(WARN, "sig.receiver.unknown", f"{sname} 的接收节点 {receiver} 未在节点列表中定义", key, sname))
            mux = signal["mux"]
            if mux["role"] in ("multiplexed", "both") and not multiplexors and not mux["ext"]:
                issues.append(_issue(ERROR, "sig.mux.orphan", f"{sname} 是被复用信号，但报文里没有多路复用选择信号", key, sname))
            if mux["ext"] and mux["ext"]["signal"] not in {s["name"] for s in signals}:
                issues.append(_issue(ERROR, "sig.mux.parent", f"{sname} 的扩展复用父信号 {mux['ext']['signal']} 不存在", key, sname))
            texts = [signal["unit"], signal["comment"], *(c["label"] for c in signal["choices"])]
            if any(not text.isascii() for text in texts):
                non_ascii += 1
            if "\n" in signal["comment"] or "\r" in signal["comment"]:
                multiline += 1
        if sum(1 for s in signals if s["mux"]["role"] == "multiplexor") > 1:
            issues.append(_issue(WARN, "sig.mux.multiple", f"{name} 有多个多路复用选择信号；简单复用只允许一个", key))

        for index, first in enumerate(signals):
            first_bits = set(first["bits"])
            for second in signals[index + 1:]:
                if not _can_coexist(first, second):
                    continue
                common = sorted(first_bits & set(second["bits"]))
                if common:
                    issues.append(_issue(ERROR, "sig.overlap",
                                         f"{first['name']} 与 {second['name']} 的位重叠（{_bit_ranges(common)}）",
                                         key, first["name"]))
    if non_ascii:
        issues.append(_issue(NOTE, "text.nonascii",
                             f"{non_ascii} 处注释、单位或值表含非 ASCII 字符；给 CANdb++ 用时请保存为 GBK"))
    if multiline:
        issues.append(_issue(NOTE, "comment.multiline",
                             f"{multiline} 处注释含换行；CANdb++ 规整保存会合并为一行"))
    return issues


def summarize(issues: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "errors": sum(1 for i in issues if i["level"] == ERROR),
        "warnings": sum(1 for i in issues if i["level"] == WARN),
        "notes": sum(1 for i in issues if i["level"] == NOTE),
    }
