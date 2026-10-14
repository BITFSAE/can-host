"""JSON-friendly semantic view of a :class:`DbcDocument` for the page.

Everything the editor and the live tables need is derived from the document
itself, so attributes, comments and value tables that cantools does not model
are never dropped.  The tests compare this view with cantools' own parse of the
same text.
"""

from __future__ import annotations

import re
from typing import Any

from .document import (
    AttrDefItem, AttrDefaultItem, AttrValueItem, DbcDocument, ExtMuxItem, GEN_SIG_START_VALUE, LONG_SYMBOL,
    MessageItem, NO_NODE, SigValTypeItem, SignalItem, TxBuItem, ValueTableItem, message_key, parse_number,
    unescape_text,
)
from .layout import INTEL, MOTOROLA, signal_bits, span_bytes

_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"', re.S)


def attribute_value(token: str) -> Any:
    """Decode a BA_ value token into str / int / float."""
    token = token.strip()
    if token.startswith('"'):
        match = _STRING.fullmatch(token)
        return unescape_text(match.group(1)) if match else token
    try:
        number = float(token)
    except ValueError:
        return token
    return int(number) if number == int(number) and "." not in token and "e" not in token.lower() else number


def value_type_of(doc: DbcDocument, raw_id: int, signal: str) -> str:
    for item in doc.items:
        if isinstance(item, SigValTypeItem) and item.raw_id == raw_id and item.signal == signal:
            return {1: "float32", 2: "float64"}.get(item.vtype, "int")
    return "int"


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
    ext = next((i for i in doc.items if isinstance(i, ExtMuxItem)
                and i.raw_id == message.raw_id and i.signal == signal.name), None)
    choices = doc.choices_of(message.raw_id, signal.name) or []
    initial = doc.attr_value(GEN_SIG_START_VALUE, "SG_", message.raw_id, signal.name)
    attrs = {i.name: attribute_value(i.value) for i in doc.items
             if isinstance(i, AttrValueItem) and i.scope == "SG_" and i.raw_id == message.raw_id
             and i.target == signal.name and i.name != LONG_SYMBOL["SG_"]}
    initial_raw = parse_number(initial.value) if initial is not None else None
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
        "initial_raw": initial_raw,
        "initial": (initial_raw * factor + offset) if initial_raw is not None else None,
        "attrs": attrs,
        "bits": signal_bits(signal.start, signal.length, order),
        "phys_range": [ends[0], ends[1]],
    }


def message_entry(doc: DbcDocument, message: MessageItem) -> dict[str, Any]:
    tx = next((i for i in doc.items if isinstance(i, TxBuItem) and i.raw_id == message.raw_id), None)
    senders = [message.sender] if message.sender != NO_NODE else []
    if tx:
        senders += [n for n in tx.nodes if n not in senders and n != NO_NODE]
    senders = [doc.node_display(name) for name in senders]
    attrs = {i.name: attribute_value(i.value) for i in doc.items
             if isinstance(i, AttrValueItem) and i.scope == "BO_" and i.raw_id == message.raw_id
             and i.name != LONG_SYMBOL["BO_"]}
    signals = [signal_entry(doc, message, s) for s in doc.signals(message)]
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
        "bytes": max(message.dlc, span_bytes(bit for s in signals for bit in s["bits"])),
    }


def build_catalog(doc: DbcDocument) -> dict[str, Any]:
    nodes = [doc.node_display(name) for name in doc.nodes()]
    sends = {name: 0 for name in nodes}
    receives = {name: 0 for name in nodes}
    messages = []
    for message in doc.messages():
        entry = message_entry(doc, message)
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
    return {
        "version": next((i.text for i in doc.items if i.kind == "VERSION"), ""),
        "comment": doc.comment_of(""),
        "nodes": [
            {"name": name, "file_name": file_name,
             "comment": doc.comment_of("BU_", None, file_name),
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
             "default": defaults.get(item.name)}
            for item in doc.of_type(AttrDefItem)
        ],
        "warnings": list(doc.warnings),
    }
