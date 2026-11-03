"""JSON-friendly semantic view of a :class:`DbcDocument` for the page.

Everything the editor and the live tables need is derived from the document
itself, so attributes, comments and value tables that cantools does not model
are never dropped.  The tests compare this view with cantools' own parse of the
same text.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .document import (
    AttrDefItem, AttrDefaultItem, AttrValueItem, DbcDocument, ExtMuxItem, GEN_MSG_CYCLE_TIME,
    GEN_SIG_START_VALUE, LONG_SYMBOL, MessageItem, NO_NODE, SigGroupItem, SigValTypeItem, SignalItem,
    TxBuItem, ValueTableItem, enum_options, message_key, parse_number, unescape_text,
)
from .layout import INTEL, MOTOROLA, signal_bits, span_bytes

_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"', re.S)
SAFE_INTEGER = 1 << 53


def _json_number(value: Any) -> Any:
    """Keep integers beyond JavaScript's exact range as text, as raw values do."""
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) >= SAFE_INTEGER:
        return str(value)
    return value


def attribute_value(token: str) -> Any:
    """Decode a BA_ value token into str / int / float."""
    token = token.strip()
    if token.startswith('"'):
        match = _STRING.fullmatch(token)
        return unescape_text(match.group(1)) if match else token
    if token.lower().lstrip("+-").startswith("0x"):
        try:
            return int(token, 16)
        except ValueError:
            return token
    if "." not in token and "e" not in token.lower():
        try:
            return int(token, 10)
        except ValueError:
            return token
    try:
        number = float(token)
    except ValueError:
        return token
    if not math.isfinite(number):
        return token
    return number


def value_type_of(doc: DbcDocument, raw_id: int, signal: str) -> str:
    for item in doc.items:
        if isinstance(item, SigValTypeItem) and item.raw_id == raw_id and item.signal == signal:
            return {1: "float32", 2: "float64"}.get(item.vtype, "int")
    return "int"


def _number_token_ok(token: str) -> bool:
    try:
        return math.isfinite(float(token))
    except (TypeError, ValueError):
        return False


def raw_range(length: int, signed: bool, value_type: str) -> tuple[float, float]:
    if value_type == "float32":
        return (-3.4028234663852886e38, 3.4028234663852886e38)
    if value_type == "float64":
        return (-1.7976931348623157e308, 1.7976931348623157e308)
    if signed:
        return (-(1 << (length - 1)), (1 << (length - 1)) - 1)
    return (0, (1 << length) - 1)


def signal_entry(doc: DbcDocument, message: MessageItem, signal: SignalItem) -> dict[str, Any]:
    order = INTEL if signal.order == 1 else MOTOROLA
    value_type = value_type_of(doc, message.raw_id, signal.name)
    factor, offset = parse_number(signal.factor, 1.0), parse_number(signal.offset)
    low, high = raw_range(signal.length, signal.signed, value_type)
    ends = sorted((low * factor + offset, high * factor + offset))
    finite_ends = [value if math.isfinite(value) else None for value in ends]
    ext = next((i for i in doc.items if isinstance(i, ExtMuxItem)
                and i.raw_id == message.raw_id and i.signal == signal.name), None)
    choices = doc.choices_of(message.raw_id, signal.name) or []
    initial = doc.attr_value(GEN_SIG_START_VALUE, "SG_", message.raw_id, signal.name)
    attrs = {i.name: _json_number(attribute_value(i.value)) for i in doc.items
             if isinstance(i, AttrValueItem) and i.scope == "SG_" and i.raw_id == message.raw_id
             and i.target == signal.name
             and i.name not in (LONG_SYMBOL["SG_"], GEN_SIG_START_VALUE)}
    initial_raw = attribute_value(initial.value) if initial is not None else None
    if initial_raw is not None and not isinstance(initial_raw, (int, float)):
        initial_raw = None
    initial_physical = None
    if initial_raw is not None:
        try:
            initial_physical = initial_raw * factor + offset
        except OverflowError:  # 超长整数乘以因子时超出 float 范围
            initial_physical = None
    if isinstance(initial_physical, float) and not math.isfinite(initial_physical):
        initial_physical = None
    parent = None
    if ext is not None:
        try:
            parent = doc.signal_display(message, doc.find_signal(message, ext.mux_signal))
        except Exception:
            parent = ext.mux_signal
    return {
        "name": doc.signal_display(message, signal),
        "file_name": signal.name,
        "start": signal.start,
        "length": signal.length,
        "byte_order": order,
        "signed": signal.signed,
        "value_type": value_type,
        "factor": factor,
        "offset": offset,
        "minimum": parse_number(signal.minimum),
        "maximum": parse_number(signal.maximum),
        "unit": signal.unit,
        "receivers": [doc.node_display(name) for name in signal.receivers if name != NO_NODE],
        "mux": {
            "role": signal.mux_role,
            "value": signal.mux_value,
            "ext": ({"signal": parent, "ranges": [list(r) for r in ext.ranges]} if ext else None),
        },
        "comment": doc.comment_of("SG_", message.raw_id, signal.name),
        "choices": [{"value": value, "label": label} for value, label in choices],
        "initial_raw": _json_number(initial_raw),
        "initial": initial_physical,
        "attrs": attrs,
        "bits": signal_bits(signal.start, signal.length, order),
        "phys_range": finite_ends if all(value is not None for value in finite_ends) else None,
        "number_errors": [name for name, token in
                          (("因子", signal.factor), ("偏移", signal.offset),
                           ("最小值", signal.minimum), ("最大值", signal.maximum))
                          if not _number_token_ok(token)],
        "initial_text": initial.value if initial is not None else None,
    }


def _signal_groups(doc: DbcDocument) -> dict[int, list[SigGroupItem]]:
    """``SIG_GROUP_`` statements by message, collected in one pass over the document."""
    groups: dict[int, list[SigGroupItem]] = {}
    for item in doc.items:
        if isinstance(item, SigGroupItem):
            groups.setdefault(item.raw_id, []).append(item)
    return groups


def message_entry(doc: DbcDocument, message: MessageItem,
                  groups_by_message: dict[int, list[SigGroupItem]] | None = None) -> dict[str, Any]:
    tx = next((i for i in doc.items if isinstance(i, TxBuItem) and i.raw_id == message.raw_id), None)
    senders = [message.sender] if message.sender != NO_NODE else []
    if tx:
        senders += [n for n in tx.nodes if n not in senders and n != NO_NODE]
    senders = [doc.node_display(name) for name in senders]
    attrs = {i.name: _json_number(attribute_value(i.value)) for i in doc.items
             if isinstance(i, AttrValueItem) and i.scope == "BO_" and i.raw_id == message.raw_id
             and i.name not in (LONG_SYMBOL["BO_"], GEN_MSG_CYCLE_TIME)}
    signals = [signal_entry(doc, message, s) for s in doc.signals(message)]
    if groups_by_message is None:
        groups_by_message = _signal_groups(doc)
    groups = []
    for item in groups_by_message.get(message.raw_id, []):
        members = []
        for file_name in item.signals:
            try:
                members.append(doc.signal_display(message, doc.find_signal(message, file_name)))
            except Exception:
                members.append(file_name)
        groups.append({"name": item.name, "repetitions": item.repetitions, "signals": members})
    return {
        "key": message_key(message.raw_id),
        "id": message.frame_id,
        "extended": message.extended,
        "id_text": (f"0x{message.frame_id:08X}" if message.extended else f"0x{message.frame_id:03X}"),
        "name": doc.message_display(message),
        "file_name": message.name,
        "dlc": message.dlc,
        "virtual": message.virtual,
        "senders": senders,
        "cycle_ms": doc.message_cycle_time(message),
        "comment": doc.comment_of("BO_", message.raw_id),
        "attrs": attrs,
        "signals": signals,
        "signal_groups": groups,
        "bytes": max(message.dlc, span_bytes(bit for s in signals for bit in s["bits"])),
    }


def build_catalog(doc: DbcDocument) -> dict[str, Any]:
    nodes = [doc.node_display(name) for name in doc.nodes()]
    sends = {name: 0 for name in nodes}
    receives = {name: 0 for name in nodes}
    messages = []
    groups_by_message = _signal_groups(doc)
    for message in doc.messages():
        entry = message_entry(doc, message, groups_by_message)
        messages.append(entry)
        for sender in entry["senders"]:
            if sender in sends:
                sends[sender] += 1
        seen: set[str] = set()
        for signal in entry["signals"]:
            seen.update(signal["receivers"])
        for receiver in seen:
            if receiver in receives:
                receives[receiver] += 1
    defaults = {i.name: attribute_value(i.value) for i in doc.items if isinstance(i, AttrDefaultItem)}
    node_attrs = {
        file_name: {i.name: _json_number(attribute_value(i.value)) for i in doc.items
                    if isinstance(i, AttrValueItem) and i.scope == "BU_" and i.target == file_name
                    and i.name != LONG_SYMBOL["BU_"]}
        for file_name in doc.nodes()
    }
    global_attrs = {i.name: _json_number(attribute_value(i.value)) for i in doc.items
                    if isinstance(i, AttrValueItem) and i.scope == ""}
    return {
        "version": next((i.text for i in doc.items if i.kind == "VERSION"), ""),
        "comment": doc.comment_of(""),
        "attrs": global_attrs,
        "nodes": [
            {"name": name, "file_name": file_name,
             "comment": doc.comment_of("BU_", None, file_name),
             "attrs": node_attrs.get(file_name, {}),
             "sends": sends[name], "receives": receives[name]}
            for name, file_name in zip(nodes, doc.nodes())
        ],
        "messages": messages,
        "value_tables": [
            {"name": item.name, "choices": [{"value": v, "label": label} for v, label in item.pairs]}
            for item in doc.of_type(ValueTableItem)
        ],
        "attr_defs": [
            {"scope": item.scope, "name": item.name, "type": item.vtype, "args": item.args.strip(),
             "default": _json_number(defaults.get(item.name)),
             "choices": enum_options(item.args) if item.vtype == "ENUM" else []}
            for item in doc.of_type(AttrDefItem)
        ],
        "attr_values": [
            {"name": item.name, "scope": item.scope, "raw_id": item.raw_id, "target": item.target,
             "value": attribute_value(item.value)}
            for item in doc.of_type(AttrValueItem)
        ],
        "attr_defaults": [{"name": item.name, "value": attribute_value(item.value)}
                          for item in doc.of_type(AttrDefaultItem)],
        "warnings": list(doc.warnings),
    }
